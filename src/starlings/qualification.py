"""Qualification driver.

Runs disposable bare authorities and example projects through the public
``starlings`` CLI only. Lane workers execute as separate OS processes
(simulated harnesses) with deterministic start barriers; the coordinator
performs assembly, staging, planning, runs, checks and promotion. Every
preset run and benchmark writes an evidence index (JSON and Markdown) under
the requested output root. Runtime dependencies are stdlib only.
"""

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import tomllib

from . import __version__, gitops
from .policy import PRESETS

SCHEMA = 1
BATCH = "qual"
LANES = ("alpha", "beta", "gamma", "delta")
CHECK_NAMES = ("static", "unit", "service", "journey")
TABLE = {
    "release-only": {"task": (0, 0), "staging": (0, 0), "release": (1, 4)},
    "combined-lanes-release": {"task": (0, 0), "staging": (1, 4), "release": (1, 4)},
    "each-lane-release": {"task": (0, 0), "staging": (4, 16), "release": (1, 4)},
    "every-task-lane-release": {"task": (32, 128), "staging": (4, 16), "release": (1, 4)},
}
FIXED_DATE = "@1767225600 +0000"
POLL = 0.1
WAIT = 300.0


def child_env():
    env = dict(os.environ)
    src = Path(__file__).resolve().parent.parent
    env["PYTHONPATH"] = str(src) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def starlings_argv():
    return [sys.executable, "-m", "starlings"]


def git_env():
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({
        "GIT_AUTHOR_NAME": "starlings-qualification",
        "GIT_AUTHOR_EMAIL": "qualification@localhost",
        "GIT_COMMITTER_NAME": "starlings-qualification",
        "GIT_COMMITTER_EMAIL": "qualification@localhost",
        "GIT_AUTHOR_DATE": FIXED_DATE,
        "GIT_COMMITTER_DATE": FIXED_DATE,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    })
    return env


def git(cwd, *args, check=True):
    argv = ["git", *(["-C", str(cwd)] if cwd is not None else []), "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", "-c", "user.name=starlings-qualification", "-c", "user.email=qualification@localhost", *args]
    proc = subprocess.run(argv, capture_output=True, text=True, env=git_env(), timeout=300, check=False)
    if check and proc.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip()}")
    return proc


def machine_profile():
    def sysctl(name):
        try:
            proc = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=15, check=False)
            return proc.stdout.strip() or None if proc.returncode == 0 else None
        except OSError:
            return None
    return {
        "hardware_model": sysctl("hw.model"),
        "memory_bytes": int(sysctl("hw.memsize") or 0) or None,
        "cpu_count": os.cpu_count(),
        "os": platform.mac_ver()[0] or platform.platform(),
        "platform_detail": platform.platform(),
        "python": platform.python_version(),
        "git": gitops.version(),
        "starlings": __version__,
    }


def feature_file(lane, index, tag=""):
    slug = f"{lane}_{index:02d}{tag.replace('-', '_')}"
    name = f"{lane}-{index:02d}{tag}"
    return f"src/starlings_example/features/{slug}.py", f'"""Feature {name}."""\n\n\ndef feature_{slug}():\n    return "{name}"\n'


API_PY = '''"""Small API surface added by the api-producer task."""

from .core import compute_total

API_VERSION = 1


def api_total(values):
    return compute_total(values)
'''

CLIENT_PY = '''"""Client wrapper added by the api-client task on top of api-producer."""

from .api import api_total


def client_total(values):
    return api_total(values)
'''

GAMMA_CORE = '''"""Gamma core added by the gamma-producer task."""


def gamma_compute(values):
    return sum(values) * 2
'''

GAMMA_CLIENT = '''"""Gamma client added by the gamma-consumer task on top of gamma-producer."""

from . import gamma_core


def gamma_client_total(values):
    return gamma_core.gamma_compute(values)
'''

SHARED_A = '"""Shared configuration edited by reviewed conflict resolutions."""\n\nSHARED_NOTE = "alpha-side"\nSHARED_RETRIES = 3\n'
SHARED_B = '"""Shared configuration edited by reviewed conflict resolutions."""\n\nSHARED_NOTE = "beta-side"\nSHARED_RETRIES = 3\n'
SHARED_RESOLVED = '"""Shared configuration edited by reviewed conflict resolutions."""\n\nSHARED_NOTE = "resolved"\nSHARED_RETRIES = 3\n'
EXCLUDED_TXT = "excluded-path task content\n"

CORE_RENAMED = '''"""Core ledger calculations."""


class Ledger:
    def __init__(self, name="default"):
        self.name = name
        self._entries = []

    def add_entry(self, label, amount):
        if not label:
            raise ValueError("entry label must be nonempty")
        entry = {"label": label, "amount": amount}
        self._entries.append(entry)
        return entry

    def entries(self):
        return list(self._entries)

    def total(self):
        return total(entry["amount"] for entry in self._entries)

    def count(self):
        return len(self._entries)


def total(values):
    return sum(values)
'''

UNIT_RENAMED = '''"""Unit tests for the core ledger, report and shared configuration."""

import unittest

from starlings_example import shared
from starlings_example.core import Ledger, total
from starlings_example.report import render_report


class CoreTests(unittest.TestCase):
    def test_total(self):
        self.assertEqual(total([1, 2, 3]), 6)
        self.assertEqual(total([]), 0)

    def test_ledger_entries_and_total(self):
        ledger = Ledger("test")
        ledger.add_entry("a", 1.5)
        ledger.add_entry("b", 2.5)
        self.assertEqual(ledger.count(), 2)
        self.assertEqual(ledger.total(), 4.0)
        self.assertEqual([entry["label"] for entry in ledger.entries()], ["a", "b"])

    def test_ledger_rejects_empty_label(self):
        with self.assertRaises(ValueError):
            Ledger().add_entry("", 1)

    def test_render_report(self):
        ledger = Ledger("r")
        ledger.add_entry("x", 2)
        report = render_report(ledger)
        self.assertIn("ledger: r", report)
        self.assertIn("total: 2.00", report)

    def test_shared_configuration(self):
        self.assertIsInstance(shared.SHARED_NOTE, str)
        self.assertGreater(shared.SHARED_RETRIES, 0)


if __name__ == "__main__":
    unittest.main()
'''

CORE_ALIAS = CORE_RENAMED + "\n\ncompute_total = total\n"

BENCH_CLIENT = '''"""Bench client consuming the original core API name."""

from .core import compute_total


def client_total(values):
    return compute_total(values)
'''


def task(name, files, after=(), merge_after=()):
    return {"name": name, "files": dict(files), "after": list(after), "merge_after": list(merge_after)}


def plain_spec(lane_names, per_lane, prefix=""):
    spec = {}
    for lane in lane_names:
        spec[lane] = [task(f"{prefix}{lane}-{index:02d}", dict([feature_file(lane, index)])) for index in range(1, per_lane + 1)]
    return spec


def qualification_spec():
    spec = {lane: [] for lane in LANES}
    alpha = spec["alpha"]
    alpha.append(task("alpha-01", dict([feature_file("alpha", 1)])))
    alpha.append(task("api-producer", {"src/starlings_example/api.py": API_PY}))
    alpha.append(task("alpha-02", dict([feature_file("alpha", 2)])))
    alpha.append(task("alpha-conflict-a", {"src/starlings_example/shared.py": SHARED_A}))
    alpha.append(task("alpha-conflict-b", {"src/starlings_example/shared.py": SHARED_B}))
    alpha.append(task("alpha-03", dict([feature_file("alpha", 3)])))
    alpha.append(task("alpha-04", dict([feature_file("alpha", 4)])))
    alpha.append(task("alpha-05", dict([feature_file("alpha", 5)])))
    spec["beta"] = [task(f"beta-{index:02d}", dict([feature_file("beta", index)])) for index in range(1, 9)]
    gamma = spec["gamma"]
    gamma.append(task("gamma-producer", {"src/starlings_example/gamma_core.py": GAMMA_CORE}))
    gamma.append(task("gamma-consumer", {"src/starlings_example/gamma_client.py": GAMMA_CLIENT}, after=("gamma-producer",), merge_after=("gamma-producer",)))
    for index in range(1, 7):
        gamma.append(task(f"gamma-{index:02d}", dict([feature_file("gamma", index)])))
    delta = spec["delta"]
    delta.append(task("delta-01", dict([feature_file("delta", 1)])))
    delta.append(task("api-client", {"src/starlings_example/client.py": CLIENT_PY}, after=("api-producer",), merge_after=("api-producer",)))
    for index in range(2, 7):
        delta.append(task(f"delta-{index:02d}", dict([feature_file("delta", index)])))
    delta.append(task("delta-excluded", {"secrets/deploy.txt": EXCLUDED_TXT}))
    return spec


def bench_spec(kind, tag=""):
    marker = f"-{tag}" if tag else ""

    def feat(lane, index):
        return task(f"{lane}-{index:02d}", dict([feature_file(lane, index, tag=marker)]))

    if kind == "plain":
        return {"alpha": [feat("alpha", i) for i in range(1, 5)], "beta": [feat("beta", i) for i in range(1, 5)]}
    if kind == "conflict":
        return {
            "alpha": [task("alpha-shared", {"src/starlings_example/shared.py": SHARED_A}), feat("alpha", 1), feat("alpha", 2)],
            "beta": [task("beta-shared", {"src/starlings_example/shared.py": SHARED_B}), feat("beta", 1)],
        }
    if kind == "late":
        return {
            "alpha": [task("alpha-rename", {"src/starlings_example/core.py": CORE_RENAMED, "tests/test_unit.py": UNIT_RENAMED}), feat("alpha", 1)],
            "beta": [task("beta-client", {"src/starlings_example/client.py": BENCH_CLIENT}), feat("beta", 1)],
        }
    if kind == "fix":
        return {
            "alpha": [task("alpha-alias", {"src/starlings_example/core.py": CORE_ALIAS})],
            "beta": [feat("beta", 1)],
        }
    raise ValueError(f"unknown bench spec {kind!r}")


class Driver:
    def __init__(self, root, preset_name, lanes, spec, batch=BATCH):
        self.root = Path(root)
        self.preset = preset_name
        self.batch = batch
        self.lanes = list(lanes)
        self.spec = spec
        self.project = self.root / "project"
        self.state = self.root / "state"
        self.authority = self.root / "authority.git"
        self.evidence = self.root / "evidence"
        self.workers_dir = self.evidence / "workers"
        self.calls = []
        self.gates = {"task": [], "staging": [], "release": []}
        self.runs = []
        self.phases = {}
        self.worker_results = {}
        self.policy_digest = None
        self.batch_data = None

    def mark(self, name, started):
        self.phases[name] = round(time.monotonic() - started, 3)

    def cli(self, *args, directory=None, timeout=900):
        argv = [*starlings_argv(), *args, "--state-dir", str(self.state), "--directory", str(directory or self.project), "--json"]
        started = time.monotonic()
        proc = subprocess.run(argv, capture_output=True, text=True, env=child_env(), timeout=timeout, check=False)
        seconds = round(time.monotonic() - started, 3)
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            envelope = {"ok": False, "error": {"code": "driver", "message": proc.stdout.strip() or proc.stderr.strip()}}
        self.calls.append({"argv": " ".join(str(arg) for arg in args), "exit": proc.returncode, "seconds": seconds, "status": envelope.get("status")})
        return proc.returncode, envelope

    def ok(self, *args, statuses=("ok",), **kwargs):
        code, envelope = self.cli(*args, **kwargs)
        status = envelope.get("status", "ok" if envelope.get("ok") else "error")
        if status not in statuses:
            raise RuntimeError(f"starlings {' '.join(args)} failed: exit {code} status {status}: {json.dumps(envelope)[:2000]}")
        return envelope.get("data", envelope)

    def status(self):
        return self.ok("status", self.batch)

    def prepare(self):
        started = time.monotonic()
        data = self.ok("batch", "prepare", self.batch, "--lanes", ",".join(self.lanes), "--authority", str(self.authority))
        self.batch_data = data
        self.policy_digest = data["policy_digest"]
        self.mark("prepare", started)
        return data

    def trust_and_signer(self):
        head = git(self.authority, "rev-parse", "refs/heads/dev").stdout.strip()
        self.ok("policy", "trust", "--authority", str(self.authority), "--commit", head, "--review", "review:policy")
        self.ok("signer", "init")
        return head

    def assign_all(self):
        started = time.monotonic()
        worktrees = {}
        order = []
        for lane in self.lanes:
            for item in self.spec[lane]:
                order.append((lane, item))
        deps = {item["name"]: set(item["after"]) for _, item in order}
        pending = list(order)
        assigned = set()
        while pending:
            progressed = False
            for lane, item in list(pending):
                if deps[item["name"]] <= assigned:
                    data = self.ok("task", "assign", self.batch, item["name"], "--lane", lane, "--owner", f"worker-{lane}", *[arg for dep in item["after"] for arg in ("--after", dep)])
                    worktrees[item["name"]] = {"lane": lane, "worktree": data["worktree"], **item}
                    assigned.add(item["name"])
                    pending.remove((lane, item))
                    progressed = True
            if not progressed:
                raise RuntimeError(f"unsatisfiable --after declarations: {sorted({dep for _, item in pending for dep in deps[item['name']] - assigned})}")
        self.mark("assign", started)
        self.worktrees = worktrees
        return worktrees

    def run_workers(self):
        started = time.monotonic()
        self.workers_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        processes = []
        for lane in self.lanes:
            tasks = [{"name": item["name"], "worktree": self.worktrees[item["name"]]["worktree"], "files": item["files"], "after": item["after"], "merge_after": item["merge_after"]} for item in self.spec[lane]]
            job = {"batch": self.batch, "lane": lane, "state_dir": str(self.state), "directory": str(self.project), "start_file": str(self.workers_dir / f"start-{lane}"), "result_file": str(self.workers_dir / f"{lane}.json"), "tasks": tasks}
            job_path = self.workers_dir / f"{lane}.job.json"
            job_path.write_text(json.dumps(job, sort_keys=True))
            process = subprocess.Popen([sys.executable, "-m", "starlings.qualification", "worker", "--job", str(job_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=child_env())
            processes.append((lane, process))
            (self.workers_dir / f"start-{lane}").write_text("go\n")
        results = {}
        try:
            for lane, process in processes:
                try:
                    out, err = process.communicate(timeout=WAIT * 4)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    raise RuntimeError(f"lane worker {lane} timed out") from None
                result_path = self.workers_dir / f"{lane}.json"
                if process.returncode != 0 or not result_path.exists():
                    raise RuntimeError(f"lane worker {lane} failed: exit {process.returncode}: {err.strip()[-2000:]}")
                results[lane] = json.loads(result_path.read_text())
        finally:
            for lane, process in processes:
                if process.poll() is None:
                    process.kill()
        self.mark("workers", started)
        self.worker_results = results
        return results

    def task_state(self, name):
        for task in self.status()["tasks"]:
            if task["task"] == name:
                return task
        raise RuntimeError(f"unknown task {name}")

    def assemble(self, lane):
        return self.ok("lane", "assemble", self.batch, lane, statuses=("ok", "pending", "parked"))

    def freeze(self, lane):
        return self.ok("lane", "freeze", self.batch, lane)

    def resolve_conflicts(self, lane):
        resolved = []
        for task in self.status()["tasks"]:
            if task["lane"] != lane or task["state"] != "parked" or task["park_kind"] != "conflict":
                continue
            scratch = self.root / f"resolution-work-{task['task']}"
            git(self.batch_data["mirror"], "worktree", "add", "--quiet", "--detach", str(scratch), self.task_tip(lane))
            try:
                git(scratch, "merge", "--no-edit", task["head"], check=False)
                conflicts = git(scratch, "diff", "--name-only", "--diff-filter=U").stdout.split()
                if conflicts != ["src/starlings_example/shared.py"]:
                    raise RuntimeError(f"unexpected resolution conflicts for {task['task']}: {conflicts}")
                (scratch / "src/starlings_example/shared.py").write_text(SHARED_RESOLVED)
                git(scratch, "add", "-A")
                git(scratch, "commit", "--quiet", "--no-edit")
                resolution = git(scratch, "rev-parse", "HEAD").stdout.strip()
            finally:
                git(self.batch_data["mirror"], "worktree", "remove", "--force", str(scratch), check=False)
            data = self.ok("lane", "resolve", self.batch, lane, "--task", task["task"], "--head", resolution, "--review", f"review:resolution-{task['task']}", statuses=("ok", "pending"))
            resolved.append({"task": task["task"], "resolution": resolution, "result": data})
            if "pending" in data:
                self.gate_cycle("task", data["pending"]["candidate_id"])
        return resolved

    def task_tip(self, lane):
        for item in self.status()["lanes"]:
            if item["lane"] == lane:
                return item["tip"]
        raise RuntimeError(f"unknown lane {lane}")

    def assemble_quiet(self, lane):
        started = time.monotonic()
        data = self.assemble(lane)
        resolved = self.resolve_conflicts(lane)
        if resolved:
            data = self.assemble(lane)
        self.mark(f"assemble-{lane}", started)
        return data, resolved

    def stage(self, lanes, resolution=None, review=None):
        args = ["stage", self.batch, "--lanes", ",".join(lanes)]
        if resolution:
            args += ["--resolution", resolution, "--review", review]
        return self.cli(*args)

    def stage_pending(self, lanes):
        code, envelope = self.stage(lanes)
        status = envelope.get("status")
        if status != "pending" or code != 4:
            raise RuntimeError(f"stage {lanes} did not pend: exit {code} {json.dumps(envelope)[:1500]}")
        return envelope["data"]["candidates"][0]["id"]

    def stage_or_resolve(self, lanes):
        code, envelope = self.stage(lanes)
        error = envelope.get("error") or {}
        if envelope.get("status") != "blocked" or error.get("code") != "conflict":
            return code, envelope
        state = self.status()
        lane_rows = {row["lane"]: row for row in state["lanes"]}
        tips = [lane_rows[name]["frozen"] for name in lanes]
        old = state["authority_heads"][self.batch_data["staging"]]
        mirror = Path(self.batch_data["mirror"])
        scratch = self.root / f"stage-resolution-{'-'.join(lanes)}"
        git(mirror, "worktree", "add", "--quiet", "--detach", str(scratch), old)
        try:
            for tip in tips:
                git(scratch, "merge", "--quiet", "--no-edit", tip, check=False)
                unmerged = git(scratch, "diff", "--name-only", "--diff-filter=U").stdout.split()
                for path in unmerged:
                    if path != "src/starlings_example/shared.py":
                        raise RuntimeError(f"unexpected staging conflict paths: {unmerged}")
                    (scratch / path).write_text(SHARED_RESOLVED)
                if unmerged:
                    git(scratch, "add", "-A")
                    git(scratch, "commit", "--quiet", "--no-edit")
            resolution = git(scratch, "rev-parse", "HEAD").stdout.strip()
        finally:
            git(mirror, "worktree", "remove", "--force", str(scratch), check=False)
        return self.stage(lanes, resolution=resolution, review=f"review:stage-{self.batch}-{'-'.join(lanes)}")

    def plan_run(self, gate, candidate=None):
        args = ["plan", self.batch, "--gate", gate]
        if candidate:
            args += ["--candidate", candidate]
        data = self.ok(*args)
        return data

    def gate_cycle(self, gate, candidate=None, expect="passed"):
        started = time.monotonic()
        plan = self.plan_run(gate, candidate)
        plan_id, checks = plan["plan_id"], plan["checks"]
        code, run_env = self.cli("run", plan_id)
        run_data = run_env.get("data", {})
        receipt_id, outcome, run_id = run_data.get("receipt_id"), run_data.get("outcome"), run_data.get("run_id")
        check_record = None
        if receipt_id:
            check_code, check_env = self.cli("check", receipt_id)
            check_data = check_env.get("data") or {}
            check_record = {"exit": check_code, "status": check_env.get("status"), "receipt_status": check_data.get("status"), "authorizes": check_data.get("authorizes")}
        promote_state = None
        if outcome == "passed":
            promoted = self.ok("promote", plan_id, "--receipt", receipt_id)
            promote_state = promoted.get("state")
        record = {"gate": gate, "plan_id": plan_id, "candidate_id": plan["candidate"]["id"], "selection": plan["selection"], "checks": checks, "run_id": run_id, "receipt_id": receipt_id, "outcome": outcome, "check": check_record, "promote": promote_state, "run_checks": run_data.get("checks", []), "seconds": round(time.monotonic() - started, 3)}
        self.gates[gate].append(record)
        self.runs.append(record)
        if expect == "passed" and outcome != "passed":
            raise RuntimeError(f"{gate} gate run {run_id} outcome {outcome}, expected passed")
        return record

    def release_gate(self):
        return self.gate_cycle("release")

    def stored_assemblies(self, batch):
        import sqlite3
        database = self.state / "starlings.db"
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            records = []
            for table in ("candidates", "task_candidates"):
                for row in connection.execute(f"SELECT id, batch, assembly_id, assembly FROM {table} WHERE batch = ?", (batch,)):
                    records.append({"table": table, "id": row["id"], "assembly_id": row["assembly_id"], "assembly": json.loads(row["assembly"])})
            return records
        finally:
            connection.close()

    def traceability(self):
        state = self.status()
        authority_head_dev = git(self.authority, "rev-parse", "refs/heads/dev").stdout.strip()
        authority_head_main = git(self.authority, "rev-parse", "refs/heads/main").stdout.strip()
        assemblies = self.stored_assemblies(self.batch)
        rows = []
        for item in self.spec_tasks():
            name = item["name"]
            found = next((task for task in state["tasks"] if task["task"] == name), None)
            if found is None:
                rows.append({"task": name, "lane": item["lane"], "error": "not in status"})
                continue
            head = found["head"]
            members = []
            for record in assemblies:
                for entry in record["assembly"]["entries"]:
                    if entry[0] == name and entry[2] == head:
                        members.append({"table": record["table"], "id": record["id"], "assembly_id": record["assembly_id"]})
            ancestor_dev = bool(head) and git(self.authority, "merge-base", "--is-ancestor", head, authority_head_dev, check=False).returncode == 0
            ancestor_main = bool(head) and git(self.authority, "merge-base", "--is-ancestor", head, authority_head_main, check=False).returncode == 0
            rows.append({
                "task": name, "lane": item["lane"], "state": found["state"], "park_kind": found["park_kind"], "reason": found["reason"],
                "head": head, "review": found["review"], "retained_ref": found["retained_ref"], "retained_sha": found["retained_sha"], "retained": found["retained"],
                "reachable_from_lane": found.get("reachable_from_lane"), "included_commit": found.get("included_commit"),
                "assembly_records": members, "ancestor_of_dev": ancestor_dev, "ancestor_of_main": ancestor_main,
            })
        return {"authority_heads": {"dev": authority_head_dev, "main": authority_head_main}, "tasks": rows, "state_tasks": state["tasks"], "promotions": state["promotions"], "candidates": state["candidates"], "task_candidates": state["task_candidates"], "resolutions": state["resolutions"], "stage_resolutions": state["stage_resolutions"]}

    def spec_tasks(self):
        return [{"name": item["name"], "lane": lane, **{k: item[k] for k in ("after",)}} for lane in self.lanes for item in self.spec[lane]]

    def evidence_index(self, extra=None):
        gates = {gate: {"executions": len(self.gates[gate]), "check_executions": sum(len(record["checks"]) for record in self.gates[gate]), "runs": self.gates[gate]} for gate in self.gates}
        expected = TABLE.get(self.preset, {})
        trace = self.traceability()
        landed = [row for row in trace["tasks"] if row.get("state") in ("included", "staged")]
        parked = [row for row in trace["tasks"] if row.get("state") == "parked"]
        scopes = {}
        manifest = self.project / "starlings.toml"
        if manifest.is_file():
            policy = tomllib.loads(manifest.read_text())
            scopes = {name: {"scope": gate.get("scope"), "mode": gate.get("mode"), "checks": gate.get("checks")} for name, gate in policy.get("gates", {}).items()}
        index = {
            "schema": SCHEMA, "kind": "starlings-qualification", "preset": self.preset, "batch": self.batch,
            "generated_at": time.time(), "machine": machine_profile(),
            "shape": {"lanes": self.lanes, "tasks_per_lane": {lane: len(self.spec[lane]) for lane in self.lanes}, "tasks": sum(len(self.spec[lane]) for lane in self.lanes), "checks_per_gate": len(CHECK_NAMES), "gate_scopes": scopes},
            "policy_digest": self.policy_digest, "authority": str(self.authority), "state_dir": str(self.state), "project": str(self.project),
            "gates": gates, "expected_gates": expected,
            "gate_match": {gate: (gates[gate]["executions"], gates[gate]["check_executions"]) == expected.get(gate, (None, None)) for gate in gates},
            "traceability": trace, "landed": len(landed), "parked": parked,
            "runs": self.runs, "phases": self.phases, "calls": self.calls,
            "workers": self.worker_results,
        }
        if extra:
            index.update(extra)
        return index

    def write_evidence(self, index, name="index"):
        self.evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.evidence / f"{name}.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
        (self.evidence / f"{name}.md").write_text(markdown(index))
        return index


def markdown(index):
    lines = [f"# Starlings qualification evidence — {index['preset']}", ""]
    machine = index["machine"]
    lines += [f"- Machine: {machine.get('hardware_model')} ({machine.get('cpu_count')} CPUs, {machine.get('memory_bytes')} bytes), {machine.get('os')}", f"- Python {machine.get('python')}, Git {machine.get('git')}, starlings {machine.get('starlings')}", f"- Shape: {index['shape']['tasks']} tasks on lanes {', '.join(index['shape']['lanes'])}, {index['shape']['checks_per_gate']} checks per gate", ""]
    lines += ["| Gate | Executions | Check executions | Expected (gates/checks) |", "|---|---|---|---|"]
    for gate, data in index["gates"].items():
        expected = index["expected_gates"].get(gate)
        lines.append(f"| {gate} | {data['executions']} | {data['check_executions']} | {expected} |" if expected else f"| {gate} | {data['executions']} | {data['check_executions']} | — |")
    lines += ["", f"Landed tasks: {index['landed']}; parked: {len(index['parked'])}", ""]
    for row in index["parked"]:
        lines.append(f"- parked `{row['task']}` ({row.get('park_kind')}): {row.get('reason')}")
    lines += ["", "| Task | Lane | State | Head | In assemblies | Ancestor dev/main |", "|---|---|---|---|---|---|"]
    for row in index["traceability"]["tasks"]:
        lines.append(f"| {row['task']} | {row['lane']} | {row.get('state')} | {(row.get('head') or '')[:12]} | {len(row.get('assembly_records', []))} | {row.get('ancestor_of_dev')}/{row.get('ancestor_of_main')} |")
    lines += ["", "## Runs", "", "| Gate | Plan | Run | Outcome | Checks | Seconds |", "|---|---|---|---|---|---|"]
    for record in index["runs"]:
        lines.append(f"| {record['gate']} | {record['plan_id'][:24]} | {record.get('run_id')} | {record['outcome']} | {len(record['checks'])} | {record['seconds']} |")
    lines += ["", "## Phases", ""]
    for name, seconds in index["phases"].items():
        lines.append(f"- {name}: {seconds}s")
    lines.append("")
    return "\n".join(lines)


def seed_project(driver):
    started = time.monotonic()
    argv = [*starlings_argv(), "examples", "init", str(driver.project), "--preset", driver.preset, "--with-qualification", "--json"]
    proc = subprocess.run(argv, capture_output=True, text=True, env=child_env(), timeout=120, check=False)
    driver.calls.append({"argv": " ".join(argv[2:-1]), "exit": proc.returncode, "seconds": round(time.monotonic() - started, 3)})
    if proc.returncode:
        raise RuntimeError(f"examples init failed: {proc.stderr.strip()}")
    git(driver.project, "init", "--quiet", "-b", "dev", ".")
    git(driver.project, "add", "-A")
    git(driver.project, "commit", "--quiet", "-m", "seed: starlings example project")
    driver.authority.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    git(None, "init", "--quiet", "--bare", str(driver.authority))
    git(driver.project, "push", "--quiet", str(driver.authority), "HEAD:refs/heads/dev", "HEAD:refs/heads/main")
    driver.mark("seed", started)


def ensure_external(root):
    root = Path(root).resolve()
    source = Path(__file__).resolve().parents[2]
    if (source / ".git").exists() and (source / "pyproject.toml").exists() and (root == source or source in root.parents):
        raise ValueError(f"qualification root must be outside the source checkout: {root}")
    return root


def run_preset(root, preset_name, lanes=LANES, per_lane=8, batch=BATCH):
    if preset_name not in PRESETS:
        raise ValueError(f"unknown preset {preset_name!r}")
    root = ensure_external(root)
    spec = qualification_spec() if (list(lanes) == list(LANES) and per_lane == 8) else plain_spec(lanes, per_lane)
    driver = Driver(root, preset_name, lanes, spec, batch)
    for directory in (driver.project, driver.state, driver.evidence, driver.workers_dir):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    started = time.monotonic()
    seed_project(driver)
    driver.trust_and_signer()
    driver.prepare()
    driver.assign_all()
    driver.run_workers()
    if preset_name == "release-only":
        for lane in driver.lanes:
            driver.assemble_quiet(lane)
            driver.freeze(lane)
            code, envelope = driver.stage([lane])
            if envelope.get("status") != "ok":
                raise RuntimeError(f"release-only stage {lane}: {json.dumps(envelope)[:1500]}")
        driver.release_gate()
    elif preset_name == "combined-lanes-release":
        for lane in driver.lanes[:-1]:
            driver.assemble_quiet(lane)
            driver.freeze(lane)
        driver.assemble_quiet(driver.lanes[-1])
        candidate = driver.stage_pending(driver.lanes[:-1])
        driver.gate_cycle("staging", candidate)
        driver.assemble_quiet(driver.lanes[-1])
        driver.freeze(driver.lanes[-1])
        candidate = driver.stage_pending([driver.lanes[-1]])
        driver.gate_cycle("staging", candidate)
        driver.release_gate()
    elif preset_name == "each-lane-release":
        for lane in driver.lanes:
            driver.assemble_quiet(lane)
            driver.freeze(lane)
            candidate = driver.stage_pending([lane])
            driver.gate_cycle("staging", candidate)
        driver.release_gate()
    elif preset_name == "every-task-lane-release":
        for lane in driver.lanes:
            for _ in range(400):
                data = driver.assemble(lane)
                if "pending" in data:
                    driver.gate_cycle("task", data["pending"]["candidate_id"])
                    continue
                conflicts = [task for task in driver.status()["tasks"] if task["lane"] == lane and task["state"] == "parked" and task["park_kind"] == "conflict"]
                if conflicts:
                    driver.resolve_conflicts(lane)
                    continue
                break
            driver.freeze(lane)
            candidate = driver.stage_pending([lane])
            driver.gate_cycle("staging", candidate)
        driver.release_gate()
    driver.mark("orchestrate", started)
    index = driver.evidence_index()
    driver.write_evidence(index)
    return index


def await_head(state_args, batch, name, timeout=WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        proc = subprocess.run([*starlings_argv(), "status", batch, *state_args, "--json"], capture_output=True, text=True, env=child_env(), timeout=120, check=False)
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            envelope = {}
        for task in envelope.get("data", {}).get("tasks", []):
            if task["task"] == name and task.get("head"):
                return task["head"]
        time.sleep(POLL)
    raise RuntimeError(f"timed out waiting for handoff of {name}")


def worker_main(job_path):
    job = json.loads(Path(job_path).read_text())
    batch, lane = job["batch"], job["lane"]
    state_args = ["--state-dir", job["state_dir"], "--directory", job["directory"]]
    deadline = time.monotonic() + WAIT
    while not Path(job["start_file"]).exists():
        if time.monotonic() > deadline:
            raise RuntimeError("start barrier timed out")
        time.sleep(0.02)
    results = []
    for item in job["tasks"]:
        heads = {}
        for dep in list(dict.fromkeys(item["after"] + item["merge_after"])):
            heads[dep] = await_head(state_args, batch, dep)
        worktree = Path(item["worktree"])
        for dep in item["merge_after"]:
            git(worktree, "merge", "--quiet", "--no-edit", heads[dep])
        for relative, content in item["files"].items():
            path = worktree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        git(worktree, "add", "-A")
        git(worktree, "commit", "--quiet", "-m", f"qual: {item['name']}")
        head = git(worktree, "rev-parse", "HEAD").stdout.strip()
        argv = [*starlings_argv(), "task", "handoff", batch, item["name"], "--head", head, "--review", f"review:{item['name']}", *state_args]
        for dep in item["after"]:
            argv += ["--prerequisite", f"{dep}={heads[dep]}"]
        argv.append("--json")
        proc = subprocess.run(argv, capture_output=True, text=True, env=child_env(), timeout=300, check=False)
        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError:
            envelope = {"ok": False, "error": {"message": proc.stderr.strip()}}
        results.append({"task": item["name"], "head": head, "exit": proc.returncode, "status": envelope.get("status"), "state": (envelope.get("data") or {}).get("state")})
        if proc.returncode != 0:
            break
    outcome = {"lane": lane, "ok": all(item["exit"] == 0 for item in results) and len(results) == len(job["tasks"]), "tasks": results}
    Path(job["result_file"]).write_text(json.dumps(outcome, sort_keys=True))
    print(json.dumps(outcome, sort_keys=True))
    return 0 if outcome["ok"] else 1


def bench_env(root):
    driver = Driver(root, "release-only", ("alpha", "beta"), {}, batch="bench-env")
    for directory in (driver.project, driver.state, driver.evidence, driver.workers_dir):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    seed_project(driver)
    driver.trust_and_signer()
    return driver


def bench_trial(env, label, spec_kind, expect="passed"):
    started = time.monotonic()
    driver = Driver(env.root, "release-only", ("alpha", "beta"), bench_spec(spec_kind, tag=label), batch=f"bench-{label}")
    driver.project, driver.state, driver.authority = env.project, env.state, env.authority
    driver.evidence = env.evidence / label
    driver.workers_dir = driver.evidence / "workers"
    driver.prepare()
    driver.assign_all()
    driver.run_workers()
    for lane in driver.lanes:
        driver.assemble_quiet(lane)
        driver.freeze(lane)
        code, envelope = driver.stage_or_resolve([lane])
        if envelope.get("status") != "ok":
            raise RuntimeError(f"bench stage {lane}: {json.dumps(envelope)[:1500]}")
    release = driver.gate_cycle("release", expect=expect)
    driver.mark("orchestrate", started)
    return driver, release


def bench(root, cold=3, warm=3):
    root = ensure_external(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    evidence = {"schema": SCHEMA, "kind": "starlings-benchmark", "generated_at": time.time(), "machine": machine_profile(), "trials": {"cold": [], "warm": [], "conflict": None, "late_failure": None}, "notes": ["cold: fresh authority, state directory and project per trial", "warm: shared authority, state directory (mirror, database, signer) and project across trials; each trial is a new batch", "workload: starlings-example project generated with --preset release-only --with-qualification; the release gate runs static, unit, service and journey unittest checks", "assembly overhead is recorded in phases separately from run check durations; per-check queue, setup and command timings come from run_checks metrics", "no thresholds are asserted; raw ranges are preserved in bench.json"]}
    for index in range(cold):
        env = bench_env(root / f"cold-{index + 1}")
        driver, release = bench_trial(env, f"cold-{index + 1}", "plain")
        evidence["trials"]["cold"].append({"label": f"cold-{index + 1}", "release": release, "phases": driver.phases, "calls": driver.calls})
        print(f"cold-{index + 1}: release run {release['seconds']}s outcome {release['outcome']}", flush=True)
    warm_env = bench_env(root / "warm")
    for index in range(warm):
        driver, release = bench_trial(warm_env, f"warm-{index + 1}", "plain")
        evidence["trials"]["warm"].append({"label": f"warm-{index + 1}", "release": release, "phases": driver.phases, "calls": driver.calls})
        print(f"warm-{index + 1}: release run {release['seconds']}s outcome {release['outcome']}", flush=True)
    conflict_env = bench_env(root / "conflict")
    driver, release = bench_trial(conflict_env, "conflict", "conflict")
    evidence["trials"]["conflict"] = {"label": "conflict", "release": release, "phases": driver.phases, "calls": driver.calls}
    print(f"conflict: release run {release['seconds']}s outcome {release['outcome']}", flush=True)
    late_env = bench_env(root / "late")
    driver, failed = bench_trial(late_env, "late", "late", expect="failed")
    late = {"label": "late", "failed_release": failed, "phases": driver.phases, "calls": driver.calls}
    fix_driver, fixed = bench_trial(late_env, "fix", "fix")
    late["fix_release"] = fixed
    late["fix_phases"] = fix_driver.phases
    late["fix_calls"] = fix_driver.calls
    evidence["trials"]["late_failure"] = late
    print(f"late: failed release {failed['seconds']}s outcome {failed['outcome']}; fix release {fixed['seconds']}s outcome {fixed['outcome']}", flush=True)
    (root / "bench.json").write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    (root / "bench.md").write_text(bench_markdown(evidence))
    return evidence


def bench_markdown(evidence):
    lines = ["# Starlings qualification benchmarks", ""]
    machine = evidence["machine"]
    lines += [f"- Machine: {machine.get('hardware_model')} ({machine.get('cpu_count')} CPUs, {machine.get('memory_bytes')} bytes), {machine.get('os')}", f"- Python {machine.get('python')}, Git {machine.get('git')}, starlings {machine.get('starlings')}", "- Workload: starlings-example project generated with `--preset release-only --with-qualification`; the release gate runs static, unit, service and journey unittest checks", "- Cold: fresh authority, state directory and project per trial. Warm: shared authority, state directory (mirror, database, signer caches) and project; each trial is a new batch.", "- No thresholds are asserted; raw ranges are preserved in bench.json.", ""]
    lines += ["| Trial | Release gate seconds | Checks | Outcome |", "|---|---|---|---|"]
    for group in ("cold", "warm"):
        for trial in evidence["trials"][group]:
            release = trial["release"]
            lines.append(f"| {trial['label']} | {release['seconds']} | {len(release['checks'])} | {release['outcome']} |")
    conflict = evidence["trials"]["conflict"]
    if conflict:
        lines.append(f"| conflict | {conflict['release']['seconds']} | {len(conflict['release']['checks'])} | {conflict['release']['outcome']} |")
    late = evidence["trials"]["late_failure"]
    if late:
        lines.append(f"| late-failure | {late['failed_release']['seconds']} | {len(late['failed_release']['checks'])} | {late['failed_release']['outcome']} |")
        lines.append(f"| late-failure fix | {late['fix_release']['seconds']} | {len(late['fix_release']['checks'])} | {late['fix_release']['outcome']} |")
    lines += ["", "Per-check command timings are under trials[].release.run_checks[].metrics.duration_seconds; admission waits are run_checks[].admission_waited (the runner records the wait as a boolean, not a duration); assembly and orchestration overhead is under trials[].phases.", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="starlings.qualification", description="Qualification driver; see docs/qualification.md.")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run one preset (or all four) end to end in a disposable root")
    run.add_argument("--root", required=True, type=Path, help="disposable output root; must be outside any checkout")
    run.add_argument("--preset", choices=(*PRESETS, "all"), default="release-only")
    run.add_argument("--lanes", type=int, default=4)
    run.add_argument("--tasks", type=int, default=8)
    run.add_argument("--batch", default=BATCH)
    bench_parser = commands.add_parser("bench", help="record cold/warm release-gate benchmarks plus conflict and late-failure trials")
    bench_parser.add_argument("--root", required=True, type=Path)
    bench_parser.add_argument("--cold", type=int, default=3)
    bench_parser.add_argument("--warm", type=int, default=3)
    worker = commands.add_parser("worker", help=argparse.SUPPRESS)
    worker.add_argument("--job", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "worker":
        return worker_main(args.job)
    if args.command == "bench":
        bench(args.root, args.cold, args.warm)
        return 0
    lanes = LANES[: args.lanes] if args.lanes <= len(LANES) else LANES + tuple(f"lane-{i}" for i in range(len(LANES), args.lanes))
    presets = PRESETS if args.preset == "all" else (args.preset,)
    for preset_name in presets:
        sub = args.root / preset_name
        index = run_preset(sub, preset_name, lanes=lanes, per_lane=args.tasks, batch=args.batch)
        totals = {gate: (data["executions"], data["check_executions"]) for gate, data in index["gates"].items()}
        print(f"{preset_name}: gates {totals} expected {TABLE[preset_name]} landed {index['landed']} parked {len(index['parked'])}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
