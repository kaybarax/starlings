"""Deterministic offline fake of the hosted GitHub adapter.

``FakeHost`` is a Python-call simulation used by tests and offline
rehearsal — no sockets, no timestamps, a JSON event store at
``adapters/fake.json`` under the state directory. It models named
repositories, pull requests, registered caller workflows (their trigger
bases and allowed sources) and workflow runs with attempts. Job
submissions are validated against the registered routing; ``drive``
completes queued runs by executing their argv payloads in a detached
worktree of the submitted ``head_sha`` and recording real logs and
declared artifacts, so receipts carry genuine evidence bytes.

It replays ``pull_request`` (opened/synchronized/merged/closed),
``check_run`` completed, ``merge_group`` checks_requested,
``workflow_dispatch``, ``push`` and run-lost (a submission whose
completion event is suppressed). What it cannot prove is documented in
docs/operations.md: real event delivery, billing, branch protections
and runner parity all remain unproven until a passing hosted gate is
recorded.
"""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from .. import gitops
from ..batches import Failure, invalid
from ..contracts import Adapter
from ..state import private_dir

SCHEMA = 1


def event_env(work):
    return {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(work), "TMPDIR": str(work), "LANG": os.environ.get("LANG", "C"), "LC_ALL": os.environ.get("LC_ALL", "C")}


class FakeHost:
    adapter = "fake"

    def __init__(self, root, host="fake.local"):
        self.root = Path(root)
        self.path = self.root / "adapters" / "fake.json"
        self.host = host
        self.store = self._load()

    def _load(self):
        try:
            store = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"schema": SCHEMA, "host": self.host, "repos": {}, "pull_requests": {}, "runs": {}, "workflows": {}, "events": [], "seq": 0, "settings": {"auto": True, "lose_next": False}, "lost": [], "forced": {}, "counters": {"run": 0, "pr": 0}}
        if store.get("schema") != SCHEMA:
            raise invalid(f"fake host store {self.path} has unsupported schema {store.get('schema')}")
        return store

    def _save(self):
        private_dir(self.root, "adapters")
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self.store, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _event(self, kind, **fields):
        self.store["seq"] += 1
        event = {"seq": self.store["seq"], "type": kind, **fields}
        self.store["events"].append(event)
        return event

    def probe(self):
        return {"adapter": "fake", "host": self.host, "store": str(self.path), "live": False, "note": "deterministic offline fake: proves routing, binding, dispositions and recovery only"}

    def adapter_record(self):
        return Adapter("fake-github", "1", ("submit_job", "completion", "replay", "pull_requests", "event_log")).to_dict()

    def configure(self, workflow, routing):
        self.store = self._load()
        recorded = {"branches": list(routing["branches"]), "sources": list(routing["sources"])}
        if self.store["workflows"].get(workflow) != recorded:
            self.store["workflows"][workflow] = recorded
            self._save()

    def _route(self, event):
        if event["type"] == "pull_request":
            base, source = event.get("base"), event.get("source")
        elif event["type"] == "merge_group":
            base, source = event.get("base"), None
        elif event["type"] in ("push", "workflow_dispatch"):
            base, source = (event.get("ref") or "").removeprefix("refs/heads/"), None
        else:
            return []
        hits = []
        for path, workflow in sorted(self.store["workflows"].items()):
            if base not in workflow["branches"]:
                continue
            if event["type"] == "pull_request" and source is not None and source not in workflow["sources"]:
                continue
            hits.append(path)
            self._event("route", workflow=path, for_seq=event["seq"])
        return hits

    def replay(self, kind, **fields):
        self.store = self._load()
        event = self._event(kind, **fields)
        routed = self._route(event)
        self._save()
        return {"event": event, "routed": routed}

    def submit_job(self, repo, name, workflow, revision, head_sha, event, payload):
        self.store = self._load()
        registered = self.store["workflows"].get(workflow)
        if registered is None:
            raise Failure("blocked", "unrouted", f"fake host has no registered workflow {workflow!r}; nothing was submitted")
        if event.get("base") not in registered["branches"] or any(source not in registered["sources"] for source in event.get("source", [])):
            raise Failure("blocked", "unrouted", f"submission to {workflow!r} is not routed: base {event.get('base')!r} with source {event.get('source')!r} is outside the registered branches {registered['branches']} and sources {registered['sources']}")
        key = "|".join((repo, name, workflow, head_sha))
        run = next((item for item in self.store["runs"].values() if item["key"] == key), None)
        if run is None:
            self.store["counters"]["run"] += 1
            run = {"id": f"ghr-{self.store['counters']['run']:04d}", "key": key, "repo": repo, "name": name, "workflow": workflow, "revision": revision, "head_sha": head_sha, "status": "queued", "conclusion": None, "attempt": 0, "result": None, "payload": payload, "event": event}
            self.store["runs"][run["id"]] = run
        run["attempt"] += 1
        run["status"], run["conclusion"], run["result"], run["payload"], run["event"] = "queued", None, None, payload, event
        self._event("workflow_run", action="requested", run_id=run["id"], attempt=run["attempt"], name=name, repo=repo, workflow=workflow, revision=revision, head_sha=head_sha, event=event)
        attempt_key = f"{run['id']}:{run['attempt']}"
        if self.store["settings"].pop("lose_next", False):
            self.store["lost"].append(attempt_key)
        forced = self.store["settings"].pop("force_next", None)
        if forced is not None:
            self.store["forced"][attempt_key] = forced
        self._save()
        return {"adapter": "fake", "host": self.host, "repo": repo, "event": event, "run_id": run["id"], "attempt": run["attempt"], "workflow": workflow, "revision": revision, "head_sha": head_sha}

    def _execute(self, run):
        payload = run["payload"] or {}
        objects = Path(payload["objects"])
        work = Path(tempfile.mkdtemp(prefix="starlings-fake-run-"))
        checkout = work / "checkout"
        stdout, stderr, exit_code, conclusion = "", "", 1, "failure"
        artifacts, missing = {}, []
        started = time.monotonic()
        try:
            gitops.git(objects, "worktree", "add", "--quiet", "--detach", str(checkout), run["head_sha"])
            try:
                process = subprocess.run(payload["argv"], cwd=checkout, env=event_env(work), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=payload.get("timeout", 60), check=False)
                stdout, stderr, exit_code = process.stdout, process.stderr, process.returncode
                conclusion = "success" if exit_code == 0 else "failure"
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout if type(exc.stdout) is str else (exc.stdout or b"").decode("utf-8", "replace")
                stderr = exc.stderr if type(exc.stderr) is str else (exc.stderr or b"").decode("utf-8", "replace")
                exit_code, conclusion = -9, "timed_out"
            except OSError as exc:
                stderr = f"starlings fake host could not start {payload['argv'][0]!r}: {exc}\n"
                exit_code, conclusion = 127, "failure"
            for relative in payload.get("artifacts", []):
                source, unsafe = checkout, False
                for part in relative.split("/"):
                    source = source / part
                    unsafe = unsafe or source.is_symlink()
                if unsafe or not source.is_file():
                    missing.append(relative)
                else:
                    artifacts[relative] = base64.b64encode(source.read_bytes()).decode("ascii")
        finally:
            gitops.git(objects, "worktree", "remove", "--force", str(checkout), ok=(0, 128))
            shutil.rmtree(work, ignore_errors=True)
        return {"conclusion": conclusion, "exit_code": exit_code, "stdout": stdout, "stderr": stderr, "artifacts": artifacts, "missing": missing, "duration_seconds": round(time.monotonic() - started, 3), "head_sha": run["head_sha"]}

    def drive(self, run_id=None):
        self.store = self._load()
        for rid, run in self.store["runs"].items():
            if run_id is not None and rid != run_id:
                continue
            if run["status"] != "queued":
                continue
            run["status"] = "in_progress"
            attempt_key = f"{rid}:{run['attempt']}"
            forced = self.store["forced"].get(attempt_key)
            if forced is not None:
                result = {"conclusion": forced.get("conclusion", "success"), "exit_code": forced.get("exit_code", 0), "stdout": forced.get("stdout", ""), "stderr": forced.get("stderr", ""), "artifacts": dict(forced.get("artifacts", {})), "missing": list(forced.get("missing", [])), "duration_seconds": 0.0, "head_sha": forced.get("head_sha", run["head_sha"])}
            else:
                result = self._execute(run)
            run["status"], run["conclusion"], run["result"] = "completed", result["conclusion"], result
            if attempt_key not in self.store["lost"]:
                self._event("workflow_run", action="completed", run_id=rid, attempt=run["attempt"], name=run["name"], repo=run["repo"], workflow=run["workflow"], revision=run["revision"], head_sha=result["head_sha"], conclusion=result["conclusion"])
                self._event("check_run", action="completed", run_id=rid, attempt=run["attempt"], name=run["name"], head_sha=result["head_sha"], conclusion=result["conclusion"])
        self._save()

    def completion(self, submission):
        self.store = self._load()
        if self.store["settings"].get("auto", True):
            self.drive()
            self.store = self._load()
        for event in reversed(self.store["events"]):
            if event.get("type") == "workflow_run" and event.get("action") == "completed" and event.get("run_id") == submission["run_id"] and event.get("attempt") == submission["attempt"]:
                run = self.store["runs"].get(submission["run_id"], {})
                return {**event, "result": run.get("result") or {}}
        return None

    def event_log(self):
        self.store = self._load()
        return list(self.store["events"])

    def _pr(self, repo, number):
        pr = self.store["pull_requests"].get(str(number))
        if pr is None or pr["repo"] != repo:
            raise invalid(f"fake host has no pull request {number} in repo {repo!r}")
        return pr

    def open_pull_request(self, repo, base, source, head=None, title=None):
        self.store = self._load()
        self.store["counters"]["pr"] += 1
        number = self.store["counters"]["pr"]
        pr = {"number": number, "repo": repo, "base": base, "source": source, "head": head, "title": title or source, "state": "open", "link": None}
        self.store["pull_requests"][str(number)] = pr
        self._event("pull_request", action="opened", number=number, repo=repo, base=base, source=source, head=head)
        self._save()
        return dict(pr)

    def synchronize_pull_request(self, repo, number, head):
        self.store = self._load()
        pr = self._pr(repo, number)
        pr["head"] = head
        self._event("pull_request", action="synchronized", number=number, repo=repo, base=pr["base"], source=pr["source"], head=head)
        self._save()
        return dict(pr)

    def merge_pull_request(self, repo, number):
        self.store = self._load()
        pr = self._pr(repo, number)
        pr["state"], pr["link"] = "merged", pr["head"]
        self._event("pull_request", action="merged", number=number, repo=repo, base=pr["base"], source=pr["source"], head=pr["head"])
        self._save()
        return dict(pr)

    def close_pull_request(self, repo, number, link=None):
        self.store = self._load()
        pr = self._pr(repo, number)
        pr["state"], pr["link"] = "closed", link
        self._event("pull_request", action="closed", number=number, repo=repo, base=pr["base"], source=pr["source"], head=pr["head"], link=link)
        self._save()
        return dict(pr)

    def pull_request(self, repo, source):
        self.store = self._load()
        for pr in self.store["pull_requests"].values():
            if pr["repo"] == repo and pr["source"] == source:
                return dict(pr)
        return None

    def lose_next(self):
        self.store = self._load()
        self.store["settings"]["lose_next"] = True
        self._save()

    def force_next(self, conclusion=None, head_sha=None, **result):
        self.store = self._load()
        forced = dict(result)
        if conclusion is not None:
            forced["conclusion"] = conclusion
        if head_sha is not None:
            forced["head_sha"] = head_sha
        self.store["settings"]["force_next"] = forced
        self._save()

    def set_auto(self, flag):
        self.store = self._load()
        self.store["settings"]["auto"] = bool(flag)
        self._save()
