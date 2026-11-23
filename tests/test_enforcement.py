import contextlib
import hashlib
import io
import json
from pathlib import Path
import stat
import subprocess
import unittest

import test_verifier as tv
from test_promotion import PromotionCase, gated_policy
from fixtures import environment
from starlings import batches, enforcement, runner, verifier
from starlings.cli import main

FIXTURE_PARENT = Path("/private/tmp/starlings-h1-tests")
FIXTURE_PARENT.mkdir(exist_ok=True)

FAST = {"unit": ["python3", "-c", "import sys; sys.exit(0)"]}


def raw_git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=environment(), check=False)


def cli(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = main(list(args))
    return code, json.loads(out.getvalue())


class EnforcementCase(PromotionCase):
    def make(self, policy, **kwargs):
        return super().make(policy, parent=str(FIXTURE_PARENT), **kwargs)

    def protect(self, f):
        return tv.ok(self, tv.call(f, enforcement.protect, str(f.authority)))

    def intent(self, f, refname, old, new, expected="ok"):
        try:
            status, data = enforcement.check_intent(f.state, f.authority, refname, old, new)
            self.assertEqual(status, expected)
            return data
        except batches.Failure as exc:
            return exc


class ProtectAndDoctorTests(EnforcementCase):
    def test_protect_installs_deterministic_hooks_and_records(self):
        f = self.make(gated_policy())
        self.ready(f)
        result = self.protect(f)
        self.assertFalse(result["repeated"])
        self.assertEqual(result["protected_refs"], ["refs/heads/dev", "refs/heads/main"])
        for name in enforcement.HOOK_NAMES:
            hook = f.authority / "hooks" / name
            self.assertTrue(hook.is_file(), name)
            self.assertFalse(hook.is_symlink(), name)
            self.assertEqual(stat.S_IMODE(hook.stat().st_mode), 0o755)
            self.assertEqual(hashlib.sha256(hook.read_bytes()).hexdigest(), result["hook_sha256"])
        again = self.protect(f)
        self.assertTrue(again["repeated"])
        self.assertEqual(again["hooks"], {"update": "already-installed", "reference-transaction": "already-installed"})
        row = self.rows(f, "SELECT * FROM authorities")[0]
        self.assertEqual(row["hook_sha256"], result["hook_sha256"])
        self.assertEqual(json.loads(row["protected_refs"]), result["protected_refs"])
        probe = enforcement.probe(f.state, f.authority)
        self.assertTrue(probe["implemented"])
        self.assertEqual(probe["mode"], "hook-enforced")
        code, out = cli("--json", "doctor", "--directory", str(f.project), "--state-dir", str(f.state), "--authority", str(f.authority))
        self.assertEqual(code, 0)
        self.assertTrue(out["data"]["enforcement_implemented"])
        self.assertTrue(out["data"]["enforcement"]["unmodified"])
        code, out = cli("--json", "doctor", "--directory", str(f.project), "--state-dir", str(f.state))
        self.assertEqual(code, 0)
        self.assertFalse(out["data"]["enforcement_implemented"])
        self.assertEqual(out["data"]["enforcement"]["mode"], "coordinator-enforced")

    def test_protect_requires_bare_authority_and_policy(self):
        f = self.make(gated_policy())
        failure = tv.ok(self, tv.call(f, enforcement.protect, str(f.authority)), "invalid")
        self.assertIn("no trusted policy or batch records", failure.message)
        self.assertEqual(tv.call(f, enforcement.protect, str(f.project))[0], "invalid")
        self.ready(f)
        (f.authority / "hooks" / "update").write_text("#!/bin/sh\nexit 0\n")
        conflict = tv.ok(self, tv.call(f, enforcement.protect, str(f.authority)), "conflict")
        self.assertEqual(conflict.code, "hook-exists")


class HookBehaviorTests(EnforcementCase):
    def test_direct_writes_to_protected_refs_rejected(self):
        f = self.make(gated_policy())
        self.ready(f)
        self.protect(f)
        dev, main0 = f.head("dev"), f.head("main")
        for ref in ("refs/heads/dev", "refs/heads/main"):
            result = raw_git(f.authority, "update-ref", ref, dev)
            self.assertNotEqual(result.returncode, 0, ref)
            self.assertIn("ref-update-refused", result.stderr)
            self.assertEqual(f.head(ref.removeprefix("refs/heads/")), dev if ref.endswith("dev") else main0)
        deleted = raw_git(f.authority, "update-ref", "-d", "refs/heads/dev")
        self.assertNotEqual(deleted.returncode, 0)
        self.assertEqual(f.head("dev"), dev)
        foreign = raw_git(f.authority, "update-ref", "refs/heads/topic", dev)
        self.assertEqual(foreign.returncode, 0, foreign.stderr)
        clone = f.root / "pusher"
        subprocess.run(["git", "clone", "--quiet", str(f.authority), str(clone)], env=environment(), check=True)
        def git(*a):
            return raw_git(clone, *a)
        git("checkout", "--quiet", "-b", "work", "origin/dev")
        (clone / "ledger.txt").write_text("rogue\n")
        committed = git("-c", "user.name=p", "-c", "user.email=p@x", "commit", "--quiet", "-am", "rogue")
        self.assertEqual(committed.returncode, 0, committed.stderr)
        rejected = git("push", "origin", "HEAD:refs/heads/dev")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("hook declined", rejected.stderr)
        self.assertEqual(f.head("dev"), dev)
        allowed = git("push", "origin", "HEAD:refs/heads/topic2")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_stage_and_promote_run_through_the_hook(self):
        f = self.make(gated_policy())
        self.ready(f)
        self.protect(f)
        dev0 = f.head("dev")
        self.staged(f)
        dev1 = f.head("dev")
        self.assertNotEqual(dev0, dev1)
        plan, run = self.release(f)
        main0 = f.head("main")
        landed = self.promote(f, plan["plan_id"], run["receipt_id"])
        self.assertEqual(landed["state"], "landed")
        self.assertEqual(f.head("main"), plan["candidate"]["commit"])
        rejected = raw_git(f.authority, "update-ref", "refs/heads/main", main0)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(f.head("main"), plan["candidate"]["commit"])
        self.assertTrue(self.promote(f, plan["plan_id"], run["receipt_id"])["repeated"])

    def test_gated_staging_promote_lands_through_the_hook(self):
        f = self.make(gated_policy(staging="full", checks=FAST))
        self.ready(f)
        self.protect(f)
        tv.ok(self, f.call("prepare", "b1", ["ledger"], str(f.authority)))
        self.lane(f, "b1", "ledger", "ledger-b1", {"ledger.txt": "total = 7\n"})
        tv.ok(self, f.call("assemble", "b1", "ledger"))
        tv.ok(self, f.call("freeze", "b1", "ledger"))
        staged = tv.ok(self, f.call("stage", "b1", ["ledger"]), "pending")
        candidate = staged["candidates"][0]
        self.assertNotEqual(f.head("dev"), candidate["commit"])
        plan = tv.ok(self, tv.call(f, verifier.plan, "b1", "staging", candidate["id"]))
        run = tv.ok(self, tv.call(f, runner.run, plan["plan_id"]))
        landed = self.promote(f, plan["plan_id"], run["receipt_id"])
        self.assertEqual(landed["state"], "landed")
        self.assertEqual(f.head("dev"), candidate["commit"])

    def test_hook_observes_journaled_intent_at_write_time(self):
        f = self.make(gated_policy())
        self.ready(f)
        self.protect(f)
        self.staged(f)
        plan, run = self.release(f)
        main0 = f.head("main")

        def sabotage(point, info):
            if point == "before-write":
                with f.context() as other:
                    other.store.connection.execute("DELETE FROM promotions WHERE id = ?", (info["promotion"],))
        refused = self.promote(f, plan["plan_id"], run["receipt_id"], "stale", hook=sabotage)
        self.assertEqual(refused.data["state"], "not-applied")
        self.assertEqual(f.head("main"), main0)
        landed = self.promote(f, plan["plan_id"], run["receipt_id"])
        self.assertEqual(landed["state"], "landed")
        self.assertEqual(f.head("main"), plan["candidate"]["commit"])

    def test_missing_or_modified_hook_blocks_promote(self):
        for corrupt in ("missing", "modified"):
            with self.subTest(corrupt=corrupt):
                f = self.make(gated_policy())
                self.ready(f)
                self.protect(f)
                self.staged(f)
                plan, run = self.release(f)
                hook = f.authority / "hooks" / "reference-transaction"
                if corrupt == "missing":
                    hook.unlink()
                else:
                    hook.write_text("#!/bin/sh\nexit 0\n")
                probe = enforcement.probe(f.state, f.authority)
                self.assertFalse(probe["implemented"])
                self.assertEqual(probe["hooks"]["reference-transaction"], corrupt)
                blocked = self.promote(f, plan["plan_id"], run["receipt_id"], "blocked")
                self.assertEqual(blocked.code, "unprotected-authority")
                landed = self.promote(f, plan["plan_id"], run["receipt_id"], allow_unprotected=True)
                self.assertEqual(landed["state"], "landed")
                self.assertEqual(f.head("main"), plan["candidate"]["commit"])
        f = self.make(gated_policy())
        self.ready(f)
        self.protect(f)
        (f.authority / "hooks" / "update").unlink()
        again = self.protect(f)
        self.assertEqual(again["hooks"]["update"], "installed")
        self.assertTrue(enforcement.probe(f.state, f.authority)["implemented"])

    def test_unprotected_authority_is_unchanged(self):
        f = self.make(gated_policy())
        self.ready(f)
        self.staged(f)
        plan, run = self.release(f)
        landed = self.promote(f, plan["plan_id"], run["receipt_id"])
        self.assertEqual(landed["state"], "landed")
        direct = raw_git(f.authority, "update-ref", "refs/heads/dev", f.head("main"))
        self.assertEqual(direct.returncode, 0, direct.stderr)
        self.assertEqual(f.head("dev"), f.head("main"))
        probe = enforcement.probe(f.state, f.authority)
        self.assertFalse(probe["recorded"])
        self.assertFalse(probe["implemented"])

    def test_check_intent_paths(self):
        f = self.make(gated_policy())
        self.ready(f)
        self.protect(f)
        dev = f.head("dev")
        allowed = self.intent(f, "refs/heads/topic", "0" * 40, dev)
        self.assertTrue(allowed["allowed"])
        self.assertFalse(allowed["protected"])
        refused = self.intent(f, "refs/heads/dev", dev, f.head("main"))
        self.assertEqual((refused.status, refused.code), ("blocked", "ref-update-refused"))
        self.assertEqual(self.intent(f, "dev", dev, dev).status, "invalid")
        self.assertEqual(self.intent(f, "refs/heads/dev", dev, "xyz").status, "invalid")
        with self.assertRaises(batches.Failure) as raised:
            enforcement.check_intent(None, f.authority, "refs/heads/dev", dev, dev)
        self.assertEqual(raised.exception.status, "invalid")
        # a state dir without a database refuses everything, even foreign refs
        code, out = cli("--json", "authority", "check-intent", "--state-dir", str(f.root / "nostate"), "--authority", str(f.authority), "refs/heads/topic", "0" * 40, dev)
        self.assertNotEqual(code, 0)
        self.assertEqual(out["error"]["code"], "ref-update-refused")


if __name__ == "__main__":
    unittest.main()
