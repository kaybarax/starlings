import copy
import io
import json
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from starlings.cli import main
from starlings.policy import PRESETS, PolicyError, parse, preset, render
from starlings.contracts import Adapter, Candidate, Evidence, Plan
from starlings.state import layout


class PolicyTests(unittest.TestCase):
    def test_presets(self):
        expected = [("none", "none", "each-lane"), ("none", "full", "combined-ready"), ("none", "full", "each-lane"), ("full", "full", "each-lane")]
        for name, (task, staging, mode) in zip(PRESETS, expected):
            with self.subTest(name=name):
                policy = parse(render(preset(name)))
                self.assertEqual(policy["gates"]["task"]["scope"], task)
                self.assertEqual(policy["gates"]["staging"]["scope"], staging)
                self.assertEqual(policy["gates"]["staging"]["mode"], mode)
                self.assertEqual(policy["gates"]["release"]["scope"], "full")

    def test_arbitrary_argv_roundtrip(self):
        policy = preset("release-only")
        policy["checks"]["example"] = ["python3", "", "\x7f", "\x01", "\n", "\t", "\U0001f426", "$(id); echo unsafe", 'quote"slash\\']
        self.assertEqual(parse(render(policy)), policy)

    def test_invalid_matrix(self):
        cases = [
            (("schema",), True), (("schema",), 2), (("extra",), 1),
            (("project",), ""), (("staging",), "main"), (("release",), "bad..ref"),
            (("limits", "cpu_slots"), True), (("limits", "memory_mib"), 0),
            (("limits", "timeout_seconds"), -1), (("limits", "max_guests"), 1.5),
            (("limits", "disk_mib"), 0), (("limits", "unknown"), 1),
            (("checks", "example"), "echo yes"), (("checks", "example"), []),
            (("checks", "example"), ["python3", 1]), (("checks", "example"), [""]),
            (("gates", "task", "executor"), "bogus"),
            (("gates", "task", "environment"), "bogus"),
            (("gates", "task", "scope"), "bogus"),
            (("gates", "release", "checks"), []),
            (("gates", "task", "checks"), ["missing"]),
            (("gates", "release", "scope"), "affected"),
            (("gates", "staging", "mode"), "bogus"),
            (("authority", "mode"), "github"),
            (("gates", "task", "checks"), ["example", "example"]),
            (("gates", "task", "unknown"), 1),
            (("gates", "unknown"), {}),
            (("gates", "task"), []),
            (("authority", "unknown"), 1),
            (("checks", "Bad-ID"), ["python3"]),
            (("fully_verified_release",), 1),
            (("limits", "timeout_seconds"), 604801),
            (("gates", "release", "environment"), "guest"),
            (("checks", "example"), ["python3", "bad\u0000arg"]),
        ]
        from starlings.policy import validate
        for keys, value in cases:
            with self.subTest(keys=keys, value=value):
                data = preset("release-only")
                target = data
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                with self.assertRaises(PolicyError):
                    validate(data)
        with self.assertRaises(PolicyError):
            parse("schema = [")
        with self.assertRaises(PolicyError):
            parse("schema = " + "[" * 1200 + "1" + "]" * 1200)


class CLITests(unittest.TestCase):
    def call(self, *args):
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
            code = main(list(args))
        return code, json.loads(out.getvalue())

    def test_offline_preview_doctor_and_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("subprocess.run", side_effect=AssertionError("subprocess")), patch("socket.socket", side_effect=AssertionError("network")):
                for name in PRESETS:
                    code, result = self.call("init", "--directory", tmp, "--preset", name, "--json")
                    self.assertEqual(code, 0)
                    self.assertFalse(result["data"]["written"])
                code, result = self.call("doctor", "--directory", tmp, "--json")
                self.assertEqual(code, 0)
                self.assertFalse(result["data"]["enforcement_implemented"])
                self.assertEqual(list(root.iterdir()), [])
                code, _ = self.call("init", "--directory", tmp, "--write", "--json")
                self.assertEqual(code, 0)
                original = (root / "starlings.toml").read_bytes()
                code, error = self.call("init", "--directory", tmp, "--write", "--json")
                self.assertEqual(code, 3)
                self.assertEqual(error["error"]["code"], "conflict")
                self.assertEqual((root / "starlings.toml").read_bytes(), original)
                self.assertEqual([p.name for p in root.iterdir()], ["starlings.toml"])
                self.assertEqual(self.call("policy", "validate", "--directory", tmp, "--json")[0], 0)

    def test_json_errors(self):
        for args in [("nonsense", "--json"), ("init", "--preset", "bad", "--json"), ("init", "--write", "--preview", "--json")]:
            code, result = self.call(*args)
            self.assertEqual(code, 2)
            self.assertFalse(result["ok"])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.call("policy", "validate", "--directory", tmp, "--json")[0], 3)
            (Path(tmp) / "starlings.toml").write_text("schema=true")
            self.assertEqual(self.call("policy", "validate", "--directory", tmp, "--json")[0], 2)
            (Path(tmp) / "starlings.toml").write_text("schema = " + "[" * 1200 + "1" + "]" * 1200)
            self.assertEqual(self.call("policy", "validate", "--directory", tmp, "--json")[0], 2)

    def test_exclusive_atomic_write(self):
        from starlings.cli import exclusive_write
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "starlings.toml"
            def write(value):
                try:
                    exclusive_write(path, value)
                    return True
                except FileExistsError:
                    return False
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(write, ["first" * 10000, "second" * 10000]))
            self.assertEqual(sorted(outcomes), [False, True])
            self.assertIn(path.read_text(), ["first" * 10000, "second" * 10000])
            self.assertEqual(list(Path(tmp).iterdir()), [path])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "starlings.toml"
            with patch("os.fsync", side_effect=OSError("injected flush failure")):
                with self.assertRaises(OSError):
                    exclusive_write(path, "complete")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_preserve_adopter_files_and_human_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".github" / "workflows").mkdir(parents=True)
            files = [root / "AGENTS.md", root / "CLAUDE.md", root / ".github" / "workflows" / "existing.yml"]
            for path in files:
                path.write_bytes(b"existing instructions")
            with patch("subprocess.Popen", side_effect=AssertionError("no subprocess")), patch("socket.create_connection", side_effect=AssertionError("no network")):
                self.assertEqual(self.call("init", "--directory", tmp, "--write", "--json")[0], 0)
            for path in files:
                self.assertEqual(path.read_bytes(), b"existing instructions")
            output = io.StringIO()
            with patch("sys.stdout", output):
                self.assertEqual(main(["init", "--directory", tmp]), 0)
            self.assertIn("Preview only:", output.getvalue())
            self.assertIn("Synthetic", output.getvalue())

    def test_symlink_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "starlings.toml").symlink_to(root / "absent")
            self.assertEqual(self.call("init", "--directory", tmp, "--write", "--json")[0], 3)
            self.assertFalse((root / "absent").exists())


class ContractTests(unittest.TestCase):
    def test_identity(self):
        candidate = Candidate("repo", "/authority.git", "refs/heads/main", "a" * 40, ("b" * 40,), "c" * 40, "d" * 40)
        self.assertEqual(candidate.id, copy.deepcopy(candidate).id)
        self.assertEqual(candidate.id, "candidate:49040dbb103ec2e248a26d62067cd265b4ab1c5735e6d057cf4bd1fa0d09cc43")
        body = candidate.to_dict()
        body.pop("id")
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        self.assertEqual(candidate.id, "candidate:" + hashlib.sha256(encoded).hexdigest())
        other = Candidate("repo", "/authority.git", "refs/heads/main", "a" * 40, ("e" * 40,), "c" * 40, "d" * 40)
        self.assertNotEqual(candidate.id, other.id)
        plan = Plan(candidate.id, "f" * 64, "release", ("example",), "local", "native")
        evidence = Evidence(plan.id, "example", 0, "e" * 64)
        adapter = Adapter("local", "1", ("inspect",))
        self.assertEqual(len({x.id for x in (candidate, plan, evidence, adapter)}), 4)
        for record in (candidate, plan, evidence, adapter):
            self.assertEqual(record.to_dict()["schema"], 1)
        with self.assertRaises(ValueError):
            Evidence(plan.id, "example", True, "e" * 64)

    def test_invalid_candidate_targets(self):
        for target in ("refs/heads/", "refs/heads/a..b", "refs/heads/a.lock", "refs/heads/a b", "refs/heads/@{bad", "refs/heads/.hidden", "refs/heads/a/", "refs/heads/a\x7f"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                Candidate("repo", "authority", target, "a" * 40, ("b" * 40,), "c" * 40, "d" * 40)
        candidate = Candidate("repo", "authority", "refs/heads/feature/nested-name", "a" * 40, ("b" * 40,), "c" * 40, "d" * 40)
        self.assertEqual(candidate.target, "refs/heads/feature/nested-name")

    def test_state_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve() / "checkout"
            root.mkdir()
            external = Path(tmp).resolve() / "private"
            result = layout(root, external)
            self.assertEqual(result["root"], str(external))
            self.assertFalse(external.exists())
            with self.assertRaises(ValueError):
                layout(root, root / "state")
            external.mkdir(mode=0o755)
            with self.assertRaises(ValueError):
                layout(root, external)
            external.chmod(0o700)
            self.assertEqual(layout(root, external)["root"], str(external))
            (external / "logs").symlink_to(root)
            with self.assertRaises(ValueError):
                layout(root, external)
            with self.assertRaises(ValueError):
                layout(root, Path("relative-state"))
            ancestor = Path(tmp).resolve() / "file"
            ancestor.write_text("not a directory")
            with self.assertRaises(ValueError):
                layout(root, ancestor / "state")


if __name__ == "__main__":
    unittest.main()
