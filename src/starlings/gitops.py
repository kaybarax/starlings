import os
import re
import subprocess

MINIMUM = (2, 38)
NO_HOOKS = ("-c", "core.hooksPath=/dev/null")
CONFIG = NO_HOOKS + ("-c", "commit.gpgSign=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "protocol.allow=never", "-c", "protocol.file.allow=always")
IDENTITY = ("starlings", "starlings@localhost")


class GitError(RuntimeError):
    def __init__(self, args, returncode, stderr):
        super().__init__(f"git {' '.join(args)} failed with status {returncode}: {stderr.strip()}")
        self.returncode = returncode


def environment(date=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file", "GIT_PROTOCOL_FROM_USER": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
    if date is not None:
        for role in ("AUTHOR", "COMMITTER"):
            env[f"GIT_{role}_NAME"] = os.environ.get("STARLINGS_COMMIT_NAME") or IDENTITY[0]
            env[f"GIT_{role}_EMAIL"] = os.environ.get("STARLINGS_COMMIT_EMAIL") or IDENTITY[1]
            env[f"GIT_{role}_DATE"] = f"{int(date)} +0000"
    return env


def git(repository, *args, ok=(0,), date=None, text=True, input=None, hooks=False):
    argv = ["git", *(CONFIG if not hooks else CONFIG[len(NO_HOOKS):]), *(("-C", str(repository)) if repository is not None else ()), *args]
    result = subprocess.run(argv, capture_output=True, text=text, env=environment(date), input=input, stdin=subprocess.DEVNULL if input is None else None, check=False)
    if result.returncode not in ok:
        raise GitError(list(args), result.returncode, result.stderr if text else result.stderr.decode("utf-8", "replace"))
    return result


def version():
    output = git(None, "--version").stdout.strip()
    match = re.match(r"git version (\d+)\.(\d+)", output)
    if not match:
        raise ValueError(f"cannot parse Git version from {output!r}")
    found = (int(match.group(1)), int(match.group(2)))
    if found < MINIMUM:
        raise ValueError(f"Git >= {MINIMUM[0]}.{MINIMUM[1]} is required for merge-tree --write-tree; found {output}")
    return output


def is_bare(path):
    try:
        return git(path, "rev-parse", "--is-bare-repository").stdout.strip() == "true"
    except GitError:
        return False


def resolve(repository, ref):
    result = git(repository, "rev-parse", "--verify", "--quiet", "--end-of-options", ref + "^{commit}", ok=(0, 1))
    return result.stdout.strip() or None


def ref_value(repository, ref):
    result = git(repository, "rev-parse", "--verify", "--quiet", "--end-of-options", ref, ok=(0, 1))
    return result.stdout.strip() or None


def zero(repository):
    return "0" * (64 if git(repository, "rev-parse", "--show-object-format").stdout.strip() == "sha256" else 40)


def is_ancestor(repository, ancestor, descendant):
    return git(repository, "merge-base", "--is-ancestor", ancestor, descendant, ok=(0, 1)).returncode == 0


def update_ref(repository, ref, new, old, message="starlings", hooks=False):
    result = git(repository, "update-ref", "-m", message, ref, new, old, ok=(0, 1, 128), hooks=hooks)
    return result.returncode == 0


def ref_transaction(repository, verify, update, message="starlings", hooks=False):
    lines = ["start"]
    lines += [f"verify {ref} {sha}" for ref, sha in verify]
    lines += [f"update {ref} {new} {old}" for ref, new, old in update]
    lines += ["prepare", "commit"]
    result = git(repository, "update-ref", "-m", message, "--stdin", input="\n".join(lines) + "\n", ok=(0, 1, 128), hooks=hooks)
    return result.returncode == 0, result.stderr.strip()


def changed_paths(repository, old, new):
    output = git(repository, "diff-tree", "-r", "-z", "--name-only", "--no-renames", "--no-commit-id", old, new).stdout
    return sorted({path for path in output.split("\0") if path})


def merge(repository, ours, theirs):
    result = git(repository, "merge-tree", "--write-tree", "-z", "--name-only", "--no-messages", ours, theirs, ok=(0, 1))
    fields = result.stdout.split("\0")
    return fields[0], sorted({path for path in fields[1:] if path}) if result.returncode == 1 else []


def commit_tree(repository, tree, parents, message, date):
    args = ["commit-tree", tree]
    for parent in parents:
        args += ["-p", parent]
    return git(repository, *args, "-m", message, date=date).stdout.strip()


def tree_of(repository, commit):
    return git(repository, "rev-parse", "--verify", commit + "^{tree}").stdout.strip()


def parents_of(repository, commit):
    return tuple(git(repository, "rev-list", "--parents", "-n", "1", commit).stdout.split()[1:])


def init_mirror(path):
    if not path.exists():
        git(None, "init", "--quiet", "--bare", str(path))
    return path


def fetch(repository, source, *refspecs):
    git(repository, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--no-auto-gc", "--", str(source), *refspecs)


def add_worktree(mirror, path, branch, base):
    if path.exists():
        return
    if ref_value(mirror, "refs/heads/" + branch):
        git(mirror, "worktree", "add", "--quiet", str(path), branch)
    else:
        git(mirror, "worktree", "add", "--quiet", "-b", branch, str(path), base)
