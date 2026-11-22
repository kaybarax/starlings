import os
from pathlib import Path
import subprocess
import tempfile

from starlings import batches
from starlings.policy import preset, render

EXCLUDED = [".github/workflows/*", "secrets/*"]


def environment():
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    return env


def git(cwd, *args, check=True):
    result = subprocess.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgSign=false", "-c", "core.hooksPath=/dev/null", "-C", str(cwd), *args], capture_output=True, text=True, env=environment(), check=False)
    if check and result.returncode:
        raise AssertionError(f"git {args} failed: {result.stderr}")
    return result.stdout.strip()


def policy_text(scope="none", mode="each-lane", excluded=EXCLUDED):
    policy = preset("each-lane-release")
    policy["project"] = "fixture-project"
    policy["gates"]["staging"]["scope"] = scope
    policy["gates"]["staging"]["mode"] = mode
    if excluded is not None:
        policy["assembly"] = {"excluded": list(excluded)}
    return render(policy)


class Fixture:
    def __init__(self, scope="none", mode="each-lane", excluded=EXCLUDED):
        self.temporary = tempfile.TemporaryDirectory(prefix="starlings-fixture-")
        self.root = Path(self.temporary.name).resolve()
        self.authority = self.root / "authority.git"
        git(self.root, "init", "--quiet", "--bare", "--initial-branch=dev", str(self.authority))
        seed = self.root / "seed"
        git(self.root, "init", "--quiet", "--initial-branch=dev", str(seed))
        (seed / "ledger.txt").write_text("total = 0\n")
        (seed / "reports.txt").write_text("report = empty\n")
        (seed / "shared.txt").write_text("alpha\nbeta\ngamma\n")
        git(seed, "add", ".")
        git(seed, "commit", "--quiet", "-m", "seed")
        git(seed, "push", "--quiet", str(self.authority), "dev:refs/heads/dev", "dev:refs/heads/main")
        self.seed = seed
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "starlings.toml").write_text(policy_text(scope, mode, excluded))
        self.state = self.root / "state"
        self.clock = None
        self.hook = None

    def close(self):
        self.temporary.cleanup()

    def head(self, branch):
        return git(self.authority, "rev-parse", f"refs/heads/{branch}")

    def context(self, hook=None, clock=None):
        return batches.Context(self.state, self.project, clock=clock or self.clock, hook=hook or self.hook)

    def call(self, name, *args, hook=None, clock=None, **kwargs):
        with self.context(hook, clock) as ctx:
            try:
                return getattr(batches, name)(ctx, *args, **kwargs)
            except batches.Failure as exc:
                return exc.status, exc

    def work(self, worktree, files, message):
        for path, content in files.items():
            target = Path(worktree) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        git(worktree, "add", "-A")
        git(worktree, "commit", "--quiet", "-m", message)
        return git(worktree, "rev-parse", "HEAD")

    def external_commit(self, branch, path, content):
        clone = self.root / f"external-{branch}"
        if not clone.exists():
            git(self.root, "clone", "--quiet", "--branch", branch, str(self.authority), str(clone))
        git(clone, "pull", "--quiet", "--ff-only")
        (clone / path).write_text(content)
        git(clone, "commit", "--quiet", "-am", "external writer")
        git(clone, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
        return git(clone, "rev-parse", "HEAD")
