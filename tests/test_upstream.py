import stat
import subprocess
import unittest
from unittest.mock import patch

from fixtures import Mirror, git, mirror_policy
import test_verifier as tv
from starlings import batches, promotion, runner, signer, upstream, verifier
from starlings.policy import PolicyError, parse, preset, validate


class MirrorCase(unittest.TestCase):
    def make(self, **options):
        mirror = Mirror(**options)
        self.addCleanup(mirror.close)
        return mirror

    def call(self, m, function, *args, expected="ok", hook=None, **kwargs):
        with m.context(hook=hook) as ctx:
            try:
                outcome = function(ctx, *args, **kwargs)
            except batches.Failure as exc:
                outcome = (exc.status, exc)
        return tv.ok(self, outcome, expected)

    def sync(self, m, expected="ok"):
        return self.call(m, upstream.sync, expected=expected)

    def ready(self, m):
        self.sync(m)
        self.call(m, verifier.trust, str(m.authority), m.head("dev"), "review:policy")
        with m.context() as ctx:
            signer.init(ctx.root)

    def land(self, m, batch="r1"):
        """Plan, run and promote the release of the synced heads; the authority is then ahead of the upstream."""
        self.call(m, batches.prepare, batch, ["release"], str(m.authority))
        plan = self.call(m, verifier.plan, batch, "release")
        run = self.call(m, runner.run, plan["plan_id"])
        landed = self.call(m, promotion.promote, plan["plan_id"], run["receipt_id"])
        self.assertEqual(m.head("main", m.authority), plan["candidate"]["commit"])
        return plan, run, landed

    def promoted(self, m):
        m.commit({"feature.txt": "one\n"})
        self.ready(m)
        return self.land(m)


class SyncTests(MirrorCase):
    def test_sync_creates_and_follows_the_upstream(self):
        m = self.make()
        self.assertFalse(m.authority.exists())
        first = self.sync(m)
        self.assertTrue(first["first_use"])
        self.assertEqual(first["state"], "synced")
        self.assertEqual([item["branch"] for item in first["updated"]], ["main", "dev"])
        self.assertEqual((m.head("main", m.authority), m.head("dev", m.authority)), (m.head("main"), m.head("dev")))
        self.assertEqual(self.sync(m)["updated"], [])
        moved = m.commit({"feature.txt": "one\n"})
        again = self.sync(m)
        self.assertEqual(again["updated"], [{"branch": "dev", "old": first["upstream_heads"]["dev"], "new": moved}])
        self.assertEqual(m.head("dev", m.authority), moved)
        self.assertEqual(git(m.authority, "rev-parse", "refs/upstream/dev"), moved)

    def test_staging_ahead_of_the_upstream_is_refused(self):
        m = self.make()
        self.sync(m)
        before = m.head("dev", m.authority)
        local = git(m.authority, "commit-tree", before + "^{tree}", "-p", before, "-m", "local only")
        git(m.authority, "update-ref", "refs/heads/dev", local, before)
        m.commit({"feature.txt": "one\n"})
        refused = self.sync(m, "blocked")
        self.assertEqual(refused.code, "staging-ahead")
        self.assertIn("starlings authority discard", refused.message)
        self.assertEqual((m.head("dev", m.authority), m.head("main", m.authority)), (local, m.head("main")))

    def test_release_ahead_needs_a_recorded_valid_promotion(self):
        m = self.make()
        self.sync(m)
        before = m.head("main", m.authority)
        stray = git(m.authority, "commit-tree", before + "^{tree}", "-p", before, "-m", "stray")
        git(m.authority, "update-ref", "refs/heads/main", stray, before)
        self.assertEqual(self.sync(m, "blocked").code, "release-ahead")
        self.assertEqual(m.head("main", m.authority), stray)

    def test_unpublished_promotion_is_recognised_until_its_receipt_breaks(self):
        m = self.make()
        plan, run, landed = self.promoted(m)
        state = self.sync(m)
        self.assertEqual(state["state"], "unpublished")
        self.assertEqual(state["unpublished"], {"commit": plan["candidate"]["commit"], "promotion_id": landed["promotion_id"], "plan_id": plan["plan_id"], "receipt_id": run["receipt_id"]})
        self.assertEqual(m.head("main", m.authority), plan["candidate"]["commit"])
        (m.state / "logs" / run["run_id"] / "unit.out").write_text("edited")
        self.assertEqual(self.sync(m, "blocked").code, "release-ahead")

    def test_first_use_reads_the_local_manifest_then_the_trusted_policy_decides(self):
        m = self.make()
        self.ready(m)
        other = m.root / "other.git"
        git(m.root, "clone", "--quiet", "--bare", str(m.upstream), str(other))
        (m.project / "starlings.toml").write_text(mirror_policy(other))
        differing = self.sync(m)
        self.assertEqual(differing["upstream"], str(m.upstream))
        self.assertIn("the trusted policy decides", differing["note"])
        (m.project / "starlings.toml").unlink()
        matching = self.sync(m)
        self.assertEqual(matching["upstream"], str(m.upstream))
        self.assertNotIn("note", matching)

    def test_local_bare_policy_has_no_upstream(self):
        m = self.make()
        policy = preset("release-only")
        from starlings.policy import render
        (m.project / "starlings.toml").write_text(render(policy))
        self.assertEqual(self.sync(m, "blocked").code, "not-mirror")
        self.assertFalse(m.authority.exists())


class PublishTests(MirrorCase):
    def test_publish_pushes_release_and_staging_together(self):
        m = self.make()
        plan, run, landed = self.promoted(m)
        commit = plan["candidate"]["commit"]
        self.assertNotEqual(m.head("main"), commit)
        done = self.call(m, upstream.publish)
        self.assertEqual((done["published"], done["state"], done["receipt_id"]), (True, "published", run["receipt_id"]))
        self.assertEqual(done["refspecs"], [f"{commit}:refs/heads/main", f"{commit}:refs/heads/dev"])
        self.assertEqual((m.head("main"), m.head("dev")), (commit, commit))
        self.assertEqual(m.head("dev", m.authority), commit)
        again = self.call(m, upstream.publish)
        self.assertEqual((again["published"], again["state"]), (False, "up-to-date"))
        self.assertEqual(self.sync(m)["state"], "synced")

    def test_publish_release_only_leaves_upstream_staging(self):
        m = self.make(publish_staging=False)
        plan, _, _ = self.promoted(m)
        staging = m.head("dev")
        done = self.call(m, upstream.publish)
        self.assertEqual(done["refspecs"], [f"{plan['candidate']['commit']}:refs/heads/main"])
        self.assertEqual((m.head("main"), m.head("dev")), (plan["candidate"]["commit"], staging))

    def test_publish_needs_a_trusted_policy_and_a_verified_release(self):
        m = self.make()
        m.commit({"feature.txt": "one\n"})
        self.sync(m)
        self.assertEqual(self.call(m, upstream.publish, expected="blocked").code, "untrusted-policy")
        self.call(m, verifier.trust, str(m.authority), m.head("dev"), "review:policy")
        before = m.head("main", m.authority)
        stray = git(m.authority, "commit-tree", before + "^{tree}", "-p", before, "-p", m.head("dev"), "-m", "stray")
        git(m.authority, "update-ref", "refs/heads/main", stray, before)
        upstream_main = m.head("main")
        self.assertEqual(self.call(m, upstream.publish, expected="blocked").code, "unverified-release")
        self.assertEqual(m.head("main"), upstream_main)

    def test_publish_retry_requires_a_live_minimum_approval(self):
        m = self.make(approvals=1)
        m.commit({"feature.txt": "one\n"})
        self.ready(m)
        self.call(m, batches.prepare, "approved-release", ["release"], str(m.authority))
        plan = self.call(m, verifier.plan, "approved-release", "release")
        approval = self.call(m, promotion.approve, plan["plan_id"], "reviewer", 86400, "review:release")
        run = self.call(m, runner.run, plan["plan_id"])
        self.call(m, verifier.check, run["receipt_id"])
        self.call(m, promotion.promote, plan["plan_id"], run["receipt_id"])
        self.call(m, promotion.revoke, approval["approval_id"])

        refused = self.call(m, upstream.publish, expected="blocked")
        self.assertEqual(refused.code, "unverified-release")
        self.assertNotEqual(m.head("main"), plan["candidate"]["commit"])
        discarded = self.call(m, upstream.discard)
        self.assertEqual(discarded["discarded"][0]["commit"], plan["candidate"]["commit"])

    def test_upstream_moved_after_promotion_is_stale_and_pushes_nothing(self):
        m = self.make()
        plan, _, _ = self.promoted(m)
        moved = m.commit({"late.txt": "late\n"})
        main = m.head("main")
        refused = self.call(m, upstream.publish, expected="stale")
        self.assertEqual(refused.code, "upstream-moved")
        self.assertIn("starlings authority discard", refused.message)
        self.assertEqual((m.head("main"), m.head("dev")), (main, moved))
        self.assertEqual(m.head("main", m.authority), plan["candidate"]["commit"])

    def test_upstream_moving_between_the_reread_and_the_push_is_refused_by_the_upstream(self):
        m = self.make()
        plan, _, _ = self.promoted(m)
        main = m.head("main")
        moved = {}

        def race(point, info):
            if point == "before-push":
                moved["dev"] = m.commit({"late.txt": "late\n"})

        refused = self.call(m, upstream.publish, expected="failed", hook=race)
        self.assertEqual(refused.code, "publish-unconfirmed")
        self.assertEqual((m.head("main"), m.head("dev")), (main, moved["dev"]))

    def test_publish_rejects_a_release_rewind_between_head_read_and_push(self):
        m = self.make()
        old_main = m.head("main")
        m.commit({"release.txt": "advance\n"}, branch="main")
        plan, _, _ = self.promoted(m)
        main = m.head("main")

        def race(point, info):
            if point == "before-push":
                git(m.upstream, "update-ref", "refs/heads/main", old_main, main)

        refused = self.call(m, upstream.publish, expected="failed", hook=race)
        self.assertEqual(refused.code, "publish-unconfirmed")
        self.assertEqual(m.head("main"), old_main)
        self.assertEqual(m.head("main", m.authority), plan["candidate"]["commit"])

    def test_release_only_publish_reports_a_staging_move_after_advertisement(self):
        m = self.make(publish_staging=False)
        plan, _, _ = self.promoted(m)
        dev = m.head("dev")
        moved = {}

        def race(point, info):
            if point == "before-push":
                moved["dev"] = m.commit({"late.txt": "late\n"})

        refused = self.call(m, upstream.publish, expected="failed", hook=race)
        self.assertEqual(refused.code, "publish-unconfirmed")
        self.assertEqual(m.head("main"), plan["candidate"]["commit"])
        self.assertEqual(m.head("dev"), moved["dev"])
        self.assertNotEqual(moved["dev"], dev)
        self.assertEqual(m.head("main", m.authority), plan["candidate"]["commit"])
        with m.context() as ctx:
            self.assertEqual(ctx.store.connection.execute("SELECT state FROM authority_journal WHERE operation = 'publish' ORDER BY created DESC LIMIT 1").fetchone()[0], "ambiguous")

    def test_a_refused_branch_fails_the_whole_atomic_push(self):
        m = self.make()
        plan, _, _ = self.promoted(m)
        main, dev = m.head("main"), m.head("dev")
        hook = m.upstream / "hooks" / "update"
        hook.write_text('#!/bin/sh\n[ "$1" = refs/heads/dev ] && { echo "dev is closed" >&2; exit 1; }\nexit 0\n')
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
        refused = self.call(m, upstream.publish, expected="failed")
        self.assertEqual(refused.code, "publish-refused")
        self.assertIn("neither branch was updated", refused.message)
        self.assertEqual((m.head("main"), m.head("dev")), (main, dev))
        self.assertEqual(m.head("main", m.authority), plan["candidate"]["commit"])
        hook.unlink()
        self.assertTrue(self.call(m, upstream.publish)["published"])
        self.assertEqual(m.head("main"), plan["candidate"]["commit"])

    def test_publish_confirms_remote_success_after_lost_push_acknowledgement(self):
        m = self.make()
        plan, _, _ = self.promoted(m)
        original = upstream.run

        def lost_ack(authority, *args):
            result = original(authority, *args)
            if args and args[0] == "push":
                return subprocess.CompletedProcess(["git", "push"], 1, "", "connection closed after update")
            return result

        with patch.object(upstream, "run", side_effect=lost_ack):
            done = self.call(m, upstream.publish)
        self.assertTrue(done["published"])
        self.assertEqual(m.head("main"), plan["candidate"]["commit"])
        with m.context() as ctx:
            row = ctx.store.connection.execute("SELECT state FROM authority_journal WHERE operation = 'publish' ORDER BY created DESC LIMIT 1").fetchone()
            self.assertEqual(row["state"], "complete")


class DiscardTests(MirrorCase):
    def test_mirror_writes_are_journalled(self):
        m = self.make()
        self.sync(m)
        with m.context() as ctx:
            self.assertEqual({row["operation"] for row in ctx.store.connection.execute("SELECT operation FROM authority_journal")}, {"sync"})
        self.promoted(m)
        self.call(m, upstream.publish)
        m.commit({"next.txt": "next\n"})
        self.sync(m)
        before = m.head("main", m.authority)
        stray = git(m.authority, "commit-tree", before + "^{tree}", "-p", before, "-m", "local stray")
        git(m.authority, "update-ref", "refs/heads/main", stray, before)
        self.call(m, upstream.discard)
        with m.context() as ctx:
            rows = ctx.store.connection.execute("SELECT operation, state FROM authority_journal").fetchall()
            self.assertEqual({row["operation"] for row in rows}, {"sync", "publish", "discard"})
            self.assertTrue(all(row["state"] == "complete" for row in rows))

    def test_discard_refuses_a_publishable_promotion_until_the_upstream_moves(self):
        m = self.make()
        plan, _, _ = self.promoted(m)
        commit = plan["candidate"]["commit"]
        refused = self.call(m, upstream.discard, expected="blocked")
        self.assertEqual(refused.code, "publishable")
        self.assertEqual(m.head("main", m.authority), commit)
        moved = m.commit({"late.txt": "late\n"})
        self.assertEqual(self.sync(m, "blocked").code, "release-ahead")
        done = self.call(m, upstream.discard)
        self.assertEqual(done["discarded"], [{"branch": "main", "commit": commit, "kept_at": f"refs/discarded/{commit}", "now": m.head("main")}])
        self.assertEqual(git(m.authority, "rev-parse", f"refs/discarded/{commit}"), commit)
        self.assertEqual(m.head("main", m.authority), m.head("main"))
        self.assertEqual(self.call(m, upstream.discard)["discarded"], [])
        self.assertEqual(self.sync(m)["authority_heads"], {"main": m.head("main"), "dev": moved})

    def test_force_sets_aside_a_publishable_promotion_and_staging(self):
        m = self.make()
        plan, _, _ = self.promoted(m)
        commit = plan["candidate"]["commit"]
        done = self.call(m, upstream.discard, force=True)
        self.assertEqual([item["commit"] for item in done["discarded"]], [commit])
        self.assertEqual(m.head("main", m.authority), m.head("main"))
        before = m.head("dev", m.authority)
        local = git(m.authority, "commit-tree", before + "^{tree}", "-p", before, "-m", "local only")
        git(m.authority, "update-ref", "refs/heads/dev", local, before)
        done = self.call(m, upstream.discard)
        self.assertEqual(done["discarded"], [{"branch": "dev", "commit": local, "kept_at": f"refs/discarded/{local}", "now": before}])


class SafetyTests(unittest.TestCase):
    def test_credentials_are_redacted_and_refused_in_the_policy(self):
        self.assertEqual(upstream.redact("fatal: https://user:secret@host.example/x.git not found"), "fatal: https://***@host.example/x.git not found")
        self.assertEqual(upstream.redact("ssh://token@host.example/x"), "ssh://***@host.example/x")
        policy = preset("release-only")
        for value in ("https://user:secret@host.example/x.git", "https://token@host.example/x.git", "ext::sh -c evil", "ext::evil", "--upload-pack=evil", "relative/path", "http://host.example/x.git", ""):
            with self.subTest(value=value), self.assertRaises(PolicyError):
                validate({**policy, "authority": {"mode": "mirror", "upstream": value}})
        for value in ("/srv/git/x.git", "https://host.example/x.git", "ssh://git@host.example/x.git", "git@host.example:team/x.git", "file:///srv/git/x.git"):
            with self.subTest(value=value):
                validate({**policy, "authority": {"mode": "mirror", "upstream": value}})
        for table in ({"mode": "mirror"}, {"mode": "local-bare", "upstream": "/srv/x.git"}, {"mode": "mirror", "upstream": "/srv/x.git", "publish_staging": 1}):
            with self.subTest(table=table), self.assertRaises(PolicyError):
                validate({**policy, "authority": table})
        self.assertEqual(parse(mirror_policy("/srv/x.git"))["authority"], {"mode": "mirror", "upstream": "/srv/x.git", "publish_staging": True})

    def test_network_git_never_prompts_or_forces(self):
        env = upstream.environment()
        self.assertEqual((env["GIT_TERMINAL_PROMPT"], env["GIT_ALLOW_PROTOCOL"]), ("0", "file:https:ssh"))
        import inspect
        source = inspect.getsource(upstream)
        self.assertNotIn('"--force"', source)
        self.assertNotIn('"+{', source)
        self.assertIn('"push", "--atomic"', source)
        from starlings import gitops
        self.assertNotIn("https", " ".join(gitops.CONFIG))


if __name__ == "__main__":
    unittest.main()
