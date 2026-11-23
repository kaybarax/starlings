"""Host adapter contract and shared helpers for executor ``github``.

Every adapter implements the same small surface:

- ``probe()`` — confirm the adapter is usable now (the real adapter
  raises ``hosted-unavailable``; the fake answers locally).
- ``configure(workflow, routing)`` — register the project caller
  workflow and its allowed trigger bases and sources (idempotent).
- ``submit_job(repo, name, workflow, revision, head_sha, event,
  payload)`` — accept one planned check for an exact commit and return
  the binding fields ``adapter``, ``host``, ``repo``, ``event``,
  ``run_id``, ``attempt``, ``workflow``, ``revision``, ``head_sha``.
- ``completion(submission)`` — the latest ``workflow_run`` completion
  event for that run id and attempt, or ``None`` while it is pending.
- ``replay(kind, **fields)`` — record a synthetic host event and report
  which registered workflows it routed to.
- ``pull_request(repo, source)`` / ``close_pull_request(repo, number,
  link)`` — PR lookup and closure used for disposition recording.
- ``event_log()`` — every recorded event, in order.

The fake adapter (``adapters.fake_github``) is the qualified offline
implementation. The real adapter (``adapters.github``) is a stub that
raises ``hosted-unavailable`` everywhere; the approved live rehearsal
must prove real event delivery, billing, protections and runner parity
before it may authorize anything.
"""

import json
from pathlib import Path

from ..batches import Failure, invalid

BINDING_FIELDS = ("adapter", "host", "repo", "event", "run_id", "attempt", "workflow", "revision", "head_sha")


def hosted_unavailable():
    return Failure("blocked", "hosted-unavailable", "the real GitHub adapter is not qualified yet: the approved live rehearsal is pending and nothing is sent anywhere; use adapter = \"fake\" for offline hosted-gate qualification")


def binding(ctx, policy):
    """Resolve the manifest's ``[github]`` table to an adapter, or ``None``."""
    config = policy.get("github")
    if config is None:
        return None
    adapter = config.get("adapter", "fake")
    if adapter == "fake":
        from .fake_github import FakeHost

        return FakeHost(ctx.root, host=config.get("host", "fake.local"))
    if adapter == "github":
        from .github import GitHubHost

        return GitHubHost(config)
    raise invalid(f"github.adapter {adapter!r} is not one of fake, github")


def gate_base(policy, gate):
    return policy["release"] if gate == "release" else policy["staging"]


def routed_gates(policy):
    return [gate for gate in ("staging", "release") if policy["gates"][gate]["scope"] != "none" and policy["gates"][gate].get("executor") == "github"]


def lane_source(batch, lane):
    return f"starlings/{batch}/lanes/{lane}"


def task_source(batch, task):
    return f"starlings/{batch}/{task}"


def pr_source(batch, kind, name):
    return lane_source(batch, name) if kind == "lane" else task_source(batch, name)


def gate_sources(policy, batch, gate, lanes):
    if gate == "release":
        return [policy["staging"]]
    return [lane_source(batch, lane) for lane in lanes]


def registration(policy, batch, lanes):
    """Caller-workflow routing table: trigger bases and allowed sources."""
    branches = sorted({gate_base(policy, gate) for gate in routed_gates(policy)})
    sources = sorted({source for gate in routed_gates(policy) for source in gate_sources(policy, batch, gate, lanes)})
    return {"branches": branches, "sources": sources}


def submission_event(policy, batch, gate, lanes):
    return {"type": "pull_request", "action": "synchronized", "base": gate_base(policy, gate), "source": gate_sources(policy, batch, gate, lanes)}


def candidate_lanes(c, plan_row):
    row = c.execute("SELECT lanes FROM candidates WHERE id = ?", (plan_row["candidate_id"],)).fetchone()
    return json.loads(row["lanes"]) if row is not None else []


def batch_lanes(c, batch):
    return [row["name"] for row in c.execute("SELECT name FROM lanes WHERE batch = ? ORDER BY position", (batch,)).fetchall()]


def included_tasks(c, batch, lane):
    return [row["name"] for row in c.execute("SELECT name FROM tasks WHERE batch = ? AND lane = ? AND state IN ('included', 'staged') ORDER BY seq", (batch, lane)).fetchall()]


def configure(ctx, adapter, policy, batch):
    """Register the caller workflow's routing on the adapter."""
    adapter.configure(policy["github"]["workflow"], registration(policy, batch, batch_lanes(ctx.store.connection, batch)))


def record_dispositions(ctx, b, lanes, commit):
    """Record each lane/task PR's observed disposition when staging lands.

    For every staged lane and every included task whose conventional
    source (``starlings/<batch>/lanes/<lane>`` or ``starlings/<batch>/
    <task>``) names an adapter pull request, the observed state is
    recorded in the ``dispositions`` table: ``merged`` for a natively
    merged PR, ``closed-with-link`` when the adapter closes an open PR
    with a link to the landed commit (or it was already observed closed
    with that link), and ``closed`` for any other closed PR. Returns the
    recorded rows; adapter failures are reported per entry, never
    silently.
    """
    policy = json.loads(b["policy"])
    adapter = binding(ctx, policy)
    if adapter is None:
        return []
    c = ctx.store.connection
    repo = b["project"]
    rows = []
    items = [("lane", lane) for lane in lanes]
    for lane in lanes:
        items += [("task", task) for task in included_tasks(c, b["name"], lane)]
    for kind, name in items:
        source = pr_source(b["name"], kind, name)
        entry = {"kind": kind, "name": name, "repo": repo, "source": source}
        try:
            pr = adapter.pull_request(repo, source)
            if pr is None:
                continue
            if pr["state"] == "open":
                adapter.close_pull_request(repo, pr["number"], link=commit)
                state, link = "closed-with-link", commit
            elif pr["state"] == "merged":
                state, link = "merged", pr.get("link")
            elif pr["state"] == "closed" and pr.get("link") == commit:
                state, link = "closed-with-link", commit
            else:
                state, link = "closed", pr.get("link")
            with ctx.store.transaction() as tx:
                tx.execute("INSERT OR REPLACE INTO dispositions(batch, kind, name, repo, source, pr, state, link, head, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (b["name"], kind, name, repo, source, pr["number"], state, link, pr.get("head"), ctx.store.clock()))
            entry.update({"pr": pr["number"], "state": state, "link": link, "head": pr.get("head")})
        except (Failure, OSError) as exc:
            entry.update({"state": "error", "error": str(exc)})
        rows.append(entry)
    return rows


def probe(root, policy):
    """Read-only doctor section describing the configured adapter."""
    config = (policy or {}).get("github")
    result = {"configured": bool(config)}
    if not config:
        return result
    adapter = config.get("adapter", "fake")
    result.update({"adapter": adapter, "host": config.get("host"), "workflow": config.get("workflow"), "reusable_workflow": config.get("reusable_workflow"), "gates": routed_gates(policy), "offline_qualified": adapter == "fake"})
    if adapter == "fake":
        path = Path(root) / "adapters" / "fake.json"
        result["store"] = str(path)
        result["store_present"] = path.is_file()
        if path.is_file():
            try:
                store = json.loads(path.read_text(encoding="utf-8"))
                result.update({"runs": len(store.get("runs", {})), "pull_requests": len(store.get("pull_requests", {})), "events": len(store.get("events", []))})
            except (OSError, ValueError) as exc:
                result["store_error"] = str(exc)
    else:
        result.update({"available": False, "note": "real GitHub adapter is pending the approved live rehearsal"})
    return result
