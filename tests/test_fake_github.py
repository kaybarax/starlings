import base64
import json
import unittest

from fixtures import EXCLUDED, Fixture
from test_verifier import FILES, LEDGER, build_policy, call, ok
from starlings import promotion, runner, signer, verifier
from starlings.adapters import host as adapters_host
from starlings.adapters import templates as adapters_templates
from starlings.adapters.fake_github import FakeHost
from starlings.batches import Failure, load_batch
from starlings.cli import main
from starlings.contracts import Receipt
from starlings.policy import PolicyError, parse, preset, render, validate

WORKFLOW = ".github/workflows/starlings-caller.yml"
REUSABLE = "example/workflows/reusable-check.yml@" + "a" * 40
CHECKS = {"journey": ["python3", "tests/journey_check.py"], "ledger": LEDGER}


def github_policy(checks=CHECKS, staging="none", release="full", adapter="fake", artifacts=None):
    policy = preset("each-lane-release")
    policy["project"] = "fixture-project"
    policy["checks"] = dict(checks)
    for name, scope in (("staging", staging), ("release", release)):
        gate = policy["gates"][name]
        gate["scope"] = scope
        gate["checks"] = list(checks) if scope != "none" else []
        if scope != "none":
            gate.update({"executor": "github", "environment": "native"})
    policy["assembly"] = {"excluded": list(EXCLUDED)}
    policy["github"] = {"adapter": adapter, "host": "fake.github.test", "reusable_workflow": REUSABLE, "workflow": WORKFLOW}
    if artifacts is not None:
        policy["artifacts"] = artifacts
    policy["limits"]["timeout_seconds"] = 60
    return render(policy)


class GithubCase(unittest.TestCase):
    def make(self, policy, files=FILES):
        fixture = Fixture(policy=policy, files=files)
        self.addCleanup(fixture.close)
        return fixture

    def ready(self, fixture):
        ok(self, call(fixture, verifier.trust, str(fixture.authority), fixture.head("dev"), "review:policy"))
        with fixture.context() as ctx:
            signer.init(ctx.root)

    def stage(self, fixture, files, batch="b1", expected="ok"):
        ok(self, fixture.call("prepare", batch, ["ledger"], str(fixture.authority)))
        work = ok(self, fixture.call("assign", batch, "solo", "ledger", "worker-a", []))
        head = fixture.work(work["worktree"], files, "solo change")
        ok(self, fixture.call("handoff", batch, "solo", head, "review:solo", []))
        ok(self, fixture.call("assemble", batch, "ledger"))
        ok(self, fixture.call("freeze", batch, "ledger"))
        return ok(self, fixture.call("stage", batch, ["ledger"]), expected)

    def release_plan(self, fixture):
        return ok(self, call(fixture, verifier.plan, "b1", "release"))

    def batch_row(self, fixture, batch="b1"):
        with fixture.context() as ctx:
            return dict(ctx.store.connection.execute("SELECT * FROM batches WHERE name = ?", (batch,)).fetchone())


class PolicyTableGithubTests(unittest.TestCase):
    def test_github_table_validation(self):
        data = parse(github_policy())
        self.assertEqual(data["github"], {"adapter": "fake", "host": "fake.github.test", "reusable_workflow": REUSABLE, "workflow": WORKFLOW})
        self.assertEqual(parse(render(data)), data)
        self.assertNotIn("github", preset("release-only"))
        github_preset = preset("each-lane-release", "github")
        self.assertEqual(github_preset["github"]["adapter"], "fake")
        cases = [("adapter", "gitlab"), ("host", ""), ("reusable_workflow", "path@main"), ("reusable_workflow", "path@" + "a" * 39), ("reusable_workflow", "path@" + "A" * 40), ("workflow", "workflows/x.yml"), ("workflow", ".github/workflows/x.txt"), ("extra", 1)]
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaises(PolicyError):
                bad = parse(github_policy())
                bad["github"][key] = value
                validate(bad)
        for key in ("reusable_workflow", "workflow"):
            with self.subTest(missing=key), self.assertRaisesRegex(PolicyError, key):
                bad = parse(github_policy())
                bad["github"].pop(key)
                validate(bad)
        with self.assertRaisesRegex(PolicyError, "github"):
            bad = parse(github_policy())
            bad.pop("github")
            validate(bad)
        with self.assertRaisesRegex(PolicyError, "environment"):
            bad = parse(github_policy())
            bad["gates"]["release"]["environment"] = "guest"
            bad["guest"] = {"image": "python@sha256:" + "b" * 64}
            validate(bad)
        with self.assertRaises(PolicyError):
            bad = parse(github_policy())
            bad["github"] = {"adapter": "fake", "surprise": 1}
            validate(bad)


class RoutingTests(GithubCase):
    def test_replay_routes_only_configured_base_and_source(self):
        fixture = self.make(github_policy())
        self.ready(fixture)
        self.stage(fixture, {"ledger.txt": "total = 1\n"})
        with fixture.context() as ctx:
            host = FakeHost(fixture.state)
            adapters_host.configure(ctx, host, json.loads(self.batch_row(fixture)["policy"]), "b1")
            registration = host.store["workflows"][WORKFLOW]
            self.assertEqual(registration, {"branches": ["main"], "sources": ["dev"]})
            self.assertEqual(host.replay("pull_request", action="opened", base="main", source="dev")["routed"], [WORKFLOW])
            self.assertEqual(host.replay("pull_request", action="synchronized", base="main", source="dev")["routed"], [WORKFLOW])
            self.assertEqual(host.replay("pull_request", action="merged", base="main", source="dev")["routed"], [WORKFLOW])
            self.assertEqual(host.replay("pull_request", action="closed", base="main", source="dev")["routed"], [WORKFLOW])
            self.assertEqual(host.replay("merge_group", action="checks_requested", base="main")["routed"], [WORKFLOW])
            self.assertEqual(host.replay("workflow_dispatch", ref="refs/heads/main")["routed"], [WORKFLOW])
            self.assertEqual(host.replay("push", ref="refs/heads/main")["routed"], [WORKFLOW])
            self.assertEqual(host.replay("check_run", action="completed")["routed"], [])
            self.assertEqual(host.replay("pull_request", action="opened", base="dev", source="starlings/b1/lanes/ledger")["routed"], [])
            self.assertEqual(host.replay("pull_request", action="opened", base="main", source="feature-unlisted")["routed"], [])
            self.assertEqual(host.replay("push", ref="refs/heads/feature-unlisted")["routed"], [])
            self.assertEqual(host.replay("workflow_dispatch", ref="refs/heads/feature-unlisted")["routed"], [])
        with self.assertRaises(Failure) as caught:
            host.submit_job("fixture-project", "ledger", WORKFLOW, REUSABLE, "b" * 40, {"type": "pull_request", "action": "synchronized", "base": "dev", "source": ["starlings/b1/lanes/ledger"]}, {})
        self.assertEqual(caught.exception.code, "unrouted")

    def test_staging_gate_routes_lane_sources(self):
        fixture = self.make(github_policy(staging="full"))
        self.ready(fixture)
        self.stage(fixture, {"ledger.txt": "total = 1\n"}, expected="pending")
        with fixture.context() as ctx:
            host = FakeHost(fixture.state)
            adapters_host.configure(ctx, host, json.loads(self.batch_row(fixture)["policy"]), "b1")
            registration = host.store["workflows"][WORKFLOW]
            self.assertEqual(registration, {"branches": ["dev", "main"], "sources": ["dev", "starlings/b1/lanes/ledger"]})
            self.assertEqual(host.replay("pull_request", action="opened", base="dev", source="starlings/b1/lanes/ledger")["routed"], [WORKFLOW])
            self.assertEqual(host.replay("pull_request", action="opened", base="dev", source="starlings/b1/lanes/other")["routed"], [])
            self.assertEqual(host.replay("push", ref="refs/heads/feature-unlisted")["routed"], [])


class LocalProfileTests(GithubCase):
    def test_local_profile_submits_zero_adapter_runs(self):
        policy = build_policy(CHECKS)
        fixture = self.make(policy)
        self.ready(fixture)
        self.stage(fixture, {"ledger.txt": "total = 1\n"}, expected="pending")
        plan = ok(self, call(fixture, verifier.plan, "b1", "staging"))
        run = ok(self, call(fixture, runner.run, plan["plan_id"]))
        self.assertEqual(run["outcome"], "passed")
        self.assertFalse((fixture.state / "adapters" / "fake.json").exists())
        landed = ok(self, call(fixture, promotion.promote, plan["plan_id"], run["receipt_id"]))
        self.assertEqual(landed["dispositions"], [])
        self.assertFalse((fixture.state / "adapters" / "fake.json").exists())


class HostedRunTests(GithubCase):
    def prepare_hosted(self, fixture, files=None):
        self.stage(fixture, files or {"journeys/onboarding.txt": "welcome back\n", "ledger.txt": "total = 1\n"})
        return self.release_plan(fixture)

    def test_completed_success_signs_receipt_with_host_binding(self):
        fixture = self.make(github_policy())
        self.ready(fixture)
        plan = self.prepare_hosted(fixture)
        commit = plan["candidate"]["commit"]
        run = ok(self, call(fixture, runner.run, plan["plan_id"]))
        self.assertEqual(run["outcome"], "passed")
        self.assertEqual(run["adapter"]["name"], "fake-github")
        self.assertEqual(len(run["checks"]), 2)
        expected_event = {"type": "pull_request", "action": "synchronized", "base": "main", "source": ["dev"]}
        for item in run["checks"]:
            bound = item["metrics"]["host"]
            self.assertEqual(bound["adapter"], "fake")
            self.assertEqual(bound["host"], "fake.github.test")
            self.assertEqual(bound["repo"], "fixture-project")
            self.assertEqual(bound["workflow"], WORKFLOW)
            self.assertEqual(bound["revision"], REUSABLE)
            self.assertEqual(bound["head_sha"], commit)
            self.assertEqual(bound["attempt"], 1)
            self.assertEqual(bound["event"], expected_event)
            self.assertEqual(item["metrics"]["conclusion"], "success")
        host = FakeHost(fixture.state)
        self.assertEqual(len(host.store["runs"]), 2)
        for record in host.store["runs"].values():
            self.assertEqual((record["status"], record["conclusion"], record["attempt"]), ("completed", "success", 1))
            self.assertEqual((record["workflow"], record["revision"], record["head_sha"]), (WORKFLOW, REUSABLE, commit))
            self.assertEqual(record["event"], expected_event)
        checked = ok(self, call(fixture, verifier.check, run["receipt_id"]))
        self.assertEqual(checked["status"], "valid")
        receipt = Receipt.from_dict(checked["receipt"])
        self.assertEqual(receipt.executor, "github")
        self.assertEqual(receipt.outcome, "passed")
        landed = ok(self, call(fixture, promotion.promote, plan["plan_id"], run["receipt_id"]))
        self.assertEqual(landed["state"], "landed")
        self.assertEqual(fixture.head("main"), commit)

    def test_journey_artifact_recorded(self):
        fixture = self.make(github_policy(artifacts={"journey": ["out/journey.txt"]}))
        self.ready(fixture)
        plan = self.prepare_hosted(fixture)
        run = ok(self, call(fixture, runner.run, plan["plan_id"]))
        self.assertEqual(run["outcome"], "passed")
        retained = fixture.state / "artifacts" / run["run_id"] / "journey" / "out" / "journey.txt"
        self.assertEqual(retained.read_text(), "WELCOME BACK\n")
        host = FakeHost(fixture.state)
        record = next(item for item in host.store["runs"].values() if item["name"] == "journey")
        self.assertEqual(base64.b64decode(record["result"]["artifacts"]["out/journey.txt"]).decode(), "WELCOME BACK\n")

    def failure_fixture(self, conclusion=None, head_sha=None, artifacts=None):
        fixture = self.make(github_policy(artifacts=artifacts))
        self.ready(fixture)
        plan = self.prepare_hosted(fixture)
        host = FakeHost(fixture.state)
        host.force_next(conclusion=conclusion, head_sha=head_sha)
        return fixture, plan

    def test_skipped_conclusion_fails_the_check(self):
        fixture, plan = self.failure_fixture(conclusion="skipped")
        run = ok(self, call(fixture, runner.run, plan["plan_id"]), "failed")
        self.assertEqual(run["checks"][0]["status"], "failed")
        self.assertEqual(run["checks"][0]["metrics"]["conclusion"], "skipped")
        self.assertEqual(ok(self, call(fixture, verifier.check, run["receipt_id"]), "failed")["outcome"], "failed")
        self.assertEqual(ok(self, call(fixture, promotion.promote, plan["plan_id"], run["receipt_id"]), "failed").code, "receipt-failed")

    def test_stale_candidate_head_rejected(self):
        fixture, plan = self.failure_fixture(conclusion="success", head_sha="0" * 40)
        run = ok(self, call(fixture, runner.run, plan["plan_id"]), "failed")
        self.assertEqual(run["checks"][0]["status"], "stale")
        checked = ok(self, call(fixture, verifier.check, run["receipt_id"]), "invalid")
        self.assertTrue(any("head_sha" in reason or "no matching completed workflow_run" in reason for reason in checked["reasons"]), checked["reasons"])

    def test_missing_required_artifact_rejected(self):
        fixture = self.make(github_policy(artifacts={"journey": ["out/never.txt"]}))
        self.ready(fixture)
        plan = self.prepare_hosted(fixture)
        run = ok(self, call(fixture, runner.run, plan["plan_id"]), "failed")
        self.assertEqual(run["checks"][0]["status"], "missing-artifact")
        self.assertEqual(run["checks"][0]["metrics"]["missing_artifacts"], ["out/never.txt"])
        self.assertEqual(ok(self, call(fixture, verifier.check, run["receipt_id"]), "failed")["status"], "failed")

    def test_cancelled_conclusion_fails(self):
        fixture, plan = self.failure_fixture(conclusion="cancelled")
        run = ok(self, call(fixture, runner.run, plan["plan_id"]), "failed")
        self.assertEqual(run["checks"][0]["status"], "failed")

    def test_tampered_host_binding_is_invalid(self):
        fixture = self.make(github_policy())
        self.ready(fixture)
        plan = self.prepare_hosted(fixture)
        run = ok(self, call(fixture, runner.run, plan["plan_id"]))
        self.assertEqual(ok(self, call(fixture, verifier.check, run["receipt_id"]))["status"], "valid")
        with fixture.context() as ctx:
            c = ctx.store.connection
            row = c.execute("SELECT check_id, metrics FROM evidence WHERE run_id = ? ORDER BY position LIMIT 1", (run["run_id"],)).fetchone()
            metrics = json.loads(row["metrics"])
            metrics["host"]["attempt"] = metrics["host"]["attempt"] + 1
            c.execute("UPDATE evidence SET metrics = ? WHERE run_id = ? AND check_id = ?", (json.dumps(metrics, sort_keys=True), run["run_id"], row["check_id"]))
        checked = ok(self, call(fixture, verifier.check, run["receipt_id"]), "invalid")
        self.assertTrue(any("attempt" in reason and "adapter event log" in reason for reason in checked["reasons"]), checked["reasons"])
        with fixture.context() as ctx:
            c = ctx.store.connection
            row = c.execute("SELECT check_id, metrics FROM evidence WHERE run_id = ? ORDER BY position LIMIT 1", (run["run_id"],)).fetchone()
            metrics = json.loads(row["metrics"])
            metrics["host"]["attempt"] = metrics["host"]["attempt"] - 1
            metrics["host"]["revision"] = "example/workflows/reusable-check.yml@" + "b" * 40
            c.execute("UPDATE evidence SET metrics = ? WHERE run_id = ? AND check_id = ?", (json.dumps(metrics, sort_keys=True), run["run_id"], row["check_id"]))
        checked = ok(self, call(fixture, verifier.check, run["receipt_id"]), "invalid")
        self.assertTrue(any("revision" in reason for reason in checked["reasons"]))
        with fixture.context() as ctx:
            c = ctx.store.connection
            row = c.execute("SELECT check_id, metrics FROM evidence WHERE run_id = ? ORDER BY position LIMIT 1", (run["run_id"],)).fetchone()
            metrics = json.loads(row["metrics"])
            metrics["host"]["revision"] = REUSABLE
            metrics["host"]["head_sha"] = "0" * 40
            c.execute("UPDATE evidence SET metrics = ? WHERE run_id = ? AND check_id = ?", (json.dumps(metrics, sort_keys=True), run["run_id"], row["check_id"]))
        checked = ok(self, call(fixture, verifier.check, run["receipt_id"]), "invalid")
        self.assertTrue(any("head_sha" in reason for reason in checked["reasons"]))


class LostRunTests(GithubCase):
    def test_lost_response_pending_recover_resubmit(self):
        fixture = self.make(github_policy())
        self.ready(fixture)
        self.stage(fixture, {"journeys/onboarding.txt": "welcome back\n", "ledger.txt": "total = 1\n"})
        plan = ok(self, call(fixture, verifier.plan, "b1", "release"))
        FakeHost(fixture.state).lose_next()
        run = ok(self, call(fixture, runner.run, plan["plan_id"], timeout=2), "pending")
        self.assertEqual(run["checks"][0]["status"], "host-pending")
        self.assertNotIn("receipt_id", run)
        submission = run["checks"][0]["submission"]
        with fixture.context() as ctx:
            row = ctx.store.connection.execute("SELECT state FROM runs WHERE id = ?", (run["run_id"],)).fetchone()
            self.assertEqual(row["state"], "running")
        recovered = ok(self, call(fixture, promotion.recover, "b1"))
        actions = [action for action in recovered["actions"] if action["kind"] == "run"]
        self.assertEqual([(action["run_id"], action["state"]) for action in actions], [(run["run_id"], "interrupted")])
        with fixture.context() as ctx:
            row = ctx.store.connection.execute("SELECT state, reason FROM runs WHERE id = ?", (run["run_id"],)).fetchone()
            self.assertEqual(row["state"], "interrupted")
        rerun = ok(self, call(fixture, runner.run, plan["plan_id"], timeout=10))
        self.assertEqual(rerun["outcome"], "passed")
        bound = rerun["checks"][0]["metrics"]["host"]
        self.assertEqual(bound["run_id"], submission["run_id"])
        self.assertEqual(bound["attempt"], submission["attempt"] + 1)
        checked = ok(self, call(fixture, verifier.check, rerun["receipt_id"]))
        self.assertEqual(checked["status"], "valid")
        host = FakeHost(fixture.state)
        attempts = [event for event in host.event_log() if event.get("type") == "workflow_run" and event.get("run_id") == submission["run_id"] and event.get("action") == "completed"]
        self.assertEqual([event["attempt"] for event in attempts], [bound["attempt"]])
        self.assertIn(f"{submission['run_id']}:{submission['attempt']}", host.store["lost"])


class DispositionTests(GithubCase):
    def test_merged_and_closed_with_link_dispositions(self):
        fixture = self.make(github_policy())
        self.ready(fixture)
        host = FakeHost(fixture.state)
        host.open_pull_request("fixture-project", "dev", "starlings/b1/lanes/ledger", head="1" * 40)
        task_pr = host.open_pull_request("fixture-project", "dev", "starlings/b1/solo", head="2" * 40)
        host.merge_pull_request("fixture-project", task_pr["number"])
        staged = self.stage(fixture, {"ledger.txt": "total = 1\n"})
        commit = staged["candidates"][0]["commit"]
        dispositions = {item["name"]: item for item in staged["candidates"][0]["dispositions"]}
        self.assertEqual(dispositions["ledger"]["state"], "closed-with-link")
        self.assertEqual(dispositions["ledger"]["link"], commit)
        self.assertEqual(dispositions["ledger"]["kind"], "lane")
        self.assertEqual(dispositions["solo"]["state"], "merged")
        self.assertEqual(dispositions["solo"]["kind"], "task")
        observed = host.pull_request("fixture-project", "starlings/b1/lanes/ledger")
        self.assertEqual((observed["state"], observed["link"]), ("closed", commit))
        merged = host.pull_request("fixture-project", "starlings/b1/solo")
        self.assertEqual(merged["state"], "merged")
        status = ok(self, fixture.call("status", "b1"))
        recorded = {item["name"]: item["state"] for item in status["dispositions"]}
        self.assertEqual(recorded, {"ledger": "closed-with-link", "solo": "merged"})
        with fixture.context() as ctx:
            rows = [dict(row) for row in ctx.store.connection.execute("SELECT kind, name, state, link FROM dispositions WHERE batch = 'b1' ORDER BY kind, name")]
        self.assertEqual([(row["kind"], row["name"], row["state"]) for row in rows], [("lane", "ledger", "closed-with-link"), ("task", "solo", "merged")])

    def test_already_closed_with_link_is_observed(self):
        fixture = self.make(github_policy())
        self.ready(fixture)
        host = FakeHost(fixture.state)
        staged_base = self.stage(fixture, {"ledger.txt": "total = 1\n"})
        self.assertEqual(staged_base["candidates"][0]["dispositions"], [])
        commit = staged_base["candidates"][0]["commit"]
        pr = host.open_pull_request("fixture-project", "dev", "starlings/b1/lanes/ledger", head="1" * 40)
        host.close_pull_request("fixture-project", pr["number"], link=commit)
        with fixture.context() as ctx:
            rows = adapters_host.record_dispositions(ctx, load_batch(ctx.store.connection, "b1"), ["ledger"], commit)
        self.assertEqual([(row["name"], row["state"], row["link"]) for row in rows], [("ledger", "closed-with-link", commit)])


class GithubStubTests(GithubCase):
    def test_real_adapter_raises_hosted_unavailable(self):
        fixture = self.make(github_policy(adapter="github"))
        self.ready(fixture)
        self.stage(fixture, {"ledger.txt": "total = 1\n"})
        plan = self.release_plan(fixture)
        outcome = call(fixture, runner.run, plan["plan_id"])
        self.assertEqual(outcome[0], "blocked")
        self.assertEqual(outcome[1].code, "hosted-unavailable")
        self.assertIn("live rehearsal", outcome[1].message)
        with fixture.context() as ctx:
            self.assertEqual(ctx.store.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

    def test_task_gate_rejects_github_executor(self):
        policy = parse(github_policy())
        policy["gates"]["task"].update({"scope": "full", "executor": "github", "environment": "native", "checks": ["ledger"]})
        fixture = self.make(render(policy))
        self.ready(fixture)
        ok(self, fixture.call("prepare", "b1", ["ledger"], str(fixture.authority)))
        outcome = call(fixture, verifier.plan, "b1", "task")
        self.assertEqual(outcome[0], "blocked")
        self.assertEqual(outcome[1].code, "unsupported")
        self.assertIn("task gate", outcome[1].message)


class TemplateTests(GithubCase):
    def test_templates_pinned_restricted_and_exclusive(self):
        fixture = self.make(github_policy())
        policy = parse((fixture.project / "starlings.toml").read_text())
        files = adapters_templates.render(policy)
        self.assertEqual(sorted(files), [".github/workflows/reusable-check.yml", WORKFLOW])
        caller, reusable = files[WORKFLOW], files[".github/workflows/reusable-check.yml"]
        self.assertIn(f"uses: {REUSABLE}", caller)
        self.assertIn("branches: [main]", caller)
        for trigger in ("pull_request", "merge_group", "workflow_dispatch"):
            self.assertIn(trigger, caller)
        self.assertIn("contents: read", caller)
        self.assertNotIn("branches: [dev]", caller)
        self.assertIn("workflow_call", reusable)
        self.assertIn("head_sha", reusable)
        self.assertIn("runs-on", reusable)
        self.assertIn("b4ffde65f46336ab88eb53be808477a3936bae11", reusable)
        staged = github_policy(staging="full")
        branches = adapters_templates.render(parse(staged))[WORKFLOW]
        self.assertIn("branches: [dev, main]", branches)
        self.assertFalse((fixture.project / ".github").exists())
        self.assertEqual(main(["templates", "github", "--write", "--directory", str(fixture.project)]), 0)
        for relative, content in files.items():
            self.assertEqual((fixture.project / relative).read_text(), content)
        self.assertEqual(main(["templates", "github", "--write", "--directory", str(fixture.project)]), 3)
        local = self.make(build_policy(CHECKS))
        self.assertEqual(main(["templates", "github", "--write", "--directory", str(local.project)]), 2)
        self.assertFalse((local.project / ".github").exists())


if __name__ == "__main__":
    unittest.main()
