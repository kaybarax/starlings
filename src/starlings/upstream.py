"""Mirror authority: follow and publish to an upstream repository.

With ``[authority] mode = "mirror"`` the local bare repository is still the
only authority: plans, receipts and promotions are made against it and
``promote`` alone lands a new release commit there. The upstream named in the
policy is a source the authority follows (``authority sync``) and a place a
landed, verified release is copied to (``authority publish``). Nothing read
from the upstream is ever treated as verified.

This module holds every Git command that can reach the network (``ls-remote``,
``fetch`` from the upstream and ``push``); ``gitops`` stays file-protocol-only.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import re
import subprocess

from . import gitops
from .batches import Failure, default_owner, invalid
from .policy import PolicyError, parse
from .store import LeaseHeld, LeaseLost
from .verifier import assess, current_trust, trusted_policy

CONFIG = gitops.NO_HOOKS + ("-c", "commit.gpgSign=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "protocol.allow=never", "-c", "protocol.file.allow=always", "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always")
KEPT = ("GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS")
CREDENTIALS = re.compile(r"(://)[^/@\s]+@")


def redact(text):
    """Never echo credentials embedded in a remote URL."""
    return CREDENTIALS.sub(r"\1***@", text)


def environment():
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_") or key in KEPT}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file:https:ssh", "GIT_PROTOCOL_FROM_USER": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
    return env


def run(repository, *args):
    return subprocess.run(["git", *CONFIG, "-C", str(repository), *args], capture_output=True, text=True, env=environment(), stdin=subprocess.DEVNULL, check=False)


def git(repository, *args):
    result = run(repository, *args)
    if result.returncode:
        raise Failure("failed", "upstream", redact(f"git {args[0]} failed with status {result.returncode}: {result.stderr.strip()}"))
    return result.stdout


def location(ctx, authority=None, create=False):
    """The bare authority: the given absolute path, or ``authority.git`` in the state directory."""
    path = Path(authority) if authority is not None else ctx.root / "authority.git"
    if not path.is_absolute():
        raise invalid("--authority must be an absolute path to a local bare repository")
    if create and not path.exists():
        gitops.git(None, "init", "--quiet", "--bare", str(path))
    if not path.is_dir():
        raise invalid(f"authority does not exist or is not a directory: {path}; run starlings authority sync to create it from the upstream")
    path = path.resolve(strict=True)
    if not gitops.is_bare(path):
        raise invalid(f"authority is not a bare Git repository: {path}")
    return path


def local_policy(ctx):
    manifest = ctx.directory / "starlings.toml"
    if not manifest.is_file():
        return None
    try:
        return parse(manifest.read_text(encoding="utf-8"))
    except (PolicyError, UnicodeError) as exc:
        raise invalid(f"{manifest} is invalid: {exc}") from exc


def governing(ctx, authority, trusted_only=False):
    """The policy that names the upstream: the trusted one, else (first use) the local manifest."""
    trusted = current_trust(ctx.store.connection, str(authority))
    local = local_policy(ctx)
    if trusted is None:
        if trusted_only:
            raise Failure("blocked", "untrusted-policy", f"no trusted policy for authority {authority}; run starlings policy trust first")
        if local is None:
            raise invalid(f"no trusted policy for {authority} and no starlings.toml in {ctx.directory}; nothing names the upstream")
        policy = local
    else:
        policy = trusted_policy(ctx, trusted["digest"])
        if local is not None and any(local[key] != policy[key] for key in ("authority", "staging", "release")):
            raise Failure("blocked", "upstream-mismatch", f"starlings.toml in {ctx.directory} names a different authority table, staging or release branch than the trusted policy {trusted['digest']}; the trusted policy decides where the authority syncs and publishes. Trust the reviewed policy first (starlings policy trust --supersedes {trusted['digest']}) or run from a directory whose policy matches", {"trusted_digest": trusted["digest"]})
    if policy["authority"]["mode"] != "mirror":
        raise Failure("blocked", "not-mirror", 'this command needs [authority] mode = "mirror" with an upstream in the policy; a local-bare authority has no upstream')
    return policy


def branches(policy):
    return policy["release"], policy["staging"]


def heads(authority, policy):
    """Read the upstream release and staging heads without writing anything."""
    url = policy["authority"]["upstream"]
    release, staging = branches(policy)
    rows = {}
    for line in git(authority, "ls-remote", "--", url, f"refs/heads/{release}", f"refs/heads/{staging}").splitlines():
        sha, _, ref = line.partition("\t")
        rows[ref] = sha
    found = rows.get(f"refs/heads/{release}"), rows.get(f"refs/heads/{staging}")
    if not all(found):
        missing = [name for name, sha in zip((release, staging), found) if not sha]
        raise Failure("blocked", "upstream-missing-branch", redact(f"the upstream {url} has no branch {' or '.join(missing)}; nothing was written"))
    return found


def fetch(authority, policy, expected):
    """Bring the upstream heads' objects in under refs/upstream/, which no release reads."""
    url = policy["authority"]["upstream"]
    release, staging = branches(policy)
    git(authority, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", "--no-auto-gc", "--", url, f"+refs/heads/{release}:refs/upstream/{release}", f"+refs/heads/{staging}:refs/upstream/{staging}")
    fetched = gitops.resolve(authority, f"refs/upstream/{release}"), gitops.resolve(authority, f"refs/upstream/{staging}")
    if fetched != tuple(expected):
        raise Failure("stale", "upstream-moved", "the upstream moved while it was being read; nothing was written. Start again.", {"read": list(expected), "fetched": list(fetched)})


def has_commit(authority, commit):
    return gitops.git(authority, "cat-file", "-e", commit + "^{commit}", ok=(0, 1, 128)).returncode == 0


def ahead(authority, ours, theirs):
    return bool(ours) and ours != theirs and not gitops.is_ancestor(authority, ours, theirs)


def landed_release(ctx, authority, policy, commit, release_head, staging_head):
    """The landed promotion that made ``commit`` from exactly these upstream heads, if its receipt still holds.

    The receipt must be passing and either fully valid (the release ref is back at
    its base) or stale for the single reason that the release ref now holds the
    promoted commit itself. Anything else — another signer, an untrusted policy, a
    modified log, a dead approval — is not a promotion to publish or restore.
    """
    target = f"refs/heads/{policy['release']}"
    if not has_commit(authority, commit) or list(gitops.parents_of(authority, commit)) != [release_head, staging_head]:
        return None
    rows = ctx.store.connection.execute("SELECT * FROM promotions WHERE repository = ? AND target = ? AND gate = 'release' AND state = 'landed' AND new = ? AND old = ? ORDER BY created DESC", (str(authority), target, commit, release_head)).fetchall()
    for row in rows:
        status, checked = assess(ctx, row["receipt_id"])
        if checked.get("outcome") != "passed":
            continue
        head = gitops.resolve(authority, target)
        moved = [f"{target} in the authority is {head}, not the receipt base {release_head}"]
        if (status == "valid" and head == release_head) or (status == "stale" and checked["reasons"] == moved and head == commit):
            return row
    return None


@contextmanager
def target_leases(ctx, authority, policy, owner, seconds, result):
    owner = owner or default_owner()
    held = []
    try:
        for branch in sorted(set(branches(policy))):
            held.append(ctx.store.acquire(f"target:{authority}:refs/heads/{branch}", owner, seconds))
    except LeaseHeld as exc:
        for lease in held:
            ctx.store.release(lease)
        raise Failure("blocked", "lease-held", f"{exc}; another promotion, trust or authority command holds it; retry") from exc

    def still_held():
        with ctx.store.transaction() as tx:
            for lease in held:
                ctx.store.check(tx, lease)
    try:
        yield still_held
    except LeaseLost as exc:
        raise Failure("stale", "lease-lost", f"{exc}; nothing further was written", result) from exc
    finally:
        released = True
        for lease in held:
            try:
                ctx.store.release(lease)
            except LeaseLost:
                released = False
        result["lease_released"] = released


def move(authority, ref, new, old, message):
    if not gitops.update_ref(authority, ref, new, old or gitops.zero(authority), message):
        raise Failure("stale", "stale", f"{ref} in the authority moved concurrently (expected {old or 'no ref'}); nothing further was written")


def view(policy, authority, release_head, staging_head):
    release, staging = branches(policy)
    return {"authority": str(authority), "upstream": redact(policy["authority"]["upstream"]), "release": release, "staging": staging, "upstream_heads": {release: release_head, staging: staging_head}}


def sync(ctx, authority=None, owner=None, seconds=600):
    authority = location(ctx, authority, create=True)
    policy = governing(ctx, authority)
    first = not gitops.git(authority, "for-each-ref", "--count=1", "refs/heads").stdout.strip()
    release, staging = branches(policy)
    result = {"first_use": first}
    with target_leases(ctx, authority, policy, owner, seconds, result) as still_held:
        release_head, staging_head = heads(authority, policy)
        fetch(authority, policy, (release_head, staging_head))
        result.update(view(policy, authority, release_head, staging_head))
        ours_release, ours_staging = gitops.resolve(authority, f"refs/heads/{release}"), gitops.resolve(authority, f"refs/heads/{staging}")
        result["authority_heads"] = {release: ours_release, staging: ours_staging}
        if ahead(authority, ours_staging, staging_head):
            raise Failure("blocked", "staging-ahead", f"authority {staging} {ours_staging} is ahead of or apart from the upstream {staging} {staging_head}. A mirror authority never releases staging work the upstream does not have. Nothing was written. To follow the upstream again, set it aside with: starlings authority discard", result)
        if ahead(authority, ours_release, release_head):
            landed = landed_release(ctx, authority, policy, ours_release, release_head, staging_head)
            if landed is None:
                raise Failure("blocked", "release-ahead", f"authority {release} {ours_release} is ahead of or apart from the upstream {release} {release_head} and is not a recorded promotion of the current upstream heads with a valid passing receipt. Nothing was written. To follow the upstream again and release afresh, set it aside with: starlings authority discard", result)
            result.update({"state": "unpublished", "unpublished": {"commit": ours_release, "promotion_id": landed["id"], "plan_id": landed["plan_id"], "receipt_id": landed["receipt_id"]}, "updated": []})
            return "ok", result
        still_held()
        updated = []
        for branch, ours, theirs in ((release, ours_release, release_head), (staging, ours_staging, staging_head)):
            if ours != theirs:
                move(authority, f"refs/heads/{branch}", theirs, ours, "starlings: sync from upstream")
                updated.append({"branch": branch, "old": ours, "new": theirs})
        result.update({"state": "synced", "unpublished": None, "updated": updated, "authority_heads": {release: release_head, staging: staging_head}})
    return "ok", result


def publish(ctx, authority=None, owner=None, seconds=600):
    authority = location(ctx, authority)
    policy = governing(ctx, authority, trusted_only=True)
    url, with_staging = policy["authority"]["upstream"], bool(policy["authority"].get("publish_staging", False))
    release, staging = branches(policy)
    result = {}
    with target_leases(ctx, authority, policy, owner, seconds, result) as still_held:
        release_head, staging_head = heads(authority, policy)
        result.update(view(policy, authority, release_head, staging_head))
        held = gitops.resolve(authority, f"refs/heads/{release}")
        result["commit"] = held
        if held is None:
            raise Failure("blocked", "nothing-to-publish", f"the authority has no {release} branch; run starlings authority sync first", result)
        if held == release_head or (has_commit(authority, release_head) and gitops.is_ancestor(authority, held, release_head)):
            result.update({"published": False, "state": "up-to-date", "note": f"the upstream {release} already contains the authority {release} {held}; nothing to publish"})
            return "ok", result
        landed = landed_release(ctx, authority, policy, held, release_head, staging_head)
        if landed is None:
            if has_commit(authority, held) and list(gitops.parents_of(authority, held)) != [release_head, staging_head]:
                raise Failure("stale", "upstream-moved", f"the upstream {release} or {staging} moved since authority {release} {held} was verified against them. Nothing was pushed; the authority holds the unpublished release. Set it aside and release the new heads: starlings authority discard", result)
            raise Failure("blocked", "unverified-release", f"authority {release} {held} is not a recorded promotion of the current upstream heads with a valid passing receipt. Nothing was pushed.", result)
        result.update({"promotion_id": landed["id"], "plan_id": landed["plan_id"], "receipt_id": landed["receipt_id"]})
        refspecs = [f"{held}:refs/heads/{release}"] + ([f"{held}:refs/heads/{staging}"] if with_staging else [])
        result["refspecs"] = refspecs
        ctx.hook("before-push", {"authority": str(authority), "commit": held, "refspecs": refspecs})
        still_held()
        pushed = run(authority, "push", "--atomic", "--porcelain", "--", url, *refspecs)
        if pushed.returncode:
            detail = redact((pushed.stderr.strip() + "\n" + pushed.stdout.strip()).strip())
            try:
                now = heads(authority, policy)
            except Failure:
                now = None
            result["push"] = {"status": pushed.returncode, "detail": detail[-2000:]}
            if now is not None and now != (release_head, staging_head):
                raise Failure("stale", "upstream-moved", f"the upstream moved during the publish and refused the push. Nothing is known to be published; read the upstream heads and start again.\n{detail}", result)
            raise Failure("failed", "publish-refused", f"the upstream refused the atomic push, so neither branch was updated; the authority still holds the unpublished release {held}. Fix the cause and publish again.\n{detail}", result)
        ctx.hook("after-push", {"authority": str(authority), "commit": held})
        confirmed = heads(authority, policy)
        expected = (held, held if with_staging else staging_head)
        if confirmed != expected:
            result["confirmed"] = list(confirmed)
            raise Failure("failed", "publish-unconfirmed", f"the push reported success but the upstream heads are {confirmed}, not {expected}", result)
        if with_staging:
            ours_staging = gitops.resolve(authority, f"refs/heads/{staging}")
            if ours_staging != held:
                move(authority, f"refs/heads/{staging}", held, ours_staging, "starlings: staging published with the release")
        for branch, sha in zip((release, staging), confirmed):
            current = gitops.ref_value(authority, f"refs/upstream/{branch}")
            if current != sha:
                gitops.update_ref(authority, f"refs/upstream/{branch}", sha, current or gitops.zero(authority), "starlings: published")
        result.update({"published": True, "state": "published", "upstream_heads": {release: confirmed[0], staging: confirmed[1]}})
    return "ok", result


def discard(ctx, authority=None, force=False, owner=None, seconds=600):
    authority = location(ctx, authority)
    policy = governing(ctx, authority)
    release, staging = branches(policy)
    result = {"discarded": []}
    with target_leases(ctx, authority, policy, owner, seconds, result) as still_held:
        release_head, staging_head = heads(authority, policy)
        fetch(authority, policy, (release_head, staging_head))
        result.update(view(policy, authority, release_head, staging_head))
        held = gitops.resolve(authority, f"refs/heads/{release}")
        if ahead(authority, held, release_head) and not force:
            landed = landed_release(ctx, authority, policy, held, release_head, staging_head)
            if landed is not None:
                result.update({"commit": held, "promotion_id": landed["id"], "receipt_id": landed["receipt_id"]})
                raise Failure("blocked", "publishable", f"authority {release} {held} is a valid, verified release of the current upstream {release} and {staging}. Nothing was discarded. Publish it with starlings authority publish; it is set aside without --force only after the upstream moves.", result)
        still_held()
        for branch, theirs in ((release, release_head), (staging, staging_head)):
            ref = f"refs/heads/{branch}"
            ours = gitops.resolve(authority, ref)
            if not ahead(authority, ours, theirs):
                continue
            kept = f"refs/discarded/{ours}"
            updates = [(ref, theirs, ours)]
            if gitops.ref_value(authority, kept) != ours:
                updates.insert(0, (kept, ours, gitops.zero(authority)))
            applied, detail = gitops.ref_transaction(authority, [], updates, "starlings: discard unpublished")
            if not applied:
                raise Failure("stale", "stale", f"{ref} in the authority moved concurrently ({detail or 'compare-and-swap failed'}); nothing further was written", result)
            result["discarded"].append({"branch": branch, "commit": ours, "kept_at": kept, "now": theirs})
        result["authority_heads"] = {release: gitops.resolve(authority, f"refs/heads/{release}"), staging: gitops.resolve(authority, f"refs/heads/{staging}")}
    return "ok", result
