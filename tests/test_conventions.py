import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from fixtures import Mirror
from starlings import __version__
from starlings.cli import default_state, main
from starlings.policy import PolicyError, parse, preset, render, validate


def cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["--json", *args])
    return code, json.loads(out.getvalue())


class DefaultPathTests(unittest.TestCase):
    def test_state_and_authority_default_to_the_project_convention(self):
        m = Mirror()
        self.addCleanup(m.close)
        home = m.root / "home"
        home.mkdir()
        with patch.dict("os.environ", {"HOME": str(home)}):
            os.environ.pop("STARLINGS_STATE_DIR", None)
            expected = home / ".local" / "state" / "starlings" / "projects" / "sample-project"
            self.assertEqual(default_state(m.project), expected)
            code, reply = cli("authority", "sync", "--directory", str(m.project))
            self.assertEqual(code, 0, reply)
            self.assertEqual(reply["data"]["authority"], str(expected.resolve() / "authority.git"))
            self.assertEqual(stat.S_IMODE(expected.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(expected.parent.parent.stat().st_mode), 0o700)
            self.assertTrue((expected / "starlings.db").is_file())
            code, reply = cli("status", "absent", "--directory", str(m.project))
            self.assertEqual((code, "unknown batch" in reply["error"]["message"]), (2, True))
            explicit = m.root / "explicit-state"
            code, reply = cli("authority", "sync", "--directory", str(m.project), "--state-dir", str(explicit))
            self.assertEqual((code, reply["data"]["authority"]), (0, str(explicit / "authority.git")))

    def test_no_default_without_a_usable_policy(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"HOME": tmp}):
            os.environ.pop("STARLINGS_STATE_DIR", None)
            project = Path(tmp) / "project"
            project.mkdir()
            self.assertIsNone(default_state(project))
            code, reply = cli("authority", "sync", "--directory", str(project))
            self.assertEqual(code, 2)
            self.assertIn("STARLINGS_STATE_DIR is required", reply["error"]["message"])
            policy = preset("release-only")
            policy["project"] = "team/project name"
            (project / "starlings.toml").write_text(render(policy))
            self.assertIsNone(default_state(project))
            self.assertFalse((Path(tmp) / ".local").exists())


class RequiresTests(unittest.TestCase):
    def test_minimum_version_is_enforced(self):
        policy = preset("release-only")
        for wanted in (">=0.1", ">=0.2", ">=" + __version__):
            with self.subTest(wanted=wanted):
                self.assertEqual(parse(render({**policy, "requires_starlings": wanted}))["requires_starlings"], wanted)
        with self.assertRaisesRegex(PolicyError, "upgrade starlings"):
            validate({**policy, "requires_starlings": ">=999.0"})
        for wanted in ("0.2", ">0.2", ">=0", ">=0.2.0.1", ">= 0.2", 2):
            with self.subTest(wanted=wanted), self.assertRaisesRegex(PolicyError, "requires_starlings"):
                validate({**policy, "requires_starlings": wanted})

    def test_version_matches_the_package_metadata(self):
        pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
        self.assertIn(f'version = "{__version__}"', pyproject)
        self.assertEqual(__version__, "0.2.0")


if __name__ == "__main__":
    unittest.main()
