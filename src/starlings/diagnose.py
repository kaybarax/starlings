"""Diagnose a failed release gate over retained frozen lane subsets.

``starlings diagnose`` takes a recorded **failed** release receipt and
attributes the failure to a task, a lane or a lane pair. It rebuilds
subset candidates from the batch's frozen lane tips and included task
history — merged onto the release candidate's release parent, never the
landed staging or release state — and re-runs the failed checks through
the runner's admission, environment-scrubbing, timeout and
process-group machinery.

Probe order: the release baseline alone, one candidate per source lane,
a task-level prefix bisection inside every lane that fails on its own
(cap ``ceil(log2(n)) + 2`` candidate runs per lane, boundary task probed
alone within the same budget), and pairwise lane candidates when no
single lane fails. Subset commits are retained only in the coordinator
mirror under ``refs/starlings/<batch>/diagnostics/<commit>`` and every
probe is recorded in the ``diagnostic_runs`` table with its basis. No
receipt is ever produced; nothing in this path writes to the runs,
evidence, receipts or promotions tables, and no diagnostic identifier
can authorize a promotion.
"""

import json
import math
import secrets
from pathlib import Path

from . import batches, gitops, runner
from .batches import Failure, default_owner, invalid, load_batch, refs
from .store import LeaseHeld


def candidate_ref(batch, commit):
    return f"refs/starlings/{batch}/diagnostics/{commit}"


class SubsetRun(runner.Run):
    """One run of the failed checks against a diagnostic subset candidate.

    Reuses the runner's admission, worktree, environment-scrubbing,
    timeout and process-group machinery unchanged. The differences: it
    never reads the ``runs`` table for cancellation (there is no such
    row), and its bookkeeping is collected for the caller instead of the
    ``run_processes`` and ``evidence`` tables — no receipt is signed.
    """

    def __init__(self, ctx, plan_row, run_id, owner, timeout, grace=runner.GRACE):
        super().__init__(ctx, plan_row, run_id, owner, timeout, grace, None)
        self.processes = []

    def cancelled(self):
        return False

    def record_process(self, check, process, job, worktree):
        self.processes.append({"check": check, "pgid": process.pid, "job": str(job), "worktree": str(worktree)})

    def record_evidence(self, check, position, evidence, status, manifest, metrics, reaped):
        self.processes = [row for row in self.processes if row["check"] != check]


class Diagnosis:
    """Ordered subset candidates and check re-runs for one failed receipt."""

    def __init__(self, ctx, b, plan_row, receipt_id, checks, owner):
        self.ctx, self.b, self.c = ctx, b, ctx.store.connection
        self.mirror = Path(b["mirror"])
        self.plan_row = plan_row
        self.plan_record = json.loads(plan_row["record"])
        self.candidate = json.loads(plan_row["candidate"])
        self.baseline = self.candidate["base"]
        self.staging_head = self.candidate["parents"][1]
        self.receipt_id, self.checks, self.owner = receipt_id, checks, owner
        self.timeout = json.loads(plan_row["policy"])["limits"]["timeout_seconds"]
        self.entries = []

    def lane_tasks(self, lane, tip):
        rows = self.c.execute("SELECT name, head, included_commit FROM tasks WHERE batch = ? AND lane = ? AND included_commit IS NOT NULL ORDER BY included_position", (self.b["name"], lane)).fetchall()
        return [row for row in rows if gitops.is_ancestor(self.mirror, row["included_commit"], tip)]

    @staticmethod
    def failed(entry):
        return any(item["status"] != "passed" for item in entry["checks"])

    def update_row(self, entry):
        with self.ctx.store.transaction() as tx:
            tx.execute("UPDATE diagnostic_runs SET basis = ?, commit_sha = ?, ref = ?, conflict = ?, results = ?, processes = ? WHERE id = ?", (json.dumps(entry["basis"], sort_keys=True), entry["commit"], entry["ref"], json.dumps(entry["conflict"], sort_keys=True) if entry["conflict"] else None, json.dumps(entry["checks"], sort_keys=True), json.dumps(entry["processes"], sort_keys=True), entry["id"]))

    def probe(self, basis, tips, labels):
        """Build one subset candidate on the release baseline and re-run the checks."""
        entry = {"position": len(self.entries) + 1, "id": f"diag-{len(self.entries) + 1:02d}-{secrets.token_hex(8)}", "basis": basis, "commit": None, "tree": None, "ref": None, "conflict": None, "checks": [], "processes": []}
        if tips:
            commit, tree, conflict = batches.build(self.mirror, self.baseline, tips, labels, self.b["name"], self.b["created"])
            if conflict:
                entry["conflict"] = {"merged_against": conflict["lane"], "paths": conflict["paths"]}
            else:
                entry["commit"], entry["tree"] = commit, tree
        else:
            entry["commit"], entry["tree"] = self.baseline, gitops.tree_of(self.mirror, self.baseline)
        if entry["conflict"] is None:
            ref = candidate_ref(self.b["name"], entry["commit"])
            retained = gitops.ref_value(self.mirror, ref)
            if retained != entry["commit"] and not gitops.update_ref(self.mirror, ref, entry["commit"], retained or gitops.zero(self.mirror), f"starlings: diagnose {self.b['name']}"):
                raise Failure("stale", "stale", f"diagnostic ref {ref} moved concurrently")
            entry["ref"] = ref
        self.entries.append(entry)
        with self.ctx.store.transaction() as tx:
            tx.execute("INSERT INTO diagnostic_runs(id, batch, plan_id, receipt_id, gate, position, basis, commit_sha, ref, conflict, checks, results, processes, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (entry["id"], self.b["name"], self.plan_row["id"], self.receipt_id, "release", entry["position"], json.dumps(entry["basis"], sort_keys=True), entry["commit"], entry["ref"], json.dumps(entry["conflict"], sort_keys=True) if entry["conflict"] else None, json.dumps(self.checks), "[]", "[]", self.ctx.store.clock()))
        if entry["conflict"] is not None:
            return entry
        shim = {"record": self.plan_row["record"], "policy": self.plan_row["policy"], "candidate": json.dumps({"id": entry["id"], "commit": entry["commit"]}), "batch": self.b["name"]}
        job = SubsetRun(self.ctx, shim, entry["id"], self.owner, self.timeout)
        for position, check in enumerate(self.checks):
            leases, waited = job.admit(check)
            if leases is None:
                break
            reaped = False
            try:
                outcome, reaped = job.execute(check, position)
            finally:
                if reaped:
                    job.release(leases)
                else:
                    entry["unreaped"] = True
            outcome["admission_waited"] = waited
            entry["checks"].append(outcome)
            entry["processes"] = list(job.processes)
            self.update_row(entry)
        return entry

    def task_view(self, row):
        ref = refs(self.b["name"])["task"](row["name"])
        return {"task": row["name"], "head": row["head"], "included_commit": row["included_commit"], "retained_ref": ref, "retained": gitops.ref_value(self.mirror, ref) == row["head"]}

    def bisect(self, lane, tasks):
        """Find the smallest failing prefix of a failing lane's ordered tasks.

        Classical first-bad-prefix bisection: prefixes are probed through
        the retained lane include commits, so the exact lane history
        (including resolutions) is reused rather than replayed. The
        boundary task of the smallest failing prefix is then probed alone
        on the release baseline. The prefix probes plus the boundary
        probe share a budget of ceil(log2(n)) + 2 candidate runs.
        """
        count = len(tasks)
        cap = math.ceil(math.log2(count)) + 2 if count > 1 else 1
        info = {"lane": lane, "tasks": [row["name"] for row in tasks], "cap": cap, "probes": [], "capped": False, "boundary_alone": None}
        lo, hi = 1, count
        while lo < hi and cap > 0:
            mid = (lo + hi) // 2
            prefix = tasks[:mid]
            entry = self.probe({"kind": "lane-prefix", "lane": lane, "tasks": [row["name"] for row in prefix], "included_commit": prefix[-1]["included_commit"]}, [prefix[-1]["included_commit"]], [lane])
            cap -= 1
            failed = self.failed(entry)
            info["probes"].append({"tasks": [row["name"] for row in prefix], "candidate": entry["id"], "failed": failed, "conflict": entry["conflict"] is not None})
            if failed:
                hi = mid
            else:
                lo = mid + 1
        info["capped"] = lo < hi
        boundary = tasks[hi - 1]
        info["smallest_failing_prefix"] = [row["name"] for row in tasks[:hi]]
        info["boundary"] = self.task_view(boundary)
        if cap > 0:
            entry = self.probe({"kind": "lane-task", "lane": lane, "task": boundary["name"], "head": boundary["head"]}, [boundary["head"]], [boundary["name"]])
            info["boundary_alone"] = {"candidate": entry["id"], "failed": self.failed(entry), "conflict": entry["conflict"] is not None}
        return info


def attribute(engine, baseline_entry, lane_entries, bisections, pairs):
    """Reduce the ordered probe results to an attribution summary."""
    checks = ", ".join(engine.checks)
    if engine.failed(baseline_entry):
        return {"kind": "baseline", "lanes": [], "tasks": [], "summary": f"the release head {engine.baseline} alone fails {checks}; the failure does not require this batch's lanes"}
    failing = [(row["name"], tasks) for row, tasks, entry in lane_entries if engine.failed(entry)]
    if len(failing) == 1:
        lane, tasks = failing[0]
        info = bisections[lane]
        alone = info["boundary_alone"]
        boundary = info["boundary"]
        if alone and alone["failed"]:
            return {"kind": "task", "lane": lane, "lanes": [lane], "tasks": [boundary["task"]], "boundary": boundary, "summary": f"task {boundary['task']!r} in lane {lane!r} fails {checks} on its own; its reviewed head {boundary['head']} is retained at {boundary['retained_ref']}"}
        if alone is not None and not alone["failed"]:
            earlier = [name for name in info["smallest_failing_prefix"] if name != boundary["task"]]
            return {"kind": "lane-interaction", "lane": lane, "lanes": [lane], "tasks": info["smallest_failing_prefix"], "boundary": boundary, "summary": f"the failure first appears at task {boundary['task']!r} in lane {lane!r} but that task alone passes; it interacts with the earlier included tasks {earlier}"}
        return {"kind": "lane", "lane": lane, "lanes": [lane], "tasks": info["smallest_failing_prefix"], "summary": f"lane {lane!r} fails alone; bisection reached its cap and bounds the failing prefix at tasks {info['smallest_failing_prefix']}"}
    if len(failing) > 1:
        tasks = [info["boundary"]["task"] for lane, _ in failing for info in (bisections[lane],) if info.get("boundary") and info.get("boundary_alone") and info["boundary_alone"]["failed"]]
        return {"kind": "multiple-lanes", "lanes": [lane for lane, _ in failing], "tasks": tasks, "summary": f"lanes {', '.join(lane for lane, _ in failing)} each fail {checks} alone; per-lane bisections are recorded under 'bisections'"}
    if pairs:
        lanes = ", ".join(f"{a!r} and {b!r}" for a, b in pairs)
        return {"kind": "lane-pair", "lanes": pairs[0], "failing_pairs": pairs, "tasks": [], "summary": f"no lane fails alone; lanes {lanes} fail together — a cross-lane interaction"}
    reasons = []
    if not lane_entries:
        reasons.append("no lane with included tasks is contained in the release candidate's staging parent")
    else:
        reasons.append("every single-lane subset passes")
        if len(lane_entries) < 2:
            reasons.append("only one source lane exists, so no lane pair can be probed")
        else:
            reasons.append("every lane pair passes")
    reasons.append("the defect may need three or more lanes, depend on content outside the batch's lanes, or be nondeterministic")
    return {"kind": "inconclusive", "lanes": [], "tasks": [], "reasons": reasons, "summary": "; ".join(reasons)}


def diagnose(ctx, batch, gate, receipt_id, plan_id=None, checks=None, owner=None, seconds=600):
    owner = owner or default_owner()
    if gate != "release":
        raise invalid("--gate: only the release gate can be diagnosed")
    c = ctx.store.connection
    b = load_batch(c, batch)
    mirror = Path(b["mirror"])
    receipt = c.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    if receipt is None:
        raise invalid(f"unknown receipt {receipt_id!r}")
    if plan_id is not None and receipt["plan_id"] != plan_id:
        raise invalid(f"receipt {receipt_id} belongs to plan {receipt['plan_id']}, not {plan_id}")
    plan_row = c.execute("SELECT * FROM plans WHERE id = ?", (receipt["plan_id"],)).fetchone()
    if plan_row is None or plan_row["batch"] != batch or plan_row["gate"] != "release":
        raise invalid(f"receipt {receipt_id} is not recorded for a release plan of batch {batch!r}")
    outcome = json.loads(receipt["record"])["outcome"]
    if outcome != "failed":
        raise Failure("blocked", "nothing-to-diagnose", f"receipt {receipt_id} records outcome {outcome!r}; diagnose needs a failed release receipt for the batch")
    plan_record = json.loads(plan_row["record"])
    candidate = json.loads(plan_row["candidate"])
    baseline, staging_head = candidate["base"], candidate["parents"][1]
    if gitops.resolve(mirror, baseline) != baseline or gitops.resolve(mirror, staging_head) != staging_head:
        raise Failure("stale", "stale", "the release candidate's parents are no longer in the coordinator mirror")
    failed_checks = {row["check_id"] for row in c.execute("SELECT check_id, status FROM evidence WHERE run_id = ?", (receipt["run_id"],)).fetchall() if row["status"] != "passed"}
    planned = list(plan_record["checks"])
    if checks is None:
        selected = [name for name in planned if name in failed_checks]
    else:
        unknown = [name for name in checks if name not in planned]
        if unknown:
            raise invalid(f"--checks names checks the plan did not run: {', '.join(unknown)}")
        selected = [name for name in planned if name in checks]
    if not selected:
        raise invalid("the receipt has no failed checks to re-run")
    lanes, skipped = [], []
    for row in c.execute("SELECT * FROM lanes WHERE batch = ? AND frozen IS NOT NULL ORDER BY position", (batch,)).fetchall():
        frozen_ref = refs(batch)["frozen"](row["name"])
        if gitops.ref_value(mirror, frozen_ref) != row["frozen"]:
            raise Failure("stale", "stale", f"frozen ref {frozen_ref} no longer equals the recorded frozen tip {row['frozen']}")
        if not gitops.is_ancestor(mirror, row["frozen"], staging_head):
            skipped.append({"lane": row["name"], "frozen_tip": row["frozen"], "reason": "frozen tip is not contained in the release candidate's staging parent"})
            continue
        lanes.append(row)
    try:
        lease = ctx.store.acquire(f"diagnose:{batch}", owner, seconds)
    except LeaseHeld as exc:
        raise Failure("blocked", "lease-held", str(exc)) from exc
    result = {"batch": batch, "gate": gate, "plan_id": plan_row["id"], "receipt_id": receipt_id, "checks": selected, "mirror": str(mirror), "baseline": {"release_head": baseline, "staging_head": staging_head}, "release_candidate": candidate["id"], "target": candidate["target"], "skipped_lanes": skipped}
    try:
        engine = Diagnosis(ctx, b, plan_row, receipt_id, selected, owner)
        taskful = []
        sources = []
        for row in lanes:
            tasks = engine.lane_tasks(row["name"], row["frozen"])
            sources.append({"lane": row["name"], "frozen_tip": row["frozen"], "frozen_ref": refs(batch)["frozen"](row["name"]), "tasks": [item["name"] for item in tasks]})
            if tasks:
                taskful.append((row, tasks))
        result["sources"] = sources
        baseline_entry = engine.probe({"kind": "baseline", "release_head": baseline}, [], [])
        lane_entries, bisections, pairs = [], {}, []
        if not engine.failed(baseline_entry):
            for row, tasks in taskful:
                entry = engine.probe({"kind": "lane", "lane": row["name"], "frozen_tip": row["frozen"]}, [row["frozen"]], [row["name"]])
                lane_entries.append((row, tasks, entry))
            for row, tasks, entry in lane_entries:
                if engine.failed(entry):
                    bisections[row["name"]] = engine.bisect(row["name"], tasks)
            if not bisections:
                for first in range(len(taskful)):
                    for second in range(first + 1, len(taskful)):
                        names = [taskful[first][0]["name"], taskful[second][0]["name"]]
                        entry = engine.probe({"kind": "lane-pair", "lanes": names}, [taskful[first][0]["frozen"], taskful[second][0]["frozen"]], names)
                        if engine.failed(entry):
                            pairs.append(names)
        result["bisections"] = bisections
        result["attribution"] = attribute(engine, baseline_entry, lane_entries, bisections, pairs)
        result["candidates"] = engine.entries
        result["diagnostic_runs"] = [entry["id"] for entry in engine.entries]
        result["note"] = "diagnostic candidates live only at refs/starlings/<batch>/diagnostics/ in the coordinator mirror and results only in the diagnostic_runs table; no receipt is produced and none can authorize a promotion"
    finally:
        batches.release(ctx, lease, result)
    return "ok", result
