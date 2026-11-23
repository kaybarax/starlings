import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from starlings import examples, qualification
from starlings.cli import main
from starlings.policy import PRESETS, parse


class ExamplesInitTests(unittest.TestCase):
    def init(self, target, preset_name="release-only", qualification_flag=False):
        return examples.init(target, preset_name, qualification_flag)

    def test_deterministic_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first"
            second = Path(tmp) / "second"
            self.init(first, "every-task-lane-release", True)
            self.init(second, "every-task-lane-release", True)
            first_files = {p.relative_to(first): p.read_bytes() for p in first.rglob("*") if p.is_file()}
            second_files = {p.relative_to(second): p.read_bytes() for p in second.rglob("*") if p.is_file()}
            self.assertEqual(first_files, second_files)
            self.assertIn(Path("starlings.toml"), first_files)
            self.assertIn(Path("src/starlings_example/core.py"), first_files)
            self.assertIn(Path("tests/test_journey.py"), first_files)

    def test_manifest_parses_for_every_preset(self):
        for name in PRESETS:
            with self.subTest(preset=name), tempfile.TemporaryDirectory() as tmp:
                result = self.init(Path(tmp) / "project", name, True)
                policy = parse((Path(tmp) / "project" / "starlings.toml").read_text())
                self.assertEqual(policy["gates"], result and parse(examples.manifest(name, True))["gates"])
                self.assertEqual(set(policy["checks"]), {"static", "unit", "service", "journey"})
                for gate in policy["gates"].values():
                    if gate["scope"] != "none":
                        self.assertEqual(gate["checks"], ["static", "unit", "service", "journey"])
                self.assertEqual(policy["assembly"]["excluded"], [".github/workflows/*", "secrets/*"])
                self.assertIn("service-ledger", policy["exclusive"]["service"])

    def test_qualification_tables_only_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain"
            qual = Path(tmp) / "qual"
            self.init(plain, "release-only", False)
            self.init(qual, "release-only", True)
            plain_policy = parse((plain / "starlings.toml").read_text())
            qual_policy = parse((qual / "starlings.toml").read_text())
            self.assertNotIn("assembly", plain_policy)
            self.assertNotIn("selection", plain_policy)
            self.assertIn("assembly", qual_policy)
            self.assertIn("artifacts", qual_policy)
            self.assertEqual(plain_policy["gates"], qual_policy["gates"])

    def test_refuses_nonempty_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "project"
            target.mkdir()
            (target / "keep.txt").write_text("keep")
            with self.assertRaises(Exception):
                self.init(target)
            self.assertEqual((target / "keep.txt").read_text(), "keep")
            self.assertFalse((target / "starlings.toml").exists())

    def test_generated_checks_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "project"
            self.init(target, "release-only", True)
            for check in ("static", "unit", "service", "journey"):
                module = check.replace("-", "_")
                proc = subprocess.run([sys.executable, "-m", "unittest", f"tests.test_{module}"], cwd=target, capture_output=True, text=True, timeout=120, check=False)
                self.assertEqual(proc.returncode, 0, f"{check} failed: {proc.stderr[-800:]}")

    def test_cli_examples_init_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cli-project"
            out = io.StringIO()
            with patch("sys.stdout", out):
                code = main(["examples", "init", str(target), "--preset", "each-lane-release", "--with-qualification", "--json"])
            self.assertEqual(code, 0)
            envelope = json.loads(out.getvalue())
            self.assertTrue(envelope["ok"])
            self.assertEqual(envelope["data"]["checks"], ["static", "unit", "service", "journey"])
            self.assertTrue((target / "starlings.toml").is_file())


class QualificationSpecTests(unittest.TestCase):
    def test_qualification_shape(self):
        spec = qualification.qualification_spec()
        self.assertEqual(sorted(spec), ["alpha", "beta", "delta", "gamma"])
        self.assertEqual(sum(len(tasks) for tasks in spec.values()), 32)
        self.assertTrue(all(len(tasks) == 8 for tasks in spec.values()))
        names = {task["name"] for tasks in spec.values() for task in tasks}
        self.assertIn("api-producer", names)
        self.assertIn("api-client", names)
        self.assertIn("gamma-producer", names)
        self.assertIn("gamma-consumer", names)
        self.assertIn("alpha-conflict-a", names)
        self.assertIn("alpha-conflict-b", names)
        self.assertIn("delta-excluded", names)

    def test_dependency_declarations(self):
        spec = qualification.qualification_spec()
        by_name = {task["name"]: task for tasks in spec.values() for task in tasks}
        self.assertEqual(by_name["api-client"]["after"], ["api-producer"])
        self.assertEqual(by_name["api-client"]["merge_after"], ["api-producer"])
        self.assertEqual(by_name["gamma-consumer"]["after"], ["gamma-producer"])
        self.assertEqual(by_name["gamma-consumer"]["merge_after"], ["gamma-producer"])
        self.assertEqual(by_name["delta-excluded"]["files"], {"secrets/deploy.txt": qualification.EXCLUDED_TXT})
        conflict_a = by_name["alpha-conflict-a"]["files"]["src/starlings_example/shared.py"]
        conflict_b = by_name["alpha-conflict-b"]["files"]["src/starlings_example/shared.py"]
        self.assertNotEqual(conflict_a, conflict_b)
        self.assertIn("alpha-side", conflict_a)
        self.assertIn("beta-side", conflict_b)

    def test_spec_tasks_are_dag_assignable(self):
        spec = qualification.qualification_spec()
        assigned = set()
        pending = [(lane, task) for lane in spec for task in spec[lane]]
        while pending:
            progressed = False
            for lane, task in list(pending):
                if set(task["after"]) <= assigned:
                    assigned.add(task["name"])
                    pending.remove((lane, task))
                    progressed = True
            if not progressed:
                self.fail("unsatisfiable --after declarations")
        self.assertEqual(len(assigned), 32)

    def test_bench_specs(self):
        for kind in ("plain", "conflict", "late", "fix"):
            spec = qualification.bench_spec(kind, tag="t1")
            self.assertEqual(sorted(spec), ["alpha", "beta"])
            self.assertTrue(all(spec[lane] for lane in spec))
        tagged = qualification.bench_spec("plain", tag="one")
        untagged = qualification.bench_spec("plain")
        self.assertNotEqual(tagged["alpha"][0]["files"], untagged["alpha"][0]["files"])
        self.assertEqual(qualification.bench_spec("plain", tag="one")["alpha"][0]["files"], tagged["alpha"][0]["files"])
        with self.assertRaises(ValueError):
            qualification.bench_spec("missing")

    def test_table_covers_presets(self):
        self.assertEqual(sorted(qualification.TABLE), sorted(PRESETS))

    def test_machine_profile_shape(self):
        profile = qualification.machine_profile()
        for key in ("hardware_model", "memory_bytes", "cpu_count", "python", "git", "starlings"):
            self.assertIn(key, profile)


if __name__ == "__main__":
    unittest.main()
