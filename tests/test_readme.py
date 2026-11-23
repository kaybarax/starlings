import argparse
import contextlib
import io
import json
import os
from pathlib import PurePosixPath
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

from starlings.cli import UsageError, parser

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
SRC = ROOT / "src"

FENCE = re.compile(r"```[^\n`]*\n(.*?)```", re.S)
PLACEHOLDER = re.compile(r"[<>$]")
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
PYTHON = re.compile(r"python\d*(\.\d+)*")


def option_kinds():
    kinds = {}

    def walk(p):
        for action in p._actions:
            for option in action.option_strings:
                kinds[option] = action
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict):
                for sub in choices.values():
                    if isinstance(sub, argparse.ArgumentParser):
                        walk(sub)

    walk(parser())
    return kinds


KINDS = option_kinds()


def dummy(action):
    if action is not None:
        choices = getattr(action, "choices", None)
        if choices and not isinstance(choices, dict):
            return next(iter(choices))
        if action.type is int:
            return "1"
        if action.type is Path:
            return "/tmp/starlings-readme-check"
    return "x"


def substitute(argv):
    out = []
    expect = None
    for token in argv:
        if expect is not None:
            out.append(dummy(KINDS[expect]) if PLACEHOLDER.search(token) else token)
            expect = None
            continue
        if token.startswith("--") and "=" not in token:
            base = token.split("=", 1)[0]
            action = KINDS.get(base)
            if action is not None and action.nargs != 0:
                expect = base
            out.append(token)
            continue
        if token.startswith("--") and "=" in token:
            base, _, value = token.partition("=")
            if PLACEHOLDER.search(value):
                token = base + "=" + dummy(KINDS.get(base))
            out.append(token)
            continue
        out.append(dummy(None) if PLACEHOLDER.search(token) else token)
    return out


def classify(tokens):
    head = PurePosixPath(tokens[0]).name
    if head == "starlings":
        return "cli", tokens[1:]
    if PYTHON.fullmatch(head) and len(tokens) > 2 and tokens[1] == "-m":
        module = tokens[2]
        if module == "starlings":
            return "cli", tokens[3:]
        if module.startswith("starlings."):
            return "module", (module, tokens[3:])
    return None


def readme_commands(text):
    for block in FENCE.findall(text):
        for raw in block.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            for segment in re.split(r"\s*(?:&&|\|\||[;|])\s*", line):
                try:
                    tokens = shlex.split(segment, comments=True)
                except ValueError:
                    continue
                while tokens and ENV_ASSIGN.match(tokens[0]):
                    tokens.pop(0)
                if tokens:
                    found = classify(tokens)
                    if found:
                        yield found


def cli_ok(argv):
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            parser().parse_args(substitute(list(argv)))
    except UsageError:
        return False
    except SystemExit as exc:
        return exc.code in (0, None)
    return True


def module_ok(module, argv):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.run([sys.executable, "-m", module, *substitute(list(argv)), "--help"], capture_output=True, text=True, env=env, timeout=60, check=False)
    return proc.returncode == 0


def github_slug(heading):
    return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", heading.strip().lower()))


def link_problems(text):
    problems = []
    for match in re.finditer(r"!?\[[^\]\n]*\]\(([^)\s]+)[^)]*\)", text):
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        path, _, fragment = target.partition("#")
        resolved = (README.parent / path).resolve() if path else README.resolve()
        if not resolved.exists():
            problems.append(f"{target}: no such file")
            continue
        if fragment and resolved.suffix == ".md":
            headings = re.findall(r"^#{1,6}\s+(.+?)\s*$", resolved.read_text(encoding="utf-8"), re.M)
            if github_slug(fragment) not in {github_slug(h) for h in headings}:
                problems.append(f"{target}: no such heading")
    return problems


class ReadmeCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.commands = list(readme_commands(README.read_text(encoding="utf-8")))

    def test_commands_found(self):
        self.assertGreaterEqual(len(self.commands), 10)

    def test_commands_parse(self):
        for kind, payload in self.commands:
            if kind == "cli":
                argv = payload
                with self.subTest(command=" ".join(argv)):
                    self.assertTrue(cli_ok(argv), f"not a valid starlings invocation: {' '.join(argv)}")
            else:
                module, argv = payload
                with self.subTest(command=f"python3 -m {module} {' '.join(argv)}"):
                    self.assertTrue(module_ok(module, argv), f"not a valid invocation: python3 -m {module} {' '.join(argv)}")

    def test_links_resolve(self):
        self.assertEqual(link_problems(README.read_text(encoding="utf-8")), [])


class DemoCommandTests(unittest.TestCase):
    def run_demo(self, root, *extra):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return subprocess.run([sys.executable, "-m", "starlings", "demo", "--directory", str(root), *extra], capture_output=True, text=True, env=env, timeout=600, check=False)

    def test_demo_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            proc = self.run_demo(root)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertIn("main is now", proc.stdout)
            self.assertTrue((root / "state" / "starlings.db").is_file())
            authority = root / "authority.git"
            head = subprocess.run(["git", "-C", str(authority), "rev-parse", "refs/heads/main"], capture_output=True, text=True, check=False)
            self.assertRegex(head.stdout.strip(), r"^[0-9a-f]{40}$")
            show = subprocess.run(["git", "-C", str(authority), "show", "refs/heads/main:src/starlings_example/features/feat_a.py"], capture_output=True, text=True, check=False)
            self.assertEqual(show.returncode, 0)
            self.assertIn("feat_a", show.stdout)

    def test_demo_json_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.run_demo(Path(tmp) / "demo", "--json")
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            envelope = json.loads(proc.stdout)
            self.assertEqual(envelope["schema"], 1)
            self.assertTrue(envelope["ok"])
            self.assertEqual(envelope["data"]["outcome"], "passed")
            self.assertRegex(envelope["data"]["main"], r"^[0-9a-f]{40}$")
            self.assertNotIn("working in", proc.stdout)

    def test_demo_refuses_nonempty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "demo"
            root.mkdir()
            (root / "keep.txt").write_text("keep")
            proc = self.run_demo(root)
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual((root / "keep.txt").read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
