"""Unit tests for the real GitHub adapter.

Every test drives ``GitHubHost`` against a deterministic fake ``gh``
binary on ``PATH`` (see ``fake_gh.py``); no socket is ever opened. The
object-upload tests build a real one-commit Git repository so the
recorded blob/tree/commit SHAs the adapter uploads are genuine.
"""

import base64
import json
import tempfile
import unittest
from pathlib import Path

import fixtures
from fake_gh import FakeGh
from starlings.adapters.github import GitHubHost, iso8601
from starlings.batches import Failure

WORKFLOW = ".github/workflows/starlings-caller.yml"
PINNED = "a" * 40
REUSABLE = f"example/workflows/.github/workflows/reusable-check.yml@{PINNED}"
REPO = "o/r"
HEAD = "b" * 40
CONFIG = {"adapter": "github", "host": "github.com", "reusable_workflow": REUSABLE, "workflow": WORKFLOW}
EVENT = {"type": "pull_request", "action": "synchronized", "base": "main", "source": ["dev"]}


def make_commit(test):
    """Create a real one-commit repository; return (path, sha, tree, blob)."""
    root = Path(tempfile.mkdtemp(prefix="starlings-gh-repo-"))
    test.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
    fixtures.git(root, "init", "--quiet", "--initial-branch=main", ".")
    (root / "a.txt").write_text("alpha\n")
    fixtures.git(root, "add", "a.txt")
    fixtures.git(root, "commit", "--quiet", "-m", "seed")
    sha = fixtures.git(root, "rev-parse", "HEAD")
    tree = fixtures.git(root, "rev-parse", "HEAD^{tree}")
    blob = fixtures.git(root, "rev-parse", "HEAD:a.txt")
    return root, sha, tree, blob


class AdapterCase(unittest.TestCase):
    def host(self):
        self.gh = FakeGh(self)
        self.state = Path(tempfile.mkdtemp(prefix="starlings-gh-state-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.state, ignore_errors=True))
        return GitHubHost(CONFIG, self.state)


class ProbeTests(AdapterCase):
    def test_probe_reports_authenticated_user(self):
        host = self.host()
        self.gh.respond("api user", stdout=json.dumps({"login": "octocat"}))
        probe = host.probe()
        self.assertEqual((probe["adapter"], probe["host"], probe["live"], probe["authenticated_as"]), ("github", "github.com", True, "octocat"))
        self.assertTrue(probe["gh"].endswith("gh"))

    def test_probe_failure_surfaces_as_adapter_error(self):
        host = self.host()
        self.gh.respond("api user", stderr="HTTP 401: Unauthorized", exit=1)
        with self.assertRaises(Failure) as caught:
            host.probe()
        self.assertEqual((caught.exception.status, caught.exception.code), ("failed", "github-api"))

    def test_missing_gh_binary_is_unavailable(self):
        host = self.host()
        import os
        kept = os.environ["PATH"]
        try:
            os.environ["PATH"] = str(self.gh.root / "empty-dir-that-does-not-exist")
            with self.assertRaises(Failure) as caught:
                host.binary()
            self.assertEqual(caught.exception.code, "github-unavailable")
        finally:
            os.environ["PATH"] = kept


class RoutingTests(AdapterCase):
    def test_unrouted_submission_sends_nothing(self):
        host = self.host()
        host.configure(WORKFLOW, {"branches": ["main"], "sources": ["dev"]})
        with self.assertRaises(Failure) as caught:
            host.submit_job(REPO, "unit", WORKFLOW, REUSABLE, HEAD, {"type": "pull_request", "action": "synchronized", "base": "dev", "source": ["dev"]}, {})
        self.assertEqual((caught.exception.status, caught.exception.code), ("blocked", "unrouted"))
        self.assertEqual(self.gh.calls(), [])
        with self.assertRaises(Failure) as caught:
            host.submit_job(REPO, "unit", WORKFLOW, REUSABLE, HEAD, {"type": "pull_request", "action": "synchronized", "base": "main", "source": ["feature-x"]}, {})
        self.assertEqual(caught.exception.code, "unrouted")
        self.assertEqual(self.gh.calls(), [])

    def test_replay_routes_registered_workflow(self):
        host = self.host()
        host.configure(WORKFLOW, {"branches": ["main"], "sources": ["dev"]})
        routed = host.replay("pull_request", action="opened", base="main", source="dev")
        self.assertEqual(routed["routed"], [WORKFLOW])
        self.assertEqual(host.replay("pull_request", action="opened", base="dev", source="dev")["routed"], [])
        self.assertEqual(host.replay("push", ref="refs/heads/main")["routed"], [WORKFLOW])
        events = host.store["events"]
        self.assertEqual([event["type"] for event in events], ["pull_request", "pull_request", "push"])


class SubmitTests(AdapterCase):
    def script_upload(self, sha, tree, blob):
        self.gh.respond(f"-X POST repos/{REPO}/git/blobs", stdout=json.dumps({"sha": blob}))
        self.gh.respond(f"-X POST repos/{REPO}/git/trees", stdout=json.dumps({"sha": tree}))
        self.gh.respond(f"-X POST repos/{REPO}/git/commits", stdout=json.dumps({"sha": sha}))
        self.gh.respond(f"-X POST repos/{REPO}/git/refs", stdout=json.dumps({"ref": f"refs/starlings/candidates/{sha}", "object": {"sha": sha, "type": "commit"}}))
        self.gh.respond(f"repos/{REPO}/git/commits/{sha}", stderr="HTTP 404: Not Found", exit=1)
        self.gh.respond(f"repos/{REPO}/git/ref/starlings/candidates/{sha}", stderr="HTTP 404: Not Found", exit=1)
        self.script_pin()
        self.gh.respond("dispatches")
        self.gh.respond("actions/workflows/reusable-check.yml/runs", stdout=json.dumps({"workflow_runs": [{"id": 12345678901, "run_attempt": 1, "name": "starlings/run-001/unit/1", "display_title": "starlings/run-001/unit/1", "html_url": "https://github.com/o/r/actions/runs/12345678901", "head_sha": PINNED}]}))

    def script_pin(self):
        """Script the pinned-workflow resolution: commit lookup plus the
        deterministic ``starlings/workflow-pin/<sha>`` branch the adapter
        dispatches on (GitHub refuses a bare SHA as the dispatch ref)."""
        self.gh.respond(f"api repos/{REPO}/git/commits/{PINNED}", exact=True, stdout=json.dumps({"sha": PINNED}))
        self.gh.respond(f"api repos/{REPO}/git/ref/heads/starlings/workflow-pin/{PINNED}", exact=True, stderr="HTTP 404: Not Found", exit=1)
        self.gh.respond(f'"ref": "refs/heads/starlings/workflow-pin/{PINNED}"', stdout=json.dumps({"ref": f"refs/heads/starlings/workflow-pin/{PINNED}", "object": {"sha": PINNED, "type": "commit"}}))

    def payload(self, objects):
        return {"argv": ["python3", "-m", "unittest", "tests.test_unit"], "artifacts": ["out/journey.txt"], "timeout": 60, "objects": str(objects), "run_id": "run-001", "plan_id": "plan:x", "candidate_id": "candidate:y"}

    def test_submit_uploads_objects_dispatches_and_binds_run(self):
        host = self.host()
        host.configure(WORKFLOW, {"branches": ["main"], "sources": ["dev"]})
        objects, sha, tree, blob = make_commit(self)
        self.script_upload(sha, tree, blob)
        submission = host.submit_job(REPO, "unit", WORKFLOW, REUSABLE, sha, EVENT, self.payload(objects))
        self.assertEqual(submission, {"adapter": "github", "host": "github.com", "repo": REPO, "event": EVENT, "run_id": "12345678901", "attempt": 1, "workflow": WORKFLOW, "revision": REUSABLE, "head_sha": sha})
        joined = self.gh.joined()
        dispatch = next(call for call in joined if "dispatches" in call)
        self.assertIn(f"-f ref=starlings/workflow-pin/{PINNED}", dispatch)
        for field in (f"inputs[head_sha]={sha}", "inputs[check_id]=unit", "inputs[run_id]=run-001", "inputs[attempt]=1", "inputs[candidate_id]=candidate:y", "inputs[plan_id]=plan:x", f"inputs[repo]={REPO}", 'inputs[argv]=["python3", "-m", "unittest", "tests.test_unit"]', 'inputs[artifacts]=["out/journey.txt"]'):
            self.assertIn(field, dispatch)
        requested = next(event for event in host.store["events"] if event.get("action") == "requested")
        self.assertEqual((requested["type"], requested["run_id"], requested["attempt"], requested["name"], requested["repo"], requested["workflow"], requested["revision"], requested["head_sha"], requested["event"]), ("workflow_run", "12345678901", 1, "unit", REPO, WORKFLOW, REUSABLE, sha, EVENT))
        record = host.store["submissions"]["12345678901"]
        self.assertEqual((record["name"], record["artifact"], record["required"], record["attempt"]), ("unit", "starlings-run-001-unit", ["out/journey.txt"], 1))
        posts = [call for call in joined if "-X POST" in call]
        self.assertTrue(any("git/blobs" in call for call in posts), posts)
        self.assertTrue(any("git/trees" in call for call in posts), posts)
        self.assertTrue(any("git/commits" in call for call in posts), posts)
        self.assertTrue(any("git/refs" in call for call in posts), posts)

    def test_pin_ref_mismatch_refuses_dispatch(self):
        host = self.host()
        host.configure(WORKFLOW, {"branches": ["main"], "sources": ["dev"]})
        objects, sha, tree, blob = make_commit(self)
        self.script_upload(sha, tree, blob)
        self.gh.respond(f'"ref": "refs/heads/starlings/workflow-pin/{PINNED}"', stdout=json.dumps({"ref": f"refs/heads/starlings/workflow-pin/{PINNED}", "object": {"sha": "0" * 40, "type": "commit"}}))
        with self.assertRaises(Failure) as caught:
            host.submit_job(REPO, "unit", WORKFLOW, REUSABLE, sha, EVENT, self.payload(objects))
        self.assertEqual(caught.exception.code, "github-workflow-pin")
        self.assertFalse(any("dispatches" in call for call in self.gh.joined()))

    def test_object_mismatch_is_a_loud_error(self):
        host = self.host()
        host.configure(WORKFLOW, {"branches": ["main"], "sources": ["dev"]})
        objects, sha, tree, blob = make_commit(self)
        self.gh.respond(f"-X POST repos/{REPO}/git/blobs", stdout=json.dumps({"sha": blob}))
        self.gh.respond(f"-X POST repos/{REPO}/git/trees", stdout=json.dumps({"sha": "0" * 40}))
        self.gh.respond(f"-X POST repos/{REPO}/git/commits", stdout=json.dumps({"sha": sha}))
        self.gh.respond(f"-X POST repos/{REPO}/git/refs", stdout="{}")
        self.gh.respond(f"repos/{REPO}/git/commits/{sha}", stderr="HTTP 404: Not Found", exit=1)
        self.gh.respond(f"repos/{REPO}/git/ref/starlings/candidates/{sha}", stderr="HTTP 404: Not Found", exit=1)
        with self.assertRaises(Failure) as caught:
            host.submit_job(REPO, "unit", WORKFLOW, REUSABLE, sha, EVENT, self.payload(objects))
        self.assertEqual(caught.exception.code, "github-object-mismatch")
        self.assertFalse(any("dispatches" in call for call in self.gh.joined()))

    def test_existing_commit_skips_upload(self):
        host = self.host()
        host.configure(WORKFLOW, {"branches": ["main"], "sources": ["dev"]})
        objects, sha, tree, blob = make_commit(self)
        self.gh.respond(f"repos/{REPO}/git/commits/{sha}", stdout=json.dumps({"sha": sha}))
        self.gh.respond(f"repos/{REPO}/git/ref/starlings/candidates/{sha}", stderr="HTTP 404: Not Found", exit=1)
        self.gh.respond(f"-X POST repos/{REPO}/git/refs", stdout=json.dumps({"object": {"sha": sha}}))
        self.script_pin()
        self.gh.respond("dispatches")
        self.gh.respond("actions/workflows/reusable-check.yml/runs", stdout=json.dumps({"workflow_runs": [{"id": 99, "run_attempt": 1, "name": "starlings/run-001/unit/1"}]}))
        submission = host.submit_job(REPO, "unit", WORKFLOW, REUSABLE, sha, EVENT, self.payload(objects))
        self.assertEqual(submission["run_id"], "99")
        self.assertFalse(any("git/blobs" in call for call in self.gh.joined()))
        self.assertFalse(any("git/trees" in call for call in self.gh.joined()))


class CompletionTests(AdapterCase):
    def recorded(self, host):
        host.store["submissions"]["12345678901"] = {"submission": "run-001|unit", "repo": REPO, "api_repo": REPO, "name": "unit", "workflow": WORKFLOW, "revision": REUSABLE, "head_sha": HEAD, "attempt": 1, "event": EVENT, "artifact": "starlings-run-001-unit", "required": ["out/journey.txt"], "run": {"id": "12345678901", "attempt": 1, "url": "u"}, "completed": None}
        host._save()

    def test_completion_collects_result_and_artifacts(self):
        host = self.host()
        self.recorded(host)
        result_json = {"exit_code": 0, "check_id": "unit", "head_sha": HEAD, "run_id": "run-001", "attempt": "1", "produced": ["out/journey.txt"]}
        self.gh.respond("actions/runs/12345678901/artifacts", stdout=json.dumps({"artifacts": [{"name": "starlings-run-001-unit", "expired": False}]}))
        self.gh.respond(f"api repos/{REPO}/actions/runs/12345678901", exact=True, stdout=json.dumps({"status": "completed", "conclusion": "success", "run_attempt": 1, "head_sha": PINNED, "run_started_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:01:30Z", "html_url": "u"}))
        self.gh.respond("run download", write={"result.json": json.dumps(result_json), "stdout.txt": "all good\n", "stderr.txt": "", "staged/out/journey.txt": "JOURNEY\n"})
        completion = host.completion({"run_id": "12345678901", "head_sha": HEAD})
        self.assertIsNotNone(completion)
        self.assertEqual((completion["action"], completion["run_id"], completion["attempt"], completion["name"], completion["conclusion"], completion["head_sha"]), ("completed", "12345678901", 1, "unit", "success", HEAD))
        result = completion["result"]
        self.assertEqual((result["exit_code"], result["stdout"], result["missing"], result["conclusion"]), (0, "all good\n", [], "success"))
        self.assertEqual(base64.b64decode(result["artifacts"]["out/journey.txt"]).decode(), "JOURNEY\n")
        self.assertEqual(result["duration_seconds"], 90.0)
        log = host.event_log()
        completed = next(event for event in log if event.get("type") == "workflow_run" and event.get("action") == "completed")
        self.assertEqual((completed["run_id"], completed["attempt"], completed["name"], completed["workflow"], completed["revision"], completed["head_sha"], completed["conclusion"]), ("12345678901", 1, "unit", WORKFLOW, REUSABLE, HEAD, "success"))
        self.assertTrue(any(event.get("type") == "check_run" for event in log))
        again = host.completion({"run_id": "12345678901", "head_sha": HEAD})
        self.assertEqual(again["result"]["exit_code"], 0)

    def test_pending_run_returns_none(self):
        host = self.host()
        self.recorded(host)
        self.gh.respond(f"api repos/{REPO}/actions/runs/12345678901", exact=True, stdout=json.dumps({"status": "in_progress", "conclusion": None}))
        self.assertIsNone(host.completion({"run_id": "12345678901", "head_sha": HEAD}))

    def test_unknown_submission_is_loud(self):
        host = self.host()
        with self.assertRaises(Failure) as caught:
            host.completion({"run_id": "55", "head_sha": HEAD})
        self.assertEqual(caught.exception.code, "github-run-missing")

    def test_missing_result_artifact_is_reported(self):
        host = self.host()
        self.recorded(host)
        self.gh.respond("actions/runs/12345678901/artifacts", stdout=json.dumps({"artifacts": []}))
        self.gh.respond(f"api repos/{REPO}/actions/runs/12345678901", exact=True, stdout=json.dumps({"status": "completed", "conclusion": "success", "run_attempt": 1, "head_sha": PINNED}))
        completion = host.completion({"run_id": "12345678901", "head_sha": HEAD})
        self.assertIn("not found", completion["result"]["stderr"])
        self.assertEqual(completion["result"]["missing"], ["out/journey.txt"])


class PullRequestTests(AdapterCase):
    def test_lookup_states_and_close_with_link(self):
        host = self.host()
        entry = {"number": 7, "state": "OPEN", "headRefName": "starlings/b1/lanes/work", "headRefOid": "h" * 40, "baseRefName": "dev", "title": "lane work", "url": "https://u", "mergeCommit": None}
        self.gh.respond("pr list", stdout=json.dumps([entry]))
        observed = host.pull_request(REPO, "starlings/b1/lanes/work")
        self.assertEqual((observed["state"], observed["number"], observed["base"], observed["source"]), ("open", 7, "dev", "starlings/b1/lanes/work"))
        self.gh.respond("pr comment")
        self.gh.respond("pr close")
        host.close_pull_request(REPO, 7, link="c" * 40)
        joined = self.gh.joined()
        self.assertTrue(any("pr comment 7" in call and "c" * 40 in call for call in joined))
        self.assertTrue(any("pr close 7" in call for call in joined))
        self.assertEqual(host.store["links"][f"{REPO}|7"], "c" * 40)
        entry["state"] = "CLOSED"
        self.gh.respond("pr list", stdout=json.dumps([entry]))
        observed = host.pull_request(REPO, "starlings/b1/lanes/work")
        self.assertEqual((observed["state"], observed["link"]), ("closed", "c" * 40))

    def test_merged_state_uses_merge_commit(self):
        host = self.host()
        entry = {"number": 9, "state": "MERGED", "headRefName": "starlings/b1/solo", "headRefOid": "d" * 40, "baseRefName": "dev", "title": "t", "url": "u", "mergeCommit": {"oid": "e" * 40}}
        self.gh.respond("pr list", stdout=json.dumps([entry]))
        observed = host.pull_request(REPO, "starlings/b1/solo")
        self.assertEqual((observed["state"], observed["link"], observed["head"]), ("merged", "e" * 40, "d" * 40))

    def test_absent_pr_is_none(self):
        host = self.host()
        self.gh.respond("pr list", stdout="[]")
        self.assertIsNone(host.pull_request(REPO, "starlings/b1/none"))


class IdentityTests(unittest.TestCase):
    def test_iso8601_conversion(self):
        self.assertEqual(iso8601("1700000000", "+0000"), "2023-11-14T22:13:20+00:00")
        self.assertEqual(iso8601("1700000000", "-0500"), "2023-11-14T17:13:20-05:00")


if __name__ == "__main__":
    unittest.main()
