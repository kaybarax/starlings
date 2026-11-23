import json
import os
from pathlib import Path
import platform
import resource
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from . import __version__, gitops, signer
from .batches import Failure, default_owner, invalid
from .contracts import Evidence, Receipt
from .state import private_dir
from .store import LeaseHeld, LeaseLost, request_digest
from .verifier import assess, current_trust, file_digest

GRACE = 2.0
POLL = 0.05
REAP = 10.0


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_group(pgid, number):
    try:
        os.killpg(pgid, number)
    except (ProcessLookupError, PermissionError):
        pass


def reap_group(process, grace):
    pgid = process.pid
    if process.poll() is None or group_alive(pgid):
        signal_group(pgid, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and (process.poll() is None or group_alive(pgid)):
            time.sleep(0.01)
        signal_group(pgid, signal.SIGKILL)
    process.wait()
    deadline = time.monotonic() + REAP
    while group_alive(pgid) and time.monotonic() < deadline:
        signal_group(pgid, signal.SIGKILL)
        time.sleep(0.01)
    return not group_alive(pgid)


def docker_binary():
    return shutil.which("docker")


def docker(*args, timeout=60):
    binary = docker_binary()
    if binary is None:
        raise FileNotFoundError("docker")
    return subprocess.run([binary, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout, check=False)


def guest_status(image):
    if docker_binary() is None:
        return False, "docker CLI was not found on PATH"
    try:
        info = docker("info", "--format", "{{.ServerVersion}}", timeout=30)
        if info.returncode:
            return False, "docker daemon is not reachable: " + (info.stderr.strip() or info.stdout.strip())
        found = docker("image", "inspect", "--format", "{{.Id}}", image)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"docker is not usable: {exc}"
    if found.returncode:
        return False, f"pinned guest image {image} is not present locally; pull it explicitly (runs use --pull never)"
    return True, info.stdout.strip()


def container_gone(name):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            listed = docker("ps", "-a", "-q", "--filter", f"name=^/{name}$")
        except (OSError, subprocess.TimeoutExpired):
            return False
        if listed.returncode == 0 and not listed.stdout.strip():
            return True
        time.sleep(0.05)
    return False


def stop_container(name):
    try:
        docker("kill", name)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return container_gone(name)


def candidate_environment(job):
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(job), "TMPDIR": str(job)}
    for key in ("LANG", "LC_ALL"):
        env[key] = os.environ.get(key, "C")
    return env


def create_private(path):
    return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)


def disk_usage(root):
    total = 0
    for directory, _, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                pass
    return total


def peak_rss():
    value = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def collect(ctx, run_id, check, worktree, required):
    files, missing = {}, []
    for relative in required:
        source = worktree / relative
        current = worktree
        unsafe = False
        for part in relative.split("/"):
            current = current / part
            unsafe = unsafe or current.is_symlink()
        if unsafe or not source.is_file():
            missing.append(relative)
            continue
        parts = relative.split("/")
        directory = private_dir(ctx.root, "artifacts", run_id, check, *parts[:-1])
        target = directory / parts[-1]
        with open(source, "rb") as reader, os.fdopen(create_private(target), "wb") as writer:
            shutil.copyfileobj(reader, writer)
        files[f"artifacts/{run_id}/{check}/{relative}"] = None
    return files, missing


class Run:
    def __init__(self, ctx, plan_row, run_id, owner, timeout, grace, interrupt):
        self.ctx, self.plan_row, self.run_id, self.owner = ctx, plan_row, run_id, owner
        self.record = json.loads(plan_row["record"])
        self.policy = json.loads(plan_row["policy"])
        self.candidate = json.loads(plan_row["candidate"])
        self.limits = self.policy["limits"]
        self.timeout, self.grace, self.interrupt = timeout, grace, interrupt
        self.guest = self.record["environment"] == "guest"
        self.mirror = Path(ctx.store.connection.execute("SELECT mirror FROM batches WHERE name = ?", (plan_row["batch"],)).fetchone()["mirror"])
        self.last_poll = 0.0
        self.cancel_reason = None

    def cancelled(self):
        if self.cancel_reason:
            return True
        if self.interrupt is not None and self.interrupt.is_set():
            self.cancel_reason = "interrupted"
            return True
        now = time.monotonic()
        if now - self.last_poll >= POLL:
            self.last_poll = now
            row = self.ctx.store.connection.execute("SELECT cancel_requested FROM runs WHERE id = ?", (self.run_id,)).fetchone()
            if row["cancel_requested"]:
                self.cancel_reason = "cancelled"
        return bool(self.cancel_reason)

    def groups(self, check):
        result = [[f"slot:cpu:{i}" for i in range(self.limits["cpu_slots"])]]
        if self.guest:
            result.append([f"slot:guest:{i}" for i in range(self.limits["max_guests"])])
        result.extend([f"slot:exclusive:{name}"] for name in self.policy.get("exclusive", {}).get(check, []))
        return result

    def admit(self, check):
        seconds = min(86400, self.timeout + int(self.grace) + 300)
        waited = False
        while True:
            leases = self.ctx.store.acquire_set(self.groups(check), self.run_id, seconds)
            if leases is not None:
                self.ctx.hook("admitted", {"run": self.run_id, "check": check, "resources": [lease["resource"] for lease in leases]})
                return leases, waited
            if not waited:
                waited = True
                self.ctx.hook("admission-wait", {"run": self.run_id, "check": check})
            if self.cancelled():
                return None, waited
            time.sleep(POLL)

    def release(self, leases):
        released = True
        for lease in leases:
            try:
                self.ctx.store.release(lease)
            except LeaseLost:
                released = False
        return released

    def execute(self, check, position):
        argv = list(self.policy["checks"][check])
        worktree = private_dir(self.ctx.root, "worktrees", "runs", self.run_id) / check
        gitops.git(self.mirror, "worktree", "add", "--quiet", "--detach", str(worktree), self.candidate["commit"])
        job = Path(tempfile.mkdtemp(prefix="starlings-job-"))
        logs = private_dir(self.ctx.root, "logs", self.run_id)
        out_path, err_path = logs / f"{check}.out", logs / f"{check}.err"
        name = f"starlings-{self.run_id.removeprefix('run-')}-{check}"
        metrics = {"environment": self.record["environment"], "timeout_seconds": self.timeout, "memory_mib_budget": self.limits["memory_mib"], "disk_mib_budget": self.limits["disk_mib"], "memory_enforced": False, "disk_enforced": False}
        if self.guest:
            if "," in str(worktree):
                raise Failure("failed", "guest-unavailable", f"checkout path {worktree} cannot be bind-mounted")
            memory = max(6, self.limits["memory_mib"] // max(1, self.limits["max_guests"]))
            argv = [docker_binary() or "docker", "run", "--rm", "--pull", "never", "--network", "none", "--cpus", "1", "--memory", f"{memory}m", "--mount", f"type=bind,source={worktree},target=/work", "-w", "/work", "--name", name, self.policy["guest"]["image"], *argv]
            metrics.update({"container": name, "guest_memory_mib": memory, "memory_enforced": True})
            env = dict(os.environ)
            for key in [key for key in env if key.startswith(("GIT_", "STARLINGS_"))]:
                env.pop(key)
        else:
            env = candidate_environment(job)
        status, code, reaped = "passed", 0, True
        started = time.monotonic()
        with os.fdopen(create_private(out_path), "wb") as out, os.fdopen(create_private(err_path), "wb") as err:
            try:
                process = subprocess.Popen(argv, cwd=worktree, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True, close_fds=True)
            except OSError as exc:
                err.write(f"starlings: could not start {argv[0]!r}: {exc}\n".encode("utf-8"))
                process = None
                status, code = "failed", 127
            if process is not None:
                self.record_process(check, process, job, worktree)
                self.ctx.hook("process-started", {"run": self.run_id, "check": check, "pid": process.pid, "worktree": str(worktree)})
                deadline = started + self.timeout
                stop = None
                while True:
                    try:
                        code = process.wait(timeout=POLL)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    if time.monotonic() >= deadline:
                        stop = "timeout"
                    elif self.cancelled():
                        stop = self.cancel_reason
                    if stop:
                        break
                if self.guest and (stop or process.poll() is None):
                    metrics["container_removed"] = stop_container(name)
                reaped = reap_group(process, self.grace)
                if self.guest:
                    metrics.setdefault("container_removed", container_gone(name))
                    reaped = reaped and metrics["container_removed"]
                code = process.returncode if process.returncode is not None else -9
                if stop == "timeout":
                    status = "timeout"
                elif stop:
                    status = "cancelled"
                elif code != 0:
                    status = "failed"
                if self.guest and code == 125 and status == "failed":
                    err.flush()
                    if "bind source path does not exist" in err_path.read_text(errors="replace"):
                        status = "guest-unavailable"
        metrics.update({"duration_seconds": round(time.monotonic() - started, 3), "peak_child_rss_bytes_high_water": peak_rss(), "checkout_disk_bytes": disk_usage(worktree), "process_group_reaped": reaped})
        files = {f"logs/{self.run_id}/{check}.out": None, f"logs/{self.run_id}/{check}.err": None}
        missing = []
        if status not in ("cancelled", "guest-unavailable"):
            artifacts, missing = collect(self.ctx, self.run_id, check, worktree, self.policy.get("artifacts", {}).get(check, []))
            files.update(artifacts)
            if missing and status == "passed":
                status = "missing-artifact"
        manifest = {path: file_digest(self.ctx.root / path) for path in sorted(files)}
        evidence = Evidence(self.record["id"], check, max(-255, min(255, code)), request_digest(manifest))
        metrics["missing_artifacts"] = missing
        removal = gitops.git(self.mirror, "worktree", "remove", "--force", str(worktree), ok=(0, 128))
        metrics["worktree_removed"] = removal.returncode == 0
        shutil.rmtree(job, ignore_errors=True)
        self.record_evidence(check, position, evidence, status, manifest, metrics, reaped)
        return {"check": check, "status": status, "exit_code": evidence.exit_code, "evidence_id": evidence.id, "files": manifest, "metrics": metrics}, reaped

    def record_process(self, check, process, job, worktree):
        with self.ctx.store.transaction() as c:
            c.execute("INSERT OR REPLACE INTO run_processes(run_id, check_id, pgid, job, worktree) VALUES (?, ?, ?, ?, ?)", (self.run_id, check, process.pid, str(job), str(worktree)))

    def record_evidence(self, check, position, evidence, status, manifest, metrics, reaped):
        with self.ctx.store.transaction() as c:
            if reaped:
                c.execute("DELETE FROM run_processes WHERE run_id = ? AND check_id = ?", (self.run_id, check))
            c.execute("INSERT INTO evidence(run_id, check_id, position, evidence_id, status, exit_code, record, files, metrics) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (self.run_id, check, position, evidence.id, status, evidence.exit_code, json.dumps(evidence.to_dict(), sort_keys=True), json.dumps(manifest, sort_keys=True), json.dumps(metrics, sort_keys=True)))


def toolchain(image):
    return {"python": platform.python_version(), "git": gitops.version(), "platform": f"{platform.system()}-{platform.machine()}", "guest_image": image}


def finish(ctx, run_id, state, reason=None, receipt_id=None):
    with ctx.store.transaction() as c:
        c.execute("UPDATE runs SET state = ?, reason = ?, receipt_id = ?, finished = ? WHERE id = ?", (state, reason, receipt_id, ctx.store.clock(), run_id))


def run(ctx, plan_id, force_fresh=False, timeout=None, owner=None, interrupt=None, grace=GRACE):
    owner = owner or default_owner()
    c = ctx.store.connection
    plan_row = c.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if plan_row is None:
        raise invalid(f"unknown plan {plan_id!r}")
    record, policy = json.loads(plan_row["record"]), json.loads(plan_row["policy"])
    limit = policy["limits"]["timeout_seconds"]
    if timeout is not None and (type(timeout) is not int or not 1 <= timeout <= limit):
        raise invalid(f"--timeout may only lower the trusted limit: expected 1..{limit}")
    timeout = timeout or limit
    trusted = current_trust(c, plan_row["authority"])
    if trusted is None or trusted["digest"] != record["policy_digest"]:
        raise Failure("stale", "stale-plan", f"plan {plan_id} was made under policy {record['policy_digest']}, which is no longer the trusted policy; plan again")
    if not signer.present(ctx.root):
        raise Failure("blocked", "no-signer", "no verifier signer in the state directory; run starlings signer init before running")
    try:
        lease = ctx.store.acquire(f"run:{plan_id}", owner, min(86400, timeout * len(record["checks"]) + 900))
    except LeaseHeld as exc:
        raise Failure("blocked", "lease-held", f"plan {plan_id} is already running: {exc}") from exc
    try:
        if not force_fresh:
            for row in c.execute("SELECT id FROM receipts WHERE plan_id = ? AND outcome = 'passed' ORDER BY issued_at DESC", (plan_id,)).fetchall():
                status, checked = assess(ctx, row["id"])
                if status == "valid":
                    return "ok", {"plan_id": plan_id, "reused": True, "receipt_id": row["id"], "outcome": "passed", "check": checked, "receipt": checked["receipt"]}
        image = policy["guest"]["image"] if record["environment"] == "guest" else None
        guest_version = None
        if image is not None:
            ready, detail = guest_status(image)
            if not ready:
                raise Failure("failed", "guest-unavailable", f"guest environment is unavailable: {detail}; there is no fallback to native execution", {"plan_id": plan_id, "image": image})
            guest_version = detail
        run_id = "run-" + secrets.token_hex(16)
        with ctx.store.transaction() as tx:
            tx.execute("INSERT INTO runs(id, plan_id, owner, state, started) VALUES (?, ?, ?, 'running', ?)", (run_id, plan_id, owner, ctx.store.clock()))
        ctx.hook("run-started", {"run": run_id, "plan": plan_id})
        job = Run(ctx, plan_row, run_id, owner, timeout, grace, interrupt)
        results, leaked = [], []
        result = {"plan_id": plan_id, "run_id": run_id, "reused": False, "gate": record["gate"], "selection": record["selection"], "environment": record["environment"], "checks": results, "logs": str(ctx.root / "logs" / run_id), "artifacts": str(ctx.root / "artifacts" / run_id), "admission": "wait", "guest_docker_version": guest_version}
        try:
            for position, check in enumerate(record["checks"]):
                if job.cancelled():
                    break
                leases, waited = job.admit(check)
                if leases is None:
                    break
                reaped = False
                try:
                    outcome, reaped = job.execute(check, position)
                    outcome["admission_waited"] = waited
                    results.append(outcome)
                finally:
                    if reaped:
                        job.release(leases)
                    else:
                        leaked.append(check)
                if outcome["status"] in ("cancelled", "guest-unavailable"):
                    break
        except BaseException:
            finish(ctx, run_id, "failed", "runner error")
            raise
        result["unreaped"] = leaked
        if job.cancel_reason:
            finish(ctx, run_id, job.cancel_reason, f"run {job.cancel_reason}; no receipt is signed")
            raise Failure("failed", job.cancel_reason, f"run {run_id} was {job.cancel_reason}; no receipt was signed", result)
        if any(item["status"] == "guest-unavailable" for item in results):
            finish(ctx, run_id, "failed", "guest-unavailable")
            raise Failure("failed", "guest-unavailable", "the checkout was not visible inside the guest (is the state directory on a path shared with the docker VM?); no receipt was signed", result)
        if leaked:
            finish(ctx, run_id, "failed", "process group not reaped")
            raise Failure("failed", "unreaped", f"process groups for {', '.join(leaked)} could not be confirmed empty; their slot leases are left to expire and no receipt was signed", result)
        candidate = json.loads(plan_row["candidate"])
        outcome = "passed" if all(item["status"] == "passed" for item in results) else "failed"
        receipt = Receipt(candidate["id"], plan_id, candidate["target"], candidate["base"], candidate["commit"], candidate["tree"], record["policy_digest"], outcome, tuple(item["evidence_id"] for item in results), tuple((path, value) for item in results for path, value in item["files"].items()), {"version": __version__, "fingerprint": signer.fingerprint(ctx.root)}, record["executor"], record["environment"], toolchain(image), ctx.store.clock())
        signature = signer.sign(ctx.root, receipt.payload())
        directory = private_dir(ctx.root, "evidence", run_id)
        for name, content in (("receipt.json", receipt.payload().decode("ascii") + "\n"), ("receipt.json.sig", signature)):
            with os.fdopen(create_private(directory / name), "w", encoding="ascii") as stream:
                stream.write(content)
        with ctx.store.transaction() as tx:
            tx.execute("INSERT INTO receipts(id, run_id, plan_id, authority, outcome, record, signature, issued_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (receipt.id, run_id, plan_id, plan_row["authority"], outcome, receipt.payload().decode("ascii"), signature, receipt.issued_at))
        finish(ctx, run_id, "completed", None, receipt.id)
        result.update({"receipt_id": receipt.id, "outcome": outcome, "receipt": receipt.to_dict(), "receipt_path": str(directory / "receipt.json"), "signature_path": str(directory / "receipt.json.sig")})
        return ("ok" if outcome == "passed" else "failed"), result
    finally:
        try:
            ctx.store.release(lease)
        except LeaseLost:
            pass


def cancel(ctx, run_id):
    with ctx.store.transaction() as c:
        row = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise invalid(f"unknown run {run_id!r}")
        if row["state"] != "running":
            return "ok", {"run_id": run_id, "state": row["state"], "cancel_requested": bool(row["cancel_requested"]), "note": "run is not running; nothing to cancel"}
        c.execute("UPDATE runs SET cancel_requested = 1 WHERE id = ?", (run_id,))
    return "ok", {"run_id": run_id, "state": "running", "cancel_requested": True}


def install_signals(event):
    import threading
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
    for number in previous:
        signal.signal(number, lambda *_: event.set())
    def restore():
        for number, handler in previous.items():
            signal.signal(number, handler)
    return restore
