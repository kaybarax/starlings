from fnmatch import fnmatchcase
import hashlib
import json
import os
from pathlib import Path

from . import gitops, signer
from .adapters import host as adapters_host
from .batches import Failure, check_object, check_text, default_owner, identity, invalid, load_batch, refs
from .contracts import LANE_REF, Candidate, Evidence, Plan, Receipt
from .policy import PolicyError, parse
from .state import private_dir
from .store import LeaseHeld, LeaseLost, request_digest

TRUST_HISTORY = "SELECT * FROM trusted_policies WHERE authority = ? ORDER BY seq DESC"


def authority_path(value):
    path = Path(value)
    if not path.is_absolute():
        raise invalid("--authority must be an absolute path to a local bare repository")
    if not path.is_dir():
        raise invalid(f"authority does not exist or is not a directory: {path}")
    path = path.resolve(strict=True)
    if not gitops.is_bare(path):
        raise invalid(f"authority is not a bare Git repository: {path}")
    return path


def current_trust(c, authority):
    return c.execute(TRUST_HISTORY, (str(authority),)).fetchone()


def trusted_policy(ctx, digest):
    path = ctx.root / "policy" / f"{digest}.toml"
    try:
        source = path.read_bytes()
    except FileNotFoundError as exc:
        raise Failure("invalid", "untrusted-policy", f"trusted policy blob {path} is missing") from exc
    if hashlib.sha256(source).hexdigest() != digest:
        raise Failure("invalid", "untrusted-policy", f"trusted policy blob {path} does not match its digest")
    return parse(source.decode("utf-8"))


def trust_resources(ctx, authority, policy):
    c = ctx.store.connection
    branches = {policy["staging"], policy["release"]}
    current = current_trust(c, authority)
    if current is not None:
        try:
            previous = trusted_policy(ctx, current["digest"])
            branches.update((previous["staging"], previous["release"]))
        except (Failure, PolicyError, UnicodeError):
            pass
    lanes = set()
    for row in c.execute("SELECT * FROM batches WHERE authority = ?", (str(authority),)).fetchall():
        branches.update((row["staging"], row["release"]))
        if json.loads(row["policy"])["gates"]["task"]["scope"] != "none":
            lanes.update(f"lane:{row['name']}/{lane['name']}" for lane in c.execute("SELECT name FROM lanes WHERE batch = ?", (row["name"],)).fetchall())
    return sorted(f"target:{authority}:refs/heads/{branch}" for branch in branches) + sorted(lanes)


def trust(ctx, authority, commit, review, supersedes=None, owner=None, seconds=600):
    authority = authority_path(authority)
    check_object(commit, "--commit")
    check_text(review, "--review")
    if supersedes is not None and not (len(supersedes) == 64 and all(ch in "0123456789abcdef" for ch in supersedes)):
        raise invalid("--supersedes: expected a SHA-256 policy digest")
    if gitops.resolve(authority, commit) != commit:
        raise invalid(f"--commit {commit} is not a commit in the authority {authority}")
    result = gitops.git(authority, "cat-file", "blob", f"{commit}:starlings.toml", ok=(0, 128), text=False)
    if result.returncode:
        raise invalid(f"commit {commit} has no starlings.toml in the authority")
    source = result.stdout
    try:
        policy = parse(source.decode("utf-8"))
    except (PolicyError, UnicodeError) as exc:
        raise invalid(f"starlings.toml at {commit} is invalid: {exc}") from exc
    digest = hashlib.sha256(source).hexdigest()
    private_dir(ctx.root, "policy")
    blob = ctx.root / "policy" / f"{digest}.toml"
    if not blob.exists():
        temporary = blob.with_name(f".{digest}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(source)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, blob)
    owner = owner or default_owner()
    leases = []
    try:
        for resource in trust_resources(ctx, authority, policy):
            leases.append(ctx.store.acquire(resource, owner, seconds))
    except LeaseHeld as exc:
        for lease in leases:
            ctx.store.release(lease)
        raise Failure("blocked", "lease-held", f"{exc}; policy trust waits for the promotion or assembly holding it; retry") from exc
    try:
        return record_trust(ctx, authority, commit, review, supersedes, digest, blob, policy, leases)
    except LeaseLost as exc:
        raise Failure("stale", "lease-lost", str(exc)) from exc
    finally:
        for lease in leases:
            try:
                ctx.store.release(lease)
            except LeaseLost:
                pass


def record_trust(ctx, authority, commit, review, supersedes, digest, blob, policy, leases):
    with ctx.store.transaction() as c:
        for lease in leases:
            ctx.store.check(c, lease)
        current = current_trust(c, authority)
        if current is not None and current["digest"] == digest and supersedes in (None, digest):
            repeated, row = True, current
        elif current is None and supersedes is not None:
            raise Failure("conflict", "trust-conflict", f"no policy is trusted yet for {authority}; the first trust takes --review only")
        elif current is not None and supersedes != current["digest"]:
            raise Failure("conflict", "trust-conflict", f"the current trusted policy for {authority} is {current['digest']}; a new trust must name it with --supersedes", {"current": current["digest"]})
        else:
            c.execute("INSERT INTO trusted_policies(authority, digest, commit_sha, review, previous_digest, trusted_at) VALUES (?, ?, ?, ?, ?, ?)", (str(authority), digest, commit, review, current["digest"] if current else None, ctx.store.clock()))
            repeated, row = False, current_trust(c, authority)
        history = [dict(item) for item in c.execute(TRUST_HISTORY, (str(authority),)).fetchall()]
    return "ok", {"authority": str(authority), "digest": digest, "commit": row["commit_sha"], "review": row["review"], "previous_digest": row["previous_digest"], "trusted_at": row["trusted_at"], "path": str(blob), "project": policy["project"], "repeated": repeated, "history": history}


def gate_target(policy, gate):
    return "refs/heads/" + (policy["release"] if gate == "release" else policy["staging"])


def select(policy, gate, changed):
    config = policy["gates"][gate]
    checks = list(config["checks"])
    if config["scope"] == "full":
        return "full", checks, [], []
    globs = policy.get("selection", {})
    unmapped_checks = [check for check in checks if check not in globs]
    unmapped = [path for path in changed if not any(fnmatchcase(path, pattern) for check in checks for pattern in globs.get(check, []))]
    selected = [check for check in checks if any(fnmatchcase(path, pattern) for path in changed for pattern in globs.get(check, []))]
    if unmapped or unmapped_checks or not selected:
        return "expanded", checks, unmapped, unmapped_checks
    return "affected", selected, [], []


def release_candidate(ctx, b, policy):
    mirror, authority = Path(b["mirror"]), Path(b["authority"])
    gitops.fetch(mirror, authority, "+refs/heads/*:refs/remotes/authority/*")
    staging = gitops.resolve(mirror, f"refs/remotes/authority/{policy['staging']}")
    release = gitops.resolve(mirror, f"refs/remotes/authority/{policy['release']}")
    if staging is None or release is None:
        raise invalid(f"authority must have both {policy['staging']!r} and {policy['release']!r} branches to plan a release")
    if gitops.is_ancestor(mirror, staging, release):
        raise Failure("blocked", "nothing-to-release", f"{policy['release']} already contains {policy['staging']} head {staging}; there is nothing to release", {"staging_head": staging, "release_head": release})
    tree, conflicts = gitops.merge(mirror, release, staging)
    if conflicts:
        raise Failure("blocked", "release-conflict", f"{policy['staging']} conflicts with {policy['release']} in: {', '.join(conflicts)}; resolve on {policy['staging']}, review and freeze again. Nothing was written.", {"paths": conflicts, "staging_head": staging, "release_head": release})
    message = f"starlings: release {b['name']}\n\nRelease-Base: {release}\nStaging-Head: {staging}\n"
    commit = gitops.commit_tree(mirror, tree, (release, staging), message, b["created"], identity(policy))
    candidate = Candidate(b["project"], str(authority), gate_target(policy, "release"), release, (release, staging), tree, commit)
    ref = refs(b["name"])["candidate"](candidate.id)
    retained = gitops.ref_value(mirror, ref)
    if retained != commit and not gitops.update_ref(mirror, ref, commit, retained or gitops.zero(mirror), f"starlings: release candidate {candidate.id}"):
        raise Failure("stale", "stale", f"{ref} moved concurrently")
    with ctx.store.transaction() as c:
        c.execute("INSERT OR IGNORE INTO release_candidates(id, batch, staging_head, release_head, commit_sha, tree, ref, record, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (candidate.id, b["name"], staging, release, commit, tree, ref, json.dumps(candidate.to_dict(), sort_keys=True), ctx.store.clock()))
    return candidate, ref, {"staging_head": staging, "release_head": release}


def staging_candidate(c, b, candidate_id):
    if candidate_id is not None:
        row = c.execute("SELECT * FROM candidates WHERE id = ? AND batch = ?", (candidate_id, b["name"])).fetchone()
        if row is None:
            raise invalid(f"unknown staging candidate {candidate_id!r} in batch {b['name']!r}")
    else:
        row = c.execute("SELECT * FROM candidates WHERE batch = ? AND state = 'pending' ORDER BY created DESC, id LIMIT 1", (b["name"],)).fetchone()
        if row is None:
            raise invalid(f"batch {b['name']!r} has no pending staging candidate; run starlings stage with an enabled staging gate first")
    if row["state"] != "pending":
        raise Failure("blocked", "not-pending", f"staging candidate {row['id']} is {row['state']}, not pending")
    record = json.loads(row["record"])
    fields = {key: value for key, value in record.items() if key not in ("schema", "kind", "id")}
    fields["parents"] = tuple(fields["parents"])
    candidate = Candidate(**fields)
    if candidate.id != row["id"]:
        raise Failure("invalid", "invalid", f"stored candidate record does not match {row['id']}")
    return candidate, row["ref"], {}


def task_candidate(c, b, candidate_id):
    if candidate_id is not None:
        row = c.execute("SELECT * FROM task_candidates WHERE id = ? AND batch = ?", (candidate_id, b["name"])).fetchone()
        if row is None:
            raise invalid(f"unknown task candidate {candidate_id!r} in batch {b['name']!r}")
    else:
        row = c.execute("SELECT * FROM task_candidates WHERE batch = ? AND state = 'pending' ORDER BY created DESC, id LIMIT 1", (b["name"],)).fetchone()
        if row is None:
            raise invalid(f"batch {b['name']!r} has no pending task candidate; run starlings lane assemble with an enabled task gate first")
    if row["state"] != "pending":
        raise Failure("blocked", "not-pending", f"task candidate {row['id']} is {row['state']}, not pending")
    record = json.loads(row["record"])
    fields = {key: value for key, value in record.items() if key not in ("schema", "kind", "id")}
    fields["parents"] = tuple(fields["parents"])
    candidate = Candidate(**fields)
    if candidate.id != row["id"]:
        raise Failure("invalid", "invalid", f"stored candidate record does not match {row['id']}")
    return candidate, row["ref"], {"lane": row["lane"], "task": row["task"]}


def plan(ctx, batch, gate, candidate_id=None):
    if gate not in ("task", "staging", "release"):
        raise invalid("--gate: expected task, staging or release")
    c = ctx.store.connection
    b = load_batch(c, batch)
    trusted = current_trust(c, b["authority"])
    if trusted is None:
        raise Failure("blocked", "untrusted-policy", f"no trusted policy for authority {b['authority']}; run starlings policy trust first")
    if trusted["digest"] != b["policy_digest"]:
        raise Failure("blocked", "untrusted-policy", f"batch {batch!r} recorded policy digest {b['policy_digest']} but the trusted policy for {b['authority']} is {trusted['digest']}", {"batch_digest": b["policy_digest"], "trusted_digest": trusted["digest"]})
    policy = trusted_policy(ctx, trusted["digest"])
    config = policy["gates"][gate]
    if config["scope"] == "none":
        raise Failure("blocked", "gate-disabled", f"the {gate} gate has scope none in the trusted policy; nothing to plan")
    executor = config["executor"]
    if executor == "github" and gate == "task":
        raise Failure("blocked", "unsupported", "the task gate runs local execution only in v1; executor github is reserved for staging and release gates")
    if executor not in ("local", "github"):
        raise Failure("blocked", "unsupported", f"the {gate} gate uses executor {executor!r}; only local and github executors are accepted")
    if executor == "github" and "github" not in policy:
        raise Failure("blocked", "unsupported", f"the {gate} gate uses executor github but the trusted policy has no [github] table; add it, review and freeze again")
    if gate == "release":
        if candidate_id is not None:
            raise invalid("--candidate applies only to the staging and task gates; release planning constructs its own candidate")
        candidate, ref, frozen = release_candidate(ctx, b, policy)
    elif gate == "task":
        candidate, ref, frozen = task_candidate(c, b, candidate_id)
    else:
        candidate, ref, frozen = staging_candidate(c, b, candidate_id)
    mirror = Path(b["mirror"])
    changed = gitops.changed_paths(mirror, candidate.base, candidate.commit)
    selection, checks, unmapped, unmapped_checks = select(policy, gate, changed)
    inputs = {"candidate": candidate.to_dict(), "policy_digest": trusted["digest"], "gate": gate, "changed": changed, "selection": selection, "checks": {check: policy["checks"][check] for check in checks}, "globs": policy.get("selection", {}), "artifacts": {check: policy.get("artifacts", {}).get(check, []) for check in checks}, "exclusive": {check: policy.get("exclusive", {}).get(check, []) for check in checks}, "limits": policy["limits"], "guest": policy.get("guest") if config["environment"] == "guest" else None, "github": policy.get("github") if executor == "github" else None}
    record = Plan(candidate.id, trusted["digest"], gate, tuple(checks), executor, config["environment"], selection, request_digest(inputs))
    with ctx.store.transaction() as tx:
        repeated = tx.execute("SELECT 1 FROM plans WHERE id = ?", (record.id,)).fetchone() is not None
        if not repeated:
            tx.execute("INSERT INTO plans(id, batch, gate, candidate_id, candidate, record, policy, authority, target, changed, unmapped, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (record.id, batch, gate, candidate.id, json.dumps(candidate.to_dict(), sort_keys=True), json.dumps(record.to_dict(), sort_keys=True), json.dumps(policy, sort_keys=True), b["authority"], candidate.target, json.dumps(changed), json.dumps({"paths": unmapped, "checks": unmapped_checks}), ctx.store.clock()))
    return "ok", {"plan_id": record.id, "plan": record.to_dict(), "batch": batch, "gate": gate, "candidate": candidate.to_dict(), "candidate_ref": ref, "target": candidate.target, "selection": selection, "checks": checks, "changed": changed, "unmapped_paths": unmapped, "unmapped_checks": unmapped_checks, "policy_digest": trusted["digest"], "repeated": repeated, **frozen, **({"note": f"release candidate retained at {ref} in the coordinator mirror only; {candidate.target} in the authority was not written"} if gate == "release" else {})}


def normalize_target(value):
    if value is None:
        return None
    check_text(value, "--target")
    return value if value.startswith("refs/") else "refs/heads/" + value


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_file(root, relative):
    path = Path(root) / relative
    if not relative.startswith(("logs/", "artifacts/")):
        return None
    current = Path(root)
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            return None
    return path if path.is_file() else None


def consumed_approvals(ctx, record):
    """Approvals the run's checks were shown, read from the retained context files the receipt binds."""
    found = {}
    for path, _ in record.artifacts:
        if not (path.startswith("logs/") and path.endswith(".context.json")):
            continue
        retained = safe_file(ctx.root, path)
        if retained is None:
            continue
        try:
            shown = json.loads(retained.read_text(encoding="utf-8")).get("approvals", [])
        except (ValueError, AttributeError, UnicodeError):
            continue
        for item in shown if type(shown) is list else []:
            if type(item) is dict and type(item.get("id")) is str and type(item.get("by")) is str:
                found[item["id"]] = {"id": item["id"], "by": item["by"], "review": item.get("review")}
    return [found[key] for key in sorted(found)]


def dead_approvals(c, plan_id, consumed, now):
    """Consumed approvals with no live grant by the same identity for the same review on this plan."""
    return [item for item in consumed if c.execute("SELECT 1 FROM approvals WHERE plan_id = ? AND by_identity = ? AND review IS ? AND revoked_at IS NULL AND expires_at > ? LIMIT 1", (plan_id, item["by"], item["review"], now)).fetchone() is None]


def hosted_reasons(ctx, plan_row, plan_record, candidate, rows):
    reasons = []
    policy = json.loads(plan_row["policy"])
    config = policy.get("github")
    if not config:
        return ["the plan's policy has no [github] table for an executor-github receipt"]
    lanes = adapters_host.candidate_lanes(ctx.store.connection, plan_row)
    expected_event = adapters_host.submission_event(policy, plan_row["batch"], plan_record["gate"], lanes)
    expected = {"adapter": config.get("adapter", "fake"), "host": config.get("host", "fake.local"), "repo": candidate["repository"], "workflow": config["workflow"], "revision": config["reusable_workflow"], "head_sha": candidate["commit"]}
    bound = []
    for item in rows:
        metrics = json.loads(item["metrics"])
        host = metrics.get("host")
        if type(host) is not dict:
            reasons.append(f"evidence for {item['check_id']} has no host binding")
            continue
        for key, value in expected.items():
            if host.get(key) != value:
                reasons.append(f"evidence for {item['check_id']} binds host {key} {host.get(key)!r}, expected {value!r}")
        if host.get("event") != expected_event:
            reasons.append(f"evidence for {item['check_id']} binds submission event {host.get('event')!r}, expected {expected_event!r}")
        if type(host.get("run_id")) is not str or type(host.get("attempt")) is not int or host.get("attempt", 0) < 1:
            reasons.append(f"evidence for {item['check_id']} has no valid host run id or attempt")
        else:
            bound.append((item, host))
    try:
        adapter = adapters_host.binding(ctx, policy)
        log = adapter.event_log()
    except Failure as exc:
        log, reasons_extra = None, [f"adapter event log is unavailable: {exc.code}"]
        reasons += reasons_extra
    if log is not None:
        for item, host in bound:
            requested = next((event for event in log if event.get("type") == "workflow_run" and event.get("action") == "requested" and event.get("run_id") == host["run_id"] and event.get("attempt") == host["attempt"]), None)
            if requested is None:
                reasons.append(f"adapter event log has no matching requested workflow_run for {item['check_id']} (run {host['run_id']} attempt {host['attempt']})")
            elif any(requested.get(key) != value for key, value in (("name", item["check_id"]), ("repo", host.get("repo")), ("workflow", host.get("workflow")), ("revision", host.get("revision")), ("head_sha", host.get("head_sha")), ("event", host.get("event")))):
                reasons.append(f"adapter event log's requested workflow_run for {item['check_id']} (run {host['run_id']} attempt {host['attempt']}) does not match the bound submission")
            found = next((event for event in log if event.get("type") == "workflow_run" and event.get("action") == "completed" and event.get("run_id") == host["run_id"] and event.get("attempt") == host["attempt"] and event.get("name") == item["check_id"] and event.get("workflow") == host.get("workflow") and event.get("revision") == host.get("revision") and event.get("head_sha") == host.get("head_sha")), None)
            if found is None:
                reasons.append(f"adapter event log has no matching completed workflow_run for {item['check_id']} (run {host['run_id']} attempt {host['attempt']})")
            elif found.get("conclusion") == "success" and item["status"] not in ("passed", "missing-artifact"):
                reasons.append(f"adapter log records conclusion 'success' for {item['check_id']} but evidence status is {item['status']!r}")
            elif found.get("conclusion") in ("failure", "cancelled", "skipped", "timed_out") and item["status"] == "passed":
                reasons.append(f"adapter log records conclusion {found['conclusion']!r} for {item['check_id']} but evidence status is 'passed'")
    return reasons


def assess(ctx, receipt_id, target=None):
    c = ctx.store.connection
    row = c.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    if row is None:
        raise invalid(f"unknown receipt {receipt_id!r}")
    invalid_reasons, stale_reasons = [], []
    try:
        record = Receipt.from_dict(json.loads(row["record"]))
    except (ValueError, TypeError) as exc:
        return "invalid", {"receipt_id": receipt_id, "status": "invalid", "reasons": [f"receipt record is malformed: {exc}"]}
    if record.id != receipt_id:
        invalid_reasons.append("receipt record does not match its ID")
    plan_row = c.execute("SELECT * FROM plans WHERE id = ?", (record.plan_id,)).fetchone()
    if plan_row is None or plan_row["id"] != row["plan_id"]:
        invalid_reasons.append("receipt plan is not recorded")
        return "invalid", {"receipt_id": receipt_id, "status": "invalid", "reasons": invalid_reasons}
    plan_record = json.loads(plan_row["record"])
    candidate = json.loads(plan_row["candidate"])
    current_fingerprint = None
    try:
        current_fingerprint = signer.fingerprint(ctx.root)
    except signer.SignerError as exc:
        stale_reasons.append(str(exc))
    if current_fingerprint is not None and record.verifier["fingerprint"] != current_fingerprint:
        stale_reasons.append(f"receipt was signed by verifier key {record.verifier['fingerprint']}, the current signer is {current_fingerprint}")
    elif current_fingerprint is not None and not signer.verify(ctx.root, record.payload(), row["signature"]):
        invalid_reasons.append("signature does not verify against the allowed signers")
    if (record.candidate_id, record.target, record.base, record.commit, record.tree, record.policy_digest) != (candidate["id"], candidate["target"], candidate["base"], candidate["commit"], candidate["tree"], plan_record["policy_digest"]):
        invalid_reasons.append("receipt does not match its plan's candidate or policy")
    if record.environment != plan_record["environment"] or record.executor != plan_record["executor"]:
        invalid_reasons.append("receipt executor or environment differs from the plan")
    expected = normalize_target(target) or plan_row["target"]
    if record.target != expected:
        invalid_reasons.append(f"receipt target {record.target} is not the expected target {expected}")
    digests = dict(record.artifacts)
    for path, value in record.artifacts:
        found = safe_file(ctx.root, path)
        if found is None:
            invalid_reasons.append(f"retained file is missing: {path}")
        elif file_digest(found) != value:
            invalid_reasons.append(f"retained file was modified: {path}")
    rows = c.execute("SELECT * FROM evidence WHERE run_id = ? ORDER BY position", (row["run_id"],)).fetchall()
    if [item["evidence_id"] for item in rows] != list(record.evidence) or [item["check_id"] for item in rows] != plan_record["checks"]:
        invalid_reasons.append("receipt evidence does not match the retained evidence for every planned check")
    else:
        for item in rows:
            manifest = json.loads(item["files"])
            if any(digests.get(path) != value for path, value in manifest.items()):
                invalid_reasons.append(f"evidence for {item['check_id']} lists files not bound by the receipt")
            elif Evidence(record.plan_id, item["check_id"], item["exit_code"], request_digest(manifest)).id != item["evidence_id"]:
                invalid_reasons.append(f"evidence for {item['check_id']} does not match its content ID")
        if sorted(path for item in rows for path in json.loads(item["files"])) != sorted(digests):
            invalid_reasons.append("receipt binds files that no evidence lists")
    if plan_record["executor"] == "github":
        invalid_reasons += hosted_reasons(ctx, plan_row, plan_record, candidate, rows)
    consumed = consumed_approvals(ctx, record)
    dead = dead_approvals(c, record.plan_id, consumed, ctx.store.clock())
    for item in dead:
        stale_reasons.append(f"approval {item['id']} by {item['by']!r} (review {item['review']!r}), which the checks were shown, is no longer live: it was revoked or has expired")
    trusted = current_trust(c, row["authority"])
    if trusted is None or trusted["digest"] != record.policy_digest:
        stale_reasons.append(f"policy {record.policy_digest} is no longer the trusted policy for {row['authority']}" + (f" (now {trusted['digest']})" if trusted else ""))
    repository = Path(c.execute("SELECT mirror FROM batches WHERE name = ?", (plan_row["batch"],)).fetchone()["mirror"]) if LANE_REF.fullmatch(record.target) else Path(row["authority"])
    head = gitops.resolve(repository, record.target)
    if head != record.base:
        stale_reasons.append(f"{record.target} in the {'coordinator mirror' if LANE_REF.fullmatch(record.target) else 'authority'} is {head}, not the receipt base {record.base}")
    status = "invalid" if invalid_reasons else "failed" if record.outcome != "passed" else "stale" if stale_reasons else "valid"
    reasons = invalid_reasons + stale_reasons + (["receipt records a failed outcome"] if record.outcome != "passed" else [])
    return status, {"receipt_id": receipt_id, "status": status, "outcome": record.outcome, "reasons": reasons, "plan_id": record.plan_id, "run_id": row["run_id"], "candidate_id": record.candidate_id, "target": record.target, "expected_target": expected, "base": record.base, "authority_head": head, "policy_digest": record.policy_digest, "trusted_digest": trusted["digest"] if trusted else None, "verifier": record.verifier, "current_fingerprint": current_fingerprint, "authorizes": status == "valid", "consumed_approvals": [{**item, "live": item not in dead} for item in consumed], "receipt": record.to_dict()}


def check(ctx, receipt_id, target=None):
    status, data = assess(ctx, receipt_id, target)
    return ("ok" if status == "valid" else status), data
