import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import time

from . import enforcement, gitops
from .adapters import host as adapters_host
from .batches import Failure, check_text, default_owner, finalize, invalid, load_batch, load_lane, refs
from .contracts import Approval, Promotion, Receipt
from .store import LeaseHeld, LeaseLost
from .verifier import assess, consumed_approvals, current_trust, dead_approvals

OPEN = ("intent", "ambiguous")


def target_resource(gate, b, target):
    if gate == "task":
        return f"lane:{b['name']}/{target.rsplit('/', 1)[1]}"
    return f"target:{b['authority']}:{target}"


def acquire(ctx, resource, owner, seconds):
    try:
        return ctx.store.acquire(resource, owner, seconds)
    except LeaseHeld as exc:
        raise Failure("blocked", "lease-held", str(exc)) from exc


def release(ctx, lease, result):
    try:
        ctx.store.release(lease)
        result["lease_released"] = True
    except LeaseLost:
        result["lease_released"] = False


def load_plan(c, plan_id):
    row = c.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    if row is None:
        raise invalid(f"unknown plan {plan_id!r}")
    return row


def repository_for(b, gate):
    return str(Path(b["mirror"]) if gate == "task" else Path(b["authority"]))


def live_approvals(c, plan_id, now):
    return c.execute("SELECT * FROM approvals WHERE plan_id = ? AND revoked_at IS NULL AND expires_at > ? ORDER BY id", (plan_id, now)).fetchall()


def required_approvals(plan_row):
    return json.loads(plan_row["policy"])["gates"][plan_row["gate"]].get("approvals", 0)


def approval_view(row, now, repeated=None):
    result = {"approval_id": row["id"], "plan_id": row["plan_id"], "candidate_id": row["candidate_id"], "by": row["by_identity"], "review": row["review"], "granted_at": row["granted_at"], "expires_at": row["expires_at"], "revoked_at": row["revoked_at"], "live": row["revoked_at"] is None and row["expires_at"] > now, "approval": json.loads(row["record"])}
    if repeated is not None:
        result["repeated"] = repeated
    return result


def approve(ctx, plan_id, by, expires_in, review=None):
    check_text(by, "--by")
    if review is not None:
        check_text(review, "--review")
    if type(expires_in) is not int or not 60 <= expires_in <= 2592000:
        raise invalid("--expires-in: expected an integer number of seconds between 60 and 2592000")
    with ctx.store.transaction() as c:
        plan_row = load_plan(c, plan_id)
        now = ctx.store.clock()
        existing = c.execute("SELECT * FROM approvals WHERE plan_id = ? AND by_identity = ? AND revoked_at IS NULL AND expires_at > ?", (plan_id, by, now)).fetchone()
        if existing is not None:
            if existing["review"] != review:
                raise Failure("conflict", "approval-exists", f"{by!r} already holds live approval {existing['id']} for plan {plan_id} with review {existing['review']!r}; revoke it first")
            return "ok", {**approval_view(existing, now, True), "required": required_approvals(plan_row)}
        record = Approval(plan_id, plan_row["candidate_id"], by, review, now, now + expires_in)
        c.execute("INSERT OR IGNORE INTO approvals(id, plan_id, candidate_id, by_identity, review, granted_at, expires_at, revoked_at, record) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)", (record.id, plan_id, plan_row["candidate_id"], by, review, record.granted_at, record.expires_at, json.dumps(record.to_dict(), sort_keys=True)))
        row = c.execute("SELECT * FROM approvals WHERE id = ?", (record.id,)).fetchone()
        return "ok", {**approval_view(row, now, False), "required": required_approvals(plan_row)}


def revoke(ctx, approval_id, owner=None, seconds=600):
    owner = owner or default_owner()
    c = ctx.store.connection
    row = c.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        raise invalid(f"unknown approval {approval_id!r}")
    if row["revoked_at"] is not None:
        return "ok", approval_view(row, ctx.store.clock(), True)
    plan_row = load_plan(c, row["plan_id"])
    b = load_batch(c, plan_row["batch"])
    lease = acquire(ctx, target_resource(plan_row["gate"], b, plan_row["target"]), owner, seconds)
    result = {}
    try:
        with ctx.store.transaction() as tx:
            ctx.store.check(tx, lease)
            tx.execute("UPDATE approvals SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (ctx.store.clock(), approval_id))
            result.update(approval_view(tx.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone(), ctx.store.clock(), False))
    except LeaseLost as exc:
        raise Failure("stale", "lease-lost", str(exc)) from exc
    finally:
        release(ctx, lease, result)
    return "ok", result


def ensure_unparked(c, repository, target):
    rows = c.execute("SELECT id FROM promotions WHERE repository = ? AND target = ? AND state = 'ambiguous' ORDER BY created", (repository, target)).fetchall()
    if rows:
        ids = [row["id"] for row in rows]
        raise Failure("blocked", "ambiguous-landing", f"{target} has ambiguous promotion outcome(s) {', '.join(ids)}; resolve with starlings recover <batch> --resolve <promotion-id> --as landed|not-applied", {"promotions": ids, "target": target})


def landed_problems(repository, target, new, tree, parents):
    problems = []
    observed = gitops.resolve(Path(repository), target)
    if observed != new:
        problems.append(f"{target} is {observed}, not the promoted commit {new}")
        return observed, problems
    if gitops.tree_of(Path(repository), new) != tree:
        problems.append(f"landed tree of {new} does not match the candidate tree {tree}")
    if list(gitops.parents_of(Path(repository), new)) != list(parents):
        problems.append(f"landed parents of {new} do not match the candidate parents")
    return observed, problems


def complete(ctx, tx, row, observed, resolution=None):
    landed_at = ctx.store.clock()
    record = Promotion(row["plan_id"], row["receipt_id"], tuple(json.loads(row["approvals"])), row["target"], row["old"], row["new"], row["tree"], tuple(json.loads(row["parents"])), landed_at)
    changed = tx.execute("UPDATE promotions SET state = 'landed', observed = ?, resolution = COALESCE(?, resolution), record_id = ?, record = ?, finished = ? WHERE id = ? AND state IN ('intent', 'ambiguous')", (observed, resolution, record.id, json.dumps(record.to_dict(), sort_keys=True), landed_at, row["id"])).rowcount
    if not changed:
        return None
    batch = row["batch"]
    if row["gate"] == "staging":
        candidate = tx.execute("SELECT lanes FROM candidates WHERE id = ?", (row["candidate_id"],)).fetchone()
        finalize(tx, batch, row["candidate_id"], json.loads(candidate["lanes"]))
    elif row["gate"] == "task":
        candidate = tx.execute("SELECT * FROM task_candidates WHERE id = ?", (row["candidate_id"],)).fetchone()
        position = tx.execute("SELECT COALESCE(MAX(included_position), 0) + 1 FROM tasks WHERE batch = ? AND lane = ?", (batch, candidate["lane"])).fetchone()[0]
        tx.execute("UPDATE tasks SET state = 'included', reason = NULL, park_kind = NULL, included_commit = ?, included_position = ? WHERE batch = ? AND name = ?", (row["new"], position, batch, candidate["task"]))
        tx.execute("UPDATE lanes SET tip = ? WHERE batch = ? AND name = ?", (row["new"], batch, candidate["lane"]))
        tx.execute("UPDATE task_candidates SET state = 'included' WHERE id = ?", (row["candidate_id"],))
        if candidate["resolution"]:
            tx.execute("INSERT OR IGNORE INTO resolutions(batch, lane, task, head, review, lane_parent, ref) VALUES (?, ?, ?, ?, ?, ?, ?)", (batch, candidate["lane"], candidate["task"], *json.loads(candidate["resolution"])))
    return record


def mark(ctx, lease, promotion_id, state, observed, resolution=None):
    with ctx.store.transaction() as tx:
        if lease is not None:
            ctx.store.check(tx, lease)
        return tx.execute("UPDATE promotions SET state = ?, observed = ?, resolution = COALESCE(?, resolution), finished = ? WHERE id = ? AND state IN ('intent', 'ambiguous')", (state, observed, resolution, ctx.store.clock(), promotion_id)).rowcount


def reconcile_target(ctx, lease, repository, target):
    actions = []
    for row in ctx.store.connection.execute("SELECT * FROM promotions WHERE repository = ? AND target = ? AND state = 'intent' ORDER BY created", (repository, target)).fetchall():
        observed, problems = landed_problems(repository, target, row["new"], row["tree"], json.loads(row["parents"]))
        action = {"kind": "promotion", "promotion_id": row["id"], "plan_id": row["plan_id"], "receipt_id": row["receipt_id"], "target": target, "old": row["old"], "new": row["new"], "observed": observed, "recovered": True}
        if observed == row["new"] and not problems:
            with ctx.store.transaction() as tx:
                ctx.store.check(tx, lease)
                record = complete(ctx, tx, row, observed)
            action.update({"state": "landed", "promotion": record.to_dict() if record else None})
            if record is not None and row["gate"] == "staging":
                action.update(disposition_view(ctx, row["batch"], row["candidate_id"], row["new"]))
        elif observed == row["old"]:
            mark(ctx, lease, row["id"], "not-applied", observed)
            action["state"] = "not-applied"
        else:
            mark(ctx, lease, row["id"], "ambiguous", observed)
            action.update({"state": "ambiguous", "reasons": problems})
        actions.append(action)
    return actions


def disposition_view(ctx, batch, candidate_id, commit):
    try:
        c = ctx.store.connection
        b = load_batch(c, batch)
        lanes = json.loads(c.execute("SELECT lanes FROM candidates WHERE id = ?", (candidate_id,)).fetchone()["lanes"])
        return {"dispositions": adapters_host.record_dispositions(ctx, b, lanes, commit)}
    except Failure as exc:
        return {"dispositions_error": str(exc)}


def landed_view(row, repeated):
    return {"promotion_id": row["id"], "plan_id": row["plan_id"], "receipt_id": row["receipt_id"], "gate": row["gate"], "target": row["target"], "old": row["old"], "new": row["new"], "state": row["state"], "promotion": json.loads(row["record"]) if row["record"] else None, "repeated": repeated}


def landed_for(c, plan_id, receipt_id):
    return c.execute("SELECT * FROM promotions WHERE plan_id = ? AND receipt_id = ? AND state = 'landed' ORDER BY created LIMIT 1", (plan_id, receipt_id)).fetchone()


def sources(c, b, plan_row, candidate):
    gate = plan_row["gate"]
    mirror = Path(b["mirror"])
    reasons, verify, found = [], [], {}
    if gate == "staging":
        row = c.execute("SELECT * FROM candidates WHERE id = ?", (candidate["id"],)).fetchone()
        if row is None or row["state"] != "pending":
            reasons.append(f"staging candidate {candidate['id']} is {row['state'] if row else 'unknown'}, not pending")
            return reasons, verify, found
        for lane, tip in zip(json.loads(row["lanes"]), candidate["parents"][1:]):
            lane_row = load_lane(c, b["name"], lane)
            found[f"lane:{lane}"] = tip
            if lane_row["frozen"] != tip or gitops.ref_value(mirror, refs(b["name"])["frozen"](lane)) != tip:
                reasons.append(f"lane {lane!r} frozen tip is no longer {tip}")
            if lane_row["staged_candidate"]:
                reasons.append(f"lane {lane!r} is already staged by {lane_row['staged_candidate']}")
    elif gate == "release":
        staging = "refs/heads/" + json.loads(plan_row["policy"])["staging"]
        parent = candidate["parents"][1]
        head = gitops.resolve(Path(b["authority"]), staging)
        found[staging] = parent
        verify.append((staging, parent))
        if head != parent:
            reasons.append(f"{staging} in the authority is {head}, not the release candidate's staging parent {parent}")
    else:
        row = c.execute("SELECT * FROM task_candidates WHERE id = ?", (candidate["id"],)).fetchone()
        if row is None or row["state"] != "pending":
            reasons.append(f"task candidate {candidate['id']} is {row['state'] if row else 'unknown'}, not pending")
            return reasons, verify, found
        task = c.execute("SELECT * FROM tasks WHERE batch = ? AND name = ?", (b["name"], row["task"])).fetchone()
        head = json.loads(row["assembly"])["entries"][0][2]
        task_ref = refs(b["name"])["task"](row["task"])
        lane_ref = candidate["target"]
        found.update({lane_ref: candidate["base"], task_ref: head})
        verify.append((task_ref, head))
        if task["state"] != "pending" or task["head"] != head:
            reasons.append(f"task {row['task']!r} is {task['state']} with head {task['head']}, not pending with {head}")
        if load_lane(c, b["name"], row["lane"])["tip"] != candidate["base"] or gitops.ref_value(mirror, lane_ref) != candidate["base"]:
            reasons.append(f"{lane_ref} no longer equals the candidate base {candidate['base']}")
        if gitops.ref_value(mirror, task_ref) != head:
            reasons.append(f"{task_ref} no longer equals the reviewed head {head}")
    return reasons, verify, found


def promote(ctx, plan_id, receipt_id, owner=None, seconds=600, allow_unprotected=False):
    owner = owner or default_owner()
    c = ctx.store.connection
    plan_row = load_plan(c, plan_id)
    receipt = c.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    if receipt is None:
        raise invalid(f"unknown receipt {receipt_id!r}")
    if receipt["plan_id"] != plan_id:
        raise invalid(f"receipt {receipt_id} belongs to plan {receipt['plan_id']}, not {plan_id}")
    b = load_batch(c, plan_row["batch"])
    gate, target = plan_row["gate"], plan_row["target"]
    candidate = json.loads(plan_row["candidate"])
    repository = repository_for(b, gate)
    landed = landed_for(c, plan_id, receipt_id)
    if landed is not None:
        return "ok", landed_view(landed, True)
    hooks = enforcement.hook_guard(c, repository, allow_unprotected)
    lease = acquire(ctx, target_resource(gate, b, target), owner, seconds)
    result = {"plan_id": plan_id, "receipt_id": receipt_id, "batch": b["name"], "gate": gate, "target": target, "repository": repository, "lease_taken_over": lease["taken_over"]}
    info = {"plan": plan_id, "receipt": receipt_id, "target": target, "repository": repository, "old": candidate["base"], "new": candidate["commit"]}
    try:
        ctx.hook("target-lease", info)
        result["recovery"] = reconcile_target(ctx, lease, repository, target)
        landed = landed_for(c, plan_id, receipt_id)
        if landed is not None:
            result.update(landed_view(landed, True))
            return "ok", result
        ensure_unparked(c, repository, target)
        status, checked = assess(ctx, receipt_id)
        result["check"] = {key: checked.get(key) for key in ("status", "outcome", "reasons", "authority_head", "base", "policy_digest", "trusted_digest")}
        if status == "invalid":
            raise Failure("invalid", "invalid-receipt", "receipt is invalid: " + "; ".join(checked["reasons"]) + "; nothing was written", result)
        if status == "failed":
            raise Failure("failed", "receipt-failed", f"receipt {receipt_id} records a failed outcome and can never promote; nothing was written", result)
        if status == "stale":
            raise Failure("stale", "stale-receipt", "receipt is stale: " + "; ".join(checked["reasons"]) + "; nothing was written", result)
        required = required_approvals(plan_row)
        approvals = [row["id"] for row in live_approvals(c, plan_id, ctx.store.clock())]
        consumed = consumed_approvals(ctx, Receipt.from_dict(json.loads(receipt["record"])))
        result["approvals"] = {"required": required, "live": approvals, "consumed": [item["id"] for item in consumed]}
        if len(approvals) < required:
            raise Failure("blocked", "approval-required", f"the {gate} gate requires {required} live approval(s) for plan {plan_id}; {len(approvals)} found; nothing was written", result)
        reasons, verify, found = sources(c, b, plan_row, candidate)
        result["sources"] = found
        if reasons:
            raise Failure("stale", "stale-source", "; ".join(reasons) + "; nothing was written", result)
        promotion_id = "promotion-" + secrets.token_hex(16)
        old, new = candidate["base"], candidate["commit"]
        with ctx.store.transaction() as tx:
            ctx.store.check(tx, lease)
            trusted = current_trust(tx, plan_row["authority"])
            if trusted is None or trusted["digest"] != json.loads(plan_row["record"])["policy_digest"]:
                raise Failure("stale", "stale-receipt", "the plan's policy is no longer trusted; nothing was written", result)
            approvals = [row["id"] for row in live_approvals(tx, plan_id, ctx.store.clock())]
            result["approvals"]["live"] = approvals
            gone = dead_approvals(tx, plan_id, consumed, ctx.store.clock())
            if gone:
                raise Failure("stale", "stale-receipt", f"approval(s) {', '.join(item['id'] for item in gone)} that the receipt's checks were shown are no longer live; nothing was written", result)
            if len(approvals) < required:
                raise Failure("blocked", "approval-required", f"the {gate} gate requires {required} live approval(s); {len(approvals)} remain; nothing was written", result)
            if tx.execute("SELECT 1 FROM promotions WHERE repository = ? AND target = ? AND state IN ('intent', 'ambiguous')", (repository, target)).fetchone():
                raise Failure("blocked", "ambiguous-landing", f"{target} has an unresolved promotion; run starlings recover", result)
            tx.execute("INSERT INTO promotions(id, batch, gate, plan_id, receipt_id, candidate_id, approvals, authority, repository, target, old, new, tree, parents, sources, state, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'intent', ?)", (promotion_id, b["name"], gate, plan_id, receipt_id, candidate["id"], json.dumps(approvals), b["authority"], repository, target, old, new, candidate["tree"], json.dumps(candidate["parents"]), json.dumps(found, sort_keys=True), ctx.store.clock()))
        result["promotion_id"] = promotion_id
        info["promotion"] = promotion_id
        ctx.hook("before-write", info)
        if gate != "task":
            gitops.fetch(Path(repository), Path(b["mirror"]), refs(b["name"])["candidate"](candidate["id"]))
        with ctx.store.transaction() as tx:
            ctx.store.check(tx, lease)
        applied, detail = gitops.ref_transaction(Path(repository), verify, [(target, new, old)], f"starlings: promote {plan_id} {receipt_id}", hooks=hooks)
        if not applied:
            observed = gitops.resolve(Path(repository), target)
            mark(ctx, None, promotion_id, "not-applied", observed)
            if gate == "staging":
                with ctx.store.transaction() as tx:
                    tx.execute("UPDATE candidates SET state = 'stale' WHERE id = ? AND state = 'pending'", (candidate["id"],))
            result["state"] = "not-applied"
            raise Failure("stale", "stale", f"the ref transaction was refused ({detail or 'verify failed'}); nothing was written", result)
        ctx.hook("after-write", info)
        observed, problems = landed_problems(repository, target, new, candidate["tree"], candidate["parents"])
        if problems:
            mark(ctx, None, promotion_id, "ambiguous", observed)
            result.update({"state": "ambiguous", "reasons": problems})
            raise Failure("failed", "ambiguous-landing", "; ".join(problems), result)
        with ctx.store.transaction() as tx:
            row = tx.execute("SELECT * FROM promotions WHERE id = ?", (promotion_id,)).fetchone()
            record = complete(ctx, tx, row, observed)
            final = tx.execute("SELECT state FROM promotions WHERE id = ?", (promotion_id,)).fetchone()["state"]
        if record is None:
            result["state"] = final
            raise Failure("stale", "lease-lost", f"promotion {promotion_id} was already reconciled as {final} by another coordinator; this worker did not finalize it", result)
        result.update({"state": "landed", "landed": {"commit": new, "tree": candidate["tree"], "parents": candidate["parents"]}, "promotion": record.to_dict(), "repeated": False})
        if gate == "staging":
            result.update(disposition_view(ctx, b["name"], candidate["id"], new))
        if gate == "release":
            result["note"] = f"{target} in the authority now records the released commit {new}; deployment is out of scope and requires its own authorization"
    except LeaseLost as exc:
        result["lease_lost"] = str(exc)
        raise Failure("stale", "lease-lost", f"{exc}; nothing further was written", result) from exc
    finally:
        release(ctx, lease, result)
    return "ok", result


def restore(ctx, plan_id, receipt_id, owner=None, seconds=600, allow_unprotected=False):
    """Put a landed release back on a release ref that `authority discard` set aside.

    Only for a promotion already journalled as landed whose commit is kept at
    refs/discarded/<commit> while the target is back at the promotion's old
    value. Everything `promote` rechecks is rechecked under the target lease; the
    write is the same compare-and-swap ref transaction, and no new promotion is
    recorded because it is the recorded one.
    """
    owner = owner or default_owner()
    c = ctx.store.connection
    plan_row = load_plan(c, plan_id)
    if plan_row["gate"] != "release":
        raise invalid("only a release promotion can be restored")
    row = landed_for(c, plan_id, receipt_id)
    if row is None:
        raise invalid(f"plan {plan_id} has no landed promotion with receipt {receipt_id}")
    b = load_batch(c, plan_row["batch"])
    candidate = json.loads(plan_row["candidate"])
    repository, target, old, new = row["repository"], row["target"], row["old"], row["new"]
    hooks = enforcement.hook_guard(c, repository, allow_unprotected)
    lease = acquire(ctx, target_resource("release", b, target), owner, seconds)
    result = {"plan_id": plan_id, "receipt_id": receipt_id, "promotion_id": row["id"], "target": target, "old": old, "new": new}
    try:
        result["recovery"] = reconcile_target(ctx, lease, repository, target)
        ensure_unparked(c, repository, target)
        if gitops.resolve(Path(repository), target) == new:
            result.update({"restored": False, "repeated": True})
            return "ok", result
        if gitops.ref_value(Path(repository), f"refs/discarded/{new}") != new:
            raise Failure("blocked", "not-discarded", f"{new} is not kept at refs/discarded/{new}; only a promotion set aside by starlings authority discard is restored. Nothing was written.", result)
        status, checked = assess(ctx, receipt_id)
        result["check"] = {key: checked.get(key) for key in ("status", "outcome", "reasons", "authority_head", "base")}
        if status != "valid":
            raise Failure("invalid" if status == "invalid" else "failed" if status == "failed" else "stale", f"{status}-receipt" if status != "failed" else "receipt-failed", f"receipt {receipt_id} is {status}: " + "; ".join(checked["reasons"]) + "; nothing was written", result)
        required = required_approvals(plan_row)
        approvals = [item["id"] for item in live_approvals(c, plan_id, ctx.store.clock())]
        if len(approvals) < required:
            raise Failure("blocked", "approval-required", f"the release gate requires {required} live approval(s) for plan {plan_id}; {len(approvals)} found; nothing was written", result)
        reasons, verify, found = sources(c, b, plan_row, candidate)
        if reasons:
            raise Failure("stale", "stale-source", "; ".join(reasons) + "; nothing was written", result)
        with ctx.store.transaction() as tx:
            ctx.store.check(tx, lease)
        applied, detail = gitops.ref_transaction(Path(repository), verify, [(target, new, old)], f"starlings: restore {plan_id} {receipt_id}", hooks=hooks)
        if not applied:
            raise Failure("stale", "stale", f"the ref transaction was refused ({detail or 'verify failed'}); nothing was written", result)
        observed, problems = landed_problems(repository, target, new, candidate["tree"], candidate["parents"])
        if problems:
            raise Failure("failed", "ambiguous-landing", "; ".join(problems), result)
        result.update({"restored": True, "repeated": False})
    except LeaseLost as exc:
        raise Failure("stale", "lease-lost", f"{exc}; nothing further was written", result) from exc
    finally:
        release(ctx, lease, result)
    return "ok", result


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def interrupt_run(ctx, run, mirror, guest):
    action = {"kind": "run", "run_id": run["id"], "plan_id": run["plan_id"], "state": "interrupted", "killed_groups": [], "released_leases": [], "removed_worktrees": []}
    c = ctx.store.connection
    for row in c.execute("SELECT * FROM run_processes WHERE run_id = ?", (run["id"],)).fetchall():
        if guest:
            from .runner import stop_container
            stop_container(f"starlings-{run['id'].removeprefix('run-')}-{row['check_id']}")
        if group_alive(row["pgid"]):
            deadline = time.monotonic() + 10
            while group_alive(row["pgid"]) and time.monotonic() < deadline:
                try:
                    os.killpg(row["pgid"], signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                time.sleep(0.01)
            action["killed_groups"].append(row["pgid"])
        shutil.rmtree(row["job"], ignore_errors=True)
    action["groups_reaped"] = not any(group_alive(row["pgid"]) for row in c.execute("SELECT pgid FROM run_processes WHERE run_id = ?", (run["id"],)).fetchall())
    runs = ctx.root / "worktrees" / "runs" / run["id"]
    if runs.is_dir():
        for worktree in sorted(runs.iterdir()):
            gitops.git(mirror, "worktree", "remove", "--force", str(worktree), ok=(0, 128))
            action["removed_worktrees"].append(str(worktree))
        shutil.rmtree(runs, ignore_errors=True)
    gitops.git(mirror, "worktree", "prune")
    with ctx.store.transaction() as tx:
        action["released_leases"] = [row["resource"] for row in tx.execute("SELECT resource FROM leases WHERE owner = ? ORDER BY resource", (run["id"],)).fetchall()]
        tx.execute("DELETE FROM leases WHERE owner = ?", (run["id"],))
        if action["groups_reaped"]:
            tx.execute("DELETE FROM run_processes WHERE run_id = ?", (run["id"],))
        tx.execute("UPDATE runs SET state = 'interrupted', reason = ?, finished = ? WHERE id = ? AND state = 'running'", ("runner lost; interrupted by recovery, no receipt is signed", ctx.store.clock(), run["id"]))
    return action


def resolve_ambiguous(ctx, b, promotion_id, outcome, owner, seconds):
    if outcome not in ("landed", "not-applied"):
        raise invalid("--as: expected landed or not-applied")
    c = ctx.store.connection
    row = c.execute("SELECT * FROM promotions WHERE id = ? AND batch = ?", (promotion_id, b["name"])).fetchone()
    if row is None:
        raise invalid(f"unknown promotion {promotion_id!r} in batch {b['name']!r}")
    if row["state"] != "ambiguous":
        raise invalid(f"promotion {promotion_id} is {row['state']}, not ambiguous")
    lease = acquire(ctx, target_resource(row["gate"], b, row["target"]), owner, seconds)
    repository = Path(row["repository"])
    result = {"batch": b["name"], "promotion_id": promotion_id, "as": outcome, "target": row["target"], "old": row["old"], "new": row["new"]}
    try:
        observed = gitops.resolve(repository, row["target"])
        result["observed"] = observed
        contains = observed is not None and gitops.git(repository, "cat-file", "-e", row["new"] + "^{commit}", ok=(0, 1, 128)).returncode == 0 and gitops.is_ancestor(repository, row["new"], observed)
        if outcome == "landed":
            shape = contains and gitops.tree_of(repository, row["new"]) == row["tree"] and list(gitops.parents_of(repository, row["new"])) == json.loads(row["parents"])
            if not shape:
                raise Failure("blocked", "resolution-refused", f"{row['target']} is {observed}, which does not contain the promoted commit {row['new']}; it cannot be recorded as landed", result)
            with ctx.store.transaction() as tx:
                ctx.store.check(tx, lease)
                record = complete(ctx, tx, row, observed, "operator:landed")
            result.update({"state": "landed", "promotion": record.to_dict() if record else None})
        else:
            if contains:
                raise Failure("blocked", "resolution-refused", f"{row['target']} is {observed}, which contains the promoted commit {row['new']}; it cannot be recorded as not applied", result)
            mark(ctx, lease, promotion_id, "not-applied", observed, "operator:not-applied")
            result["state"] = "not-applied"
    except LeaseLost as exc:
        raise Failure("stale", "lease-lost", str(exc), result) from exc
    finally:
        release(ctx, lease, result)
    return "ok", result


def recover(ctx, batch, resolve=None, outcome=None, owner=None, seconds=600):
    from .batches import reconcile_lane
    owner = owner or default_owner()
    c = ctx.store.connection
    b = load_batch(c, batch)
    if (resolve is None) != (outcome is None):
        raise invalid("--resolve and --as must be given together")
    if resolve is not None:
        return resolve_ambiguous(ctx, b, resolve, outcome, owner, seconds)
    result = {"batch": batch, "actions": [], "skipped": [], "parked": []}
    authority = str(Path(b["authority"]))
    targets = {(authority, f"refs/heads/{b['staging']}", "staging"), (authority, f"refs/heads/{b['release']}", "release")}
    for row in c.execute("SELECT DISTINCT repository, target, gate FROM promotions WHERE batch = ? AND state = 'intent' AND gate != 'task'", (batch,)).fetchall():
        targets.add((row["repository"], row["target"], row["gate"]))
    for repository, target, gate in sorted(targets):
        try:
            lease = ctx.store.acquire(target_resource(gate, b, target), owner, seconds)
        except LeaseHeld as exc:
            result["skipped"].append({"target": target, "reason": str(exc)})
            continue
        held = {}
        try:
            result["actions"].extend(reconcile_target(ctx, lease, repository, target))
        finally:
            release(ctx, lease, held)
    for lane in [row["name"] for row in c.execute("SELECT name FROM lanes WHERE batch = ? ORDER BY position", (batch,)).fetchall()]:
        try:
            lease = ctx.store.acquire(f"lane:{batch}/{lane}", owner, seconds)
        except LeaseHeld as exc:
            result["skipped"].append({"lane": lane, "reason": str(exc)})
            continue
        held = {}
        try:
            result["actions"].extend(reconcile_lane(ctx, lease, b, lane, strict=False))
        finally:
            release(ctx, lease, held)
    mirror = Path(b["mirror"])
    for run in c.execute("SELECT r.*, p.record AS plan_record FROM runs r JOIN plans p ON p.id = r.plan_id WHERE p.batch = ? AND r.state = 'running' ORDER BY r.started", (batch,)).fetchall():
        try:
            lease = ctx.store.acquire(f"run:{run['plan_id']}", owner, 60)
        except LeaseHeld as exc:
            result["skipped"].append({"run_id": run["id"], "reason": str(exc)})
            continue
        held = {}
        try:
            result["actions"].append(interrupt_run(ctx, run, mirror, json.loads(run["plan_record"])["environment"] == "guest"))
        finally:
            release(ctx, lease, held)
    result["parked"] = [{"promotion_id": row["id"], "target": row["target"], "old": row["old"], "new": row["new"], "observed": row["observed"]} for row in c.execute("SELECT * FROM promotions WHERE batch = ? AND state = 'ambiguous' ORDER BY created", (batch,)).fetchall()]
    return ("blocked" if result["parked"] or result["skipped"] else "ok"), result
