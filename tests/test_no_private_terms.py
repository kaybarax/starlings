"""Guard that no private terms leak into tracked files.

Always-on checks scan every tracked file for absolute home paths
(``/Users/``, ``/home/``), PEM private-key blocks and the
``agent-coordination`` workspace marker. This test file is excluded from
the scan because it necessarily names the patterns it checks for.

An optional operator denylist extends the scan with additional terms. It
is read from the file named by ``$STARLINGS_DENYLIST``, or from
``~/.local/state/starlings/denylist.txt`` when the variable is unset.
The denylist lives outside the checkout by design: its terms name
private projects and must never be committed, so the denylist check
skips cleanly when no file is present rather than failing.
"""
import os
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()

ALWAYS = [
    (b"/Users/", "absolute home path /Users/"),
    (b"/home/", "absolute home path /home/"),
    (re.compile(rb"BEGIN .*PRIVATE KEY"), "PEM private-key block"),
    (b"agent-coordination", "agent-coordination marker"),
]

DEFAULT_DENYLIST = Path("~/.local/state/starlings/denylist.txt").expanduser()


def tracked_files():
    """Every tracked file as an absolute path; fall back to a filesystem walk."""
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return [ROOT / name.decode("utf-8", errors="surrogateescape") for name in result.stdout.split(b"\0") if name]
    return [path for path in sorted(ROOT.rglob("*")) if path.is_file() and ".git" not in path.relative_to(ROOT).parts]


def violations(terms):
    """All (path, line, description) hits for byte terms and regexes."""
    hits = []
    for path in tracked_files():
        if path == SELF or not path.is_file():
            continue
        try:
            lines = path.read_bytes().split(b"\n")
        except OSError:
            continue
        for needle, description in terms:
            for number, line in enumerate(lines, start=1):
                found = needle.search(line) if hasattr(needle, "search") else needle in line
                if found:
                    hits.append(f"{path.relative_to(ROOT)}:{number}: {description}")
    return hits


def denylist_path():
    configured = os.environ.get("STARLINGS_DENYLIST")
    return Path(configured).expanduser() if configured else DEFAULT_DENYLIST


def denylist_terms():
    path = denylist_path()
    if not path.is_file():
        return None
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        term = line.strip()
        if term and not term.startswith("#"):
            terms.append(term.encode("utf-8"))
    return terms


class PrivateTermsTests(unittest.TestCase):
    def test_no_private_terms_in_tracked_files(self):
        hits = violations(ALWAYS)
        self.assertEqual([], hits)

    def test_no_denylist_terms_in_tracked_files(self):
        terms = denylist_terms()
        if terms is None:
            self.skipTest(f"no denylist at {denylist_path()}; it stays outside the checkout by design")
        hits = violations([(term, "denylist term") for term in terms])
        self.assertEqual([], hits)


if __name__ == "__main__":
    unittest.main()
