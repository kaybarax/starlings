import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import unittest

from fixtures import Mirror, git
import test_verifier as tv
from test_upstream import MirrorCase
from starlings import promotion, release, upstream, verifier

SRC = Path(__file__).resolve().parent.parent / "src"
FAIL = ["python3", "-c", "import sys; sys.exit(3)"]
KILLED = ["python3", "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]


class ReleaseCase(MirrorCase):
    def release(self, m, mode=None, expected="ok", hook=None, **kwargs):
        return self.call(m, release.release, None, mode, expected=expected, hook=hook, **kwargs)

    def landed(self, m):
        with m.context() as ctx:
            return [dict(row) for row in ctx.store.connection.execute("SELECT * FROM promotions WHERE state = 'landed' ORDER BY created")]


class EndToEndTests(unittest.TestCase):
    """A project that holds only starlings.toml and a one-line check releases with no project code."""

    def cli(self, m, *args, cwd=None):
        home = m.root / "home"
        home.mkdir(exist_ok=True)
        env = {key: value for key, value in os.environ.items() if not key.startswith(("GIT_", "STARLINGS_"))}
        env.update({"PYTHONPATH": str(SRC), "STARLINGS_STATE_DIR": str(m.state), "HOME": str(home), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})
        result = subprocess.run([sys.executable, "-B", "-m", "starlings", *args], capture_output=True, text=True, env=env, cwd=cwd or m.project, timeout=300, check=False)
        return result.returncode, result.stdout, result.stderr

    def test_release_publish_from_a_policy_and_one_check(self):
        m = Mirror()
        self.addCleanup(m.close)
        feature = m.commit({"feature.txt": "one\n"})
        self.assertEqual(sorted(path.name for path in m.project.iterdir()), ["starlings.toml"])
        code, out, err = self.cli(m, "--json", "release", "--publish")
        self.assertEqual(code, 5, out + err)
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], "untrusted-policy")
        command = error["data"]["detail"]["trust_command"]
        self.assertIn(command, error["message"])
        self.assertIn("BLOCKED (exit 5)", error["message"])
        self.assertIn(f"--commit {feature}", command)
        self.assertEqual(m.head("main"), git(m.upstream, "rev-parse", "refs/heads/main"))
        argv = shlex.split(command.replace("<review-reference>", "review:policy"))
        self.assertEqual(argv[0], "starlings")
        code, out, err = self.cli(m, *argv[1:])
        self.assertEqual(code, 0, out + err)
        code, out, err = self.cli(m, "release", "--publish")
        self.assertEqual(code, 5, out + err)
        self.assertIn("no-signer", err)
        self.assertIn("starlings signer init", err)
        self.assertEqual(self.cli(m, "signer", "init")[0], 0)
        before = m.head("main")
        code, out, err = self.cli(m, "release", "--publish")
        self.assertEqual(code, 0, out + err)
        published = m.head("main")
        self.assertNotEqual(published, before)
        self.assertEqual(m.head("dev"), published)
        self.assertEqual(git(m.upstream, "rev-list", "--parents", "-n", "1", published).split()[1:], [before, feature])
        self.assertIn(f"published: upstream dev and main at {published}", out)
        self.assertIn("unit: passed (exit 0)", out)
        self.assertEqual(sorted(path.name for path in m.project.iterdir()), ["starlings.toml"])
        code, out, err = self.cli(m, "release", "--publish")
        self.assertEqual(code, 0, out + err)
        self.assertIn("Nothing to release", out)
        code, out, err = self.cli(m, "--json", "authority", "sync")
        self.assertEqual((code, json.loads(out)["data"]["state"]), (0, "synced"))
        self.assertFalse((m.root / "home" / ".local").exists())


class ReleaseFlowTests(ReleaseCase):
    def test_dry_run_receipt_is_reused_then_promote_then_publish(self):
        m = self.make()
        m.commit({"feature.txt": "one\n"})
        self.ready(m)
        main = m.head("main")
        dry = self.release(m, "dry-run")
        self.assertEqual((dry["state"], dry["reused"], dry["steps"]), ("verified", False, ["sync", "prepare", "plan", "run", "check"]))
        self.assertEqual((m.head("main"), m.head("main", m.authority)), (main, main))
        self.assertRegex(dry["batch"], r"^release-[0-9a-f]{12}-[0-9a-f]{12}$")
        promoted = self.release(m)
        self.assertEqual((promoted["state"], promoted["reused"], promoted["receipt_id"], promoted["batch"]), ("promoted", True, dry["receipt_id"], dry["batch"]))
        self.assertEqual((m.head("main"), m.head("main", m.authority)), (main, promoted["commit"]))
        self.assertIn("waits for starlings release --publish", promoted["summary"][-1])
        earlier = self.release(m, "dry-run")
        self.assertEqual((earlier["state"], earlier["steps"]), ("unpublished", ["sync"]))
        published = self.release(m, "publish")
        self.assertEqual((published["state"], published["steps"], published["commit"]), ("published", ["sync", "publish"], promoted["commit"]))
        self.assertEqual((m.head("main"), m.head("dev")), (promoted["commit"], promoted["commit"]))
        self.assertEqual(self.release(m, "publish")["state"], "nothing-to-release")
        self.assertEqual(len(self.landed(m)), 1)

    def test_staging_ahead_stops_before_anything_is_planned(self):
        m = self.make()
        m.commit({"feature.txt": "one\n"})
        self.ready(m)
        before = m.head("dev", m.authority)
        local = git(m.authority, "commit-tree", before + "^{tree}", "-p", before, "-m", "local only")
        git(m.authority, "update-ref", "refs/heads/dev", local, before)
        refused = self.release(m, "publish", "blocked")
        self.assertEqual((refused.code, refused.data["stopped_at"], refused.data["steps"]), ("staging-ahead", "sync", []))
        self.assertIn("BLOCKED (exit 5)", refused.message)
        self.assertEqual(m.head("main"), m.head("main", m.authority))

    def test_upstream_moving_during_the_run_promotes_and_publishes_nothing(self):
        for branch in ("dev", "main"):
            with self.subTest(moved=branch):
                m = self.make()
                m.commit({"feature.txt": "one\n"})
                self.ready(m)
                before = {"main": m.head("main"), "dev": m.head("dev")}
                moved = {}

                def move(point, info):
                    if point == "release-before-promote":
                        moved[branch] = m.commit({"late.txt": "late\n"}, branch=branch)
                refused = self.release(m, "publish", "stale", hook=move)
                self.assertEqual((refused.code, refused.data["stopped_at"]), ("upstream-moved", "reread"))
                self.assertIn("STALE (exit 6)", refused.message)
                self.assertIn("Nothing was promoted or published", refused.message)
                self.assertEqual((m.head("main"), m.head("dev")), tuple({**before, **moved}[name] for name in ("main", "dev")))
                self.assertEqual(m.head("main", m.authority), before["main"])
                self.assertEqual(self.landed(m), [])
                again = self.release(m, "publish")
                self.assertEqual((again["state"], again["reused"]), ("published", False))
                self.assertEqual(m.head("main"), again["commit"])

    def test_failed_and_signal_killed_checks_never_promote(self):
        for argv, ended in ((FAIL, "exit 3"), (KILLED, "killed by signal 9")):
            with self.subTest(ended=ended):
                m = self.make(checks={"unit": argv})
                m.commit({"feature.txt": "one\n"})
                self.ready(m)
                main = m.head("main")
                refused = self.release(m, "publish", "failed")
                self.assertEqual((refused.code, refused.data["stopped_at"]), ("checks-failed", "run"))
                self.assertIn(f"unit: failed ({ended})", refused.message)
                self.assertIn("FAILED (exit 7)", refused.message)
                self.assertEqual((m.head("main"), m.head("main", m.authority)), (main, main))

    def test_signal_stops_before_the_next_step(self):
        m = self.make()
        m.commit({"feature.txt": "one\n"})
        self.ready(m)
        event = threading.Event()
        event.set()
        refused = self.release(m, "publish", "failed", interrupt=event)
        self.assertEqual(refused.code, "interrupted")
        self.assertIn("INTERRUPTED", refused.message)
        self.assertIn("Nothing was promoted or published", refused.message)
        self.assertEqual(m.head("main"), m.head("main", m.authority) or m.head("main"))

    def test_unpublishable_promotion_is_a_dead_end_until_discarded(self):
        m = self.make()
        m.commit({"feature.txt": "one\n"})
        self.ready(m)
        first = self.release(m)
        m.commit({"late.txt": "late\n"})
        refused = self.release(m, "publish", "blocked")
        self.assertEqual((refused.code, refused.data["stopped_at"]), ("release-ahead", "sync"))
        self.assertIn("starlings authority discard", refused.message)
        self.assertNotEqual(m.head("main"), first["commit"])
        done = self.call(m, upstream.discard)
        self.assertEqual([item["commit"] for item in done["discarded"]], [first["commit"]])
        second = self.release(m, "publish")
        self.assertEqual(second["state"], "published")
        self.assertNotEqual(second["commit"], first["commit"])
        self.assertNotEqual(second["batch"], first["batch"])
        self.assertEqual(m.head("main"), second["commit"])
        self.assertFalse(tv.git(m.upstream, "branch", "--contains", first["commit"], check=False))

    def test_releasing_the_same_heads_again_restores_the_set_aside_promotion(self):
        m = self.make()
        m.commit({"feature.txt": "one\n"})
        self.ready(m)
        first = self.release(m)
        self.call(m, upstream.discard, force=True)
        self.assertEqual(m.head("main", m.authority), m.head("main"))
        again = self.release(m, "publish")
        self.assertEqual((again["state"], again["commit"], again["reused"]), ("published", first["commit"], True))
        self.assertIn("restore", again["steps"])
        self.assertEqual((m.head("main"), m.head("dev")), (first["commit"], first["commit"]))
        self.assertEqual(len(self.landed(m)), 1)

    def test_hook_protected_authority_releases_restores_and_refuses_direct_writes(self):
        from starlings import enforcement
        m = self.make()
        m.commit({"feature.txt": "one\n"})
        self.ready(m)
        protected = self.call(m, enforcement.protect, str(m.authority))
        self.assertEqual(protected["protected_refs"], ["refs/heads/dev", "refs/heads/main"])
        main = m.head("main", m.authority)
        stray = git(m.authority, "commit-tree", main + "^{tree}", "-p", main, "-m", "stray")
        self.assertIn("ref-update-refused", self.raw_update(m, "refs/heads/main", stray, main))
        first = self.release(m)
        self.assertEqual(m.head("main", m.authority), first["commit"])
        self.call(m, upstream.discard, force=True)
        self.assertEqual(m.head("main", m.authority), main)
        again = self.release(m, "publish")
        self.assertIn("restore", again["steps"])
        self.assertEqual((m.head("main"), m.head("dev"), m.head("dev", m.authority)), (first["commit"],) * 3)
        m.commit({"next.txt": "two\n"})
        self.assertEqual(self.release(m, "publish")["state"], "published")
        self.assertEqual(m.head("main", m.authority), m.head("main"))

    def raw_update(self, m, ref, new, old):
        import subprocess
        from fixtures import environment
        result = subprocess.run(["git", "-C", str(m.authority), "update-ref", ref, new, old], capture_output=True, text=True, env={**environment(), "PYTHONPATH": str(SRC)}, check=False)
        self.assertNotEqual(result.returncode, 0)
        return result.stderr + result.stdout

    def test_policy_change_on_staging_names_the_superseding_trust_command(self):
        m = self.make()
        self.ready(m)
        with m.context() as ctx:
            trusted = verifier.current_trust(ctx.store.connection, str(m.authority))["digest"]
        head = m.commit({"starlings.toml": m.policy + "\n# reviewed change\n"})
        refused = self.release(m, "dry-run", "blocked")
        self.assertEqual(refused.code, "untrusted-policy")
        command = refused.data["detail"]["trust_command"]
        self.assertIn(f"--commit {head}", command)
        self.assertTrue(command.endswith(f"--supersedes {trusted}"))


NEEDS = ["python3", "-c", "import json, sys; sys.exit(0 if any(a['review'] == 'exception:1' for a in json.load(open(sys.argv[1]))['approvals']) else 1)", "{context}"]


class ConsumedApprovalTests(ReleaseCase):
    def verified(self, clock=None):
        m = self.make(checks={"unit": NEEDS})
        m.clock = clock
        m.commit({"feature.txt": "one\n"})
        self.ready(m)
        self.assertEqual(self.release(m, "dry-run", "failed").code, "checks-failed")
        dry = self.release(m, "dry-run", approve="exception:1", by="alice")
        self.assertEqual((dry["state"], dry["approval"]["review"]), ("verified", "exception:1"))
        return m, dry

    def test_revoked_approval_stops_the_promotion(self):
        m, dry = self.verified()
        approval = dry["approval"]["approval_id"]
        checked = self.call(m, verifier.check, dry["receipt_id"])
        self.assertEqual(checked["consumed_approvals"], [{"id": approval, "by": "alice", "review": "exception:1", "live": True}])
        main = m.head("main")

        def revoke(point, info):
            if point == "release-before-promote":
                self.call(m, promotion.revoke, approval)
        refused = self.release(m, "publish", "stale", hook=revoke)
        self.assertEqual((refused.code, refused.data["stopped_at"]), ("stale-receipt", "promote"))
        self.assertIn(approval, refused.message)
        self.assertIn("no longer live", refused.message)
        self.assertEqual((m.head("main"), m.head("main", m.authority)), (main, main))
        self.assertEqual(self.call(m, verifier.check, dry["receipt_id"], expected="stale")["consumed_approvals"][0]["live"], False)
        self.assertEqual(self.call(m, promotion.promote, dry["plan_id"], dry["receipt_id"], expected="stale").code, "stale-receipt")
        rerun = self.release(m, "publish", "failed")
        self.assertEqual(rerun.code, "checks-failed")
        again = self.release(m, "publish", approve="exception:1", by="alice")
        self.assertEqual((again["state"], again["reused"]), ("published", True))
        self.assertEqual(m.head("main"), again["commit"])

    def test_expired_approval_stops_until_it_is_granted_again(self):
        import time
        from test_promotion import Clock
        clock = Clock(time.time())
        m, dry = self.verified(clock)
        clock.advance(86401)
        refused = self.call(m, promotion.promote, dry["plan_id"], dry["receipt_id"], expected="stale")
        self.assertEqual(refused.code, "stale-receipt")
        self.call(m, promotion.approve, dry["plan_id"], "bob", 600, "exception:1")
        self.assertEqual(self.call(m, promotion.promote, dry["plan_id"], dry["receipt_id"], expected="stale").code, "stale-receipt")
        self.call(m, promotion.approve, dry["plan_id"], "alice", 600, "exception:1")
        self.assertEqual(self.call(m, promotion.promote, dry["plan_id"], dry["receipt_id"])["state"], "landed")

    def test_revocation_after_landing_stops_the_publish(self):
        m, dry = self.verified()
        promoted = self.release(m)
        self.assertEqual(promoted["state"], "promoted")
        self.call(m, promotion.revoke, dry["approval"]["approval_id"])
        main = m.head("main")
        refused = self.release(m, "publish", "blocked")
        self.assertEqual(refused.code, "release-ahead")
        self.assertEqual(self.call(m, upstream.publish, expected="blocked").code, "unverified-release")
        self.assertEqual(m.head("main"), main)

    def test_checks_that_ignore_the_context_consume_nothing(self):
        m = self.make()
        m.commit({"feature.txt": "one\n"})
        self.ready(m)
        dry = self.release(m, "dry-run", approve="note:1", by="alice")
        self.call(m, promotion.revoke, dry["approval"]["approval_id"])
        self.assertEqual(self.call(m, verifier.check, dry["receipt_id"])["consumed_approvals"], [])
        self.assertEqual(self.release(m, "publish")["state"], "published")


if __name__ == "__main__":
    unittest.main()
