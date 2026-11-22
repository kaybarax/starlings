import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from fixtures import EXCLUDED, Fixture, git
from starlings import batches, runner, signer, verifier
from starlings.cli import main
from starlings.contracts import Receipt
from starlings.policy import PolicyError, parse, preset, render, validate

JOURNEY = """import pathlib, sys
text = pathlib.Path("journeys/onboarding.txt").read_text()
pathlib.Path("out").mkdir(exist_ok=True)
pathlib.Path("out/journey.txt").write_text(text.upper())
sys.exit(0 if "welcome" in text else 1)
"""
FILES = {"journeys/onboarding.txt": "welcome\n", "tests/journey_check.py": JOURNEY}
LEDGER = ["python3", "-c", "import pathlib, sys; sys.exit(0 if 'total' in pathlib.Path('ledger.txt').read_text() else 1)"]
LEADER = """import os, pathlib, subprocess, sys, time
pids = pathlib.Path(sys.argv[1])
pids.mkdir(exist_ok=True)
(pids / "leader").write_text(str(os.getpid()))
child = subprocess.Popen([sys.executable, "-c", sys.argv[2], sys.argv[1], sys.argv[3]])
(pids / "child").write_text(str(child.pid))
while not (pids / "grandchild").exists():
    time.sleep(0.01)
(pids / "ready").write_text("1")
time.sleep(300)
"""
CHILD = """import pathlib, subprocess, sys, time
grand = subprocess.Popen([sys.executable, "-c", sys.argv[2]])
pathlib.Path(sys.argv[1], "grandchild.tmp").write_text(str(grand.pid))
pathlib.Path(sys.argv[1], "grandchild.tmp").rename(pathlib.Path(sys.argv[1], "grandchild"))
time.sleep(300)
"""
GRANDCHILD = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"
WAIT = "import pathlib, sys, time\nwhile not pathlib.Path(sys.argv[1]).exists():\n    time.sleep(0.02)\n"
ENVIRON = "import json, os, pathlib; pathlib.Path('out').mkdir(exist_ok=True); pathlib.Path('out/env.json').write_text(json.dumps(dict(os.environ)))"


def build_policy(checks, scope="full", environment="native", selection=None, artifacts=None, exclusive=None, guest=None, cpu_slots=2, max_guests=0, timeout=60):
    policy = preset("each-lane-release")
    policy["project"] = "fixture-project"
    policy["checks"] = dict(checks)
    for gate in ("staging", "release"):
        policy["gates"][gate].update({"checks": list(checks), "environment": environment})
    policy["gates"]["staging"]["scope"] = scope
    policy["assembly"] = {"excluded": list(EXCLUDED)}
    for key, value in (("selection", selection), ("artifacts", artifacts), ("exclusive", exclusive)):
        if value is not None:
            policy[key] = value
    if guest is not None:
        policy["guest"] = {"image": guest}
    policy["limits"].update({"cpu_slots": cpu_slots, "max_guests": max_guests, "timeout_seconds": timeout})
    return render(policy)


def standard_policy(scope="affected", **extra):
    checks = {"journey": ["python3", "tests/journey_check.py"], "ledger": LEDGER}
    return build_policy(checks, scope=scope, selection={"journey": ["journeys/*", "tests/journey_check.py"], "ledger": ["ledger.txt"]}, artifacts={"journey": ["out/journey.txt"]}, exclusive={"ledger": ["port-5432"]}, **extra)


def call(fixture, function, *args, hook=None, **kwargs):
    with fixture.context(hook=hook) as ctx:
        try:
            return function(ctx, *args, **kwargs)
        except batches.Failure as exc:
            return exc.status, exc


def ok(testcase, outcome, expected="ok"):
    status, data = outcome
    testcase.assertEqual(status, expected, getattr(data, "message", data))
    return data


def wait_for(predicate, seconds=30):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


class VerifierCase(unittest.TestCase):
    def make(self, policy, files=FILES, parent=None):
        fixture = Fixture(policy=policy, files=files, parent=parent)
        self.addCleanup(fixture.close)
        return fixture

    def trust(self, fixture, supersedes=None, commit=None):
        return ok(self, call(fixture, verifier.trust, str(fixture.authority), commit or fixture.head("dev"), "review:policy", supersedes))

    def ready(self, fixture):
        self.trust(fixture)
        with fixture.context() as ctx:
            signer.init(ctx.root)

    def stage(self, fixture, files, batch="b1"):
        ok(self, fixture.call("prepare", batch, ["ledger"], str(fixture.authority)))
        work = ok(self, fixture.call("assign", batch, "solo", "ledger", "worker-a", []))
        head = fixture.work(work["worktree"], files, "solo change")
        ok(self, fixture.call("handoff", batch, "solo", head, "review:solo", []))
        ok(self, fixture.call("assemble", batch, "ledger"))
        ok(self, fixture.call("freeze", batch, "ledger"))
        staged = ok(self, fixture.call("stage", batch, ["ledger"]), "pending")
        return staged["candidates"][0]["id"]

    def plan(self, fixture, gate="staging", expected="ok", **kwargs):
        return ok(self, call(fixture, verifier.plan, "b1", gate, **kwargs), expected)

    def run_plan(self, fixture, plan_id, expected="ok", **kwargs):
        return ok(self, call(fixture, runner.run, plan_id, **kwargs), expected)

    def check(self, fixture, receipt_id, expected="ok", target=None):
        return ok(self, call(fixture, verifier.check, receipt_id, target), expected)

    def slot_leases(self, fixture):
        with fixture.context() as ctx:
            return [row[0] for row in ctx.store.connection.execute("SELECT resource FROM leases WHERE resource LIKE 'slot:%' OR resource LIKE 'run:%'")]


class PolicyTableTests(unittest.TestCase):
    def test_tables_roundtrip_and_errors(self):
        text = standard_policy(guest="python@sha256:" + "a" * 64)
        policy = parse(text)
        self.assertEqual(parse(render(policy)), policy)
        self.assertIn("[selection]", text)
        self.assertNotIn("selection", preset("release-only"))
        cases = [("selection", {"missing": ["a"]}), ("artifacts", {"journey": ["/abs"]}), ("artifacts", {"journey": ["a/../b"]}), ("artifacts", {"journey": ["out/*.txt"]}), ("exclusive", {"ledger": ["Port 5432"]}), ("selection", {"journey": []}), ("guest", {"image": "python:3.13"}), ("guest", {"image": "python@sha256:abc"}), ("guest", {"image": "x@sha256:" + "a" * 64, "extra": 1}), ("selection", ["journey"])]
        for key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaises(PolicyError):
                data = parse(text)
                data[key] = value
                validate(data)
        data = parse(text)
        data.pop("guest")
        data["gates"]["release"]["environment"] = "guest"
        data["limits"]["max_guests"] = 1
        with self.assertRaisesRegex(PolicyError, "guest.image"):
            validate(data)


class SignerTests(VerifierCase):
    def test_init_idempotent_and_diagnosed(self):
        fixture = self.make(standard_policy())
        with fixture.context() as ctx:
            first = signer.init(ctx.root)
            self.assertTrue(first["created"])
            key = Path(first["key"])
            before = key.read_bytes()
            second = signer.init(ctx.root)
            self.assertFalse(second["created"])
            self.assertEqual(key.read_bytes(), before)
            self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((ctx.root / "signer-references").stat().st_mode), 0o700)
            self.assertTrue(first["fingerprint"].startswith("SHA256:"))
            signature = signer.sign(ctx.root, b"payload")
            self.assertTrue(signer.verify(ctx.root, b"payload", signature))
            self.assertFalse(signer.verify(ctx.root, b"payloaD", signature))
            Path(first["allowed_signers"]).write_text("starlings-verifier ssh-ed25519 AAAA\n")
            with self.assertRaisesRegex(signer.SignerError, "refusing to overwrite"):
                signer.init(ctx.root)
            with patch("shutil.which", return_value=None), self.assertRaisesRegex(signer.SignerError, "ssh-keygen was not found"):
                signer.init(ctx.root)


class TrustTests(VerifierCase):
    def test_trust_chain_and_untrusted_plans(self):
        fixture = self.make(standard_policy())
        self.stage(fixture, {"journeys/onboarding.txt": "welcome back\n"})
        self.assertEqual(self.plan(fixture, expected="blocked").code, "untrusted-policy")
        first = self.trust(fixture)
        self.assertIsNone(first["previous_digest"])
        self.assertFalse(first["repeated"])
        self.assertTrue(self.trust(fixture)["repeated"])
        self.assertEqual(stat.S_IMODE(Path(first["path"]).stat().st_mode), 0o600)
        self.assertEqual(ok(self, call(fixture, verifier.trust, str(fixture.authority), fixture.head("dev"), "review:x", "f" * 64), "conflict").code, "trust-conflict")
        changed = fixture.external_commit("dev", "starlings.toml", standard_policy(cpu_slots=3))
        self.assertEqual(ok(self, call(fixture, verifier.trust, str(fixture.authority), changed, "review:v2"), "conflict").code, "trust-conflict")
        self.assertEqual(ok(self, call(fixture, verifier.trust, str(fixture.authority), changed, "review:v2", "e" * 64), "conflict").code, "trust-conflict")
        second = self.trust(fixture, first["digest"], changed)
        self.assertEqual(second["previous_digest"], first["digest"])
        self.assertEqual([item["digest"] for item in second["history"]], [second["digest"], first["digest"]])
        failure = self.plan(fixture, expected="blocked")
        self.assertEqual(failure.code, "untrusted-policy")
        self.assertEqual(failure.data["trusted_digest"], second["digest"])
        self.assertEqual(ok(self, call(fixture, verifier.trust, str(fixture.authority), fixture.head("main"), "review:x", second["digest"]), "ok")["previous_digest"], second["digest"])
        self.assertEqual(ok(self, call(fixture, verifier.trust, str(fixture.authority), "0" * 40, "review:x"), "invalid").status, "invalid")


class SelectionTests(VerifierCase):
    def test_affected_expanded_and_full(self):
        fixture = self.make(standard_policy())
        self.ready(fixture)
        self.stage(fixture, {"journeys/onboarding.txt": "welcome back\n"})
        plan = self.plan(fixture)
        self.assertEqual((plan["selection"], plan["checks"], plan["changed"]), ("affected", ["journey"], ["journeys/onboarding.txt"]))
        self.assertEqual(plan["plan"]["schema"], 2)
        again = self.plan(fixture)
        self.assertEqual(again["plan_id"], plan["plan_id"])
        self.assertTrue(again["repeated"])
        run = self.run_plan(fixture, plan["plan_id"])
        self.assertEqual([item["check"] for item in run["checks"]], ["journey"])

        fixture = self.make(standard_policy())
        self.ready(fixture)
        self.stage(fixture, {"journeys/onboarding.txt": "welcome back\n", "shared.txt": "alpha\nunmapped\ngamma\n"})
        plan = self.plan(fixture)
        self.assertEqual((plan["selection"], plan["checks"], plan["unmapped_paths"]), ("expanded", ["journey", "ledger"], ["shared.txt"]))

        policy = parse(standard_policy())
        policy["selection"].pop("ledger")
        fixture = self.make(render(policy))
        self.ready(fixture)
        self.stage(fixture, {"journeys/onboarding.txt": "welcome back\n"})
        plan = self.plan(fixture)
        self.assertEqual((plan["selection"], plan["checks"], plan["unmapped_checks"]), ("expanded", ["journey", "ledger"], ["ledger"]))

        fixture = self.make(standard_policy(scope="full"))
        self.ready(fixture)
        self.stage(fixture, {"journeys/onboarding.txt": "welcome back\n"})
        plan = self.plan(fixture)
        self.assertEqual((plan["selection"], plan["checks"]), ("full", ["journey", "ledger"]))
        self.assertEqual(self.plan(fixture, gate="task", expected="blocked").code, "gate-disabled")


class RunTests(VerifierCase):
    def test_receipt_reuse_force_fresh_tamper_and_staleness(self):
        policy = parse(standard_policy(scope="full"))
        policy["checks"]["environ"] = ["python3", "-c", ENVIRON]
        policy["gates"]["staging"]["checks"].append("environ")
        policy["artifacts"]["environ"] = ["out/env.json"]
        fixture = self.make(render(policy))
        self.ready(fixture)
        main_before = fixture.head("main")
        self.stage(fixture, {"journeys/onboarding.txt": "welcome back\n"})
        plan = self.plan(fixture)
        first = self.run_plan(fixture, plan["plan_id"])
        self.assertEqual((first["outcome"], first["reused"]), ("passed", False))
        receipt = Receipt.from_dict(first["receipt"])
        self.assertEqual(receipt.outcome, "passed")
        self.assertEqual(len(receipt.evidence), 3)
        self.assertIsNone(receipt.toolchain["guest_image"])
        valid = self.check(fixture, first["receipt_id"])
        self.assertEqual(valid["status"], "valid")
        self.assertTrue(valid["authorizes"])
        root = fixture.state
        environ = json.loads((root / "artifacts" / first["run_id"] / "environ" / "out" / "env.json").read_text())
        self.assertTrue(set(environ) <= {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PWD", "__CF_USER_TEXT_ENCODING", "SHLVL", "_"}, environ)
        self.assertEqual(environ["HOME"], environ["TMPDIR"])
        self.assertNotIn(str(root), environ["HOME"] + environ["PATH"])
        self.assertFalse(Path(environ["HOME"]).exists())
        self.assertEqual(self.slot_leases(fixture), [])
        self.assertFalse((root / "worktrees" / "runs" / first["run_id"] / "journey").exists())
        reused = self.run_plan(fixture, plan["plan_id"])
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["receipt_id"], first["receipt_id"])
        fresh = self.run_plan(fixture, plan["plan_id"], force_fresh=True)
        self.assertFalse(fresh["reused"])
        self.assertNotEqual(fresh["receipt_id"], first["receipt_id"])
        third = self.run_plan(fixture, plan["plan_id"], force_fresh=True)

        log = root / "logs" / first["run_id"] / "journey.out"
        log.write_bytes(log.read_bytes() + b"x")
        tampered = self.check(fixture, first["receipt_id"], "invalid")
        self.assertIn(f"retained file was modified: logs/{first['run_id']}/journey.out", tampered["reasons"])
        self.assertFalse(tampered["authorizes"])
        artifact = root / "artifacts" / fresh["run_id"] / "journey" / "out" / "journey.txt"
        artifact.unlink()
        self.assertIn(f"retained file is missing: artifacts/{fresh['run_id']}/journey/out/journey.txt", self.check(fixture, fresh["receipt_id"], "invalid")["reasons"])
        artifact = root / "artifacts" / third["run_id"] / "journey" / "out" / "journey.txt"
        original = artifact.read_bytes()
        artifact.write_bytes(b"X" + original[1:])
        self.check(fixture, third["receipt_id"], "invalid")
        artifact.write_bytes(original)
        self.check(fixture, third["receipt_id"])
        wrong = self.check(fixture, third["receipt_id"], "invalid", target="main")
        self.assertIn("receipt target refs/heads/dev is not the expected target refs/heads/main", wrong["reasons"])
        with fixture.context() as ctx:
            ctx.store.connection.execute("UPDATE receipts SET record = replace(record, '\"outcome\":\"passed\"', '\"outcome\":\"failed\"') WHERE id = ?", (third["receipt_id"],))
        self.check(fixture, third["receipt_id"], "invalid")
        with fixture.context() as ctx:
            ctx.store.connection.execute("UPDATE receipts SET record = replace(record, '\"outcome\":\"failed\"', '\"outcome\":\"passed\"') WHERE id = ?", (third["receipt_id"],))
        self.check(fixture, third["receipt_id"])

        fixture.external_commit("dev", "reports.txt", "report = moved\n")
        stale = self.check(fixture, third["receipt_id"], "stale")
        self.assertTrue(any("not the receipt base" in reason for reason in stale["reasons"]))
        self.assertEqual(self.run_plan(fixture, plan["plan_id"], expected="ok")["reused"], False)
        changed = fixture.external_commit("dev", "starlings.toml", render(parse(render(policy)) | {"project": "fixture-project-v2"}))
        self.trust(fixture, plan["policy_digest"], changed)
        stale = self.check(fixture, third["receipt_id"], "stale")
        self.assertTrue(any("no longer the trusted policy" in reason for reason in stale["reasons"]))
        self.assertEqual(ok(self, call(fixture, runner.run, plan["plan_id"]), "stale").code, "stale-plan")
        self.assertEqual(fixture.head("main"), main_before)

    def test_failing_and_missing_artifact(self):
        checks = {"ledger": LEDGER, "produce": ["python3", "-c", "print('no artifact')"]}
        fixture = self.make(build_policy(checks, artifacts={"produce": ["out/never.txt"]}))
        self.ready(fixture)
        self.stage(fixture, {"ledger.txt": "broken = 1\n"})
        plan = self.plan(fixture)
        result = self.run_plan(fixture, plan["plan_id"], expected="failed")
        self.assertEqual(result["outcome"], "failed")
        self.assertEqual([(item["check"], item["status"]) for item in result["checks"]], [("ledger", "failed"), ("produce", "missing-artifact")])
        self.assertEqual(result["checks"][1]["metrics"]["missing_artifacts"], ["out/never.txt"])
        self.assertTrue((fixture.state / "logs" / result["run_id"] / "ledger.err").is_file())
        checked = self.check(fixture, result["receipt_id"], "failed")
        self.assertFalse(checked["authorizes"])
        again = self.run_plan(fixture, plan["plan_id"], expected="failed")
        self.assertNotEqual(again["receipt_id"], result["receipt_id"])
        with fixture.context() as ctx:
            self.assertEqual(ctx.store.connection.execute("SELECT COUNT(*) FROM evidence WHERE status = 'failed'").fetchone()[0], 2)

    def slow_fixture(self, timeout=60, cpu_slots=2):
        holder = {}
        fixture = self.make(build_policy({"slow": ["python3", "-c", LEADER, "PIDS", CHILD, GRANDCHILD]}, cpu_slots=cpu_slots, timeout=timeout))
        pids = fixture.root / "pids"
        policy = parse((fixture.project / "starlings.toml").read_text())
        policy["checks"]["slow"][3] = str(pids)
        text = render(policy)
        (fixture.project / "starlings.toml").write_text(text)
        commit = fixture.external_commit("dev", "starlings.toml", text)
        self.trust(fixture, commit=commit)
        with fixture.context() as ctx:
            signer.init(ctx.root)
        self.stage(fixture, {"ledger.txt": "total = 1\n"})
        holder["plan"] = self.plan(fixture)
        return fixture, pids, holder["plan"]

    def assert_tree_gone(self, pids):
        for name in ("leader", "child", "grandchild"):
            pid = int((pids / name).read_text())
            wait_for(lambda: gone(pid), 5)
            self.assertTrue(gone(pid), name)

    def test_timeout_reaps_process_tree(self):
        fixture, pids, plan = self.slow_fixture(timeout=60)
        result = self.run_plan(fixture, plan["plan_id"], expected="failed", timeout=3, grace=0.5)
        self.assertTrue((pids / "ready").exists())
        self.assertEqual(result["checks"][0]["status"], "timeout")
        self.assertTrue(result["checks"][0]["metrics"]["process_group_reaped"])
        self.assert_tree_gone(pids)
        self.assertEqual(self.slot_leases(fixture), [])
        self.check(fixture, result["receipt_id"], "failed")
        self.assertEqual(ok(self, call(fixture, runner.run, plan["plan_id"], timeout=61), "invalid").status, "invalid")

    def test_cancel_and_interrupt_never_sign(self):
        fixture, pids, plan = self.slow_fixture()
        started = {}
        outcome = {}
        def hook(point, info):
            if point == "run-started":
                started["run"] = info["run"]
        thread = threading.Thread(target=lambda: outcome.update(value=call(fixture, runner.run, plan["plan_id"], hook=hook, grace=0.5)))
        thread.start()
        wait_for(lambda: (pids / "ready").exists())
        ok(self, call(fixture, runner.cancel, started["run"]))
        thread.join(60)
        status, failure = outcome["value"]
        self.assertEqual((status, failure.code), ("failed", "cancelled"))
        self.assert_tree_gone(pids)
        self.assertEqual(self.slot_leases(fixture), [])
        with fixture.context() as ctx:
            self.assertEqual(ctx.store.connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)
            self.assertEqual(ctx.store.connection.execute("SELECT state FROM runs WHERE id = ?", (started["run"],)).fetchone()[0], "cancelled")
        self.assertEqual(ok(self, call(fixture, runner.cancel, started["run"]))["state"], "cancelled")
        shutil.rmtree(pids)
        event = threading.Event()
        thread = threading.Thread(target=lambda: outcome.update(value=call(fixture, runner.run, plan["plan_id"], interrupt=event, grace=0.5)))
        thread.start()
        wait_for(lambda: (pids / "ready").exists())
        event.set()
        thread.join(60)
        self.assertEqual(outcome["value"][1].code, "interrupted")
        self.assert_tree_gone(pids)
        self.assertEqual(self.slot_leases(fixture), [])
        with fixture.context() as ctx:
            self.assertEqual(ctx.store.connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)

    def test_sigterm_to_cli_runner_cancels(self):
        fixture, pids, plan = self.slow_fixture()
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        process = subprocess.Popen([sys.executable, "-m", "starlings", "run", plan["plan_id"], "--state-dir", str(fixture.state), "--directory", str(fixture.project), "--json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        wait_for(lambda: (pids / "ready").exists())
        process.terminate()
        out, _ = process.communicate(timeout=60)
        self.assertEqual(process.returncode, 7)
        self.assertEqual(json.loads(out)["error"]["code"], "interrupted")
        self.assert_tree_gone(pids)
        self.assertEqual(self.slot_leases(fixture), [])
        with fixture.context() as ctx:
            self.assertEqual(ctx.store.connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)

    def test_same_plan_blocked_and_admission_serialized(self):
        gate = None
        fixture = self.make(build_policy({"wait": ["python3", "-c", WAIT, "GATE"]}, cpu_slots=1))
        gate = fixture.root / "gate"
        policy = parse((fixture.project / "starlings.toml").read_text())
        policy["checks"]["wait"][3] = str(gate)
        text = render(policy)
        (fixture.project / "starlings.toml").write_text(text)
        self.trust(fixture, commit=fixture.external_commit("dev", "starlings.toml", text))
        with fixture.context() as ctx:
            signer.init(ctx.root)
        self.stage(fixture, {"ledger.txt": "total = 1\n"})
        staging = self.plan(fixture)
        release = self.plan(fixture, gate="release")
        events, lock, outcomes = [], threading.Lock(), {}
        def hook(label):
            def record(point, info):
                with lock:
                    events.append((label, point))
            return record
        first = threading.Thread(target=lambda: outcomes.update(a=call(fixture, runner.run, staging["plan_id"], hook=hook("a"))))
        first.start()
        wait_for(lambda: ("a", "process-started") in events)
        blocked = ok(self, call(fixture, runner.run, staging["plan_id"]), "blocked")
        self.assertEqual(blocked.code, "lease-held")
        second = threading.Thread(target=lambda: outcomes.update(b=call(fixture, runner.run, release["plan_id"], hook=hook("b"))))
        second.start()
        wait_for(lambda: ("b", "admission-wait") in events)
        self.assertNotIn(("b", "admitted"), events)
        gate.write_text("go")
        first.join(60)
        second.join(60)
        self.assertEqual(outcomes["a"][0], "ok")
        self.assertEqual(outcomes["b"][0], "ok")
        self.assertTrue(outcomes["b"][1]["checks"][0]["admission_waited"])
        self.assertLess(events.index(("a", "admitted")), events.index(("b", "admitted")))
        self.assertEqual(self.slot_leases(fixture), [])


class ReleasePlanTests(VerifierCase):
    def test_release_candidate_parents_and_conflict(self):
        fixture = self.make(standard_policy())
        self.ready(fixture)
        ok(self, fixture.call("prepare", "b1", ["ledger"], str(fixture.authority)))
        self.assertEqual(self.plan(fixture, gate="release", expected="blocked").code, "nothing-to-release")
        dev = fixture.external_commit("dev", "reports.txt", "report = dev\n")
        main_before = fixture.head("main")
        plan = self.plan(fixture, gate="release")
        self.assertEqual(plan["selection"], "full")
        self.assertEqual(plan["target"], "refs/heads/main")
        self.assertEqual(plan["candidate"]["parents"], [main_before, dev])
        self.assertEqual(plan["candidate"]["base"], main_before)
        self.assertEqual(fixture.head("main"), main_before)
        mirror = fixture.state / "mirrors"
        mirror = next(mirror.iterdir())
        self.assertEqual(git(mirror, "rev-parse", plan["candidate_ref"]), plan["candidate"]["commit"])
        self.assertEqual(git(mirror, "rev-list", "--parents", "-n", "1", plan["candidate"]["commit"]).split()[1:], [main_before, dev])
        self.assertEqual(self.plan(fixture, gate="release")["plan_id"], plan["plan_id"])
        run = self.run_plan(fixture, plan["plan_id"])
        self.assertEqual(self.check(fixture, run["receipt_id"])["target"], "refs/heads/main")
        self.assertEqual(fixture.head("main"), main_before)
        fixture.external_commit("main", "reports.txt", "report = main\n")
        conflict = self.plan(fixture, gate="release", expected="blocked")
        self.assertEqual((conflict.code, conflict.data["paths"]), ("release-conflict", ["reports.txt"]))


FAKE_DOCKER = r'''#!PYTHON
import json, os, pathlib, signal, subprocess, sys
state = pathlib.Path(os.environ["FAKE_DOCKER_STATE"])
args = sys.argv[1:]
with open(state / "calls.jsonl", "a") as stream:
    stream.write(json.dumps(args) + "\n")
containers = state / "containers"
containers.mkdir(exist_ok=True)
if args[0] == "info":
    sys.exit(1 if (state / "down").exists() else print("fake-29.0") or 0)
if args[:2] == ["image", "inspect"]:
    sys.exit(0 if args[-1] in (state / "images").read_text().split() else 1)
if args[0] == "ps":
    name = args[-1].split("=", 1)[1].strip("^$/")
    print(name if (containers / name).exists() else "", end="")
    sys.exit(0)
if args[0] == "kill":
    path = containers / args[1]
    if path.exists():
        try:
            os.killpg(int(path.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass
        path.unlink()
    sys.exit(0)
if args[0] == "run":
    i, options = 1, {}
    while args[i].startswith("-"):
        if args[i] == "--rm":
            i += 1
            continue
        options[args[i]] = args[i + 1]
        i += 2
    source = dict(item.split("=", 1) for item in options["--mount"].split(","))["source"]
    child = subprocess.Popen(args[i + 1:], cwd=source, start_new_session=True, env={"PATH": os.environ["PATH"], "HOME": "/root"})
    (containers / options["--name"]).write_text(str(child.pid))
    code = child.wait()
    (containers / options["--name"]).unlink(missing_ok=True)
    sys.exit(code)
sys.exit(2)
'''
IMAGE = "python@sha256:" + "b" * 64


class GuestTests(VerifierCase):
    def fake(self, fixture):
        bin_dir = fixture.root / "fakebin"
        bin_dir.mkdir()
        state = fixture.root / "fakedocker"
        state.mkdir()
        (state / "images").write_text(IMAGE + "\n")
        script = bin_dir / "docker"
        script.write_text(FAKE_DOCKER.replace("PYTHON", sys.executable))
        script.chmod(0o755)
        patcher = patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_DOCKER_STATE": str(state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return state

    def test_fake_docker_adapter(self):
        checks = {"journey": ["python3", "tests/journey_check.py"], "hang": ["python3", "-c", "import time; time.sleep(300)"]}
        fixture = self.make(build_policy({"journey": checks["journey"]}, environment="guest", guest=IMAGE, max_guests=1, artifacts={"journey": ["out/journey.txt"]}))
        state = self.fake(fixture)
        self.ready(fixture)
        self.stage(fixture, {"journeys/onboarding.txt": "welcome guest\n"})
        plan = self.plan(fixture)
        self.assertEqual(plan["plan"]["environment"], "guest")
        result = self.run_plan(fixture, plan["plan_id"])
        self.assertEqual(result["outcome"], "passed")
        self.assertEqual(result["receipt"]["toolchain"]["guest_image"], IMAGE)
        self.check(fixture, result["receipt_id"])
        runs = [json.loads(line) for line in (state / "calls.jsonl").read_text().splitlines() if json.loads(line)[0] == "run"]
        argv = runs[0]
        self.assertEqual(argv[:9], ["run", "--rm", "--pull", "never", "--network", "none", "--cpus", "1", "--memory"])
        self.assertIn(IMAGE, argv)
        self.assertEqual(argv[-2:], ["python3", "tests/journey_check.py"])
        self.assertTrue(any(item.startswith("type=bind,source=") and item.endswith(",target=/work") for item in argv))
        (state / "down").write_text("")
        down = ok(self, call(fixture, runner.run, plan["plan_id"], force_fresh=True), "failed")
        self.assertEqual(down.code, "guest-unavailable")
        (state / "down").unlink()
        (state / "images").write_text("")
        missing = ok(self, call(fixture, runner.run, plan["plan_id"], force_fresh=True), "failed")
        self.assertEqual(missing.code, "guest-unavailable")
        self.assertIn("not present locally", missing.message)

    def test_fake_docker_timeout_kills_container(self):
        fixture = self.make(build_policy({"hang": ["python3", "-c", "import time; time.sleep(300)"]}, environment="guest", guest=IMAGE, max_guests=1))
        state = self.fake(fixture)
        self.ready(fixture)
        self.stage(fixture, {"ledger.txt": "total = 2\n"})
        plan = self.plan(fixture)
        result = self.run_plan(fixture, plan["plan_id"], expected="failed", timeout=2, grace=0.5)
        check = result["checks"][0]
        self.assertEqual(check["status"], "timeout")
        self.assertTrue(check["metrics"]["container_removed"])
        self.assertTrue(check["metrics"]["process_group_reaped"])
        calls = [json.loads(line) for line in (state / "calls.jsonl").read_text().splitlines()]
        self.assertIn(["kill", check["metrics"]["container"]], calls)
        self.assertEqual(list((state / "containers").iterdir()), [])
        self.assertEqual(self.slot_leases(fixture), [])


@unittest.skipUnless(os.environ.get("STARLINGS_TEST_GUEST_IMAGE") and os.environ.get("STARLINGS_TEST_GUEST_ROOT"), "real guest parity needs STARLINGS_TEST_GUEST_IMAGE (pulled locally) and STARLINGS_TEST_GUEST_ROOT (a directory shared with the docker VM)")
class GuestParityTests(VerifierCase):
    def test_native_and_guest_parity(self):
        image = os.environ["STARLINGS_TEST_GUEST_IMAGE"]
        ready, detail = runner.guest_status(image)
        if not ready:
            self.skipTest(detail)
        outcomes = {}
        for environment in ("native", "guest"):
            for content, expected in (("welcome parity\n", "ok"), ("goodbye\n", "failed")):
                fixture = self.make(build_policy({"journey": ["python3", "tests/journey_check.py"]}, environment=environment, guest=image, max_guests=1, artifacts={"journey": ["out/journey.txt"]}), parent=os.environ["STARLINGS_TEST_GUEST_ROOT"])
                self.ready(fixture)
                self.stage(fixture, {"journeys/onboarding.txt": content})
                result = self.run_plan(fixture, self.plan(fixture)["plan_id"], expected=expected)
                artifact = next(value for path, value in result["checks"][0]["files"].items() if path.endswith("out/journey.txt"))
                outcomes[(environment, expected)] = (result["outcome"], result["checks"][0]["exit_code"], artifact)
        self.assertEqual(outcomes[("native", "ok")], outcomes[("guest", "ok")])
        self.assertEqual(outcomes[("native", "failed")], outcomes[("guest", "failed")])


class CLITests(VerifierCase):
    def cli(self, fixture, *args):
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
            code = main([*args, "--state-dir", str(fixture.state), "--directory", str(fixture.project), "--json"])
        return code, json.loads(out.getvalue())

    def test_cli_exit_codes(self):
        fixture = self.make(standard_policy())
        code, result = self.cli(fixture, "policy", "trust", "--authority", str(fixture.authority), "--commit", fixture.head("dev"), "--review", "review:1")
        self.assertEqual(code, 0, result)
        self.assertEqual(self.cli(fixture, "signer", "init")[0], 0)
        self.stage(fixture, {"journeys/onboarding.txt": "welcome cli\n"})
        code, planned = self.cli(fixture, "plan", "b1", "--gate", "staging")
        self.assertEqual(code, 0, planned)
        code, ran = self.cli(fixture, "run", planned["data"]["plan_id"])
        self.assertEqual(code, 0, ran)
        receipt = ran["data"]["receipt_id"]
        self.assertEqual(self.cli(fixture, "check", receipt)[0], 0)
        self.assertEqual(self.cli(fixture, "check", receipt, "--target", "main")[0], 2)
        fixture.external_commit("dev", "reports.txt", "moved\n")
        self.assertEqual(self.cli(fixture, "check", receipt)[1]["status"], "stale")
        self.assertEqual(self.cli(fixture, "check", receipt)[0], 6)
        code, doctor = self.cli(fixture, "doctor")
        self.assertEqual(code, 0)
        self.assertTrue(doctor["data"]["signer_present"])
        self.assertTrue(doctor["data"]["git"]["ok"])


if __name__ == "__main__":
    unittest.main()
