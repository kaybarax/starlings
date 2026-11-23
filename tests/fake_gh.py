"""Deterministic fake ``gh`` CLI for tests of the real GitHub adapter.

The fake is an executable Python script placed on ``PATH``. It answers
every invocation by substring-matching the joined argv against a
scripted response list in ``FAKE_GH_SCRIPT`` (a JSON file),
logs every call as one JSON line to ``FAKE_GH_LOG``, and can
materialize files next to a ``--dir`` argument (used by
``gh run download``). No network is ever touched: an unmatched call
exits 9, so a missing script entry is a loud failure, not a socket.
"""

import json
import os
from pathlib import Path
import tempfile

SCRIPT = """#!/usr/bin/env python3
import json
import os
import pathlib
import sys

argv = sys.argv[1:]
log = os.environ.get("FAKE_GH_LOG")
if log:
    with open(log, "a") as stream:
        stream.write(json.dumps(argv) + "\\n")
joined = " ".join(argv)
if "--input" in argv:
    try:
        joined += " " + pathlib.Path(argv[argv.index("--input") + 1]).read_text()
    except (OSError, ValueError):
        pass
script = os.environ.get("FAKE_GH_SCRIPT")
responses = json.loads(pathlib.Path(script).read_text()) if script else []
for entry in reversed(responses):
    if (entry["pattern"] == joined) if entry.get("exact") else (entry["pattern"] in joined):
        files = entry.get("write") or {}
        if files:
            base = pathlib.Path(argv[argv.index("--dir") + 1]) if "--dir" in argv else pathlib.Path(".")
            for relative, content in files.items():
                target = base / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
        sys.stdout.write(entry.get("stdout", ""))
        sys.stderr.write(entry.get("stderr", ""))
        sys.exit(entry.get("exit", 0))
sys.stderr.write("fake gh: no scripted response for: " + joined + "\\n")
sys.exit(9)
"""


class FakeGh:
    def __init__(self, test):
        self.temporary = tempfile.TemporaryDirectory(prefix="starlings-fake-gh-")
        test.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.script = self.root / "responses.json"
        self.script.write_text("[]")
        self.log = self.root / "calls.jsonl"
        binary = self.root / "gh"
        binary.write_text(SCRIPT)
        binary.chmod(0o755)
        self._path = os.environ.get("PATH", "")
        self._script_env = os.environ.get("FAKE_GH_SCRIPT")
        self._log_env = os.environ.get("FAKE_GH_LOG")
        os.environ["PATH"] = str(self.root) + os.pathsep + self._path
        os.environ["FAKE_GH_SCRIPT"] = str(self.script)
        os.environ["FAKE_GH_LOG"] = str(self.log)
        test.addCleanup(self.restore)

    def restore(self):
        os.environ["PATH"] = self._path
        if self._script_env is None:
            os.environ.pop("FAKE_GH_SCRIPT", None)
        else:
            os.environ["FAKE_GH_SCRIPT"] = self._script_env
        if self._log_env is None:
            os.environ.pop("FAKE_GH_LOG", None)
        else:
            os.environ["FAKE_GH_LOG"] = self._log_env

    def respond(self, pattern, stdout="", stderr="", exit=0, write=None, exact=False):
        responses = json.loads(self.script.read_text())
        entry = {"pattern": pattern, "exit": exit}
        if exact:
            entry["exact"] = True
        if stdout:
            entry["stdout"] = stdout
        if stderr:
            entry["stderr"] = stderr
        if write:
            entry["write"] = write
        responses.append(entry)
        self.script.write_text(json.dumps(responses))
        return entry

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def joined(self):
        return [" ".join(argv) for argv in self.calls()]
