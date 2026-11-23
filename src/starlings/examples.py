"""Deterministic example project generator.

`starlings examples init` writes a small pure-Python project (source package,
unittest suites and a starlings.toml) outside any existing checkout. Every
byte is a fixed function of the preset and qualification flag: no timestamps,
no randomness, no network access.
"""

import os
from pathlib import Path

from .batches import Failure, invalid
from .policy import PRESETS, preset, render

CHECKS = {
    "static": ["python3", "-m", "unittest", "tests.test_static"],
    "unit": ["python3", "-m", "unittest", "tests.test_unit"],
    "service": ["python3", "-m", "unittest", "tests.test_service"],
    "journey": ["python3", "-m", "unittest", "tests.test_journey"],
}

EXCLUDED = [".github/workflows/*", "secrets/*"]

SELECTION = {
    "static": ["src/*", "tests/test_static.py", "pyproject.toml", "starlings.toml"],
    "unit": ["src/*", "tests/test_unit.py"],
    "service": ["src/*", "tests/test_service.py"],
    "journey": ["src/*", "tests/test_journey.py", "journeys/*"],
}

ARTIFACTS = {"journey": ["out/journey.txt"]}

EXCLUSIVE = {"service": ["service-ledger"]}

FILES = {
    "src/starlings_example/__init__.py": '"""Small deterministic example project used by starlings examples init."""\n\n__version__ = "1.0.0"\n',
    "src/starlings_example/core.py": '''"""Core ledger calculations."""


class Ledger:
    def __init__(self, name="default"):
        self.name = name
        self._entries = []

    def add_entry(self, label, amount):
        if not label:
            raise ValueError("entry label must be nonempty")
        entry = {"label": label, "amount": amount}
        self._entries.append(entry)
        return entry

    def entries(self):
        return list(self._entries)

    def total(self):
        return compute_total(entry["amount"] for entry in self._entries)

    def count(self):
        return len(self._entries)


def compute_total(values):
    return sum(values)
''',
    "src/starlings_example/report.py": '''"""Report rendering for a ledger."""

from .core import Ledger


def render_report(ledger):
    if not isinstance(ledger, Ledger):
        raise TypeError("render_report expects a Ledger")
    lines = [f"ledger: {ledger.name}", f"entries: {ledger.count()}", f"total: {ledger.total():.2f}"]
    for entry in ledger.entries():
        lines.append(f"- {entry['label']}: {entry['amount']:.2f}")
    return "\\n".join(lines) + "\\n"
''',
    "src/starlings_example/service.py": '''"""Tiny in-process HTTP service wrapping a ledger."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .core import Ledger
from .report import render_report


class LedgerService:
    def __init__(self, ledger=None):
        self.ledger = ledger or Ledger()

    def add(self, label, amount):
        return self.ledger.add_entry(label, amount)

    def report(self):
        return render_report(self.ledger)

    def summary(self):
        return {"name": self.ledger.name, "entries": self.ledger.count(), "total": self.ledger.total()}


def make_server(service, host="127.0.0.1", port=0):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(service.summary()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer((host, port), Handler)
''',
    "src/starlings_example/journey.py": '''"""End-to-end journey: exercise the service, the report and any landed client."""

import importlib.util
import json
import pathlib
import threading
import urllib.request

from .report import render_report
from .service import LedgerService, make_server


def run_journey():
    service = LedgerService()
    service.add("opening", 10.0)
    service.add("sale", 5.5)
    server = make_server(service)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as response:
            summary = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=10)
    report = render_report(service.ledger)
    result = {"summary": summary, "report": report}
    if importlib.util.find_spec("starlings_example.client") is not None:
        from . import client

        result["client_total"] = client.client_total([1, 2, 3])
    out = pathlib.Path("out")
    out.mkdir(exist_ok=True)
    (out / "journey.txt").write_text(json.dumps(result, sort_keys=True) + "\\n")
    return result
''',
    "src/starlings_example/shared.py": '''"""Shared configuration edited by reviewed conflict resolutions."""

SHARED_NOTE = "base"
SHARED_RETRIES = 3
''',
    "src/starlings_example/features/__init__.py": '"""Feature modules added one reviewed task at a time."""\n',
    "tests/__init__.py": '''"""Test package; puts the src/ tree on sys.path for the example package."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
''',
    "tests/test_static.py": '''"""Static checks: every src module compiles and imports stay stdlib-local."""

import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
LOCAL = {"starlings_example"}


class StaticTests(unittest.TestCase):
    def test_sources_compile(self):
        sources = sorted(SRC.rglob("*.py"))
        self.assertTrue(sources)
        for path in sources:
            with self.subTest(path=str(path.relative_to(ROOT))):
                compile(path.read_bytes(), str(path), "exec")

    def test_imports_are_stdlib_or_local(self):
        allowed = set(sys.stdlib_module_names) | LOCAL
        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_bytes(), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    self.assertIn(name, allowed, f"{path.name} imports {name}")


if __name__ == "__main__":
    unittest.main()
''',
    "tests/test_unit.py": '''"""Unit tests for the core ledger, report and shared configuration."""

import unittest

from starlings_example import shared
from starlings_example.core import Ledger, compute_total
from starlings_example.report import render_report


class CoreTests(unittest.TestCase):
    def test_compute_total(self):
        self.assertEqual(compute_total([1, 2, 3]), 6)
        self.assertEqual(compute_total([]), 0)

    def test_ledger_entries_and_total(self):
        ledger = Ledger("test")
        ledger.add_entry("a", 1.5)
        ledger.add_entry("b", 2.5)
        self.assertEqual(ledger.count(), 2)
        self.assertEqual(ledger.total(), 4.0)
        self.assertEqual([entry["label"] for entry in ledger.entries()], ["a", "b"])

    def test_ledger_rejects_empty_label(self):
        with self.assertRaises(ValueError):
            Ledger().add_entry("", 1)

    def test_render_report(self):
        ledger = Ledger("r")
        ledger.add_entry("x", 2)
        report = render_report(ledger)
        self.assertIn("ledger: r", report)
        self.assertIn("total: 2.00", report)

    def test_shared_configuration(self):
        self.assertIsInstance(shared.SHARED_NOTE, str)
        self.assertGreater(shared.SHARED_RETRIES, 0)


if __name__ == "__main__":
    unittest.main()
''',
    "tests/test_service.py": '''"""Service tests: the HTTP ledger endpoint answers correctly."""

import json
import threading
import unittest
import urllib.request

from starlings_example.service import LedgerService, make_server


class ServiceTests(unittest.TestCase):
    def test_summary_endpoint(self):
        service = LedgerService()
        service.add("deposit", 3.25)
        server = make_server(service)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            port = server.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as response:
                self.assertEqual(response.status, 200)
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=10)
        self.assertEqual(payload["entries"], 1)
        self.assertEqual(payload["total"], 3.25)


if __name__ == "__main__":
    unittest.main()
''',
    "tests/test_journey.py": '''"""Journey test: run the full example pipeline and keep its artifact."""

import pathlib
import unittest

from starlings_example.journey import run_journey


class JourneyTests(unittest.TestCase):
    def test_journey_produces_report_and_artifact(self):
        result = run_journey()
        self.assertEqual(result["summary"]["entries"], 2)
        self.assertEqual(result["summary"]["total"], 15.5)
        self.assertIn("total: 15.50", result["report"])
        artifact = pathlib.Path("out") / "journey.txt"
        self.assertTrue(artifact.is_file())
        self.assertIn('"total"', artifact.read_text())


if __name__ == "__main__":
    unittest.main()
''',
}


def manifest(preset_name, qualification):
    if preset_name not in PRESETS:
        raise invalid(f"--preset: expected one of {', '.join(PRESETS)}")
    policy = preset(preset_name, "local")
    policy["project"] = "starlings-example"
    for gate in policy["gates"].values():
        if gate["scope"] != "none":
            gate["checks"] = list(CHECKS)
    policy["checks"] = dict(CHECKS)
    if qualification:
        policy["assembly"] = {"excluded": list(EXCLUDED)}
        policy["selection"] = {name: list(patterns) for name, patterns in SELECTION.items()}
        policy["artifacts"] = {name: list(paths) for name, paths in ARTIFACTS.items()}
        policy["exclusive"] = {name: list(resources) for name, resources in EXCLUSIVE.items()}
    return render(policy)


def write_exclusive(path, content):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def init(target, preset_name="release-only", qualification=False):
    target = Path(target)
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise Failure("conflict", "conflict", f"examples target must be absent or an empty directory: {target}; nothing was written")
    else:
        target.mkdir(mode=0o755, parents=True)
    written = []
    try:
        for relative, content in FILES.items():
            path = target / relative
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            write_exclusive(path, content)
            written.append(relative)
        write_exclusive(target / "starlings.toml", manifest(preset_name, qualification))
        written.append("starlings.toml")
    except FileExistsError as exc:
        raise Failure("conflict", "conflict", f"examples target already contains {exc.filename}; nothing was replaced") from exc
    return {"written": True, "path": str(target), "preset": preset_name, "qualification": qualification, "files": written, "checks": list(CHECKS), "warnings": ["Deterministic example project; review starlings.toml and replace the example scope before adoption."]}
