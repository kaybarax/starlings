import os
from pathlib import Path
import stat


ENTRIES = ("policy", "logs", "evidence", "artifacts", "signer-references", "journal.sqlite3", "starlings.db", "starlings.db-wal", "starlings.db-shm", "mirrors", "worktrees")
FILES = ("journal.sqlite3", "starlings.db", "starlings.db-wal", "starlings.db-shm")


def layout(checkout, root=None):
    checkout = Path(checkout).resolve(strict=True)
    root = Path(root) if root is not None else Path.home() / ".local" / "state" / "starlings"
    if not root.is_absolute():
        raise ValueError("state root must be an absolute path outside the checkout")
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise ValueError("state root and its ancestors must not be symlinks")
    if any(part.exists() and not part.is_dir() for part in root.parents):
        raise ValueError("state root ancestors must be directories")
    root = root.resolve()
    if root == checkout or checkout in root.parents or root in checkout.parents:
        raise ValueError("state root must be separate from, not inside or above, the checkout")
    if root.exists():
        info = root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError("existing state root must be an operator-owned directory with mode 0700 or stricter")
        for name in ENTRIES:
            child = root / name
            if child.is_symlink():
                raise ValueError(f"state entry must not be a symlink: {child}")
            if child.exists():
                info = child.stat()
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077 or (name not in FILES and not child.is_dir()) or (name in FILES and not child.is_file()):
                    raise ValueError(f"state entry has unsafe owner, permissions or type: {child}")
    return {"root": str(root), **{name: str(root / name) for name in ENTRIES}}


def prepare(checkout, root):
    if root is None:
        raise ValueError("--state-dir or STARLINGS_STATE_DIR is required for this command")
    layout(checkout, root)
    root = Path(root)
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    result = layout(checkout, root)
    for name in ("mirrors", "worktrees"):
        try:
            Path(result[name]).mkdir(mode=0o700)
        except FileExistsError:
            pass
    database = Path(result["starlings.db"])
    if not database.exists():
        os.close(os.open(database, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600))
    return layout(checkout, root)
