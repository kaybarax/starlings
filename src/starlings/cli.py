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
from . import batches, demo, diagnose, enforcement, examples, gitops, promotion, runner, signer, upstream, verifier
from .adapters import host as adapters_host
from .adapters import templates as adapters_templates
from .gitops import GitError
from .policy import PRESETS, PolicyError, parse, preset, render
from .state import layout


class UsageError(ValueError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def parser():
    result = Parser(prog="starlings", description="Offline policy, local assembly, trusted policy, planning, local runs, signed receipts, approvals, journalled promotion and recovery against a local bare authority.")
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
    plan.add_argument("--candidate", help="staging or task candidate ID (default: the latest pending one)")
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
    approve = commands.add_parser("approve", help="record an expiring approval of a plan by an identity")
    approve.add_argument("plan_id")
    approve.add_argument("--by", required=True, help="approving identity")
    approve.add_argument("--expires-in", required=True, type=int, help="seconds until expiry (60..2592000)")
    approve.add_argument("--review", help="review reference for the approval")
    revoke = commands.add_parser("revoke", help="revoke an approval (serialized with promotion through the target lease)")
    revoke.add_argument("approval_id")
    promote = commands.add_parser("promote", help="recheck a receipt and land its candidate with one atomic ref transaction")
    promote.add_argument("plan_id")
    promote.add_argument("--receipt", required=True, help="receipt ID issued for the plan")
    diagnose_cmd = commands.add_parser("diagnose", help="attribute a failed release gate to a task, lane or lane pair over frozen subsets; never produces a receipt")
    diagnose_cmd.add_argument("batch")
    diagnose_cmd.add_argument("--gate", required=True, choices=("release",))
    diagnose_cmd.add_argument("--receipt", required=True, help="failed receipt ID issued for the batch's release plan")
    diagnose_cmd.add_argument("--plan", help="release plan ID the receipt must belong to")
    diagnose_cmd.add_argument("--checks", type=lambda value: value.split(","), help="comma-separated plan check IDs to diagnose (default: every check that failed)")
    recover = commands.add_parser("recover", help="reconcile journalled promotions, include intents and lost runs for a batch")
    recover.add_argument("batch")
    recover.add_argument("--resolve", help="ambiguous promotion ID to resolve")
    recover.add_argument("--as", dest="outcome", choices=("landed", "not-applied"), help="operator resolution for --resolve")
    example = commands.add_parser("examples", help="generate deterministic example projects").add_subparsers(dest="action", required=True, parser_class=Parser)
    example_init = example.add_parser("init", help="write a small pure-Python project with unittest checks and starlings.toml; never overwrites")
    example_init.add_argument("target", type=Path, help="directory to create; must be absent or empty")
    example_init.add_argument("--preset", choices=PRESETS, default=PRESETS[0])
    example_init.add_argument("--with-qualification", action="store_true", help="add the assembly, selection, artifact and exclusive tables used by qualification runs")
    demo = commands.add_parser("demo", help="run a small end-to-end demo in a disposable directory")
    demo.add_argument("--directory", required=True, type=Path, help="disposable directory for the demo; created if absent and must be empty if it exists")
    templates = commands.add_parser("templates", help="preview or exclusively write optional workflow templates").add_subparsers(dest="action", required=True, parser_class=Parser)
    templates_github = templates.add_parser("github", help="write the caller and reusable check workflow templates for a [github] policy into .github/workflows")
    writes = templates_github.add_mutually_exclusive_group()
    writes.add_argument("--preview", action="store_true", help="preview only (default)")
    writes.add_argument("--write", action="store_true", help="write the workflow files; never replace existing files")
    authority = commands.add_parser("authority", help="protect a local bare authority with the generated enforcement hook, evaluate a ref update for the hook, or follow and publish to the upstream of a mirror authority").add_subparsers(dest="action", required=True, parser_class=Parser)
    sync = authority.add_parser("sync", help="mirror authority: bring the upstream staging and release heads into the bare authority; refuses when the authority is ahead")
    publish = authority.add_parser("publish", help="mirror authority: push the landed, verified release to the upstream in one atomic, non-forced push")
    discard = authority.add_parser("discard", help="mirror authority: set an unpublished promotion aside under refs/discarded/ and follow the upstream again")
    discard.add_argument("--force", action="store_true", help="also set aside a valid, publishable promotion of the current upstream heads")
    for command in (sync, publish, discard):
        command.add_argument("--authority", type=Path, help="absolute path to the local bare authority (default: authority.git in the state directory)")
    protect = authority.add_parser("protect", help="install the generated update/reference-transaction hook into a bare authority and record its digest")
    protect.add_argument("--authority", required=True, type=Path, help="absolute path to a local bare repository")
    check_intent = authority.add_parser("check-intent", help="hook callback: allow a protected ref update only with a matching journaled intent")
    check_intent.add_argument("refname")
    check_intent.add_argument("old")
    check_intent.add_argument("new")
    check_intent.add_argument("--authority", required=True, type=Path, help="absolute path to the bare authority being updated")
    doctor = commands.add_parser("doctor", help="read-only diagnostics; does not certify enforcement")
    doctor.add_argument("--authority", type=Path, help="absolute path to a local bare authority to probe for hook enforcement")
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
    for command in (stage, promote):
        command.add_argument("--allow-unprotected", action="store_true", help="proceed even when a recorded hook-protected authority is missing or has a modified enforcement hook")
    for command in (assemble, resolve, freeze, stage, revoke, promote, recover, policy, diagnose_cmd, protect, sync, publish, discard):
        command.add_argument("--owner", help="lease owner label (default host:pid)")
        command.add_argument("--lease-seconds", type=int, default=600)
    for command in (prepare, assign, handoff, assemble, resolve, freeze, stage, status, policy, signer_init, plan, run, cancel, check, approve, revoke, promote, recover, diagnose_cmd, protect, check_intent, sync, publish, discard):
        command.add_argument("--state-dir", type=Path, default=Path(os.environ["STARLINGS_STATE_DIR"]) if os.environ.get("STARLINGS_STATE_DIR") else None, help="absolute private state root (or STARLINGS_STATE_DIR)")
    for command in (init, policy, doctor, prepare, assign, handoff, assemble, resolve, freeze, stage, status, signer_init, plan, run, cancel, check, approve, revoke, promote, recover, diagnose_cmd, protect, templates_github, sync, publish, discard):
        command.add_argument("--directory", type=Path, default=Path.cwd(), help="existing project directory")
    return result


def batch_command(args):
    with batches.Context(args.state_dir, args.directory, create=args.command not in ("status", "check", "cancel", "approve", "revoke", "promote", "recover", "diagnose")) as ctx:
        if args.command == "policy":
            for name in ("authority", "commit", "review"):
                if getattr(args, name) is None:
                    raise batches.invalid(f"policy trust requires --{name}")
            return verifier.trust(ctx, args.authority, args.commit, args.review, args.supersedes, args.owner, args.lease_seconds)
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
        if args.command == "approve":
            return promotion.approve(ctx, args.plan_id, args.by, args.expires_in, args.review)
        if args.command == "revoke":
            return promotion.revoke(ctx, args.approval_id, args.owner, args.lease_seconds)
        if args.command == "promote":
            return promotion.promote(ctx, args.plan_id, args.receipt, args.owner, args.lease_seconds, args.allow_unprotected)
        if args.command == "recover":
            return promotion.recover(ctx, args.batch, args.resolve, args.outcome, args.owner, args.lease_seconds)
        if args.command == "diagnose":
            return diagnose.diagnose(ctx, args.batch, args.gate, args.receipt, args.plan, args.checks, args.owner, args.lease_seconds)
        if args.command == "authority" and args.action == "sync":
            return upstream.sync(ctx, args.authority, args.owner, args.lease_seconds)
        if args.command == "authority" and args.action == "publish":
            return upstream.publish(ctx, args.authority, args.owner, args.lease_seconds)
        if args.command == "authority" and args.action == "discard":
            return upstream.discard(ctx, args.authority, args.force, args.owner, args.lease_seconds)
        if args.command == "authority":
            return enforcement.protect(ctx, args.authority, args.owner, args.lease_seconds)
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
            return batches.stage(ctx, args.batch, args.lanes, args.owner, args.lease_seconds, args.resolution, args.review, args.allow_unprotected)
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
    result = ["Only starlings promote advances staging or release, after a complete recheck of a passed receipt; deployment is out of scope. On an authority hardened with `starlings authority protect`, direct writes to the recorded staging and release refs are refused by a generated update hook; the hook guards against drift and accidents, not credential separation — anyone who can edit or disable the hook still bypasses it."]
    settings = policy.get("environment") or {}
    if settings.get("home") == "operator":
        result.append("environment: native checks run with the operator's HOME, so they can read and change everything under it — credentials, tool configuration and caches. Use it only for checks you would run by hand as yourself.")
    if settings.get("pass"):
        result.append(f"environment: native checks receive the operator's values of {', '.join(settings['pass'])}; a check can print or send them, and retained logs may contain them.")
    for name, gate in policy["gates"].items():
        if gate["scope"] == "none":
            continue
        if gate["executor"] == "github":
            if name == "task":
                result.append(f"{name}: GitHub execution is unsupported on the task gate; configuration is not execution support.")
            elif (policy.get("github") or {}).get("adapter") == "github":
                result.append(f"{name}: the real GitHub adapter submits checks as workflow dispatches to the configured repository through the gh CLI; it is implemented but unqualified until a passing hosted gate is recorded.")
            else:
                result.append(f"{name}: GitHub execution uses the deterministic local fake adapter — offline-qualified for routing, receipt binding, dispositions and recovery only; it proves nothing about real event delivery, protections or runner parity.")
        if gate["environment"] == "guest":
            result.append(f"{name}: guest execution uses the local docker daemon with the pinned image; parity is documented per profile and no security isolation is certified.")
        if gate["scope"] == "affected":
            result.append(f"{name}: affected selection expands to the full gate when a changed path or check has no [selection] mapping.")
    return result


def perform(args, json_output=False):
    if args.command == "examples":
        return examples.init(args.target, args.preset, args.with_qualification)
    if args.command == "demo":
        return demo.run(args.directory, json_output)
    if args.command == "authority" and args.action == "check-intent":
        return enforcement.check_intent(args.state_dir, args.authority, args.refname, args.old, args.new)
    if args.command in ("batch", "task", "lane", "stage", "status", "signer", "plan", "run", "cancel", "check", "approve", "revoke", "promote", "recover", "diagnose", "authority") or (args.command == "policy" and args.action == "trust"):
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
    if args.command == "templates":
        policy = parse(manifest.read_text(encoding="utf-8"))
        files = adapters_templates.render(policy)
        if args.write:
            for relative in files:
                target = root / relative
                if target.exists() or target.is_symlink():
                    raise batches.Failure("conflict", "conflict", f"{relative} already exists; nothing was replaced", {"path": str(target)})
            for relative, content in files.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                exclusive_write(target, content)
        preview = "".join(f"# === {relative} ===\n{content}\n" for relative, content in files.items())
        return {"written": args.write, "path": str(root / ".github" / "workflows"), "files": files, "toml": preview, "warnings": warnings(policy)}
    state = layout(root, args.state_dir)
    policy = parse(manifest.read_text(encoding="utf-8")) if manifest.exists() else None
    hook = enforcement.probe(args.state_dir, args.authority)
    return {"version": __version__, "python": platform.python_version(), "platform": platform.system(), "git_available": shutil.which("git") is not None, "git": probe(gitops.version), "ssh_keygen": shutil.which("ssh-keygen"), "signer_present": signer.present(state["root"]) if Path(state["root"]).is_dir() else False, "docker": probe(docker_probe), "colima": probe(colima_probe), "manifest_present": policy is not None, "policy_valid": True if policy is not None else None, "state": state, "state_created": False, "authority_mode": policy["authority"]["mode"] if policy is not None else "local-bare", "authority_configured": False, "enforcement": hook, "enforcement_implemented": hook["implemented"], "signing_implemented": True, "adapters": adapters_host.probe(state["root"], policy), "unsupported": ["remote authorities", "deployment", "hosted execution", "guest security isolation", "memory and disk enforcement for native checks", "credential separation on the same login", "harness adoption"], "warnings": warnings(policy) if policy is not None else ["No starlings.toml found. Preview with starlings init.", "Local bare refs are the first authority design; no authority is registered or enforced."]}


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
    elif isinstance(data, str):
        print(data)
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
        result = perform(args, json_output)
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
