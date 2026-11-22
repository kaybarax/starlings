import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import test_verifier as tv
from fixtures import EXCLUDED, Fixture, git
from starlings import promotion, runner, signer, store, verifier
from starlings.cli import main
from starlings.contracts import Approval, Candidate, Promotion
from starlings.policy import PolicyError, parse, preset, render, validate

SRC = str(Path(__file__).resolve().parents[1] / "src")
SEMANTIC = {
    "calc.py": "def compute_total(xs):\n    return sum(xs)\n",
    "report.py": "from calc import compute_total\nprint(compute_total([1]))\n",
    "tests/journeys.py": "import pathlib, sys\nsys.path.insert(0, '.')\nimport report\nif pathlib.Path('summary.py').exists():\n    import summary\n",
}
CRASH = """import os, pathlib, sys
from starlings import batches, promotion
state, project, plan, receipt, marker, fifo = sys.argv[1:]
def hook(point, info):
    if point == "after-write":
        pathlib.Path(marker).write_text(info["promotion"])
        open(fifo).read()
with batches.Context(state, project, hook=hook) as ctx:
    promotion.promote(ctx, plan, receipt)
"""
HOLD = """import os, pathlib, subprocess, sys, time
pids = pathlib.Path(sys.argv[1])
pids.mkdir(exist_ok=True)
(pids / "leader").write_text(str(os.getpid()))
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
(pids / "child").write_text(str(child.pid))
(pids / "ready").write_text("1")
while not pathlib.Path(sys.argv[2]).exists():
    time.sleep(0.02)
"""


class Clock:
    def __init__(self, value):
        self.value = value
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def advance(self, seconds):
        with self.lock:
            self.value += seconds


def gated_policy(task="none", staging="none", checks=None, approvals=0, files_check="journey"):
    policy = preset("each-lane-release")
    policy["project"] = "fixture-project"
    policy["checks"] = dict(checks or {"journey": ["python3", "tests/journey_check.py"]})
    names = list(policy["checks"])
    for gate, scope in (("task", task), ("staging", staging), ("release", "full")):
        policy["gates"][gate].update({"scope": scope, "executor": "local", "environment": "native", "checks": names if scope != "none" else []})
    policy["gates"]["release"]["approvals"] = approvals
    policy["assembly"] = {"excluded": list(EXCLUDED)}
    policy["limits"]["timeout_seconds"] = 60
    return render(policy)


class PromotionCase(tv.VerifierCase):
    def lane(self, f, batch, lane, task, files, freeze=True):
        work = tv.ok(self, f.call("assign", batch, task, lane, "worker-" + task, []))
        head = f.work(work["worktree"], files, task)
        tv.ok(self, f.call("handoff", batch, task, head, "review:" + task, []))
        return head

    def staged(self, f, batch="b1", changes=None):
        changes = changes or {"ledger": {"journeys/onboarding.txt": "welcome release\n"}}
        tv.ok(self, f.call("prepare", batch, list(changes), str(f.authority)))
        for lane, files in changes.items():
            self.lane(f, batch, lane, f"{lane}-{batch}", files)
            tv.ok(self, f.call("assemble", batch, lane))
            tv.ok(self, f.call("freeze", batch, lane))
        return tv.ok(self, f.call("stage", batch, list(changes)))

    def release(self, f, batch="b1", expected="ok"):
        plan = tv.ok(self, tv.call(f, verifier.plan, batch, "release"))
        run = tv.ok(self, tv.call(f, runner.run, plan["plan_id"]), expected)
        return plan, run

    def promote(self, f, plan, receipt, expected="ok", hook=None, **kwargs):
        return tv.ok(self, tv.call(f, promotion.promote, plan, receipt, hook=hook, **kwargs), expected)

    def rows(self, f, sql, *args):
        with f.context() as ctx:
            return [dict(row) for row in ctx.store.connection.execute(sql, args).fetchall()]

    def gated_task(self, f, batch, lane, task, files):
        self.lane(f, batch, lane, task, files)
        pending = tv.ok(self, f.call("assemble", batch, lane), "pending")["pending"]
        plan = tv.ok(self, tv.call(f, verifier.plan, batch, "task", pending["candidate_id"]))
        run = tv.ok(self, tv.call(f, runner.run, plan["plan_id"]))
        landed = self.promote(f, plan["plan_id"], run["receipt_id"])
        return pending, plan, run, landed


class PolicyAndContractTests(unittest.TestCase):
    def test_approvals_policy_and_records(self):
        policy = preset("release-only")
        self.assertNotIn("approvals", render(policy))
        for value in (0, 16):
            policy["gates"]["release"]["approvals"] = value
            self.assertEqual(parse(render(policy)), policy)
        for value in (True, 17, -1, "1", 1.0):
            with self.subTest(value=value), self.assertRaises(PolicyError):
                data = preset("release-only")
                data["gates"]["staging"]["approvals"] = value
                validate(data)
        lane = Candidate("repo", "/a.git", "refs/starlings/b1/lanes/ledger", "a" * 40, ("a" * 40, "b" * 40), "c" * 40, "d" * 40)
        self.assertTrue(lane.id.startswith("candidate:"))
        for target in ("refs/starlings/B1/lanes/x", "refs/starlings/b1/tasks/x", "refs/starlings/b1/lanes/"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                Candidate("repo", "/a.git", target, "a" * 40, ("b" * 40,), "c" * 40, "d" * 40)
        plan_id, receipt_id = "plan:" + "1" * 64, "receipt:" + "2" * 64
        approval = Approval(plan_id, lane.id, "alice", None, 10.0, 70.0)
        self.assertEqual(approval.id, Approval(plan_id, lane.id, "alice", None, 10.0, 70.0).id)
        self.assertNotEqual(approval.id, Approval(plan_id, lane.id, "bob", None, 10.0, 70.0).id)
        with self.assertRaises(ValueError):
            Approval(plan_id, lane.id, "alice", None, 10.0, 10.0)
        record = Promotion(plan_id, receipt_id, (approval.id,), "refs/heads/main", "a" * 40, "d" * 40, "c" * 40, ("a" * 40, "b" * 40), 99.0)
        self.assertTrue(record.id.startswith("promotion:"))
        self.assertEqual(record.to_dict()["schema"], 1)
        with self.assertRaises(ValueError):
            Promotion(plan_id, receipt_id, (approval.id, approval.id), "refs/heads/main", "a" * 40, "d" * 40, "c" * 40, ("a" * 40,), 1.0)


class ReleasePromotionTests(PromotionCase):
    def test_release_only_happy_path_and_repeat(self):
        f = self.make(gated_policy())
        self.ready(f)
        main0 = f.head("main")
        self.staged(f)
        dev = f.head("dev")
        plan, run = self.release(f)
        self.assertEqual(self.check(f, run["receipt_id"])["status"], "valid")
        landed = self.promote(f, plan["plan_id"], run["receipt_id"])
        candidate = plan["candidate"]
        self.assertEqual(f.head("main"), candidate["commit"])
        self.assertEqual(git(f.authority, "rev-list", "--parents", "-n", "1", "main").split()[1:], [main0, dev])
        self.assertEqual(git(f.authority, "rev-parse", "main^{tree}"), candidate["tree"])
        self.assertEqual(landed["promotion"]["kind"], "promotion")
        self.assertEqual((landed["promotion"]["old"], landed["promotion"]["new"]), (main0, candidate["commit"]))
        self.assertEqual(landed["sources"], {"refs/heads/dev": dev})
        self.assertFalse(landed["repeated"])
        again = self.promote(f, plan["plan_id"], run["receipt_id"])
        self.assertTrue(again["repeated"])
        self.assertEqual(again["promotion"]["id"], landed["promotion"]["id"])
        self.assertEqual(f.head("main"), candidate["commit"])
        self.assertEqual([row["state"] for row in self.rows(f, "SELECT state FROM promotions")], ["landed"])
        self.assertEqual(tv.ok(self, f.call("status", "b1"))["promotions"][0]["record"]["id"], landed["promotion"]["id"])
        self.assertEqual(self.check(f, run["receipt_id"], "stale")["status"], "stale")

    def test_semantic_defect_blocks_release_until_additive_fix(self):
        checks = {"journeys": ["python3", "tests/journeys.py"]}
        f = self.make(gated_policy(task="full", checks=checks), files=SEMANTIC)
        self.ready(f)
        main0 = f.head("main")
        tv.ok(self, f.call("prepare", "b1", ["ledger", "reports"], str(f.authority)))
        for lane, task, files in (("ledger", "rename", {"calc.py": "def total(xs):\n    return sum(xs)\n", "report.py": "from calc import total\nprint(total([1]))\n"}), ("reports", "summary", {"summary.py": "from calc import compute_total\nprint(compute_total([2]))\n"})):
            pending, plan, run, landed = self.gated_task(f, "b1", lane, task, files)
            self.assertEqual(run["outcome"], "passed")
            self.assertEqual(landed["state"], "landed")
            tv.ok(self, f.call("freeze", "b1", lane))
        tv.ok(self, f.call("stage", "b1", ["ledger", "reports"]))
        self.assertNotEqual(git(f.authority, "rev-list", "--count", f"{main0}..dev"), "0")
        plan, failed = self.release(f, expected="failed")
        refused = self.promote(f, plan["plan_id"], failed["receipt_id"], "failed")
        self.assertEqual(refused.code, "receipt-failed")
        self.assertEqual(f.head("main"), main0)
        self.assertEqual([item["status"] for item in failed["checks"]], ["failed"])
        self.assertIn("cannot import name 'compute_total'", (f.state / "logs" / failed["run_id"] / "journeys.err").read_text())
        self.assertEqual(len(self.rows(f, "SELECT 1 FROM evidence WHERE run_id = ? AND status = 'failed'", failed["run_id"])), 1)
        self.assertEqual(self.rows(f, "SELECT COUNT(*) AS n FROM promotions WHERE gate = 'release'")[0]["n"], 0)
        tv.ok(self, f.call("prepare", "b2", ["ledger"], str(f.authority)))
        self.gated_task(f, "b2", "ledger", "alias", {"calc.py": "def total(xs):\n    return sum(xs)\n\n\ncompute_total = total\n"})
        tv.ok(self, f.call("freeze", "b2", "ledger"))
        tv.ok(self, f.call("stage", "b2", ["ledger"]))
        fixed_plan, fixed = self.release(f, "b2")
        self.assertEqual((fixed_plan["selection"], fixed_plan["checks"]), ("full", ["journeys"]))
        landed = self.promote(f, fixed_plan["plan_id"], fixed["receipt_id"])
        self.assertEqual(f.head("main"), fixed_plan["candidate"]["commit"])
        self.assertEqual(landed["state"], "landed")
        still = self.check(f, failed["receipt_id"], "failed")
        self.assertEqual((still["status"], still["outcome"]), ("failed", "failed"))
        self.assertFalse(still["authorizes"])
        mirror = Path(tv.ok(self, f.call("status", "b1"))["mirror"])
        self.assertEqual(git(mirror, "rev-parse", plan["candidate_ref"] + "^{tree}"), plan["candidate"]["tree"])
        self.assertEqual(self.promote(f, plan["plan_id"], failed["receipt_id"], "failed").code, "receipt-failed")

    def test_concurrent_promotions_land_once(self):
        f = self.make(gated_policy())
        self.ready(f)
        self.staged(f)
        plan, run = self.release(f)
        entered, go, outcomes = threading.Event(), threading.Event(), {}

        def hold(point, info):
            if point == "target-lease":
                entered.set()
                go.wait(30)
        first = threading.Thread(target=lambda: outcomes.update(a=tv.call(f, promotion.promote, plan["plan_id"], run["receipt_id"], hook=hold)))
        first.start()
        entered.wait(30)
        second = threading.Thread(target=lambda: outcomes.update(b=tv.call(f, promotion.promote, plan["plan_id"], run["receipt_id"])))
        second.start()
        second.join(30)
        go.set()
        first.join(30)
        self.assertEqual(outcomes["a"][0], "ok")
        self.assertEqual((outcomes["b"][0], outcomes["b"][1].code), ("blocked", "lease-held"))
        self.assertTrue(self.promote(f, plan["plan_id"], run["receipt_id"])["repeated"])
        self.assertEqual(f.head("main"), plan["candidate"]["commit"])
        self.assertEqual(self.rows(f, "SELECT COUNT(*) AS n FROM promotions")[0]["n"], 1)

        f = self.make(gated_policy())
        self.ready(f)
        self.staged(f)
        older, older_run = self.release(f)
        f.external_commit("dev", "reports.txt", "report = newer\n")
        newer, newer_run = self.release(f)
        entered.clear()
        go.clear()

        def hold_write(point, info):
            if point == "before-write":
                entered.set()
                go.wait(30)
        first = threading.Thread(target=lambda: outcomes.update(c=tv.call(f, promotion.promote, newer["plan_id"], newer_run["receipt_id"], hook=hold_write)))
        first.start()
        entered.wait(30)
        blocked = self.promote(f, older["plan_id"], older_run["receipt_id"], "blocked")
        self.assertEqual(blocked.code, "lease-held")
        go.set()
        first.join(30)
        self.assertEqual(outcomes["c"][0], "ok")
        stale = self.promote(f, older["plan_id"], older_run["receipt_id"], "stale")
        self.assertEqual(stale.code, "stale-receipt")
        self.assertEqual(f.head("main"), newer["candidate"]["commit"])
        self.assertEqual([row["state"] for row in self.rows(f, "SELECT state FROM promotions")], ["landed"])

    def test_target_and_source_movement_are_stale(self):
        f = self.make(gated_policy())
        self.ready(f)
        self.staged(f)
        plan, run = self.release(f)
        moved = f.external_commit("main", "reports.txt", "report = hotfix\n")
        stale = self.promote(f, plan["plan_id"], run["receipt_id"], "stale")
        self.assertEqual(stale.code, "stale-receipt")
        self.assertEqual(f.head("main"), moved)
        self.assertEqual(self.rows(f, "SELECT COUNT(*) AS n FROM promotions")[0]["n"], 0)

        f = self.make(gated_policy())
        self.ready(f)
        self.staged(f)
        main0 = f.head("main")
        plan, run = self.release(f)
        moved = {}

        def move(point, info):
            if point == "before-write":
                moved["dev"] = f.external_commit("dev", "reports.txt", "report = racing\n")
        stale = self.promote(f, plan["plan_id"], run["receipt_id"], "stale", hook=move)
        self.assertEqual((stale.code, stale.data["state"]), ("stale", "not-applied"))
        self.assertEqual(f.head("main"), main0)
        self.assertEqual(f.head("dev"), moved["dev"])
        self.assertEqual([row["state"] for row in self.rows(f, "SELECT state FROM promotions")], ["not-applied"])
        self.assertEqual(self.promote(f, plan["plan_id"], run["receipt_id"], "stale").code, "stale-source")
        self.assertEqual(f.head("main"), main0)

    def test_approvals_expiry_and_revocation(self):
        f = self.make(gated_policy(approvals=1))
        f.clock = Clock(time.time())
        self.ready(f)
        self.staged(f)
        plan, run = self.release(f)
        pid, rid = plan["plan_id"], run["receipt_id"]
        main0 = f.head("main")
        self.assertEqual(self.promote(f, pid, rid, "blocked").code, "approval-required")
        self.assertEqual(tv.call(f, promotion.approve, pid, "alice", 59)[0], "invalid")
        self.assertEqual(tv.call(f, promotion.approve, pid, "alice", True)[0], "invalid")
        first = tv.ok(self, tv.call(f, promotion.approve, pid, "alice", 60))
        self.assertEqual(first["required"], 1)
        f.clock.advance(61)
        self.assertEqual(self.promote(f, pid, rid, "blocked").code, "approval-required")
        second = tv.ok(self, tv.call(f, promotion.approve, pid, "alice", 600, "review:alice"))
        self.assertNotEqual(second["approval_id"], first["approval_id"])
        self.assertTrue(tv.ok(self, tv.call(f, promotion.approve, pid, "alice", 900, "review:alice"))["repeated"])
        self.assertEqual(tv.call(f, promotion.approve, pid, "alice", 900, "review:other")[0], "conflict")
        revoked = tv.ok(self, tv.call(f, promotion.revoke, second["approval_id"]))
        self.assertIsNotNone(revoked["revoked_at"])
        self.assertTrue(tv.ok(self, tv.call(f, promotion.revoke, second["approval_id"]))["repeated"])
        self.assertEqual(self.promote(f, pid, rid, "blocked").code, "approval-required")
        third = tv.ok(self, tv.call(f, promotion.approve, pid, "bob", 600))
        attempt = {}

        def revoke_during(point, info):
            if point == "target-lease":
                attempt["revoke"] = tv.call(f, promotion.revoke, third["approval_id"])
        landed = self.promote(f, pid, rid, hook=revoke_during)
        self.assertEqual((attempt["revoke"][0], attempt["revoke"][1].code), ("blocked", "lease-held"))
        self.assertEqual(landed["promotion"]["approvals"], [third["approval_id"]])
        self.assertEqual(f.head("main"), plan["candidate"]["commit"])
        self.assertNotEqual(main0, f.head("main"))
        approvals = tv.ok(self, f.call("status", "b1"))["approvals"]
        self.assertEqual([(a["by"], a["live"]) for a in approvals], [("alice", False), ("alice", False), ("bob", True)])

    def test_trust_change_serializes_and_staleness(self):
        f = self.make(gated_policy())
        self.ready(f)
        self.staged(f)
        plan, run = self.release(f)
        clone = f.root / "policy-review"
        git(f.root, "clone", "--quiet", "--branch", "dev", str(f.authority), str(clone))
        (clone / "starlings.toml").write_text(gated_policy().replace("fixture-project", "fixture-project-v2"))
        git(clone, "-c", "user.name=r", "-c", "user.email=r@example.invalid", "commit", "--quiet", "-am", "policy v2")
        git(clone, "push", "--quiet", "origin", "HEAD:refs/heads/policy-review")
        commit = git(clone, "rev-parse", "HEAD")
        digest = plan["policy_digest"]
        attempt = {}

        def trust_during(point, info):
            if point == "target-lease":
                attempt["trust"] = tv.call(f, verifier.trust, str(f.authority), commit, "review:v2", digest)
        self.promote(f, plan["plan_id"], run["receipt_id"], hook=trust_during)
        self.assertEqual((attempt["trust"][0], attempt["trust"][1].code), ("blocked", "lease-held"))
        self.assertEqual(f.head("main"), plan["candidate"]["commit"])
        f.external_commit("dev", "reports.txt", "report = second\n")
        second, second_run = self.release(f)
        main1 = f.head("main")
        self.trust(f, digest, commit)
        stale = self.promote(f, second["plan_id"], second_run["receipt_id"], "stale")
        self.assertEqual(stale.code, "stale-receipt")
        self.assertIn("no longer the trusted policy", stale.message)
        self.assertEqual(f.head("main"), main1)


class RecoveryTests(PromotionCase):
    def test_crash_before_write_recovers_not_applied(self):
        f = self.make(gated_policy())
        self.ready(f)
        self.staged(f)
        plan, run = self.release(f)
        main0 = f.head("main")

        def crash(point, info):
            if point == "before-write":
                raise KeyboardInterrupt("crash before the ref write")
        with self.assertRaises(KeyboardInterrupt):
            tv.call(f, promotion.promote, plan["plan_id"], run["receipt_id"], hook=crash)
        self.assertEqual([row["state"] for row in self.rows(f, "SELECT state FROM promotions")], ["intent"])
        recovered = tv.ok(self, tv.call(f, promotion.recover, "b1"))
        self.assertEqual([(a["kind"], a["state"]) for a in recovered["actions"]], [("promotion", "not-applied")])
        self.assertEqual(f.head("main"), main0)
        landed = self.promote(f, plan["plan_id"], run["receipt_id"])
        self.assertEqual(landed["recovery"], [])
        self.assertEqual(f.head("main"), plan["candidate"]["commit"])
        self.assertEqual(sorted(row["state"] for row in self.rows(f, "SELECT state FROM promotions")), ["landed", "not-applied"])
        self.assertEqual(git(f.authority, "rev-list", "--first-parent", "--count", f"{main0}..main"), "1")

    def test_killed_promoter_after_write_recovers_landed(self):
        f = self.make(gated_policy())
        self.ready(f)
        self.staged(f)
        plan, run = self.release(f)
        main0 = f.head("main")
        marker, fifo = f.root / "after-write", f.root / "block.fifo"
        os.mkfifo(fifo)
        env = {**os.environ, "PYTHONPATH": SRC}
        process = subprocess.Popen([sys.executable, "-c", CRASH, str(f.state), str(f.project), plan["plan_id"], run["receipt_id"], str(marker), str(fifo)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        tv.wait_for(lambda: marker.exists() or process.poll() is not None)
        self.assertIsNone(process.poll(), process.stderr.read() if process.poll() is not None else "")
        process.kill()
        process.communicate(timeout=30)
        self.assertEqual(process.returncode, -9)
        self.assertEqual(f.head("main"), plan["candidate"]["commit"])
        self.assertEqual([row["state"] for row in self.rows(f, "SELECT state FROM promotions")], ["intent"])
        self.assertEqual(self.promote(f, plan["plan_id"], run["receipt_id"], "blocked").code, "lease-held")
        held = tv.ok(self, tv.call(f, promotion.recover, "b1"), "blocked")
        self.assertTrue(any(item.get("target") == "refs/heads/main" for item in held["skipped"]))
        with f.context(clock=lambda: time.time() + 700) as ctx:
            status, recovered = promotion.recover(ctx, "b1")
        self.assertEqual(status, "ok")
        action = next(a for a in recovered["actions"] if a["kind"] == "promotion")
        self.assertEqual((action["state"], action["recovered"], action["promotion_id"]), ("landed", True, marker.read_text()))
        self.assertEqual(f.head("main"), plan["candidate"]["commit"])
        self.assertEqual(git(f.authority, "rev-list", "--first-parent", "--count", f"{main0}..main"), "1")
        with f.context(clock=lambda: time.time() + 700) as ctx:
            status, again = promotion.promote(ctx, plan["plan_id"], run["receipt_id"])
        self.assertEqual((status, again["repeated"]), ("ok", True))
        self.assertEqual([row["state"] for row in self.rows(f, "SELECT state FROM promotions")], ["landed"])

    def test_ambiguous_landing_parks_until_operator_resolves(self):
        f = self.make(gated_policy())
        self.ready(f)
        self.staged(f)
        plan, run = self.release(f)

        def crash(point, info):
            if point == "before-write":
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            tv.call(f, promotion.promote, plan["plan_id"], run["receipt_id"], hook=crash)
        unrelated = f.external_commit("main", "reports.txt", "report = unrelated\n")
        parked = tv.ok(self, tv.call(f, promotion.recover, "b1"), "blocked")
        promotion_id = parked["parked"][0]["promotion_id"]
        self.assertEqual(parked["actions"][0]["state"], "ambiguous")
        self.assertEqual(parked["parked"][0]["observed"], unrelated)
        self.assertEqual(self.promote(f, plan["plan_id"], run["receipt_id"], "blocked").code, "ambiguous-landing")
        refused = tv.ok(self, tv.call(f, promotion.recover, "b1", promotion_id, "landed"), "blocked")
        self.assertEqual(refused.code, "resolution-refused")
        self.assertEqual(tv.call(f, promotion.recover, "b1", promotion_id)[0], "invalid")
        resolved = tv.ok(self, tv.call(f, promotion.recover, "b1", promotion_id, "not-applied"))
        self.assertEqual(resolved["state"], "not-applied")
        self.assertEqual(self.promote(f, plan["plan_id"], run["receipt_id"], "stale").code, "stale-receipt")
        self.assertEqual(f.head("main"), unrelated)

        f = self.make(gated_policy())
        self.ready(f)
        self.staged(f)
        plan, run = self.release(f)

        def lost(point, info):
            if point == "after-write":
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            tv.call(f, promotion.promote, plan["plan_id"], run["receipt_id"], hook=lost)
        on_top = f.external_commit("main", "reports.txt", "report = on top\n")
        parked = tv.ok(self, tv.call(f, promotion.recover, "b1"), "blocked")
        promotion_id = parked["parked"][0]["promotion_id"]
        self.assertEqual(tv.ok(self, tv.call(f, promotion.recover, "b1", promotion_id, "not-applied"), "blocked").code, "resolution-refused")
        resolved = tv.ok(self, tv.call(f, promotion.recover, "b1", promotion_id, "landed"))
        self.assertEqual((resolved["state"], resolved["observed"]), ("landed", on_top))
        self.assertTrue(self.promote(f, plan["plan_id"], run["receipt_id"])["repeated"])
        self.assertEqual(tv.ok(self, tv.call(f, promotion.recover, "b1"))["parked"], [])

    def test_killed_runner_is_interrupted_by_recovery(self):
        f = self.make(tv.build_policy({"hold": ["python3", "-c", HOLD, "PIDS", "GATE"]}))
        pids, gate = f.root / "pids", f.root / "gate"
        policy = parse((f.project / "starlings.toml").read_text())
        policy["checks"]["hold"][3:5] = [str(pids), str(gate)]
        text = render(policy)
        (f.project / "starlings.toml").write_text(text)
        self.trust(f, commit=f.external_commit("dev", "starlings.toml", text))
        with f.context() as ctx:
            signer.init(ctx.root)
        self.stage(f, {"ledger.txt": "total = 1\n"})
        plan = self.plan(f)
        env = {**os.environ, "PYTHONPATH": SRC}
        process = subprocess.Popen([sys.executable, "-m", "starlings", "run", plan["plan_id"], "--state-dir", str(f.state), "--directory", str(f.project), "--json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        tv.wait_for(lambda: (pids / "ready").exists())
        process.kill()
        process.communicate(timeout=30)
        leader, child = int((pids / "leader").read_text()), int((pids / "child").read_text())
        self.assertFalse(tv.gone(leader))
        run_id = self.rows(f, "SELECT id FROM runs WHERE state = 'running'")[0]["id"]
        self.assertTrue((f.state / "worktrees" / "runs" / run_id / "hold").exists())
        live = tv.ok(self, tv.call(f, promotion.recover, "b1"), "blocked")
        self.assertEqual(live["skipped"][-1]["run_id"], run_id)
        with f.context(clock=lambda: time.time() + 5000) as ctx:
            status, recovered = promotion.recover(ctx, "b1")
        self.assertEqual(status, "ok")
        action = next(a for a in recovered["actions"] if a["kind"] == "run")
        self.assertEqual((action["run_id"], action["state"], action["killed_groups"], action["groups_reaped"]), (run_id, "interrupted", [leader], True))
        self.assertTrue(action["released_leases"])
        tv.wait_for(lambda: tv.gone(leader) and tv.gone(child), 10)
        self.assertEqual(self.slot_leases(f), [])
        self.assertFalse((f.state / "worktrees" / "runs" / run_id).exists())
        self.assertEqual(self.rows(f, "SELECT state, receipt_id FROM runs WHERE id = ?", run_id), [{"state": "interrupted", "receipt_id": None}])
        self.assertEqual(self.rows(f, "SELECT COUNT(*) AS n FROM receipts")[0]["n"], 0)
        gate.write_text("go")
        rerun = self.run_plan(f, plan["plan_id"])
        self.assertEqual(rerun["outcome"], "passed")


class GatedAssemblyTests(PromotionCase):
    def test_staging_gate_each_lane_promotes_then_continues(self):
        f = self.make(gated_policy(staging="full"))
        self.ready(f)
        tv.ok(self, f.call("prepare", "b1", ["ledger", "reports"], str(f.authority)))
        for lane, files in (("ledger", {"ledger.txt": "total = 5\n"}), ("reports", {"reports.txt": "report = five\n"})):
            self.lane(f, "b1", lane, lane + "-task", files)
            tv.ok(self, f.call("assemble", "b1", lane))
            tv.ok(self, f.call("freeze", "b1", lane))
        for lane in ("ledger", "reports"):
            dev = f.head("dev")
            pending = tv.ok(self, f.call("stage", "b1", ["ledger", "reports"]), "pending")
            candidate = pending["candidates"][0]
            self.assertEqual((candidate["lanes"], candidate["base"]), ([lane], dev))
            self.assertEqual(f.head("dev"), dev)
            plan = tv.ok(self, tv.call(f, verifier.plan, "b1", "staging", candidate["id"]))
            run = tv.ok(self, tv.call(f, runner.run, plan["plan_id"]))
            landed = self.promote(f, plan["plan_id"], run["receipt_id"])
            self.assertEqual(f.head("dev"), candidate["commit"])
            self.assertEqual(landed["sources"], {f"lane:{lane}": candidate["parents"][1]})
        state = tv.ok(self, f.call("status", "b1"))
        self.assertEqual([c["state"] for c in state["candidates"]], ["staged", "staged"])
        self.assertEqual({t["state"] for t in state["tasks"]}, {"staged"})
        done = tv.ok(self, f.call("stage", "b1", ["ledger", "reports"]))
        self.assertEqual(done["candidates"], [])

    def test_every_task_gate_promotes_each_task(self):
        f = self.make(gated_policy(task="full", staging="full"))
        self.ready(f)
        tv.ok(self, f.call("prepare", "b1", ["ledger"], str(f.authority)))
        self.lane(f, "b1", "ledger", "first", {"journeys/onboarding.txt": "welcome first\n"})
        self.lane(f, "b1", "ledger", "second", {"ledger.txt": "total = 2\n"})
        lane_ref = "refs/starlings/b1/lanes/ledger"
        for task in ("first", "second"):
            state = tv.ok(self, f.call("status", "b1"))
            tip = state["lanes"][0]["tip"]
            pending = tv.ok(self, f.call("assemble", "b1", "ledger"), "pending")
            item = pending["pending"]
            self.assertEqual((item["task"], item["target"], item["base"]), (task, lane_ref, tip))
            again = tv.ok(self, f.call("assemble", "b1", "ledger"), "pending")
            self.assertTrue(again["pending"]["repeated"])
            self.assertEqual(again["pending"]["candidate_id"], item["candidate_id"])
            mirror = Path(state["mirror"])
            self.assertEqual(git(mirror, "rev-parse", lane_ref), tip)
            self.assertEqual(tv.ok(self, f.call("status", "b1"))["tasks"][["first", "second"].index(task)]["state"], "pending")
            plan = tv.ok(self, tv.call(f, verifier.plan, "b1", "task"))
            self.assertEqual((plan["target"], plan["candidate"]["id"]), (lane_ref, item["candidate_id"]))
            run = tv.ok(self, tv.call(f, runner.run, plan["plan_id"]))
            self.assertEqual(self.check(f, run["receipt_id"])["status"], "valid")
            landed = self.promote(f, plan["plan_id"], run["receipt_id"])
            self.assertEqual(git(mirror, "rev-parse", lane_ref), item["commit"])
            self.assertEqual(landed["promotion"]["target"], lane_ref)
            self.assertEqual(self.check(f, run["receipt_id"], "stale")["status"], "stale")
        state = tv.ok(self, f.call("status", "b1"))
        self.assertEqual([t["state"] for t in state["tasks"]], ["included", "included"])
        self.assertEqual([t["included_position"] for t in state["tasks"]], [1, 2])
        self.assertEqual([c["state"] for c in state["task_candidates"]], ["included", "included"])
        finished = tv.ok(self, f.call("assemble", "b1", "ledger"))
        self.assertEqual(finished["included"], [])
        tv.ok(self, f.call("freeze", "b1", "ledger"))
        self.assertEqual(tv.ok(self, f.call("stage", "b1", ["ledger"]), "pending")["candidates"][0]["parents"][1], state["lanes"][0]["tip"])

    def test_include_intent_reconciles_both_ways(self):
        f = Fixture()
        self.addCleanup(f.close)
        tv.ok(self, f.call("prepare", "b1", ["ledger"], str(f.authority)))
        mirror = Path(tv.ok(self, f.call("status", "b1"))["mirror"])
        lane_ref = "refs/starlings/b1/lanes/ledger"

        def crash(name):
            def hook(point, info):
                if point == name:
                    raise KeyboardInterrupt(name)
            return hook
        self.lane(f, "b1", "ledger", "one", {"ledger.txt": "total = 1\n"})
        with self.assertRaises(KeyboardInterrupt):
            f.call("assemble", "b1", "ledger", hook=crash("include-after-write"))
        state = tv.ok(self, f.call("status", "b1"))
        self.assertNotEqual(git(mirror, "rev-parse", lane_ref), state["lanes"][0]["tip"])
        self.assertEqual(len(state["include_intents"]), 1)
        resumed = tv.ok(self, f.call("assemble", "b1", "ledger"))
        self.assertEqual([(a["action"], a["task"]) for a in resumed["recovery"]], [("finished", "one")])
        state = tv.ok(self, f.call("status", "b1"))
        self.assertEqual((state["tasks"][0]["state"], state["include_intents"]), ("included", []))
        self.assertEqual(state["lanes"][0]["tip"], git(mirror, "rev-parse", lane_ref))

        self.lane(f, "b1", "ledger", "two", {"reports.txt": "report = two\n"})
        tip = state["lanes"][0]["tip"]
        with self.assertRaises(KeyboardInterrupt):
            f.call("assemble", "b1", "ledger", hook=crash("include-before-write"))
        self.assertEqual(git(mirror, "rev-parse", lane_ref), tip)
        resumed = tv.ok(self, f.call("assemble", "b1", "ledger"))
        self.assertEqual([a["action"] for a in resumed["recovery"]], ["dropped"])
        self.assertEqual([i["task"] for i in resumed["included"]], ["two"])

        self.lane(f, "b1", "ledger", "three", {"shared.txt": "alpha\nthree\ngamma\n"})
        with self.assertRaises(KeyboardInterrupt):
            f.call("assemble", "b1", "ledger", hook=crash("include-before-write"))
        git(mirror, "update-ref", lane_ref, state["base"])
        diverged = f.call("assemble", "b1", "ledger")
        self.assertEqual((diverged[0], diverged[1].code), ("stale", "lane-diverged"))
        self.assertIn("include intent for task 'three'", diverged[1].message)
        self.assertEqual(len(tv.ok(self, f.call("status", "b1"))["include_intents"]), 1)


class MigrationTests(unittest.TestCase):
    def legacy(self, version):
        fixture = Fixture()
        self.addCleanup(fixture.close)
        fixture.state.mkdir(mode=0o700)
        path = fixture.state / "starlings.db"
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        added = ("run_processes", "task_candidates", "approvals", "promotions", "include_intents")
        verifier_tables = ("trusted_policies", "release_candidates", "plans", "runs", "evidence", "receipts")
        statements = [item.replace("'parked', 'pending', 'included'", "'parked', 'included'") for item in store.SCHEMA.split(";") if item.strip() and not any(f"EXISTS {name}(" in item for name in added)]
        if version == 1:
            statements = [item for item in statements if not any(f"EXISTS {name}(" in item for name in verifier_tables)]
        connection = sqlite3.connect(path)
        for statement in statements:
            connection.execute(statement)
        connection.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
        connection.execute("INSERT INTO batches VALUES ('b1', 'p', '/a.git', '/m.git', 'dev', 'main', 'd', '{}', 'base', '', 1)")
        connection.execute("INSERT INTO lanes(batch, name, position, tip) VALUES ('b1', 'ledger', 0, 'base')")
        for seq, name in enumerate(("one", "two"), 1):
            connection.execute("INSERT INTO tasks(batch, name, lane, owner, seq, worktree, branch, state, head) VALUES ('b1', ?, 'ledger', 'w', ?, '/w', 'br', 'included', 'h')", (name, seq))
        connection.execute("INSERT INTO dependencies VALUES ('b1', 'two', 'one', 0, 'h')")
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE tasks SET state = 'pending' WHERE name = 'one'")
        connection.commit()
        connection.close()
        return fixture, path

    def test_migrates_version_1_and_2_in_place(self):
        for version in (1, 2):
            with self.subTest(version=version):
                fixture, path = self.legacy(version)
                with store.Store(path) as migrated:
                    c = migrated.connection
                    self.assertEqual(c.execute("SELECT version FROM schema_version").fetchall()[0][0], 3)
                    self.assertEqual([row[0] for row in c.execute("SELECT name FROM tasks ORDER BY seq")], ["one", "two"])
                    c.execute("UPDATE tasks SET state = 'pending' WHERE name = 'one'")
                    self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(c.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                    tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                    self.assertTrue({"approvals", "promotions", "include_intents", "task_candidates", "run_processes", "receipts"} <= tables)
                    self.assertNotIn("tasks_v3", tables)
                    with self.assertRaises(sqlite3.IntegrityError):
                        c.execute("DELETE FROM tasks WHERE name = 'one'")
                with store.Store(path) as reopened:
                    self.assertEqual(reopened.connection.execute("SELECT state FROM tasks WHERE name = 'one'").fetchone()[0], "pending")


class PromotionCLITests(PromotionCase):
    def cli(self, f, *args):
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
            code = main([*args, "--state-dir", str(f.state), "--directory", str(f.project), "--json"])
        return code, json.loads(out.getvalue())

    def test_cli_approve_promote_recover(self):
        f = self.make(gated_policy(approvals=1))
        self.ready(f)
        self.staged(f)
        plan, run = self.release(f)
        pid, rid = plan["plan_id"], run["receipt_id"]
        code, blocked = self.cli(f, "promote", pid, "--receipt", rid)
        self.assertEqual((code, blocked["error"]["code"]), (5, "approval-required"))
        self.assertEqual(self.cli(f, "approve", pid, "--by", "alice", "--expires-in", "10")[0], 2)
        code, approved = self.cli(f, "approve", pid, "--by", "alice", "--expires-in", "600", "--review", "review:alice")
        self.assertEqual(code, 0, approved)
        code, landed = self.cli(f, "promote", pid, "--receipt", rid)
        self.assertEqual((code, landed["data"]["state"]), (0, "landed"))
        self.assertEqual(f.head("main"), plan["candidate"]["commit"])
        code, again = self.cli(f, "promote", pid, "--receipt", rid)
        self.assertEqual((code, again["data"]["repeated"]), (0, True))
        self.assertEqual(self.cli(f, "recover", "b1")[0], 0)
        self.assertEqual(self.cli(f, "recover", "b1", "--resolve", "promotion-x")[0], 2)
        self.assertEqual(self.cli(f, "revoke", approved["data"]["approval_id"])[0], 0)
        self.assertEqual(self.cli(f, "promote", pid, "--receipt", "receipt:" + "0" * 64)[0], 2)


if __name__ == "__main__":
    unittest.main()
