"""Disposable end-to-end demo for ``starlings demo``.

Generates the example project, seeds a local bare authority and drives one
small release-only batch (one lane, two tasks) through the public CLI:
assign, reviewed handoffs, assemble, freeze, stage, then plan, run, check
and promote the release gate. Prints a short readable summary in human
mode; ``--json`` returns the same facts as data. Stdlib only; no network.
"""

import json
from pathlib import Path
import time

from .batches import Failure
from .qualification import Driver, ensure_external, git, seed_project, task

BATCH = "demo"
LANE = "work"
TASKS = ("feat-a", "feat-b")


def demo_spec():
    spec = {LANE: []}
    for name in TASKS:
        slug = name.replace("-", "_")
        relative = f"src/starlings_example/features/{slug}.py"
        content = f'"""Feature added by task {name}."""\n\n\ndef feature_{slug}():\n    return "{name}"\n'
        spec[LANE].append(task(name, {relative: content}))
    return spec


def handoffs(driver):
    for name in TASKS:
        item = driver.worktrees[name]
        worktree = Path(item["worktree"])
        for relative, content in item["files"].items():
            path = worktree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        git(worktree, "add", "-A")
        git(worktree, "commit", "--quiet", "-m", f"demo: {name}")
        head = git(worktree, "rev-parse", "HEAD").stdout.strip()
        driver.ok("task", "handoff", BATCH, name, "--head", head, "--review", f"review:{name}")


def run(directory, json_output=False):
    root = ensure_external(Path(directory).expanduser())
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise Failure("conflict", "conflict", f"demo directory must be absent or an empty directory: {root}; nothing was written")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    driver = Driver(root, "release-only", (LANE,), demo_spec(), batch=BATCH)
    if not json_output:
        print(f"starlings demo — release-only preset, lane {LANE!r}, tasks {', '.join(TASKS)}")
        print(f"  working in {root}")
    started = time.monotonic()
    try:
        seed_project(driver)
        driver.trust_and_signer()
        driver.prepare()
        driver.assign_all()
        handoffs(driver)
        driver.assemble_quiet(LANE)
        driver.freeze(LANE)
        code, envelope = driver.stage([LANE])
        if envelope.get("status") != "ok":
            raise RuntimeError(f"stage {LANE} did not complete: exit {code} {json.dumps(envelope)[:1500]}")
        release = driver.release_gate()
        trace = driver.traceability()
    except RuntimeError as exc:
        raise Failure("failed", "demo", str(exc)) from exc
    missing = [row["task"] for row in trace["tasks"] if not row.get("ancestor_of_main")]
    if missing:
        raise Failure("failed", "demo", f"demo traceability check failed: {', '.join(missing)} is not an ancestor of main")
    seconds = round(time.monotonic() - started, 3)
    main_head = trace["authority_heads"]["main"]
    result = {
        "directory": str(root),
        "project": str(driver.project),
        "authority": str(driver.authority),
        "state": str(driver.state),
        "batch": BATCH,
        "preset": driver.preset,
        "lane": LANE,
        "tasks": list(TASKS),
        "plan_id": release["plan_id"],
        "run_id": release["run_id"],
        "receipt_id": release["receipt_id"],
        "outcome": release["outcome"],
        "checks": release["checks"],
        "dev": trace["authority_heads"]["dev"],
        "main": main_head,
        "seconds": seconds,
    }
    if json_output:
        return result
    return "\n".join([
        "  example project and bare authority seeded",
        "  policy trusted; signer ready; batch demo prepared",
        f"  {', '.join(TASKS)} committed and handed off; lane {LANE} assembled, frozen and staged onto dev",
        f"  release gate passed: {len(release['checks'])} checks, receipt valid, main promoted",
        f"  done in {seconds}s — main is now {main_head[:12]}",
        f"  project:   {driver.project}",
        f"  authority: {driver.authority}",
        f"  state:     {driver.state}",
        f"  inspect:   starlings status {BATCH} --state-dir {driver.state} --directory {driver.project}",
    ])
