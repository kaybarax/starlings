"""Real GitHub adapter — submits hosted checks through the ``gh`` CLI.

``GitHubHost`` implements the same adapter surface as
``fake_github.FakeHost`` against a live GitHub repository. Every host
interaction is a ``gh`` argv subprocess (``gh api`` for the REST API,
``gh run`` and ``gh pr`` for run artifacts and pull requests): no raw
tokens, no sockets and never ``shell=True`` — ``gh`` authenticates with
its own configured credentials, which this module never reads, prints
or stores. Every subprocess has a timeout and API failures surface as
adapter errors, never silently.

Submission flow for each planned check:

1. The routing registered by ``configure`` is enforced locally, exactly
   like the fake — a submission event whose base or sources are outside
   the caller workflow's registered sets is refused as ``unrouted``
   before anything is sent anywhere.
2. The candidate's exact objects are uploaded to the repository through
   the Git data API (blobs, trees, then commits in parent-first order).
   Because commit IDs are content-derived, each created object is
   verified byte-identical by comparing the API's returned SHA with the
   local one — a mismatch is an error, never silent. The candidate
   commit is retained at ``refs/starlings/candidates/<sha>`` so the
   workflow can check it out.
3. The reusable check workflow is dispatched with
   ``workflow_dispatch`` at the pinned ``github.reusable_workflow``
   revision (``path@<40 lowercase hex>``), carrying the candidate head
   SHA, check id, starlings run id, plan and candidate ids, check argv
   and declared artifact paths as inputs. The workflow's ``run-name``
   carries a ``starlings/<run-id>/<check-id>`` token so the created
   Actions run is matched unambiguously. GitHub's dispatch endpoint
   accepts only branch or tag names for ``ref`` — never a commit SHA —
   so a SHA pin is first held under the deterministic branch
   ``starlings/workflow-pin/<sha>``; the workflow file that runs is
   still exactly the pinned revision.
4. ``completion`` polls the run and, once completed, downloads the
   ``starlings-<run-id>-<check-id>`` artifact (result.json, stdout,
   stderr and the staged declared artifacts) into the same result shape
   the fake produces.

A JSON journal at ``adapters/github.json`` under the state directory
records registered workflows, submissions, observed completions and PR
close links; ``event_log`` replays those events and refreshes any
unfinished submissions from the live API on every call.

Honest limits: object upload is one API call per blob, tree and commit
(it does not scale to large histories); run discovery relies on the
``run-name`` token rather than a dispatch correlation id (the dispatch
endpoint returns none); GitHub ``pull_request`` triggers cannot filter
head branches, so the registered-source restriction is enforced by this
adapter at submission time, not by the platform; and each SHA-pinned
workflow revision leaves a ``starlings/workflow-pin/<sha>`` branch in
the repository, which is the only dispatchable form GitHub accepts for
a pinned commit.
"""

import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from .. import gitops
from ..batches import Failure, invalid
from ..contracts import Adapter
from ..state import private_dir

SCHEMA = 1
MIN_POLL = 3.0
DISPATCH_WAIT = 120.0
HTTP_ERROR = re.compile(r"HTTP (\d+)")
IDENTITY = re.compile(r"^(.*) <([^>]*)> (\d+) ([+-]\d{4})$")


def environment():
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "STARLINGS_"))}
    env["GH_PROMPT_DISABLED"] = "1"
    return env


def iso8601(timestamp, offset):
    sign = -1 if offset.startswith("-") else 1
    delta = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5])) * sign
    return datetime.fromtimestamp(int(timestamp), timezone(delta)).isoformat()


def parse_timestamp(value):
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class GitHubHost:
    adapter = "github"

    def __init__(self, config, root=None):
        self.config = dict(config)
        self.host = self.config.get("host", "github.com")
        self.root = Path(root) if root is not None else None
        self.path = self.root / "adapters" / "github.json" if self.root is not None else None
        self.store = self._load()
        self.login = None
        self.last_poll = 0.0

    def _empty(self):
        return {"schema": SCHEMA, "host": self.host, "workflows": {}, "submissions": {}, "links": {}, "events": [], "seq": 0}

    def _load(self):
        if self.path is None:
            return self._empty()
        try:
            store = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._empty()
        if store.get("schema") != SCHEMA:
            raise invalid(f"github host journal {self.path} has unsupported schema {store.get('schema')}")
        return store

    def _save(self):
        if self.path is None:
            return
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

    # ---- gh subprocess plumbing -------------------------------------------------

    def binary(self):
        found = shutil.which("gh")
        if found is None:
            raise Failure("failed", "github-unavailable", "the gh CLI is not on PATH; the real GitHub adapter runs every host interaction through gh")
        return found

    def _gh(self, args, timeout=60, input=None):
        argv = [self.binary(), *args]
        try:
            return subprocess.run(argv, capture_output=True, text=True, input=input, timeout=timeout, check=False, env=environment())
        except subprocess.TimeoutExpired as exc:
            raise Failure("failed", "github-timeout", f"gh {args[0]} {' '.join(args[1:3])} timed out after {timeout}s") from exc
        except OSError as exc:
            raise Failure("failed", "github-unavailable", f"could not run gh: {exc}") from exc

    def _error(self, result):
        detail = (result.stderr or result.stdout).strip()
        match = HTTP_ERROR.search(detail)
        status = int(match.group(1)) if match else None
        return status, detail or f"exit {result.returncode}"

    def api(self, *args, timeout=60, input=None, missing=()):
        """Run ``gh api``; return parsed JSON, or ``None`` for a whitelisted status."""
        base = ["api", "--hostname", self.host] if self.host != "github.com" else ["api"]
        result = self._gh([*base, *args], timeout=timeout, input=input)
        if result.returncode:
            status, detail = self._error(result)
            if status is not None and status in missing:
                return None
            raise Failure("failed", "github-api", f"gh api {args[0]} failed" + (f" (HTTP {status})" if status else "") + f": {detail}")
        text = result.stdout.strip()
        return json.loads(text) if text else None

    def api_body(self, path, body, timeout=60, missing=()):
        work = Path(tempfile.mkdtemp(prefix="starlings-gh-body-"))
        try:
            payload = work / "body.json"
            payload.write_text(json.dumps(body), encoding="utf-8")
            return self.api("-X", "POST", path, "--input", str(payload), timeout=timeout, missing=missing)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _run(self, args, timeout=60):
        """Run a ``gh`` subcommand whose output is not parsed."""
        result = self._gh(list(args), timeout=timeout)
        if result.returncode:
            status, detail = self._error(result)
            raise Failure("failed", "github-api", f"gh {args[0]} {args[1] if len(args) > 1 else ''} failed" + (f" (HTTP {status})" if status else "") + f": {detail}")
        return result

    def _gh_json(self, args, timeout=60):
        result = self._run(list(args), timeout=timeout)
        text = result.stdout.strip()
        return json.loads(text) if text else None

    def _repo(self, repo):
        if "/" in repo:
            return repo
        if self.login is None:
            self.login = self.api("user")["login"]
        return f"{self.login}/{repo}"

    # ---- probe and records --------------------------------------------------------

    def probe(self):
        binary = self.binary()
        user = self.api("user")
        return {"adapter": "github", "host": self.host, "gh": binary, "live": True, "authenticated_as": user.get("login"), "note": "real adapter: submits workflow dispatches through gh; implemented but unqualified until a passing hosted gate is recorded"}

    def adapter_record(self):
        return Adapter("github", "1", ("submit_job", "completion", "replay", "pull_requests", "event_log")).to_dict()

    # ---- routing ------------------------------------------------------------------

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
        return hits

    def replay(self, kind, **fields):
        self.store = self._load()
        event = self._event(kind, **fields)
        routed = self._route(event)
        self._save()
        return {"event": event, "routed": routed}

    # ---- object upload (Git data API; verified by SHA equality) --------------------

    def _identity(self, line):
        match = IDENTITY.match(line or "")
        if match is None:
            raise Failure("failed", "github-object-mismatch", f"cannot parse commit identity line {line!r}")
        name, email, timestamp, offset = match.groups()
        return {"name": name, "email": email, "date": iso8601(timestamp, offset)}

    def _mismatch(self, kind, expected, created):
        observed = (created or {}).get("sha")
        if observed != expected:
            raise Failure("failed", "github-object-mismatch", f"uploaded {kind} for {expected} returned {observed}; objects could not be replicated byte-identically, refusing to continue")

    def _ensure_blob(self, repo, objects, sha, seen):
        if sha in seen:
            return
        content = gitops.git(objects, "cat-file", "blob", sha, text=False).stdout
        created = self.api_body(f"repos/{repo}/git/blobs", {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"})
        self._mismatch("blob", sha, created)
        seen.add(sha)

    def _ensure_tree(self, repo, objects, sha, seen):
        if sha in seen:
            return
        raw = gitops.git(objects, "ls-tree", "-z", sha).stdout
        entries = []
        for item in raw.split("\0"):
            if not item:
                continue
            meta, _, name = item.partition("\t")
            mode, kind, child = meta.split(" ")
            if kind == "blob":
                self._ensure_blob(repo, objects, child, seen)
                entries.append({"path": name, "mode": mode, "type": "blob", "sha": child})
            elif kind == "tree":
                self._ensure_tree(repo, objects, child, seen)
                entries.append({"path": name, "mode": "040000", "type": "tree", "sha": child})
            else:
                entries.append({"path": name, "mode": mode, "type": "commit", "sha": child})
        created = self.api_body(f"repos/{repo}/git/trees", {"tree": entries})
        self._mismatch("tree", sha, created)
        seen.add(sha)

    def _ensure_commit(self, repo, objects, sha, seen):
        if sha in seen:
            return
        if self.api(f"repos/{repo}/git/commits/{sha}", missing=(404, 422)) is not None:
            seen.add(sha)
            return
        raw = gitops.git(objects, "cat-file", "commit", sha).stdout
        header, _, message = raw.partition("\n\n")
        tree, parents, author, committer = None, [], None, None
        for line in header.splitlines():
            key, _, value = line.partition(" ")
            if key == "tree":
                tree = value
            elif key == "parent":
                parents.append(value)
            elif key == "author":
                author = value
            elif key == "committer":
                committer = value
        for parent in parents:
            self._ensure_commit(repo, objects, parent, seen)
        self._ensure_tree(repo, objects, tree, seen)
        body = {"message": message, "tree": tree, "parents": parents}
        if author is not None:
            body["author"] = self._identity(author)
        if committer is not None:
            body["committer"] = self._identity(committer)
        created = self.api_body(f"repos/{repo}/git/commits", body)
        self._mismatch("commit", sha, created)
        seen.add(sha)

    def _ensure_candidate_ref(self, repo, objects, head_sha):
        seen = set()
        self._ensure_commit(repo, objects, head_sha, seen)
        existing = self.api(f"repos/{repo}/git/ref/starlings/candidates/{head_sha}", missing=(404, 422))
        if existing is None:
            existing = self.api_body(f"repos/{repo}/git/refs", {"ref": f"refs/starlings/candidates/{head_sha}", "sha": head_sha})
        observed = ((existing or {}).get("object") or {}).get("sha")
        if observed != head_sha:
            raise Failure("failed", "github-object-mismatch", f"refs/starlings/candidates/{head_sha} points to {observed}; refusing to dispatch against an unverified ref")

    # ---- submission and completion -------------------------------------------------

    def _pin_ref(self, repo, pinned):
        """Resolve a pinned workflow revision to a dispatchable ref name.

        ``POST .../dispatches`` accepts only a branch or tag name for
        ``ref`` — a bare commit SHA is refused with ``No ref found``.
        A SHA pin is therefore verified to exist upstream and held under
        the deterministic branch ``starlings/workflow-pin/<sha>``, so the
        workflow file GitHub reads is still exactly the pinned commit.
        A non-SHA pin (a branch or tag name) is dispatched directly.
        """
        if re.fullmatch(r"[0-9a-f]{40}", pinned) is None:
            return pinned
        if self.api(f"repos/{repo}/git/commits/{pinned}", missing=(404, 422)) is None:
            raise Failure("failed", "github-workflow-pin", f"pinned reusable workflow revision {pinned} is not a commit in {repo}")
        name = f"starlings/workflow-pin/{pinned}"
        ref = self.api(f"repos/{repo}/git/ref/heads/{name}", missing=(404, 422))
        if ref is None:
            ref = self.api_body(f"repos/{repo}/git/refs", {"ref": f"refs/heads/{name}", "sha": pinned})
        observed = ((ref or {}).get("object") or {}).get("sha")
        if observed != pinned:
            raise Failure("failed", "github-workflow-pin", f"workflow pin ref refs/heads/{name} points to {observed}, not the pinned revision {pinned}; refusing to dispatch")
        return name

    def _find_run(self, repo, filename, marker):
        deadline = time.monotonic() + DISPATCH_WAIT
        while time.monotonic() < deadline:
            # ``gh api path -f k=v`` silently switches to POST; the runs
            # endpoint is GET-only and answers 404 to POST, so the method
            # must be pinned explicitly. A real 404 inside the wait window
            # means "not yet visible", not failure.
            data = self.api(f"repos/{repo}/actions/workflows/{filename}/runs", "-X", "GET", "-f", "event=workflow_dispatch", "-f", "per_page=10", missing=(404,))
            for run in (data or {}).get("workflow_runs", []):
                title = f"{run.get('name') or ''}\n{run.get('display_title') or ''}"
                if marker in title:
                    return run
            time.sleep(3)
        raise Failure("failed", "github-run-missing", f"the dispatched run for {marker!r} did not appear in {repo}'s Actions list within {int(DISPATCH_WAIT)}s; inspect the Actions tab")

    def submit_job(self, repo, name, workflow, revision, head_sha, event, payload):
        self.store = self._load()
        registered = self.store["workflows"].get(workflow)
        if registered is None:
            raise Failure("blocked", "unrouted", f"adapter github has no registered workflow {workflow!r}; nothing was submitted")
        if event.get("base") not in registered["branches"] or any(source not in registered["sources"] for source in event.get("source", [])):
            raise Failure("blocked", "unrouted", f"submission to {workflow!r} is not routed: base {event.get('base')!r} with source {event.get('source')!r} is outside the registered branches {registered['branches']} and sources {registered['sources']}")
        objects = payload.get("objects")
        if not objects or not Path(objects).is_dir():
            raise invalid("hosted submission payload has no usable objects path")
        api_repo = self._repo(repo)
        self._ensure_candidate_ref(api_repo, objects, head_sha)
        path, _, pinned = revision.rpartition("@")
        filename = Path(path).name
        dispatch_ref = self._pin_ref(api_repo, pinned)
        run_token = str(payload.get("run_id") or "adhoc")
        inputs = {"head_sha": head_sha, "check_id": name, "run_id": run_token, "attempt": "1", "candidate_id": str(payload.get("candidate_id") or ""), "plan_id": str(payload.get("plan_id") or ""), "repo": repo, "argv": json.dumps(payload.get("argv") or []), "artifacts": json.dumps(payload.get("artifacts") or [])}
        args = ["-X", "POST", f"repos/{api_repo}/actions/workflows/{filename}/dispatches", "-f", f"ref={dispatch_ref}"]
        for key, value in inputs.items():
            args += ["-f", f"inputs[{key}]={value}"]
        self.api(*args)
        requested = self._event("workflow_run", action="requested", run_id=None, attempt=1, name=name, repo=repo, workflow=workflow, revision=revision, head_sha=head_sha, event=event)
        self._save()
        marker = f"{run_token}/{name}"
        run = self._find_run(api_repo, filename, marker)
        run_id, attempt = str(run["id"]), int(run.get("run_attempt") or 1)
        requested["run_id"], requested["attempt"] = run_id, attempt
        self.store["submissions"][run_id] = {"submission": f"{run_token}|{name}", "repo": repo, "api_repo": api_repo, "name": name, "workflow": workflow, "revision": revision, "dispatch_ref": dispatch_ref, "head_sha": head_sha, "attempt": attempt, "event": event, "artifact": f"starlings-{run_token}-{name}", "required": list(payload.get("artifacts") or []), "run": {"id": run_id, "attempt": attempt, "url": run.get("html_url")}, "completed": None}
        self._save()
        return {"adapter": "github", "host": self.host, "repo": repo, "event": event, "run_id": run_id, "attempt": attempt, "workflow": workflow, "revision": revision, "head_sha": head_sha}

    def _collect_result(self, record, run):
        result = {"conclusion": run.get("conclusion"), "exit_code": 1, "stdout": "", "stderr": "", "artifacts": {}, "missing": list(record["required"]), "duration_seconds": None, "head_sha": run.get("head_sha")}
        started, finished = parse_timestamp(run.get("run_started_at")), parse_timestamp(run.get("updated_at"))
        if started is not None and finished is not None:
            result["duration_seconds"] = round(finished - started, 3)
        artifacts = self.api(f"repos/{record['api_repo']}/actions/runs/{record['run']['id']}/artifacts", "-X", "GET", "-f", "per_page=50")
        entry = next((item for item in (artifacts or {}).get("artifacts", []) if item.get("name") == record["artifact"] and not item.get("expired")), None)
        if entry is None:
            result["stderr"] = f"starlings: result artifact {record['artifact']!r} was not found on run {record['run']['id']}"
            return result
        work = Path(tempfile.mkdtemp(prefix="starlings-gh-result-"))
        try:
            self._run(["run", "download", str(record["run"]["id"]), "--repo", record["api_repo"], "--name", record["artifact"], "--dir", str(work)], timeout=180)
            produced = {}
            result_json = work / "result.json"
            if result_json.is_file():
                try:
                    data = json.loads(result_json.read_text(encoding="utf-8"))
                except ValueError:
                    data = {}
                produced["result"] = data
                if type(data.get("exit_code")) is int:
                    result["exit_code"] = data["exit_code"]
                if type(data.get("head_sha")) is str:
                    result["head_sha"] = data["head_sha"]
            else:
                result["stderr"] = f"starlings: result.json was missing from artifact {record['artifact']!r}"
            for name, key in (("stdout.txt", "stdout"), ("stderr.txt", "stderr")):
                path = work / name
                if path.is_file():
                    result[key] = path.read_text(encoding="utf-8", errors="replace")
            staged = work / "staged"
            files = {}
            if staged.is_dir():
                for path in sorted(staged.rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        files[path.relative_to(staged).as_posix()] = base64.b64encode(path.read_bytes()).decode("ascii")
            result["artifacts"] = files
            result["missing"] = [item for item in record["required"] if item not in files]
            result["produced"] = produced
            return result
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _observe(self, run_id, record):
        if record.get("completed") is not None:
            return record["completed"]
        run = self.api(f"repos/{record['api_repo']}/actions/runs/{record['run']['id']}")
        if run.get("status") != "completed":
            return None
        record["run"].update({"status": run.get("status"), "conclusion": run.get("conclusion"), "url": run.get("html_url")})
        result = self._collect_result(record, run)
        record["completed"] = result
        completed = self._event("workflow_run", action="completed", run_id=run_id, attempt=record["attempt"], name=record["name"], repo=record["repo"], workflow=record["workflow"], revision=record["revision"], head_sha=result.get("head_sha"), conclusion=result.get("conclusion"))
        record["completed_seq"] = completed["seq"]
        self._event("check_run", action="completed", run_id=run_id, attempt=record["attempt"], name=record["name"], head_sha=result.get("head_sha"), conclusion=result.get("conclusion"))
        return result

    def _completed_view(self, run_id, record):
        event = next((item for item in self.store["events"] if item.get("seq") == record.get("completed_seq")), None)
        if event is None:
            event = {"seq": record.get("completed_seq"), "type": "workflow_run", "action": "completed", "run_id": run_id, "attempt": record["attempt"], "name": record["name"], "repo": record["repo"], "workflow": record["workflow"], "revision": record["revision"], "head_sha": record["completed"].get("head_sha"), "conclusion": record["completed"].get("conclusion")}
        return {**event, "result": record["completed"]}

    def completion(self, submission):
        self.store = self._load()
        record = self.store["submissions"].get(submission["run_id"])
        if record is None:
            raise Failure("failed", "github-run-missing", f"adapter journal has no submission for run {submission['run_id']}; it cannot be attributed")
        now = time.monotonic()
        if record.get("completed") is None and now - self.last_poll < MIN_POLL:
            return None
        self.last_poll = now
        if self._observe(submission["run_id"], record) is None:
            return None
        self._save()
        return self._completed_view(submission["run_id"], record)

    def event_log(self):
        self.store = self._load()
        for run_id, record in sorted(self.store["submissions"].items()):
            if record.get("completed") is None:
                try:
                    if self._observe(run_id, record) is not None:
                        self._save()
                except Failure:
                    continue
        return list(self.store["events"])

    # ---- pull requests ---------------------------------------------------------------

    def pull_request(self, repo, source):
        self.store = self._load()
        api_repo = self._repo(repo)
        entries = self._gh_json(["pr", "list", "--repo", api_repo, "--head", source, "--state", "all", "--json", "number,state,headRefName,headRefOid,baseRefName,title,url,mergeCommit"])
        for pr in entries or []:
            state = (pr.get("state") or "OPEN").lower()
            link = None
            if state == "merged":
                link = ((pr.get("mergeCommit") or {}).get("oid")) or pr.get("headRefOid")
            elif state == "closed":
                link = self.store["links"].get(f"{repo}|{pr['number']}")
            return {"number": pr["number"], "repo": repo, "base": pr.get("baseRefName"), "source": pr.get("headRefName"), "head": pr.get("headRefOid"), "title": pr.get("title"), "url": pr.get("url"), "state": state, "link": link}
        return None

    def close_pull_request(self, repo, number, link=None):
        api_repo = self._repo(repo)
        if link:
            self._run(["pr", "comment", str(number), "--repo", api_repo, "--body", f"starlings: landed {link}"])
        self._run(["pr", "close", str(number), "--repo", api_repo])
        self.store = self._load()
        if link:
            self.store["links"][f"{repo}|{number}"] = link
        self._event("pull_request", action="closed", number=number, repo=repo, link=link)
        self._save()
        return {"number": number, "repo": repo, "state": "closed", "link": link}
