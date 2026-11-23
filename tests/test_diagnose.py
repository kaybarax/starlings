import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_promotion as tp
import test_verifier as tv
from fixtures import git
from starlings import diagnose, promotion, runner, verifier
from starlings.cli import main

SEMANTIC = {
    "calc.py": "def compute_total(xs):\n    return sum(xs)\n",
    "report.py": "from calc import compute_total\nprint(compute_total([1]))\n",
    "tests/journeys.py": "import pathlib, sys\nsys.path.insert(0, '.')\nimport report\nif pathlib.Path('summary.py').exists():\n    import summary\n",
}
CHECKS = {"journeys": ["python3", "tests/journeys.py"]}
RENAMED = {"calc.py": "def total(xs):\n    return sum(xs)\n", "report.py": "from calc import total\nprint(total([1]))\n"}
SUMMARY = {"summary.py": "from calc import compute_total\nprint(compute_total([2]))\n"}
BROKEN_REPORT = {"report.py": "from calc import missing_name\nprint(missing_name([1]))\n"}


class DiagnoseCase(tp.PromotionCase):
    def semantic(self):
        fixture = self.make(tp.gated_policy(checks=CHECKS), files=SEMANTIC)
        self.ready(fixture)
        return fixture

    def four_lanes(self, fixture, specs, batch="b1"):
        names = [lane for lane, _ in specs]
        tv.ok(self, fixture.call("prepare", batch, names, str(fixture.authority)))
        for lane, tasks in specs:
            for task, files in tasks:
                self.lane(fixture, batch, lane, task, files)
            tv.ok(self, fixture.call("assemble", batch, lane))
            tv.ok(self, fixture.call("freeze", batch, lane))
        return tv.ok(self, fixture.call("stage", batch, names))

    def failed_release(self, fixture, batch="b1"):
        plan = tv.ok(self, tv.call(fixture, verifier.plan, batch, "release"))
        run = tv.ok(self, tv.call(fixture, runner.run, plan["plan_id"]), "failed")
        self.assertEqual(run["outcome"], "failed")
        return plan, run

    def run_diagnose(self, fixture, *args, **kwargs):
        return tv.ok(self, tv.call(fixture, diagnose.diagnose, *args, **kwargs))

    def checks_of(self, entry):
        return [item["status"] for item in entry["checks"]]

    def assert_nothing_landed(self, fixture, plan_id, receipt_id, main_head, run_ids):
        self.assertEqual(fixture.head("main"), main_head)
        self.assertEqual(self.rows(fixture, "SELECT COUNT(*) AS n FROM promotions")[0]["n"], 0)
        self.assertEqual(self.rows(fixture, "SELECT COUNT(*) AS n FROM receipts")[0]["n"], 1)
        self.assertEqual(self.rows(fixture, "SELECT COUNT(*) AS n FROM runs")[0]["n"], 1)
        self.assertEqual(sorted(row["id"] for row in self.rows(fixture, "SELECT id FROM diagnostic_runs ORDER BY position")), sorted(run_ids))
        refused = tv.ok(self, tv.call(fixture, promotion.promote, plan_id, receipt_id), "failed")
        self.assertEqual(refused.code, "receipt-failed")
        status, exc = tv.call(fixture, promotion.promote, plan_id, run_ids[0])
        self.assertEqual((status, exc.code), ("invalid", "invalid"))
        self.assertIn("unknown receipt", exc.message)
        self.assertEqual(tv.call(fixture, verifier.check, run_ids[0])[0], "invalid")


class AcceptanceTests(DiagnoseCase):
    def test_single_task_defect_bisection(self):
        fixture = self.semantic()
        main0 = fixture.head("main")
        self.four_lanes(fixture, [
            ("alpha", [("alpha-notes", {"notes-alpha.txt": "alpha\n"})]),
            ("beta", [("beta-notes", {"notes-beta.txt": "beta\n"})]),
            ("gamma", [("g1", {"notes-g1.txt": "one\n"}), ("g2", BROKEN_REPORT), ("g3", {"notes-g3.txt": "three\n"}), ("g4", {"notes-g4.txt": "four\n"})]),
            ("delta", [("delta-notes", {"reports.txt": "report = delta\n"})]),
        ])
        plan, run = self.failed_release(fixture)
        result = self.run_diagnose(fixture, "b1", "release", run["receipt_id"], plan_id=plan["plan_id"])
        self.assertEqual(result["checks"], ["journeys"])
        kinds = [entry["basis"]["kind"] for entry in result["candidates"]]
        self.assertEqual(kinds[:5], ["baseline", "lane", "lane", "lane", "lane"])
        per_lane = {entry["basis"]["lane"]: entry for entry in result["candidates"] if entry["basis"]["kind"] == "lane"}
        self.assertEqual(sorted(per_lane), ["alpha", "beta", "delta", "gamma"])
        self.assertEqual(self.checks_of(per_lane["gamma"]), ["failed"])
        for lane in ("alpha", "beta", "delta"):
            self.assertEqual(self.checks_of(per_lane[lane]), ["passed"], lane)
        attribution = result["attribution"]
        self.assertEqual((attribution["kind"], attribution["lane"], attribution["tasks"]), ("task", "gamma", ["g2"]))
        info = result["bisections"]["gamma"]
        self.assertEqual(info["smallest_failing_prefix"], ["g1", "g2"])
        self.assertFalse(info["capped"])
        self.assertTrue(info["boundary_alone"]["failed"])
        boundary = info["boundary"]
        self.assertEqual((boundary["task"], boundary["retained_ref"]), ("g2", "refs/starlings/b1/tasks/g2"))
        self.assertTrue(boundary["retained"])
        mirror = Path(result["mirror"])
        self.assertEqual(git(mirror, "rev-parse", boundary["retained_ref"]), boundary["head"])
        subset = next(entry for entry in result["candidates"] if entry["basis"].get("task") == "g2")
        self.assertEqual(self.checks_of(subset), ["failed"])
        self.assertTrue(subset["ref"].startswith("refs/starlings/b1/diagnostics/"))
        self.assertEqual(git(mirror, "rev-parse", subset["ref"]), subset["commit"])
        self.assertEqual(git(fixture.authority, "for-each-ref", "refs/starlings"), "")
        self.assert_nothing_landed(fixture, plan["plan_id"], run["receipt_id"], main0, result["diagnostic_runs"])

    def test_lane_pair_interaction(self):
        fixture = self.semantic()
        main0 = fixture.head("main")
        self.four_lanes(fixture, [
            ("alpha", [("rename", RENAMED)]),
            ("beta", [("summary", SUMMARY)]),
            ("gamma", [("gamma-notes", {"notes-gamma.txt": "gamma\n"})]),
            ("delta", [("delta-notes", {"ledger.txt": "total = 9\n"})]),
        ])
        plan, run = self.failed_release(fixture)
        result = self.run_diagnose(fixture, "b1", "release", run["receipt_id"])
        self.assertEqual(result["attribution"]["kind"], "lane-pair")
        self.assertEqual(result["attribution"]["lanes"], ["alpha", "beta"])
        self.assertEqual(result["attribution"]["failing_pairs"], [["alpha", "beta"]])
        self.assertEqual(result["bisections"], {})
        for entry in result["candidates"]:
            if entry["basis"]["kind"] == "lane-pair":
                expected = "failed" if entry["basis"]["lanes"] == ["alpha", "beta"] else "passed"
                self.assertEqual(self.checks_of(entry), [expected], entry["basis"]["lanes"])
        pair = next(entry for entry in result["candidates"] if entry["basis"].get("lanes") == ["alpha", "beta"])
        self.assertEqual(git(Path(result["mirror"]), "rev-parse", pair["ref"]), pair["commit"])
        self.assert_nothing_landed(fixture, plan["plan_id"], run["receipt_id"], main0, result["diagnostic_runs"])
        narrowed = self.run_diagnose(fixture, "b1", "release", run["receipt_id"], checks=["journeys"])
        self.assertEqual(narrowed["attribution"]["kind"], "lane-pair")
        status, exc = tv.call(fixture, diagnose.diagnose, "b1", "release", run["receipt_id"], checks=["bogus"])
        self.assertEqual(status, "invalid")
        self.assertIn("bogus", exc.message)

    def test_passed_release_is_blocked_and_bad_ids_invalid(self):
        fixture = self.semantic()
        self.four_lanes(fixture, [("work", [("notes", {"notes.txt": "n\n"})])])
        plan = tv.ok(self, tv.call(fixture, verifier.plan, "b1", "release"))
        run = tv.ok(self, tv.call(fixture, runner.run, plan["plan_id"]))
        self.assertEqual(run["outcome"], "passed")
        status, exc = tv.call(fixture, diagnose.diagnose, "b1", "release", run["receipt_id"])
        self.assertEqual((status, exc.code), ("blocked", "nothing-to-diagnose"))
        status, exc = tv.call(fixture, diagnose.diagnose, "b1", "release", "receipt:" + "0" * 64)
        self.assertEqual((status, exc.code), ("invalid", "invalid"))
        status, exc = tv.call(fixture, diagnose.diagnose, "b1", "release", run["receipt_id"], plan_id="plan:" + "0" * 64)
        self.assertEqual((status, exc.code), ("invalid", "invalid"))
        self.assertEqual(self.rows(fixture, "SELECT COUNT(*) AS n FROM diagnostic_runs")[0]["n"], 0)

    def test_cli_diagnose(self):
        fixture = self.semantic()
        self.four_lanes(fixture, [
            ("alpha", [("a1", {"notes-a.txt": "a\n"})]),
            ("gamma", [("g1", {"notes-g1.txt": "one\n"}), ("bad", BROKEN_REPORT)]),
        ])
        plan, run = self.failed_release(fixture)
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
            code = main(["diagnose", "b1", "--gate", "release", "--receipt", run["receipt_id"], "--plan", plan["plan_id"], "--state-dir", str(fixture.state), "--directory", str(fixture.project), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual((payload["data"]["attribution"]["kind"], payload["data"]["attribution"]["tasks"]), ("task", ["bad"]))


if __name__ == "__main__":
    unittest.main()
