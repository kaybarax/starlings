from fnmatch import fnmatchcase
import hashlib
import json
import os
from pathlib import Path
import re
import socket

from . import gitops
from .contracts import Assembly, Candidate
from .policy import parse
from .state import prepare as prepare_state
from .store import LeaseHeld, LeaseLost, RequestConflict, Store

EXIT = {"ok": 0, "invalid": 2, "conflict": 3, "pending": 4, "parked": 5, "blocked": 5, "stale": 6, "failed": 7}
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
OBJECT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class Failure(Exception):
    def __init__(self, status, code, message, data=None):
        super().__init__(message)
        self.status, self.code, self.message, self.data = status, code, message, data


def invalid(message):
    return Failure("invalid", "invalid", message)


def check_name(value, label):
    if type(value) is not str or not NAME.fullmatch(value):
        raise invalid(f"{label}: expected 1-63 lowercase letters, digits, hyphens or underscores, starting with a letter or digit")
    return value


def check_object(value, label):
    if type(value) is not str or not OBJECT.fullmatch(value):
        raise invalid(f"{label}: expected a full lowercase hexadecimal commit ID")
    return value


def check_text(value, label):
    if type(value) is not str or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise invalid(f"{label}: expected a nonempty string without control characters")
    return value


def default_owner():
    return f"{socket.gethostname()}:{os.getpid()}"


def refs(batch):
    prefix = f"refs/starlings/{batch}"
    return {
        "base": f"{prefix}/base",
        "lane": lambda lane: f"{prefix}/lanes/{lane}",
        "frozen": lambda lane: f"{prefix}/frozen/{lane}",
        "task": lambda task: f"{prefix}/tasks/{task}",
        "resolution": lambda lane, task: f"{prefix}/resolutions/{lane}/{task}",
        "stage_resolution": lambda candidate: f"{prefix}/resolutions/@stage/{candidate.split(':', 1)[1]}",
        "candidate": lambda candidate: f"{prefix}/candidates/{candidate.split(':', 1)[1]}",
    }


class Context:
    def __init__(self, state_dir, directory, clock=None, hook=None, create=True):
        directory = Path(directory).resolve(strict=True)
        if not directory.is_dir():
            raise invalid("--directory must identify an existing directory")
        self.directory = directory
        if state_dir is None:
            raise invalid("--state-dir or STARLINGS_STATE_DIR is required")
        if not create and not Path(state_dir).is_dir():
            raise invalid(f"state directory does not exist: {state_dir}")
        try:
            self.paths = prepare_state(directory, state_dir)
        except ValueError as exc:
            raise invalid(str(exc)) from exc
        self.root = Path(self.paths["root"])
        self.store = Store(self.paths["starlings.db"], clock)
        self.hook = hook or (lambda point, info: None)

    def close(self):
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def load_batch(c, batch):
    row = c.execute("SELECT * FROM batches WHERE name = ?", (batch,)).fetchone()
    if row is None:
        raise invalid(f"unknown batch {batch!r}")
    return row


def load_lane(c, batch, lane):
    row = c.execute("SELECT * FROM lanes WHERE batch = ? AND name = ?", (batch, lane)).fetchone()
    if row is None:
        raise invalid(f"unknown lane {lane!r} in batch {batch!r}")
    return row


def load_task(c, batch, task):
    row = c.execute("SELECT * FROM tasks WHERE batch = ? AND name = ?", (batch, task)).fetchone()
    if row is None:
        raise invalid(f"unknown task {task!r} in batch {batch!r}")
    return row


def batch_summary(row, repeated):
    policy = json.loads(row["policy"])
    return {"batch": row["name"], "project": row["project"], "authority": row["authority"], "mirror": row["mirror"], "staging": row["staging"], "release": row["release"], "base": row["base"], "release_base": row["release_base"] or None, "base_ref": refs(row["name"])["base"], "policy_digest": row["policy_digest"], "policy_trust": "untrusted", "staging_gate": {"scope": policy["gates"]["staging"]["scope"], "mode": policy["gates"]["staging"]["mode"]}, "excluded": policy.get("assembly", {}).get("excluded", []), "repeated": repeated}


def prepare(ctx, batch, lanes, authority):
    check_name(batch, "batch")
    if type(lanes) is not list or not lanes or len(set(lanes)) != len(lanes):
        raise invalid("--lanes: expected a nonempty comma-separated list without duplicates")
    for lane in lanes:
        check_name(lane, "lane")
    try:
        gitops.version()
    except ValueError as exc:
        raise Failure("invalid", "unsupported-git", str(exc)) from exc
    authority = Path(authority)
    if not authority.is_absolute():
        raise invalid("--authority must be an absolute path to a local bare repository")
    if not authority.is_dir():
        raise invalid(f"authority does not exist or is not a directory: {authority}")
    authority = authority.resolve(strict=True)
    if not gitops.is_bare(authority):
        raise invalid(f"authority is not a bare Git repository: {authority}")
    manifest = ctx.directory / "starlings.toml"
    source = manifest.read_bytes()
    policy = parse(source.decode("utf-8"))
    digest = hashlib.sha256(source).hexdigest()
    staging, release = policy["staging"], policy["release"]
    mirror = gitops.init_mirror(ctx.root / "mirrors" / (hashlib.sha256(str(authority).encode("utf-8")).hexdigest() + ".git"))
    gitops.fetch(mirror, authority, "+refs/heads/*:refs/remotes/authority/*")
    base = gitops.resolve(mirror, f"refs/remotes/authority/{staging}")
    if base is None:
        raise invalid(f"authority has no staging branch refs/heads/{staging}")
    release_base = gitops.resolve(mirror, f"refs/remotes/authority/{release}") or ""
    now = int(ctx.store.clock())
    with ctx.store.transaction() as c:
        try:
            repeated = ctx.store.request(c, "batch-prepare", batch, {"authority": str(authority), "lanes": lanes, "policy_digest": digest})
        except RequestConflict as exc:
            raise Failure("conflict", "conflict", f"batch {batch!r} already exists with different authority, lanes or policy") from exc
        if not repeated:
            c.execute("INSERT INTO batches(name, project, authority, mirror, staging, release, policy_digest, policy, base, release_base, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (batch, policy["project"], str(authority), str(mirror), staging, release, digest, json.dumps(policy, sort_keys=True), base, release_base, now))
            for position, lane in enumerate(lanes):
                c.execute("INSERT INTO lanes(batch, name, position, tip) VALUES (?, ?, ?, ?)", (batch, lane, position, base))
        row = load_batch(c, batch)
    ref = refs(batch)["base"]
    if gitops.ref_value(mirror, ref) != row["base"]:
        gitops.update_ref(mirror, ref, row["base"], gitops.ref_value(mirror, ref) or gitops.zero(mirror))
    return "ok", {**batch_summary(row, repeated), "lanes": lanes}


def assign(ctx, batch, task, lane, owner, after):
    check_name(task, "task")
    check_text(owner, "--owner")
    after = list(after or [])
    for prerequisite in after:
        check_name(prerequisite, "--after")
    if len(set(after)) != len(after) or task in after:
        raise invalid("--after: prerequisites must be distinct and must not name the task itself")
    with ctx.store.transaction() as c:
        b = load_batch(c, batch)
        load_lane(c, batch, lane)
        worktree = ctx.root / "worktrees" / batch / task
        branch = f"starlings/{batch}/{task}"
        try:
            repeated = ctx.store.request(c, "task-assign", f"{batch}/{task}", {"lane": lane, "owner": owner, "after": after})
        except RequestConflict as exc:
            existing = load_task(c, batch, task)
            raise Failure("conflict", "claimed", f"task {task!r} is already claimed by {existing['owner']!r} in lane {existing['lane']!r}") from exc
        if not repeated:
            for prerequisite in after:
                if c.execute("SELECT 1 FROM tasks WHERE batch = ? AND name = ?", (batch, prerequisite)).fetchone() is None:
                    raise invalid(f"--after {prerequisite!r}: prerequisites must already be assigned in this batch")
            seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM tasks WHERE batch = ?", (batch,)).fetchone()[0]
            c.execute("INSERT INTO tasks(batch, name, lane, owner, seq, worktree, branch, state) VALUES (?, ?, ?, ?, ?, ?, ?, 'assigned')", (batch, task, lane, owner, seq, str(worktree), branch))
            for position, prerequisite in enumerate(after):
                c.execute("INSERT INTO dependencies(batch, task, prerequisite, position) VALUES (?, ?, ?, ?)", (batch, task, prerequisite, position))
        row = load_task(c, batch, task)
    worktree.parent.mkdir(mode=0o700, exist_ok=True)
    gitops.add_worktree(Path(b["mirror"]), worktree, branch, b["base"])
    return "ok", {"batch": batch, "task": task, "lane": lane, "owner": owner, "after": after, "seq": row["seq"], "worktree": str(worktree), "branch": branch, "base": b["base"], "mirror": b["mirror"], "repeated": repeated}


def dependencies(c, batch, task):
    return c.execute("SELECT d.prerequisite, d.head AS declared, d.position, p.lane, p.state, p.head FROM dependencies d JOIN tasks p ON p.batch = d.batch AND p.name = d.prerequisite WHERE d.batch = ? AND d.task = ? ORDER BY d.position", (batch, task)).fetchall()


def handoff(ctx, batch, task, head, review, prerequisites):
    check_object(head, "--head")
    check_text(review, "--review")
    given = {}
    for item in prerequisites or []:
        prerequisite, _, sha = item.partition("=")
        check_name(prerequisite, "--prerequisite")
        check_object(sha, "--prerequisite")
        if prerequisite in given:
            raise invalid(f"--prerequisite {prerequisite!r} given more than once")
        given[prerequisite] = sha
    with ctx.store.transaction() as c:
        b = load_batch(c, batch)
        t = load_task(c, batch, task)
        declared = [row["prerequisite"] for row in dependencies(c, batch, task)]
    mirror = Path(b["mirror"])
    if gitops.resolve(mirror, head) != head:
        raise invalid(f"--head {head} is not a commit in the coordinator object store {mirror}")
    if not gitops.is_ancestor(mirror, b["base"], head):
        raise invalid(f"--head {head} does not descend from batch base {b['base']}")
    same = t["head"] == head and t["review"] == review and {row["prerequisite"]: row["declared"] for row in dependencies_rows(ctx, batch, task)} == {p: given.get(p) for p in declared} and set(given) <= set(declared)
    if t["state"] in ("included", "staged"):
        if same:
            return "ok", task_view(ctx, batch, task, repeated=True)
        raise Failure("conflict", "conflict", f"task {task!r} is already {t['state']}; its handoff cannot change")
    ref = refs(batch)["task"](task)
    current = gitops.ref_value(mirror, ref)
    if current != head and not gitops.update_ref(mirror, ref, head, current or gitops.zero(mirror), f"starlings: handoff {batch}/{task}"):
        raise Failure("stale", "stale", f"retained task ref {ref} moved concurrently")
    with ctx.store.transaction() as c:
        reasons = [f"prerequisite {p!r} is declared with --after but no --prerequisite {p}=<sha> was given" for p in declared if p not in given]
        reasons += [f"--prerequisite {p!r} is not declared with --after at assignment" for p in given if p not in declared]
        for row in dependencies(c, batch, task):
            if row["prerequisite"] not in given:
                continue
            if row["head"] is None:
                reasons.append(f"prerequisite {row['prerequisite']!r} has not been handed off")
            elif row["head"] != given[row["prerequisite"]]:
                reasons.append(f"prerequisite {row['prerequisite']!r} head {given[row['prerequisite']]} does not match handed-off head {row['head']}")
        state = load_task(c, batch, task)["state"]
        if state in ("included", "staged"):
            raise Failure("conflict", "conflict", f"task {task!r} became {state} concurrently")
        c.execute("UPDATE tasks SET head = ?, review = ?, state = ?, reason = ?, park_kind = ? WHERE batch = ? AND name = ?", (head, review, "parked" if reasons else "handed-off", "; ".join(reasons) or None, "handoff" if reasons else None, batch, task))
        for prerequisite in declared:
            c.execute("UPDATE dependencies SET head = ? WHERE batch = ? AND task = ? AND prerequisite = ?", (given.get(prerequisite), batch, task, prerequisite))
    return ("parked" if reasons else "ok"), task_view(ctx, batch, task, repeated=same)


def dependencies_rows(ctx, batch, task):
    return ctx.store.connection.execute("SELECT prerequisite, head AS declared FROM dependencies WHERE batch = ? AND task = ?", (batch, task)).fetchall()


def task_view(ctx, batch, task, repeated=None):
    c = ctx.store.connection
    t = load_task(c, batch, task)
    b = load_batch(c, batch)
    ref = refs(batch)["task"](task)
    retained = gitops.ref_value(Path(b["mirror"]), ref) if t["head"] else None
    result = {"batch": batch, "task": task, "lane": t["lane"], "owner": t["owner"], "seq": t["seq"], "state": t["state"], "reason": t["reason"], "park_kind": t["park_kind"], "head": t["head"], "review": t["review"], "base": b["base"], "worktree": t["worktree"], "branch": t["branch"], "after": [row["prerequisite"] for row in dependencies(c, batch, task)], "prerequisites": {row["prerequisite"]: row["declared"] for row in dependencies(c, batch, task) if row["declared"]}, "included_commit": t["included_commit"], "included_position": t["included_position"], "candidate": t["candidate"], "retained_ref": ref if t["head"] else None, "retained_sha": retained, "retained": bool(t["head"]) and retained == t["head"]}
    if repeated is not None:
        result["repeated"] = repeated
    return result


def park(ctx, lease, batch, task, kind, reason):
    with ctx.store.transaction() as c:
        ctx.store.check(c, lease)
        c.execute("UPDATE tasks SET state = 'parked', park_kind = ?, reason = ? WHERE batch = ? AND name = ?", (kind, reason, batch, task))


def lane_lease(ctx, batch, lane, owner, seconds):
    try:
        return ctx.store.acquire(f"lane:{batch}/{lane}", owner, seconds)
    except LeaseHeld as exc:
        raise Failure("blocked", "lease-held", str(exc)) from exc


def release(ctx, lease, result):
    try:
        ctx.store.release(lease)
        result["lease_released"] = True
    except LeaseLost:
        result["lease_released"] = False


def lane_tip(ctx, b, lane_row):
    mirror = Path(b["mirror"])
    ref = refs(b["name"])["lane"](lane_row["name"])
    current = gitops.ref_value(mirror, ref)
    if current != lane_row["tip"] and not (current is None and lane_row["tip"] == b["base"]):
        raise Failure("stale", "lane-diverged", f"{ref} is {current} but the journal records {lane_row['tip']}; reconciliation is not implemented in this milestone")
    return ref, current


def excluded_globs(b):
    return json.loads(b["policy"]).get("assembly", {}).get("excluded", [])


def excluded_paths(b, old, new):
    globs = excluded_globs(b)
    if not globs:
        return []
    return [path for path in gitops.changed_paths(Path(b["mirror"]), old, new) if any(fnmatchcase(path, pattern) for pattern in globs)]


def declared(c, batch, task):
    seen, stack = set(), [task]
    while stack:
        for row in c.execute("SELECT prerequisite FROM dependencies WHERE batch = ? AND task = ?", (batch, stack.pop())).fetchall():
            if row["prerequisite"] not in seen:
                seen.add(row["prerequisite"])
                stack.append(row["prerequisite"])
    return seen


def undeclared(c, b, head, allowed, contained=()):
    mirror = Path(b["mirror"])
    result = []
    for row in c.execute("SELECT name, head FROM tasks WHERE batch = ? AND head IS NOT NULL ORDER BY seq", (b["name"],)).fetchall():
        if row["name"] in allowed or row["head"] == b["base"] or not gitops.is_ancestor(mirror, row["head"], head):
            continue
        if any(gitops.is_ancestor(mirror, row["head"], other) for other in contained):
            continue
        result.append(row["name"])
    return result


def undeclared_reason(names):
    return "head contains undeclared task content from " + ", ".join(repr(name) for name in names) + "; declare them with --after or remove them"


def include(ctx, lease, b, lane, task, ref, current, commit, resolution=None):
    mirror = Path(b["mirror"])
    with ctx.store.transaction() as c:
        ctx.store.check(c, lease)
    if not gitops.update_ref(mirror, ref, commit, current or gitops.zero(mirror), f"starlings: include {b['name']}/{task}"):
        raise Failure("stale", "stale", f"{ref} moved concurrently; nothing was recorded")
    with ctx.store.transaction() as c:
        position = c.execute("SELECT COALESCE(MAX(included_position), 0) + 1 FROM tasks WHERE batch = ? AND lane = ?", (b["name"], lane)).fetchone()[0]
        c.execute("UPDATE tasks SET state = 'included', reason = NULL, park_kind = NULL, included_commit = ?, included_position = ? WHERE batch = ? AND name = ?", (commit, position, b["name"], task))
        c.execute("UPDATE lanes SET tip = ? WHERE batch = ? AND name = ?", (commit, b["name"], lane))
        if resolution is not None:
            c.execute("INSERT INTO resolutions(batch, lane, task, head, review, lane_parent, ref) VALUES (?, ?, ?, ?, ?, ?, ?)", (b["name"], lane, task, *resolution))


def assemble(ctx, batch, lane, owner=None, seconds=600):
    owner = owner or default_owner()
    c = ctx.store.connection
    b = load_batch(c, batch)
    if load_lane(c, batch, lane)["frozen"]:
        raise Failure("blocked", "frozen", f"lane {lane!r} is frozen; further assembly is refused")
    lease = lane_lease(ctx, batch, lane, owner, seconds)
    result = {"batch": batch, "lane": lane, "included": [], "parked": [], "waiting": []}
    try:
        ctx.hook("lane-lease", {"batch": batch, "lane": lane})
        lane_row = load_lane(c, batch, lane)
        if lane_row["frozen"]:
            raise Failure("blocked", "frozen", f"lane {lane!r} is frozen; further assembly is refused")
        ref, current = lane_tip(ctx, b, lane_row)
        tip = lane_row["tip"]
        mirror = Path(b["mirror"])
        for name in [row["name"] for row in c.execute("SELECT name FROM tasks WHERE batch = ? AND lane = ? ORDER BY seq", (batch, lane))]:
            t = load_task(c, batch, name)
            if t["state"] in ("included", "staged"):
                continue
            if t["state"] == "assigned":
                result["waiting"].append(name)
                continue
            if t["state"] == "parked" and t["park_kind"] == "handoff":
                result["parked"].append({"task": name, "kind": "handoff", "reason": t["reason"]})
                continue
            reasons = []
            for row in dependencies(c, batch, name):
                if row["declared"] is None or row["head"] != row["declared"]:
                    reasons.append(f"prerequisite {row['prerequisite']!r} head no longer matches the handed-off prerequisite SHA")
                elif row["lane"] == lane and row["state"] not in ("included", "staged"):
                    reasons.append(f"prerequisite {row['prerequisite']!r} is not yet included in lane {lane!r}")
                elif row["lane"] != lane and row["state"] != "staged":
                    reasons.append(f"prerequisite {row['prerequisite']!r} in lane {row['lane']!r} is not yet staged into {b['staging']!r}")
            hidden = undeclared(c, b, t["head"], declared(c, batch, name) | {name})
            if hidden:
                reasons.append(undeclared_reason(hidden))
            if reasons:
                park(ctx, lease, batch, name, "dependency", "; ".join(reasons))
                result["parked"].append({"task": name, "kind": "dependency", "reason": "; ".join(reasons), **({"undeclared": hidden} if hidden else {})})
                continue
            hits = excluded_paths(b, b["base"], t["head"])
            if hits:
                reason = "touches excluded paths: " + ", ".join(hits)
                park(ctx, lease, batch, name, "excluded", reason)
                result["parked"].append({"task": name, "kind": "excluded", "reason": reason, "paths": hits})
                continue
            tree, conflicts = gitops.merge(mirror, tip, t["head"])
            if conflicts:
                reason = "merge conflict with lane tip in: " + ", ".join(conflicts)
                park(ctx, lease, batch, name, "conflict", reason)
                result["parked"].append({"task": name, "kind": "conflict", "reason": reason, "paths": conflicts, "lane_tip": tip})
                continue
            commit = gitops.commit_tree(mirror, tree, (tip, t["head"]), f"starlings: include {name} in {batch}/{lane}\n\nTask-Head: {t['head']}\nReview: {t['review']}\n", b["created"])
            include(ctx, lease, b, lane, name, ref, current, commit)
            result["included"].append({"task": name, "head": t["head"], "commit": commit})
            tip = current = commit
        result["tip"] = tip
        result["ref"] = ref
    except LeaseLost as exc:
        raise Failure("stale", "lease-lost", str(exc)) from exc
    finally:
        release(ctx, lease, result)
    return ("parked" if result["parked"] else "ok"), result


def resolve(ctx, batch, lane, task, head, review, owner=None, seconds=600):
    owner = owner or default_owner()
    check_object(head, "--head")
    c = ctx.store.connection
    b = load_batch(c, batch)
    mirror = Path(b["mirror"])
    t = load_task(c, batch, task)
    if t["lane"] != lane:
        raise invalid(f"task {task!r} is not in lane {lane!r}")
    existing = c.execute("SELECT * FROM resolutions WHERE batch = ? AND lane = ? AND task = ?", (batch, lane, task)).fetchone()
    if existing is not None:
        if existing["head"] == head and existing["review"] == review:
            return "ok", {**task_view(ctx, batch, task, repeated=True), "resolution": dict(existing)}
        raise Failure("conflict", "conflict", f"task {task!r} already has a recorded resolution {existing['head']}")
    if t["state"] != "parked" or t["park_kind"] != "conflict":
        raise invalid(f"task {task!r} is not parked on a lane conflict")
    if load_lane(c, batch, lane)["frozen"]:
        raise Failure("blocked", "frozen", f"lane {lane!r} is frozen")
    if gitops.resolve(mirror, head) != head:
        raise invalid(f"--head {head} is not a commit in the coordinator object store {mirror}")
    if review is None:
        reason = f"merge conflict awaiting a reviewed resolution; unreviewed resolution {head} was rejected"
        with ctx.store.transaction() as tx:
            tx.execute("UPDATE tasks SET reason = ? WHERE batch = ? AND name = ?", (reason, batch, task))
        return "parked", {**task_view(ctx, batch, task), "rejected_resolution": head}
    check_text(review, "--review")
    lease = lane_lease(ctx, batch, lane, owner, seconds)
    result = {}
    try:
        lane_row = load_lane(c, batch, lane)
        if lane_row["frozen"]:
            raise Failure("blocked", "frozen", f"lane {lane!r} is frozen")
        ref, current = lane_tip(ctx, b, lane_row)
        tip = lane_row["tip"]
        if not gitops.is_ancestor(mirror, tip, head) or not gitops.is_ancestor(mirror, t["head"], head):
            raise invalid(f"resolution {head} must have the current lane tip {tip} and task head {t['head']} as ancestors")
        hits = excluded_paths(b, tip, head)
        if hits:
            reason = f"merge conflict awaiting a reviewed resolution; resolution {head} was refused because it touches excluded paths: " + ", ".join(hits)
            park(ctx, lease, batch, task, "conflict", reason)
            result.update(task_view(ctx, batch, task))
            result.update({"kind": "excluded", "paths": hits, "rejected_resolution": head})
            return "parked", result
        hidden = undeclared(c, b, head, declared(c, batch, task) | {task}, (tip,))
        if hidden:
            reason = f"merge conflict awaiting a reviewed resolution; resolution {head} was refused: " + undeclared_reason(hidden)
            park(ctx, lease, batch, task, "conflict", reason)
            result.update(task_view(ctx, batch, task))
            result.update({"kind": "dependency", "undeclared": hidden, "rejected_resolution": head})
            return "parked", result
        resolution_ref = refs(batch)["resolution"](lane, task)
        retained = gitops.ref_value(mirror, resolution_ref)
        if retained != head and not gitops.update_ref(mirror, resolution_ref, head, retained or gitops.zero(mirror), f"starlings: resolution {batch}/{lane}/{task}"):
            raise Failure("stale", "stale", f"{resolution_ref} moved concurrently")
        include(ctx, lease, b, lane, task, ref, current, head, (head, review, tip, resolution_ref))
        result.update(task_view(ctx, batch, task))
        result["resolution"] = dict(c.execute("SELECT * FROM resolutions WHERE batch = ? AND lane = ? AND task = ?", (batch, lane, task)).fetchone())
    except LeaseLost as exc:
        raise Failure("stale", "lease-lost", str(exc)) from exc
    finally:
        release(ctx, lease, result)
    return "ok", result


def left_out(c, batch, lane):
    return [{"task": row["name"], "state": row["state"], "reason": row["reason"]} for row in c.execute("SELECT name, state, reason FROM tasks WHERE batch = ? AND lane = ? AND state NOT IN ('included', 'staged') ORDER BY seq", (batch, lane)).fetchall()]


def freeze(ctx, batch, lane, owner=None, seconds=600):
    owner = owner or default_owner()
    c = ctx.store.connection
    b = load_batch(c, batch)
    mirror = Path(b["mirror"])
    lane_row = load_lane(c, batch, lane)
    frozen_ref = refs(batch)["frozen"](lane)
    if lane_row["frozen"]:
        return "ok", {"batch": batch, "lane": lane, "frozen": lane_row["frozen"], "frozen_ref": frozen_ref, "left_out": left_out(c, batch, lane), "repeated": True}
    lease = lane_lease(ctx, batch, lane, owner, seconds)
    result = {"batch": batch, "lane": lane}
    try:
        lane_row = load_lane(c, batch, lane)
        lane_tip(ctx, b, lane_row)
        tip = lane_row["tip"]
        current = gitops.ref_value(mirror, frozen_ref)
        if current != tip and not gitops.update_ref(mirror, frozen_ref, tip, current or gitops.zero(mirror), f"starlings: freeze {batch}/{lane}"):
            raise Failure("stale", "stale", f"{frozen_ref} moved concurrently")
        with ctx.store.transaction() as tx:
            ctx.store.check(tx, lease)
            tx.execute("UPDATE lanes SET frozen = ? WHERE batch = ? AND name = ? AND frozen IS NULL", (tip, batch, lane))
        result.update({"frozen": tip, "frozen_ref": frozen_ref, "left_out": left_out(c, batch, lane), "repeated": False})
    except LeaseLost as exc:
        raise Failure("stale", "lease-lost", str(exc)) from exc
    finally:
        release(ctx, lease, result)
    return "ok", result


def entries(c, batch, lanes):
    result = []
    for lane in lanes:
        for t in c.execute("SELECT * FROM tasks WHERE batch = ? AND lane = ? AND state IN ('included', 'staged') ORDER BY included_position", (batch, lane)).fetchall():
            result.append((t["name"], lane, t["head"], t["review"], tuple((row["prerequisite"], row["declared"]) for row in dependencies(c, batch, t["name"]))))
    resolutions = tuple((row["lane"], row["task"], row["head"], row["review"]) for lane in lanes for row in c.execute("SELECT * FROM resolutions r JOIN tasks t ON t.batch = r.batch AND t.name = r.task WHERE r.batch = ? AND r.lane = ? ORDER BY t.included_position", (batch, lane)).fetchall())
    return tuple(result), resolutions


def build(mirror, old, tips, lanes, batch, date):
    current = old
    tree = gitops.tree_of(mirror, old)
    for lane, tip in zip(lanes, tips):
        tree, conflicts = gitops.merge(mirror, current, tip)
        if conflicts:
            return None, None, {"lane": lane, "paths": conflicts}
        if len(tips) > 1:
            current = gitops.commit_tree(mirror, tree, (current, tip), f"starlings: intermediate {batch} {lane}", date)
    message = f"starlings: stage {batch} lanes {', '.join(lanes)}\n\n" + "".join(f"Lane-{lane}: {tip}\n" for lane, tip in zip(lanes, tips))
    return gitops.commit_tree(mirror, tree, (old, *tips), message, date), tree, None


def finalize(tx, batch, candidate_id, names):
    tx.execute("UPDATE candidates SET state = 'staged' WHERE id = ?", (candidate_id,))
    for name in names:
        tx.execute("UPDATE lanes SET staged_candidate = ? WHERE batch = ? AND name = ?", (candidate_id, batch, name))
        tx.execute("UPDATE tasks SET state = 'staged', candidate = ? WHERE batch = ? AND lane = ? AND state = 'included'", (candidate_id, batch, name))


def candidate_entry(row):
    return {"id": row["id"], "lanes": json.loads(row["lanes"]), "base": row["base"], "commit": row["commit_sha"], "parents": json.loads(row["record"])["parents"], "tree": json.loads(row["record"])["tree"], "ref": row["ref"], "assembly_id": row["assembly_id"], "assembly": json.loads(row["assembly"]), "candidate": json.loads(row["record"])}


def apply_resolution(ctx, b, old, tips, names, resolution, review, result):
    c = ctx.store.connection
    mirror = Path(b["mirror"])
    if gitops.resolve(mirror, resolution) != resolution:
        raise invalid(f"--resolution {resolution} is not a commit in the coordinator object store {mirror}")
    missing = [name for name, tip in zip(names, tips) if not gitops.is_ancestor(mirror, tip, resolution)]
    if missing:
        raise invalid(f"--resolution {resolution} must contain the frozen tips of lanes: {', '.join(missing)}")
    if not gitops.is_ancestor(mirror, old, resolution):
        raise Failure("stale", "stale", f"{b['staging']!r} moved to {old} since the resolution was made; resolve against the current staging head", result)
    own = set()
    for tip in tips:
        own.update(gitops.changed_paths(mirror, old, tip))
    globs = excluded_globs(b)
    hits = [path for path in gitops.changed_paths(mirror, old, resolution) if path not in own and any(fnmatchcase(path, pattern) for pattern in globs)]
    if hits:
        result["paths"] = hits
        raise Failure("blocked", "excluded", "staging resolution touches excluded paths not changed by the lanes: " + ", ".join(hits) + "; nothing was written", result)
    hidden = undeclared(c, b, resolution, set(), (old, *tips))
    if hidden:
        result["undeclared"] = hidden
        raise Failure("blocked", "undeclared-dependency", "staging resolution " + undeclared_reason(hidden) + "; nothing was written", result)
    tree = gitops.tree_of(mirror, resolution)
    message = f"starlings: stage {b['name']} lanes {', '.join(names)} with reviewed resolution\n\nResolution: {resolution}\nReview: {review}\n" + "".join(f"Lane-{lane}: {tip}\n" for lane, tip in zip(names, tips))
    return gitops.commit_tree(mirror, tree, (old, *tips), message, b["created"]), tree


def stage(ctx, batch, lanes, owner=None, seconds=600, resolution=None, review=None):
    owner = owner or default_owner()
    c = ctx.store.connection
    b = load_batch(c, batch)
    mirror, authority = Path(b["mirror"]), Path(b["authority"])
    if type(lanes) is not list or not lanes or len(set(lanes)) != len(lanes):
        raise invalid("--lanes: expected a nonempty comma-separated list without duplicates")
    if review is not None and resolution is None:
        raise invalid("--review applies only together with --resolution")
    if resolution is not None:
        check_object(resolution, "--resolution")
        if review is None:
            raise Failure("blocked", "unreviewed-resolution", f"staging resolution {resolution} has no --review; it was rejected and nothing was written")
        check_text(review, "--review")
    rows = sorted((load_lane(c, batch, lane) for lane in lanes), key=lambda row: row["position"])
    unfrozen = [row["name"] for row in rows if not row["frozen"]]
    if unfrozen:
        raise Failure("blocked", "not-frozen", f"lanes must be frozen before staging: {', '.join(unfrozen)}")
    if b["staging"] == b["release"]:
        raise invalid("staging and release branches must differ")
    policy = json.loads(b["policy"])
    gate = policy["gates"]["staging"]
    target = f"refs/heads/{b['staging']}"
    try:
        lease = ctx.store.acquire(f"target:{authority}:{target}", owner, seconds)
    except LeaseHeld as exc:
        raise Failure("blocked", "lease-held", str(exc)) from exc
    result = {"batch": batch, "mode": gate["mode"], "scope": gate["scope"], "target": target, "authority": str(authority), "candidates": [], "lease_taken_over": lease["taken_over"]}
    status = "ok"
    try:
        ctx.hook("target-lease", {"batch": batch, "lanes": lanes})
        rows = sorted((load_lane(c, batch, lane) for lane in lanes), key=lambda row: row["position"])
        pending = [row for row in rows if not row["staged_candidate"]]
        result["already_staged"] = [{"lane": row["name"], "candidate": row["staged_candidate"]} for row in rows if row["staged_candidate"]]
        if resolution is not None and gate["mode"] == "each-lane" and (len(rows) != 1 or len(pending) != 1):
            raise invalid("in each-lane mode --resolution requires --lanes to name exactly one lane that is not yet staged")
        if resolution is not None and not pending:
            raise invalid("--resolution given but every listed lane is already staged")
        rounds = [[row] for row in pending] if gate["mode"] == "each-lane" else ([pending] if pending else [])
        for group in rounds:
            names = [row["name"] for row in group]
            tips = [row["frozen"] for row in group]
            gitops.fetch(mirror, authority, "+refs/heads/*:refs/remotes/authority/*")
            old = gitops.resolve(mirror, f"refs/remotes/authority/{b['staging']}")
            if old is None:
                raise Failure("stale", "stale", f"authority no longer has {target}")
            recorded = c.execute("SELECT * FROM candidates WHERE batch = ? AND lanes = ? AND state = 'prepared' ORDER BY created DESC", (batch, json.dumps(names))).fetchall()
            landed = next((row for row in recorded if gitops.is_ancestor(mirror, row["commit_sha"], old)), None)
            if landed is not None:
                with ctx.store.transaction() as tx:
                    ctx.store.check(tx, lease)
                    finalize(tx, batch, landed["id"], names)
                result["candidates"].append({**candidate_entry(landed), "state": "staged", "reconciled": True})
                continue
            already = [name for name, tip in zip(names, tips) if tip != b["base"] and gitops.is_ancestor(mirror, tip, old)]
            if already:
                raise Failure("blocked", "already-landed", f"frozen tips of lanes {', '.join(already)} are already contained in {target} without a recorded landing; nothing was written", result)
            commit, tree, conflict = build(mirror, old, tips, names, batch, b["created"])
            if conflict and resolution is None:
                result["conflict"] = conflict
                raise Failure("blocked", "conflict", f"lane {conflict['lane']!r} conflicts with {target} in: {', '.join(conflict['paths'])}; nothing was written. Supply a reviewed --resolution containing {target} and the frozen lane tips", result)
            if conflict:
                result["conflict"] = conflict
                commit, tree = apply_resolution(ctx, b, old, tips, names, resolution, review, result)
            elif resolution is not None:
                raise invalid(f"lanes {', '.join(names)} merge cleanly onto {target}; --resolution does not apply")
            candidate = Candidate(b["project"], str(authority), target, old, (old, *tips), tree, commit)
            members, resolutions = entries(c, batch, names)
            if conflict:
                resolutions = (*resolutions, ("@stage", ",".join(names), resolution, review))
            assembly = Assembly(candidate.id, batch, tuple(names), members, resolutions)
            candidate_ref = refs(batch)["candidate"](candidate.id)
            retained = gitops.ref_value(mirror, candidate_ref)
            if retained != commit and not gitops.update_ref(mirror, candidate_ref, commit, retained or gitops.zero(mirror), f"starlings: candidate {candidate.id}"):
                raise Failure("stale", "stale", f"{candidate_ref} moved concurrently")
            entry = {"id": candidate.id, "lanes": names, "base": old, "commit": commit, "parents": list(candidate.parents), "tree": tree, "ref": candidate_ref, "assembly_id": assembly.id, "assembly": assembly.to_dict(), "candidate": candidate.to_dict()}
            if conflict:
                resolution_ref = refs(batch)["stage_resolution"](candidate.id)
                kept = gitops.ref_value(mirror, resolution_ref)
                if kept != resolution and not gitops.update_ref(mirror, resolution_ref, resolution, kept or gitops.zero(mirror), f"starlings: staging resolution {candidate.id}"):
                    raise Failure("stale", "stale", f"{resolution_ref} moved concurrently")
                entry["resolution_ref"] = resolution_ref
            state = "prepared" if gate["scope"] == "none" else "pending"
            with ctx.store.transaction() as tx:
                ctx.store.check(tx, lease)
                tx.execute("INSERT OR IGNORE INTO candidates(id, batch, lanes, base, commit_sha, ref, state, record, assembly_id, assembly, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (candidate.id, batch, json.dumps(names), old, commit, candidate_ref, state, json.dumps(candidate.to_dict(), sort_keys=True), assembly.id, json.dumps(assembly.to_dict(), sort_keys=True), ctx.store.clock()))
                if conflict:
                    tx.execute("INSERT OR IGNORE INTO stage_resolutions(candidate, batch, lanes, head, review, base, ref) VALUES (?, ?, ?, ?, ?, ?, ?)", (candidate.id, batch, ",".join(names), resolution, review, old, entry["resolution_ref"]))
            if gate["scope"] != "none":
                entry["state"] = "pending"
                result["candidates"].append(entry)
                result["waiting_lanes"] = [row["name"] for row in pending if row["name"] not in names]
                result["note"] = f"staging gate scope is {gate['scope']}; the candidate awaits verification (a later milestone) and {target} was not advanced"
                status = "pending"
                break
            ctx.hook("before-write", {"batch": batch, "candidate": candidate.id, "old": old, "commit": commit})
            with ctx.store.transaction() as tx:
                ctx.store.check(tx, lease)
            gitops.fetch(authority, mirror, candidate_ref)
            if not gitops.update_ref(authority, target, commit, old, f"starlings: stage {candidate.id}"):
                with ctx.store.transaction() as tx:
                    tx.execute("UPDATE candidates SET state = 'stale' WHERE id = ? AND state = 'prepared'", (candidate.id,))
                entry["state"] = "stale"
                result["candidates"].append(entry)
                raise Failure("stale", "stale", f"{target} moved after planning (expected {old}); nothing was written", result)
            ctx.hook("after-write", {"batch": batch, "candidate": candidate.id, "old": old, "commit": commit})
            with ctx.store.transaction() as tx:
                finalize(tx, batch, candidate.id, names)
            entry["state"] = "staged"
            result["candidates"].append(entry)
        result["staging_head"] = gitops.resolve(authority, target)
    except LeaseLost as exc:
        result["lease_lost"] = str(exc)
        raise Failure("stale", "lease-lost", f"{exc}; nothing further was written", result) from exc
    finally:
        release(ctx, lease, result)
    return status, result


def status(ctx, batch):
    c = ctx.store.connection
    b = load_batch(c, batch)
    mirror, authority = Path(b["mirror"]), Path(b["authority"])
    names = refs(batch)
    lanes = []
    for row in c.execute("SELECT * FROM lanes WHERE batch = ? ORDER BY position", (batch,)).fetchall():
        lanes.append({"lane": row["name"], "position": row["position"], "tip": row["tip"], "ref": names["lane"](row["name"]), "ref_sha": gitops.ref_value(mirror, names["lane"](row["name"])), "frozen": row["frozen"], "frozen_ref": names["frozen"](row["name"]) if row["frozen"] else None, "staged_candidate": row["staged_candidate"], "left_out": left_out(c, batch, row["name"]) if row["frozen"] else None})
    tasks = [task_view(ctx, batch, row["name"]) for row in c.execute("SELECT name FROM tasks WHERE batch = ? ORDER BY seq", (batch,)).fetchall()]
    for task in tasks:
        lane = next(item for item in lanes if item["lane"] == task["lane"])
        task["reachable_from_lane"] = task["state"] in ("included", "staged") and gitops.is_ancestor(mirror, task["head"], lane["tip"])
    resolutions = [dict(row) for row in c.execute("SELECT r.* FROM resolutions r JOIN tasks t ON t.batch = r.batch AND t.name = r.task WHERE r.batch = ? ORDER BY t.seq", (batch,)).fetchall()]
    stage_resolutions = [dict(row) for row in c.execute("SELECT * FROM stage_resolutions WHERE batch = ? ORDER BY rowid", (batch,)).fetchall()]
    candidates = []
    for row in c.execute("SELECT * FROM candidates WHERE batch = ? ORDER BY created, id", (batch,)).fetchall():
        candidates.append({"id": row["id"], "state": row["state"], "lanes": json.loads(row["lanes"]), "base": row["base"], "commit": row["commit_sha"], "ref": row["ref"], "ref_sha": gitops.ref_value(mirror, row["ref"]), "candidate": json.loads(row["record"]), "assembly_id": row["assembly_id"], "assembly": json.loads(row["assembly"])})
    return "ok", {**batch_summary(b, None), "lanes": lanes, "tasks": tasks, "resolutions": resolutions, "stage_resolutions": stage_resolutions, "candidates": candidates, "authority_heads": {b["staging"]: gitops.resolve(authority, f"refs/heads/{b['staging']}"), b["release"]: gitops.resolve(authority, f"refs/heads/{b['release']}")}}
