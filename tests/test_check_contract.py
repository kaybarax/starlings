import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_verifier as tv
from starlings import promotion
from starlings.policy import PolicyError, parse, preset, render, validate

PROBE = """import json, pathlib, sys
pathlib.Path("out").mkdir(exist_ok=True)
context = json.loads(pathlib.Path(sys.argv[3].removeprefix("--context=")).read_text())
pathlib.Path("out/probe.json").write_text(json.dumps({"argv": sys.argv[1:], "context": context}))
"""


def probe_policy(**extra):
    policy = parse(tv.build_policy({"probe": ["python3", "-c", PROBE, "{base}", "{head}", "--context={context}", "{other}"]}, scope="full", artifacts={"probe": ["out/probe.json"]}))
    policy.update(extra)
    return render(policy)


class PlaceholderTests(tv.VerifierCase):
    def test_base_head_and_context_reach_the_check(self):
        fixture = self.make(probe_policy())
        self.ready(fixture)
        self.stage(fixture, {"journeys/onboarding.txt": "welcome back\n"})
        plan = self.plan(fixture)
        approval = tv.ok(self, tv.call(fixture, promotion.approve, plan["plan_id"], "alice", 600, "review:alice"))
        run = self.run_plan(fixture, plan["plan_id"])
        probe = json.loads((fixture.state / "artifacts" / run["run_id"] / "probe" / "out" / "probe.json").read_text())
        candidate = plan["candidate"]
        self.assertEqual(probe["argv"][:2], [candidate["base"], candidate["commit"]])
        self.assertEqual(probe["argv"][3], "{other}")
        given = Path(probe["argv"][2].removeprefix("--context="))
        self.assertFalse(given.exists())
        self.assertNotIn(str(fixture.state), str(given))
        context = probe["context"]
        self.assertEqual((context["schema"], context["kind"], context["plan_id"], context["gate"], context["batch"], context["check"], context["run_id"]), (1, "check-context", plan["plan_id"], "staging", "b1", "probe", run["run_id"]))
        self.assertEqual((context["base"], context["head"]), (candidate["base"], candidate["commit"]))
        self.assertEqual(context["candidate"], {key: candidate[key] for key in ("id", "commit", "tree", "parents", "target")})
        self.assertEqual([(item["id"], item["by"], item["review"]) for item in context["approvals"]], [(approval["approval_id"], "alice", "review:alice")])
        retained = f"logs/{run['run_id']}/probe.context.json"
        self.assertIn(retained, dict(run["receipt"]["artifacts"]))
        self.assertEqual(json.loads((fixture.state / retained).read_text()), context)
        self.assertEqual(self.check(fixture, run["receipt_id"])["status"], "valid")
        (fixture.state / retained).write_text("{}")
        self.assertEqual(self.check(fixture, run["receipt_id"], "invalid")["status"], "invalid")

    def test_checks_without_context_retain_no_context_file(self):
        fixture = self.make(tv.build_policy({"plain": ["python3", "-c", "import sys; sys.exit(0 if len(sys.argv[1]) >= 40 else 1)", "{head}"]}, scope="full"))
        self.ready(fixture)
        self.stage(fixture, {"journeys/onboarding.txt": "welcome back\n"})
        run = self.run_plan(fixture, self.plan(fixture)["plan_id"])
        self.assertEqual(sorted(dict(run["receipt"]["artifacts"])), [f"logs/{run['run_id']}/plain.err", f"logs/{run['run_id']}/plain.out"])

    def test_context_is_refused_on_hosted_gates(self):
        policy = preset("release-only", "github")
        policy["checks"]["example"] = ["python3", "check.py", "{base}", "{head}"]
        validate(policy)
        policy["checks"]["example"].append("{context}")
        with self.assertRaisesRegex(PolicyError, "local checks only"):
            validate(policy)


class EnvironmentTests(tv.VerifierCase):
    def environ(self, settings, operator):
        policy = parse(tv.build_policy({"environ": ["python3", "-c", tv.ENVIRON]}, scope="full", artifacts={"environ": ["out/env.json"]}))
        if settings is not None:
            policy["environment"] = settings
        fixture = self.make(render(policy))
        home = fixture.root / "operator-home"
        home.mkdir()
        with patch.dict("os.environ", {"HOME": str(home), **operator}):
            self.ready(fixture)
            self.stage(fixture, {"journeys/onboarding.txt": "welcome back\n"})
            run = self.run_plan(fixture, self.plan(fixture)["plan_id"])
        seen = json.loads((fixture.state / "artifacts" / run["run_id"] / "environ" / "out" / "env.json").read_text())
        return home, seen, run["checks"][0]["metrics"]

    def test_default_is_isolated_and_passes_nothing(self):
        home, seen, metrics = self.environ(None, {"SAMPLE_TOKEN": "value"})
        self.assertEqual(seen["HOME"], seen["TMPDIR"])
        self.assertNotEqual(seen["HOME"], str(home))
        self.assertNotIn("SAMPLE_TOKEN", seen)
        self.assertEqual((metrics["home"], metrics["env_passed"], metrics["env_missing"]), ("isolated", [], []))

    def test_operator_home_and_passed_variables(self):
        home, seen, metrics = self.environ({"home": "operator", "pass": ["SAMPLE_TOKEN", "SAMPLE_ABSENT"]}, {"SAMPLE_TOKEN": "value", "SAMPLE_OTHER": "hidden", "STARLINGS_STATE_DIR": "/nowhere"})
        self.assertEqual(seen["HOME"], str(home))
        self.assertNotEqual(seen["TMPDIR"], str(home))
        self.assertEqual(seen["SAMPLE_TOKEN"], "value")
        self.assertFalse({"SAMPLE_OTHER", "SAMPLE_ABSENT", "STARLINGS_STATE_DIR"} & set(seen))
        self.assertEqual((metrics["home"], metrics["env_passed"], metrics["env_missing"]), ("operator", ["SAMPLE_TOKEN"], ["SAMPLE_ABSENT"]))
        self.assertNotIn("value", json.dumps(metrics))

    def test_policy_validation_and_roundtrip(self):
        policy = preset("release-only")
        policy["environment"] = {"home": "operator", "pass": ["SAMPLE_TOKEN"]}
        self.assertEqual(parse(render(policy))["environment"], policy["environment"])
        for settings in ({"home": "shared"}, {"pass": ["GIT_DIR"]}, {"pass": ["STARLINGS_STATE_DIR"]}, {"pass": ["HOME"]}, {"pass": ["not a name"]}, {"pass": "SAMPLE_TOKEN"}, {"other": 1}):
            with self.subTest(settings=settings), self.assertRaises(PolicyError):
                validate({**policy, "environment": settings})
        from starlings.cli import warnings
        self.assertEqual(len([line for line in warnings(policy) if line.startswith("environment:")]), 2)


if __name__ == "__main__":
    unittest.main()
