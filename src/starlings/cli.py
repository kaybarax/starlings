import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import sys
import subprocess
import tempfile
import threading

from . import __version__
from . import batches, gitops, runner, signer, verifier
from .gitops import GitError
from .policy import PRESETS, PolicyError, parse, preset, render
from .state import layout


class UsageError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def parser():
    result = Parser(prog="starlings", description="Offline policy, local assembly, trusted policy, planning, local runs and signed receipts; promotion is not implemented.")
    result.add_argument("--version", action="version", version=f"starlings {__version__}")
    result.add_argument("--json", action="store_true", help="emit schema-1 JSON, including errors")
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="preview or exclusively write a synthetic example policy")
    init.add_argument("--preset", choices=PRESETS, default=PRESETS[0])
    init.add_argument("--executor", choices=("local", "github"), default="local")
    writes = init.add_mutually_exclusive_group()
    writes.add_argument("--preview", action="store_true", help="preview only (default)")
    writes.add_argument("--write", action="store_true", help="write starlings.toml only; never replace a file")
    policy = commands.add_parser("policy", help="inspect or validate the local manifest, or trust a reviewed manifest from the authority")
    policy.add_argument("action", choices=("show", "validate", "trust"))
    policy.add_argument("--authority", type=Path, help="trust: absolute path to the local bare authority")
    policy.add_argument("--commit", help="trust: full commit ID whose starlings.toml is trusted")
    policy.add_argument("--review", help="trust: review reference approving the policy")
    policy.add_argument("--supersedes", help="trust: digest of the currently trusted policy (required after the first trust)")
    signer_parser = commands.add_parser("signer", help="manage the local receipt signer").add_subparsers(dest="action", required=True, parser_class=Parser)
    signer_init = signer_parser.add_parser("init", help="create the Ed25519 signer key and allowed_signers file; never overwrites")
    plan = commands.add_parser("plan", help="plan a gate against the trusted policy")
    plan.add_argument("batch")
    plan.add_argument("--gate", required=True, choices=("task", "staging", "release"))
    plan.add_argument("--candidate", help="staging candidate ID (default: the latest pending one)")
    run = commands.add_parser("run", help="run a plan and sign a receipt when every check completes")
    run.add_argument("plan_id")
    run.add_argument("--force-fresh", action="store_true", help="ignore a reusable passed receipt")
    run.add_argument("--timeout", type=int, help="per-check timeout in seconds; may only lower the trusted limit")
    run.add_argument("--owner", help="run lease owner label (default host:pid)")
    cancel = commands.add_parser("cancel", help="request cancellation of a running run")
    cancel.add_argument("run_id")
    check = commands.add_parser("check", help="check a signed receipt: valid, invalid, stale or failed")
    check.add_argument("receipt_id")
    check.add_argument("--target", help="expected target branch or ref (default: the plan's target)")
    doctor = commands.add_parser("doctor", help="read-only diagnostics; does not certify enforcement")
    doctor.add_argument("--state-dir", type=Path, default=Path(os.environ["STARLINGS_STATE_DIR"]) if os.environ.get("STARLINGS_STATE_DIR") else None, help="absolute private state root (or STARLINGS_STATE_DIR); inspected, never created")
    batch = commands.add_parser("batch", help="prepare a batch against a local bare authority").add_subparsers(dest="action", required=True, parser_class=Parser)
    prepare = batch.add_parser("prepare", help="pin the staging base, lane order and untrusted policy digest")
    prepare.add_argument("batch")
    prepare.add_argument("--lanes", required=True, type=lambda value: value.split(","), help="comma-separated lane order")
    prepare.add_argument("--authority", required=True, type=Path, help="absolute path to a local bare repository")
    task = commands.add_parser("task", help="claim or hand off a task").add_subparsers(dest="action", required=True, parser_class=Parser)
    assign = task.add_parser("assign", help="atomically claim a task and create its worktree")
    assign.add_argument("batch")
    assign.add_argument("task")
    assign.add_argument("--lane", required=True)
    assign.add_argument("--owner", required=True)
    assign.add_argument("--after", action="append", default=[], help="prerequisite task, repeatable")
    handoff = task.add_parser("handoff", help="hand off an exact reviewed head")
    handoff.add_argument("batch")
    handoff.add_argument("task")
    handoff.add_argument("--head", required=True)
    handoff.add_argument("--review", required=True)
    handoff.add_argument("--prerequisite", action="append", default=[], help="TASK=SHA, repeatable")
    lane = commands.add_parser("lane", help="assemble, resolve or freeze a lane").add_subparsers(dest="action", required=True, parser_class=Parser)
    assemble = lane.add_parser("assemble", help="include handed-off tasks in dependency order (single writer per lane)")
    resolve = lane.add_parser("resolve", help="record a reviewed resolution for a conflict-parked task")
    freeze = lane.add_parser("freeze", help="record the frozen lane tip")
    for command in (assemble, resolve, freeze):
        command.add_argument("batch")
        command.add_argument("lane")
    resolve.add_argument("--task", required=True)
    resolve.add_argument("--head", required=True)
    resolve.add_argument("--review")
    stage = commands.add_parser("stage", help="serialize frozen lanes onto the staging branch under a target lease")
    stage.add_argument("batch")
    stage.add_argument("--lanes", required=True, type=lambda value: value.split(","))
    stage.add_argument("--resolution", help="reviewed commit resolving a staging conflict; requires --review")
    stage.add_argument("--review", help="review reference for --resolution")
    status = commands.add_parser("status", help="show batch, task, lane and candidate state")
    status.add_argument("batch")
    for command in (assemble, resolve, freeze, stage):
        command.add_argument("--owner", help="lease owner label (default host:pid)")
        command.add_argument("--lease-seconds", type=int, default=600)
    for command in (prepare, assign, handoff, assemble, resolve, freeze, stage, status, policy, signer_init, plan, run, cancel, check):
        command.add_argument("--state-dir", type=Path, default=Path(os.environ["STARLINGS_STATE_DIR"]) if os.environ.get("STARLINGS_STATE_DIR") else None, help="absolute private state root (or STARLINGS_STATE_DIR)")
    for command in (init, policy, doctor, prepare, assign, handoff, assemble, resolve, freeze, stage, status, signer_init, plan, run, cancel, check):
        command.add_argument("--directory", type=Path, default=Path.cwd(), help="existing project directory")
    return result


def batch_command(args):
    with batches.Context(args.state_dir, args.directory, create=args.command not in ("status", "check", "cancel")) as ctx:
        if args.command == "policy":
            for name in ("authority", "commit", "review"):
                if getattr(args, name) is None:
                    raise batches.invalid(f"policy trust requires --{name}")
            return verifier.trust(ctx, args.authority, args.commit, args.review, args.supersedes)
        if args.command == "signer":
            try:
                return "ok", signer.init(ctx.root)
            except signer.SignerError as exc:
                raise batches.Failure("invalid", "signer", str(exc)) from exc
        if args.command == "plan":
            return verifier.plan(ctx, args.batch, args.gate, args.candidate)
        if args.command == "run":
            event = threading.Event()
            restore = runner.install_signals(event)
            try:
                return runner.run(ctx, args.plan_id, args.force_fresh, args.timeout, args.owner, event)
            finally:
                restore()
        if args.command == "cancel":
            return runner.cancel(ctx, args.run_id)
        if args.command == "check":
            return verifier.check(ctx, args.receipt_id, args.target)
        if args.command == "batch":
            return batches.prepare(ctx, args.batch, args.lanes, args.authority)
        if args.command == "task" and args.action == "assign":
            return batches.assign(ctx, args.batch, args.task, args.lane, args.owner, args.after)
        if args.command == "task":
            return batches.handoff(ctx, args.batch, args.task, args.head, args.review, args.prerequisite)
        if args.command == "lane" and args.action == "assemble":
            return batches.assemble(ctx, args.batch, args.lane, args.owner, args.lease_seconds)
        if args.command == "lane" and args.action == "resolve":
            return batches.resolve(ctx, args.batch, args.lane, args.task, args.head, args.review, args.owner, args.lease_seconds)
        if args.command == "lane":
            return batches.freeze(ctx, args.batch, args.lane, args.owner, args.lease_seconds)
        if args.command == "stage":
            return batches.stage(ctx, args.batch, args.lanes, args.owner, args.lease_seconds, args.resolution, args.review)
        return batches.status(ctx, args.batch)


def exclusive_write(path, content):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", prefix=".starlings-", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink()


def warnings(policy):
    result = ["Runs and receipts never promote: there is no promotion to the release branch, no staging advance after verification and no ref enforcement."]
    for name, gate in policy["gates"].items():
        if gate["scope"] == "none":
            continue
        if gate["executor"] == "github":
            result.append(f"{name}: GitHub execution is unsupported; configuration is not execution support.")
        if gate["environment"] == "guest":
            result.append(f"{name}: guest execution uses the local docker daemon with the pinned image; parity is documented per profile and no security isolation is certified.")
        if gate["scope"] == "affected":
            result.append(f"{name}: affected selection expands to the full gate when a changed path or check has no [selection] mapping.")
    return result


def perform(args):
    if args.command in ("batch", "task", "lane", "stage", "status", "signer", "plan", "run", "cancel", "check") or (args.command == "policy" and args.action == "trust"):
        return batch_command(args)
    root = args.directory.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("--directory must identify an existing directory")
    manifest = root / "starlings.toml"
    if args.command == "init":
        policy = preset(args.preset, args.executor)
        content = render(policy)
        if args.write:
            exclusive_write(manifest, content)
        return {"written": args.write, "path": str(manifest), "preset": args.preset, "policy": policy, "toml": content, "warnings": ["Synthetic example command deliberately fails. Replace it with reviewed project checks before adoption.", *warnings(policy)]}
    if args.command == "policy":
        policy = parse(manifest.read_text(encoding="utf-8"))
        return {"valid": True, "path": str(manifest), "policy": policy, "warnings": warnings(policy)}
    state = layout(root, args.state_dir)
    policy = parse(manifest.read_text(encoding="utf-8")) if manifest.exists() else None
    return {"version": __version__, "python": platform.python_version(), "platform": platform.system(), "git_available": shutil.which("git") is not None, "git": probe(gitops.version), "ssh_keygen": shutil.which("ssh-keygen"), "signer_present": signer.present(state["root"]) if Path(state["root"]).is_dir() else False, "docker": probe(docker_probe), "colima": probe(colima_probe), "manifest_present": policy is not None, "policy_valid": True if policy is not None else None, "state": state, "state_created": False, "authority_mode": "local-bare", "authority_configured": False, "enforcement_implemented": False, "signing_implemented": True, "unsupported": ["promotion", "recovery", "hosted execution", "guest security isolation", "memory and disk enforcement for native checks", "harness adoption"], "warnings": warnings(policy) if policy is not None else ["No starlings.toml found. Preview with starlings init.", "Local bare refs are the first authority design; no authority is registered or enforced."]}


def probe(function):
    try:
        return {"ok": True, "detail": function()}
    except Exception as exc:
        return {"ok": False, "detail": str(exc) or type(exc).__name__}


def docker_probe():
    if shutil.which("docker") is None:
        raise ValueError("docker CLI not found")
    result = runner.docker("info", "--format", "{{.ServerVersion}}", timeout=15)
    if result.returncode:
        raise ValueError("docker daemon unreachable: " + (result.stderr.strip() or result.stdout.strip()))
    return "server " + result.stdout.strip()


def colima_probe():
    binary = shutil.which("colima")
    if binary is None:
        raise ValueError("colima not found")
    result = subprocess.run([binary, "status"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15, check=False)
    if result.returncode:
        raise ValueError("colima is not running")
    return "running"


def emit(data, json_output, code=0, error=None, status=None):
    result = {"schema": 1, "ok": code == 0}
    if status is not None:
        result["status"] = status
    result["error" if error else "data"] = error or data
    if json_output:
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
    elif error:
        print(f"starlings: {error['code']}: {error['message']}", file=sys.stderr)
    elif "toml" in data:
        for warning in data["warnings"]:
            print(warning)
        print("Written:" if data["written"] else "Preview only:", data["path"])
        print(data["toml"], end="")
    else:
        print(json.dumps(data, indent=2, sort_keys=True))
    return code


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in argv
    argv = [arg for arg in argv if arg != "--json"]
    if json_output and argv == ["--version"]:
        return emit({"version": __version__}, True)
    try:
        args = parser().parse_args(argv)
        result = perform(args)
        if isinstance(result, tuple):
            status, data = result
            return emit(data, json_output, batches.EXIT[status], status=status)
        return emit(result, json_output)
    except batches.Failure as exc:
        error = {"code": exc.code, "message": exc.message}
        if exc.data is not None:
            error["data"] = exc.data
        return emit(None, json_output, batches.EXIT[exc.status], error, exc.status)
    except GitError as exc:
        return emit(None, json_output, batches.EXIT["failed"], {"code": "git", "message": str(exc)}, "failed")
    except sqlite3.Error as exc:
        return emit(None, json_output, batches.EXIT["failed"], {"code": "store", "message": str(exc)}, "failed")
    except (PolicyError, UsageError, ValueError, UnicodeError) as exc:
        return emit(None, json_output, 2, {"code": "invalid", "message": str(exc)})
    except FileExistsError:
        return emit(None, json_output, 3, {"code": "conflict", "message": "starlings.toml already exists; inspect it with policy show. No existing file was replaced."})
    except OSError as exc:
        return emit(None, json_output, 3, {"code": "io", "message": str(exc)})
