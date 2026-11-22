import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from fixtures import Fixture, git
from starlings import batches, gitops
from starlings.cli import main
from starlings.contracts import Assembly, Candidate
from starlings.policy import PolicyError, parse, preset, render, validate
from starlings.store import LeaseHeld, LeaseLost, Store


class Clock:
    def __init__(self, value=1_800_000_000.0):
        self.value = value
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def advance(self, seconds):
        with self.lock:
            self.value += seconds


def ok(testcase, outcome, expected="ok"):
    status, data = outcome
    testcase.assertEqual(status, expected, getattr(data, "message", data))
    return data


class PolicyAssemblyTests(unittest.TestCase):
    def test_excluded_globs(self):
        policy = preset("release-only")
        self.assertNotIn("assembly", policy)
        self.assertNotIn("[assembly]", render(policy))
        policy["assembly"] = {"excluded": [".github/workflows/*", "secrets/**"]}
        self.assertEqual(parse(render(policy)), policy)
        policy["assembly"] = {}
        self.assertEqual(parse(render(policy)), policy)
        for value in ({"excluded": "x"}, {"excluded": ["/abs"]}, {"excluded": ["a", "a"]}, {"excluded": [""]}, {"excluded": [" a"]}, {"other": []}, []):
            with self.subTest(value=value), self.assertRaises(PolicyError):
                data = preset("release-only")
                data["assembly"] = value
                validate(data)


class AssemblyRecordTests(unittest.TestCase):
    def test_identity_and_validation(self):
        candidate = Candidate("repo", "/authority.git", "refs/heads/dev", "a" * 40, ("a" * 40, "b" * 40), "c" * 40, "d" * 40)
        record = Assembly(candidate.id, "demo", ("ledger",), (("totals", "ledger", "e" * 40, "review-1", (("seed", "f" * 40),)),), (("ledger", "totals", "1" * 40, "review-2"),))
        again = Assembly(candidate.id, "demo", ("ledger",), (("totals", "ledger", "e" * 40, "review-1", (("seed", "f" * 40),)),), (("ledger", "totals", "1" * 40, "review-2"),))
        self.assertEqual(record.id, again.id)
        self.assertTrue(record.id.startswith("assembly:"))
        self.assertEqual(record.to_dict()["schema"], 1)
        self.assertNotEqual(record.id, Assembly(candidate.id, "demo", ("ledger",), (), ()).id)
        for args in [("bad", "demo", ("ledger",), (), ()), (candidate.id, "Demo", ("ledger",), (), ()), (candidate.id, "demo", (), (), ()), (candidate.id, "demo", ("ledger",), (("t", "other", "e" * 40, "r", ()),), ()), (candidate.id, "demo", ("ledger",), (("t", "ledger", "e" * 40, "r", ()),) * 2, ()), (candidate.id, "demo", ("ledger",), (), (("ledger", "t", "1" * 40, "r"),))]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                Assembly(*args)


class StoreTests(unittest.TestCase):
    def test_lease_expiry_takeover_refuses_old_owner(self):
        fixture = Fixture()
        self.addCleanup(fixture.close)
        clock = Clock()
        fixture.context(clock=clock).close()
        with Store(fixture.state / "starlings.db", clock) as store:
            first = store.acquire("target:x", "a", 10)
            with self.assertRaises(LeaseHeld):
                store.acquire("target:x", "b", 10)
            clock.advance(10)
            second = store.acquire("target:x", "b", 10)
            self.assertTrue(second["taken_over"])
            with self.assertRaises(LeaseLost):
                store.release(first)
            with store.transaction() as c, self.assertRaises(LeaseLost):
                store.check(c, first)
            store.release(second)
            self.assertEqual(store.connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(store.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(store.connection.execute("SELECT version FROM schema_version").fetchall()[0][0], 1)
        self.assertEqual((fixture.state.stat().st_mode & 0o777), 0o700)
        self.assertEqual(((fixture.state / "starlings.db").stat().st_mode & 0o777), 0o600)

    def test_old_git_is_diagnosed(self):
        class Result:
            stdout = "git version 2.37.1"
        with patch("starlings.gitops.git", return_value=Result()), self.assertRaisesRegex(ValueError, "2.38"):
            gitops.version()


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)

    def prepare(self, fixture=None, lanes=("ledger", "reports")):
        fixture = fixture or self.fixture
        return ok(self, fixture.call("prepare", "b1", list(lanes), str(fixture.authority)))

    def single_lane(self, fixture, content="beta-ledger"):
        self.prepare(fixture, ("ledger",))
        work = ok(self, fixture.call("assign", "b1", "solo", "ledger", "worker-a", []))
        head = fixture.work(work["worktree"], {"shared.txt": f"alpha\n{content}\ngamma\n"}, "solo")
        ok(self, fixture.call("handoff", "b1", "solo", head, "review:solo", []))
        ok(self, fixture.call("assemble", "b1", "ledger"))
        ok(self, fixture.call("freeze", "b1", "ledger"))
        return head

    def test_two_lane_batch(self):
        f = self.fixture
        main_before = f.head("main")
        batch = self.prepare()
        self.assertEqual(batch["base"], f.head("dev"))
        self.assertEqual(batch["policy_trust"], "untrusted")
        self.assertFalse(batch["repeated"])
        self.assertTrue(self.prepare()["repeated"])
        self.assertEqual(f.call("prepare", "b1", ["reports", "ledger"], str(f.authority))[0], "conflict")
        assigned = {}
        for task, lane, after in [("producer", "ledger", []), ("independent", "ledger", []), ("excluded", "ledger", []), ("consumer", "reports", ["producer"]), ("overlap", "reports", [])]:
            assigned[task] = ok(self, f.call("assign", "b1", task, lane, "worker-" + task, after))
            self.assertEqual(assigned[task]["base"], batch["base"])
            self.assertTrue(Path(assigned[task]["worktree"]).is_dir())
        self.assertTrue(ok(self, f.call("assign", "b1", "producer", "ledger", "worker-producer", []))["repeated"])
        self.assertEqual(f.call("assign", "b1", "producer", "ledger", "intruder", [])[0], "conflict")
        heads = {
            "producer": f.work(assigned["producer"]["worktree"], {"ledger.txt": "total = 0\napi = 1\n"}, "producer"),
            "independent": f.work(assigned["independent"]["worktree"], {"shared.txt": "alpha\nbeta-ledger\ngamma\n"}, "independent"),
            "excluded": f.work(assigned["excluded"]["worktree"], {".github/workflows/ci.yml": "on: push\n"}, "excluded"),
        }
        consumer = assigned["consumer"]["worktree"]
        git(consumer, "merge", "--quiet", "--no-edit", heads["producer"])
        heads["consumer"] = f.work(consumer, {"reports.txt": "report = uses api\n"}, "consumer")
        heads["overlap"] = f.work(assigned["overlap"]["worktree"], {"reports.txt": "report = overlap\n"}, "overlap")
        for task in ("producer", "independent", "excluded"):
            ok(self, f.call("handoff", "b1", task, heads[task], f"review:{task}", []))
        parked = ok(self, f.call("handoff", "b1", "consumer", heads["consumer"], "review:consumer", [f"producer={heads['independent']}"]), "parked")
        self.assertIn("does not match", parked["reason"])
        self.assertIn("no --prerequisite", ok(self, f.call("handoff", "b1", "consumer", heads["consumer"], "review:consumer", []), "parked")["reason"])
        ok(self, f.call("handoff", "b1", "consumer", heads["consumer"], "review:consumer", [f"producer={heads['producer']}"]))
        self.assertEqual(f.call("handoff", "b1", "overlap", "0" * 40, "review", [])[0], "invalid")

        barrier = threading.Barrier(2, timeout=30)
        outcomes = {}

        def lane_worker(lane):
            outcomes[lane] = f.call("assemble", "b1", lane, hook=lambda point, info: barrier.wait() if point == "lane-lease" else None)
        threads = [threading.Thread(target=lane_worker, args=(lane,)) for lane in ("ledger", "reports")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(barrier.broken)
        ledger = ok(self, outcomes["ledger"], "parked")
        self.assertEqual([item["task"] for item in ledger["included"]], ["producer", "independent"])
        self.assertEqual([(item["task"], item["kind"]) for item in ledger["parked"]], [("excluded", "excluded")])
        self.assertEqual(ledger["parked"][0]["paths"], [".github/workflows/ci.yml"])
        reports = ok(self, outcomes["reports"], "parked")
        self.assertEqual(reports["included"], [])
        self.assertEqual([(item["task"], item["kind"]) for item in reports["parked"]], [("consumer", "dependency")])
        self.assertEqual(reports["waiting"], ["overlap"])
        self.assertIn("not yet staged", reports["parked"][0]["reason"])

        frozen_ledger = ok(self, f.call("freeze", "b1", "ledger"))
        self.assertEqual(frozen_ledger["left_out"], [{"task": "excluded", "state": "parked", "reason": "touches excluded paths: .github/workflows/ci.yml"}])
        self.assertTrue(ok(self, f.call("freeze", "b1", "ledger"))["repeated"])
        self.assertEqual(f.call("assemble", "b1", "ledger")[1].code, "frozen")
        self.assertEqual(f.call("stage", "b1", ["reports"])[1].code, "not-frozen")

        reached, go, competing = threading.Event(), threading.Event(), {}

        def hold(point, info):
            if point == "before-write":
                reached.set()
                go.wait(30)
        thread = threading.Thread(target=lambda: competing.setdefault("a", f.call("stage", "b1", ["ledger"], owner="coordinator-a", hook=hold)))
        dev_before = f.head("dev")
        thread.start()
        self.assertTrue(reached.wait(30))
        competing["b"] = f.call("stage", "b1", ["ledger"], owner="coordinator-b")
        go.set()
        thread.join()
        self.assertEqual(competing["b"][0], "blocked")
        self.assertEqual(competing["b"][1].code, "lease-held")
        first = ok(self, competing["a"])
        candidate = first["candidates"][0]
        self.assertEqual(f.head("dev"), candidate["commit"])
        ledger_tip = ok(self, f.call("status", "b1"))["lanes"][0]["frozen"]
        self.assertEqual(git(f.authority, "rev-list", "--parents", "-n", "1", "dev").split()[1:], [dev_before, ledger_tip])
        self.assertEqual(f.head("main"), main_before)
        rerun = ok(self, f.call("stage", "b1", ["ledger"]))
        self.assertEqual(rerun["candidates"], [])
        self.assertEqual(f.head("dev"), candidate["commit"])

        ok(self, f.call("handoff", "b1", "overlap", heads["overlap"], "review:overlap", []))
        reports = ok(self, f.call("assemble", "b1", "reports"), "parked")
        self.assertEqual([item["task"] for item in reports["included"]], ["consumer"])
        conflict = reports["parked"][0]
        self.assertEqual((conflict["task"], conflict["kind"], conflict["paths"]), ("overlap", "conflict", ["reports.txt"]))
        mirror = Path(batch["mirror"])
        resolution_tree = f.root / "resolution"
        git(mirror, "worktree", "add", "--quiet", "--detach", str(resolution_tree), conflict["lane_tip"])
        git(resolution_tree, "merge", "--no-edit", heads["overlap"], check=False)
        self.assertEqual(git(resolution_tree, "diff", "--name-only", "--diff-filter=U"), "reports.txt")
        (resolution_tree / "reports.txt").write_text("report = uses api and overlap\n")
        git(resolution_tree, "add", "reports.txt")
        git(resolution_tree, "commit", "--quiet", "--no-edit")
        resolution = git(resolution_tree, "rev-parse", "HEAD")
        rejected = ok(self, f.call("resolve", "b1", "reports", "overlap", resolution, None), "parked")
        self.assertIn("unreviewed", rejected["reason"])
        self.assertEqual(f.call("resolve", "b1", "reports", "overlap", heads["overlap"], "review:bad")[0], "invalid")
        resolved = ok(self, f.call("resolve", "b1", "reports", "overlap", resolution, "review:resolution"))
        self.assertEqual(resolved["state"], "included")
        self.assertEqual(resolved["resolution"]["head"], resolution)
        self.assertTrue(ok(self, f.call("resolve", "b1", "reports", "overlap", resolution, "review:resolution"))["repeated"])
        ok(self, f.call("freeze", "b1", "reports"))
        dev_before = f.head("dev")
        second = ok(self, f.call("stage", "b1", ["ledger", "reports"]))
        self.assertEqual([item["lanes"] for item in second["candidates"]], [["reports"]])
        self.assertEqual(git(f.authority, "rev-list", "--parents", "-n", "1", "dev").split()[1:], [dev_before, resolution])
        self.assertEqual(git(f.authority, "show", "dev:reports.txt"), "report = uses api and overlap")
        self.assertEqual(git(f.authority, "show", "dev:shared.txt"), "alpha\nbeta-ledger\ngamma")
        self.assertEqual(f.head("main"), main_before)

        state = ok(self, f.call("status", "b1"))
        tasks = {task["task"]: task for task in state["tasks"]}
        self.assertEqual({name: task["state"] for name, task in tasks.items()}, {"producer": "staged", "independent": "staged", "excluded": "parked", "consumer": "staged", "overlap": "staged"})
        members = {entry[0]: entry for record in state["candidates"] for entry in record["assembly"]["entries"]}
        for name in ("producer", "independent", "consumer", "overlap"):
            task = tasks[name]
            self.assertTrue(task["retained"])
            self.assertEqual(git(mirror, "rev-parse", task["retained_ref"]), heads[name])
            self.assertTrue(task["reachable_from_lane"])
            self.assertEqual(git(mirror, "merge-base", "--is-ancestor", heads[name], f.head("dev"), check=False), "")
            self.assertEqual(members[name][2:4], [heads[name], f"review:{name}"])
        self.assertEqual(members["consumer"][4], [["producer", heads["producer"]]])
        self.assertEqual(state["candidates"][1]["assembly"]["resolutions"], [["reports", "overlap", resolution, "review:resolution"]])
        self.assertTrue(tasks["excluded"]["retained"])
        self.assertIn("excluded paths", tasks["excluded"]["reason"])
        for record in state["candidates"]:
            self.assertEqual(record["state"], "staged")
            self.assertEqual(record["ref_sha"], record["commit"])
            self.assertEqual(Assembly(record["assembly"]["candidate_id"], "b1", tuple(record["assembly"]["lanes"]), tuple(tuple(tuple(tuple(p) for p in x) if i == 4 else x for i, x in enumerate(e)) for e in record["assembly"]["entries"]), tuple(tuple(r) for r in record["assembly"]["resolutions"])).id, record["assembly_id"])
        self.assertEqual(state["authority_heads"]["main"], main_before)
        lanes = {lane["lane"]: lane for lane in state["lanes"]}
        self.assertEqual(lanes["ledger"]["left_out"], frozen_ledger["left_out"])
        self.assertEqual(lanes["reports"]["left_out"], [])

    def test_dev_moved_after_planning_is_stale(self):
        f = self.fixture
        self.single_lane(f)
        moved = {}

        def move(point, info):
            if point == "before-write":
                moved["sha"] = f.external_commit("dev", "reports.txt", "report = external\n")
        stale = f.call("stage", "b1", ["ledger"], hook=move)
        self.assertEqual(stale[0], "stale")
        self.assertEqual(f.head("dev"), moved["sha"])
        self.assertEqual(stale[1].data["candidates"][0]["state"], "stale")
        retry = ok(self, f.call("stage", "b1", ["ledger"]))
        self.assertEqual(retry["candidates"][0]["base"], moved["sha"])
        self.assertEqual(f.head("dev"), retry["candidates"][0]["commit"])

    def test_expired_lease_takeover_refuses_old_owner(self):
        f = self.fixture
        clock = Clock()
        f.clock = clock
        self.single_lane(f)
        main_before, dev_before = f.head("main"), f.head("dev")
        taken = {}

        def stall(point, info):
            if point == "before-write" and "b" not in taken:
                clock.advance(601)
                taken["b"] = f.call("stage", "b1", ["ledger"], owner="coordinator-b", hook=lambda *a: None)
        old = f.call("stage", "b1", ["ledger"], owner="coordinator-a", hook=stall)
        self.assertEqual(old[0], "stale")
        self.assertEqual(old[1].code, "lease-lost")
        self.assertFalse(old[1].data["lease_released"])
        newer = ok(self, taken["b"])
        self.assertTrue(newer["lease_taken_over"])
        self.assertEqual(f.head("dev"), newer["candidates"][0]["commit"])
        self.assertEqual(git(f.authority, "rev-list", "--parents", "-n", "1", "dev").split()[1], dev_before)
        self.assertEqual(git(f.authority, "rev-list", "--count", f"{dev_before}..dev"), "3")
        self.assertEqual(f.head("main"), main_before)

    def test_enabled_staging_scope_is_pending(self):
        for scope, mode in (("affected", "each-lane"), ("full", "combined-ready")):
            with self.subTest(scope=scope, mode=mode):
                f = Fixture(scope=scope, mode=mode)
                self.addCleanup(f.close)
                self.prepare(f)
                heads = {}
                for task, lane, content in (("one", "ledger", "ledger.txt"), ("two", "reports", "reports.txt")):
                    work = ok(self, f.call("assign", "b1", task, lane, "worker", []))
                    heads[lane] = f.work(work["worktree"], {content: task + "\n"}, task)
                    ok(self, f.call("handoff", "b1", task, heads[lane], "review:" + task, []))
                    ok(self, f.call("assemble", "b1", lane))
                    ok(self, f.call("freeze", "b1", lane))
                dev, main_sha = f.head("dev"), f.head("main")
                pending = ok(self, f.call("stage", "b1", ["reports", "ledger"]), "pending")
                again = ok(self, f.call("stage", "b1", ["ledger", "reports"]), "pending")
                self.assertEqual(f.head("dev"), dev)
                self.assertEqual(f.head("main"), main_sha)
                candidate = pending["candidates"][0]
                self.assertEqual(candidate["id"], again["candidates"][0]["id"])
                self.assertEqual(candidate["assembly"]["candidate_id"], candidate["id"])
                tips = ok(self, f.call("status", "b1"))
                frozen = [lane["frozen"] for lane in tips["lanes"]]
                if mode == "each-lane":
                    self.assertEqual(candidate["lanes"], ["ledger"])
                    self.assertEqual(candidate["parents"], [dev, frozen[0]])
                    self.assertEqual(pending["waiting_lanes"], ["reports"])
                else:
                    self.assertEqual(candidate["lanes"], ["ledger", "reports"])
                    self.assertEqual(candidate["parents"], [dev, *frozen])
                self.assertEqual([c["state"] for c in tips["candidates"]], ["pending"])
                self.assertEqual({t["state"] for t in tips["tasks"]}, {"included"})

    def test_combined_ready_lands_one_candidate_and_stage_conflict_blocks(self):
        f = Fixture(mode="combined-ready")
        self.addCleanup(f.close)
        self.prepare(f)
        for task, lane, line in (("one", "ledger", "beta-one"), ("two", "reports", "beta-two")):
            work = ok(self, f.call("assign", "b1", task, lane, "worker", []))
            head = f.work(work["worktree"], {"shared.txt": f"alpha\n{line}\ngamma\n"}, task)
            ok(self, f.call("handoff", "b1", task, head, "review:" + task, []))
            ok(self, f.call("assemble", "b1", lane))
            ok(self, f.call("freeze", "b1", lane))
        dev = f.head("dev")
        blocked = f.call("stage", "b1", ["ledger", "reports"])
        self.assertEqual((blocked[0], blocked[1].code), ("blocked", "conflict"))
        self.assertEqual(blocked[1].data["conflict"], {"lane": "reports", "paths": ["shared.txt"]})
        self.assertEqual(f.head("dev"), dev)

        g = Fixture(mode="combined-ready")
        self.addCleanup(g.close)
        self.prepare(g)
        for task, lane, path in (("one", "ledger", "ledger.txt"), ("two", "reports", "reports.txt")):
            work = ok(self, g.call("assign", "b1", task, lane, "worker", []))
            head = g.work(work["worktree"], {path: task + "\n"}, task)
            ok(self, g.call("handoff", "b1", task, head, "review:" + task, []))
            ok(self, g.call("assemble", "b1", lane))
            ok(self, g.call("freeze", "b1", lane))
        dev = g.head("dev")
        staged = ok(self, g.call("stage", "b1", ["ledger", "reports"]))
        self.assertEqual(len(staged["candidates"]), 1)
        frozen = [lane["frozen"] for lane in ok(self, g.call("status", "b1"))["lanes"]]
        self.assertEqual(git(g.authority, "rev-list", "--parents", "-n", "1", "dev").split()[1:], [dev, *frozen])

    def test_concurrent_claims_are_atomic(self):
        self.prepare()
        barrier = threading.Barrier(2, timeout=30)
        outcomes = []

        def claim(owner):
            barrier.wait()
            outcomes.append(self.fixture.call("assign", "b1", "shared", "ledger", owner, []))
        threads = [threading.Thread(target=claim, args=(owner,)) for owner in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(status for status, _ in outcomes), ["conflict", "ok"])

    def conflicting_lanes(self, fixture):
        self.prepare(fixture)
        heads = {}
        for task, lane, line in (("one", "ledger", "beta-one"), ("two", "reports", "beta-two")):
            work = ok(self, fixture.call("assign", "b1", task, lane, "worker", []))
            heads[task] = fixture.work(work["worktree"], {"shared.txt": f"alpha\n{line}\ngamma\n"}, task)
            ok(self, fixture.call("handoff", "b1", task, heads[task], "review:" + task, []))
            ok(self, fixture.call("assemble", "b1", lane))
            ok(self, fixture.call("freeze", "b1", lane))
        return heads

    def stage_resolution(self, fixture, base, tips, content, extra=None):
        mirror = Path(ok(self, fixture.call("status", "b1"))["mirror"])
        tree = fixture.root / ("stage-resolution-" + str(len(list(fixture.root.iterdir()))))
        git(mirror, "worktree", "add", "--quiet", "--detach", str(tree), base)
        for tip in tips:
            git(tree, "merge", "--no-edit", tip, check=False)
            if git(tree, "diff", "--name-only", "--diff-filter=U"):
                (tree / "shared.txt").write_text(content)
                git(tree, "add", "-A")
                git(tree, "commit", "--quiet", "--no-edit")
        for path, text in (extra or {}).items():
            (tree / path).parent.mkdir(parents=True, exist_ok=True)
            (tree / path).write_text(text)
            git(tree, "add", "-A")
            git(tree, "commit", "--quiet", "-m", "extra")
        return git(tree, "rev-parse", "HEAD")

    def test_undeclared_task_content_parks(self):
        f = self.fixture
        self.prepare(f)
        work = {task: ok(self, f.call("assign", "b1", task, lane, "worker", [])) for task, lane in (("hidden", "ledger"), ("smuggler", "reports"), ("first", "reports"), ("second", "reports"))}
        hidden = f.work(work["hidden"]["worktree"], {"ledger.txt": "hidden\n"}, "hidden")
        git(work["smuggler"]["worktree"], "merge", "--quiet", "--no-edit", hidden)
        smuggler = f.work(work["smuggler"]["worktree"], {"other.txt": "smuggler\n"}, "smuggler")
        first = f.work(work["first"]["worktree"], {"reports.txt": "first\n"}, "first")
        second = f.work(work["second"]["worktree"], {"reports.txt": "second\n"}, "second")
        for task, head in (("hidden", hidden), ("smuggler", smuggler), ("first", first), ("second", second)):
            ok(self, f.call("handoff", "b1", task, head, "review:" + task, []))
        result = ok(self, f.call("assemble", "b1", "reports"), "parked")
        parked = {item["task"]: item for item in result["parked"]}
        self.assertEqual(parked["smuggler"]["kind"], "dependency")
        self.assertIn("'hidden'", parked["smuggler"]["reason"])
        self.assertEqual(parked["second"]["kind"], "conflict")
        self.assertEqual([item["task"] for item in result["included"]], ["first"])
        mirror = Path(ok(self, f.call("status", "b1"))["mirror"])
        tree = f.root / "smuggling-resolution"
        git(mirror, "worktree", "add", "--quiet", "--detach", str(tree), result["tip"])
        git(tree, "merge", "--no-edit", second, check=False)
        (tree / "reports.txt").write_text("first and second\n")
        git(tree, "add", "reports.txt")
        git(tree, "commit", "--quiet", "--no-edit")
        clean = git(tree, "rev-parse", "HEAD")
        git(tree, "merge", "--quiet", "--no-edit", hidden)
        smuggled = git(tree, "rev-parse", "HEAD")
        refused = ok(self, f.call("resolve", "b1", "reports", "second", smuggled, "review:resolution"), "parked")
        self.assertEqual(refused["kind"], "dependency")
        self.assertEqual(refused["undeclared"], ["hidden"])
        self.assertEqual(refused["state"], "parked")
        resolved = ok(self, f.call("resolve", "b1", "reports", "second", clean, "review:resolution"))
        self.assertEqual(resolved["state"], "included")

    def test_stage_resolution_for_cross_lane_conflict(self):
        f = Fixture(mode="combined-ready")
        self.addCleanup(f.close)
        self.conflicting_lanes(f)
        dev, main_before = f.head("dev"), f.head("main")
        blocked = f.call("stage", "b1", ["ledger", "reports"])
        self.assertEqual((blocked[0], blocked[1].code), ("blocked", "conflict"))
        tips = [lane["frozen"] for lane in ok(self, f.call("status", "b1"))["lanes"]]
        resolution = self.stage_resolution(f, dev, tips, "alpha\nbeta-one-two\ngamma\n")
        unreviewed = f.call("stage", "b1", ["ledger", "reports"], resolution=resolution)
        self.assertEqual((unreviewed[0], unreviewed[1].code), ("blocked", "unreviewed-resolution"))
        partial = self.stage_resolution(f, dev, tips[:1], "alpha\nbeta-one\ngamma\n")
        self.assertEqual(f.call("stage", "b1", ["ledger", "reports"], resolution=partial, review="review:stage")[0], "invalid")
        sneaky = self.stage_resolution(f, dev, tips, "alpha\nbeta-one-two\ngamma\n", {".github/workflows/x.yml": "on: push\n"})
        excluded = f.call("stage", "b1", ["ledger", "reports"], resolution=sneaky, review="review:stage")
        self.assertEqual((excluded[0], excluded[1].code), ("blocked", "excluded"))
        loose = ok(self, f.call("assign", "b1", "loose", "ledger", "worker", []))
        loose_head = f.work(loose["worktree"], {"ledger.txt": "loose\n"}, "loose")
        ok(self, f.call("handoff", "b1", "loose", loose_head, "review:loose", []))
        mirror = Path(ok(self, f.call("status", "b1"))["mirror"])
        smuggling = f.root / "stage-smuggling"
        git(mirror, "worktree", "add", "--quiet", "--detach", str(smuggling), resolution)
        git(smuggling, "merge", "--quiet", "--no-edit", loose_head)
        undeclared = f.call("stage", "b1", ["ledger", "reports"], resolution=git(smuggling, "rev-parse", "HEAD"), review="review:stage")
        self.assertEqual((undeclared[0], undeclared[1].code, undeclared[1].data["undeclared"]), ("blocked", "undeclared-dependency", ["loose"]))
        self.assertEqual(f.head("dev"), dev)
        staged = ok(self, f.call("stage", "b1", ["ledger", "reports"], resolution=resolution, review="review:stage"))
        candidate = staged["candidates"][0]
        self.assertEqual(f.head("dev"), candidate["commit"])
        self.assertEqual(git(f.authority, "rev-list", "--parents", "-n", "1", "dev").split()[1:], [dev, *tips])
        self.assertEqual(git(f.authority, "rev-parse", "dev^{tree}"), git(Path(ok(self, f.call("status", "b1"))["mirror"]), "rev-parse", resolution + "^{tree}"))
        self.assertEqual(candidate["assembly"]["resolutions"], [["@stage", "ledger,reports", resolution, "review:stage"]])
        self.assertEqual(git(Path(ok(self, f.call("status", "b1"))["mirror"]), "rev-parse", candidate["resolution_ref"]), resolution)
        self.assertEqual(f.head("main"), main_before)
        state = ok(self, f.call("status", "b1"))
        self.assertEqual(state["stage_resolutions"][0]["head"], resolution)
        self.assertEqual({t["task"]: t["state"] for t in state["tasks"]}, {"one": "staged", "two": "staged", "loose": "handed-off"})

    def test_stage_resolution_rules_each_lane(self):
        f = self.fixture
        self.conflicting_lanes(f)
        ok(self, f.call("stage", "b1", ["ledger"]))
        dev = f.head("dev")
        tips = [lane["frozen"] for lane in ok(self, f.call("status", "b1"))["lanes"]]
        self.assertEqual(f.call("stage", "b1", ["ledger", "reports"], resolution=dev, review="r")[0], "invalid")
        old_resolution = self.stage_resolution(f, tips[0], tips[1:], "alpha\nbeta-old\ngamma\n")
        stale = f.call("stage", "b1", ["reports"], resolution=old_resolution, review="review:stage")
        self.assertEqual((stale[0], stale[1].code), ("stale", "stale"))
        self.assertEqual(f.call("stage", "b1", ["reports"], review="review:stage")[0], "invalid")
        resolution = self.stage_resolution(f, dev, tips[1:], "alpha\nbeta-one-two\ngamma\n")
        staged = ok(self, f.call("stage", "b1", ["reports"], resolution=resolution, review="review:stage"))
        self.assertEqual(staged["candidates"][0]["parents"], [dev, tips[1]])
        self.assertEqual(f.head("dev"), staged["candidates"][0]["commit"])

    def test_lost_acknowledgement_reconciles(self):
        f = self.fixture
        self.single_lane(f)
        dev = f.head("dev")

        def crash(point, info):
            if point == "after-write":
                raise KeyboardInterrupt("simulated crash after authority write")
        with self.assertRaises(KeyboardInterrupt):
            f.call("stage", "b1", ["ledger"], hook=crash)
        landed = f.head("dev")
        self.assertNotEqual(landed, dev)
        self.assertEqual(ok(self, f.call("status", "b1"))["candidates"][0]["state"], "prepared")
        again = ok(self, f.call("stage", "b1", ["ledger"]))
        self.assertTrue(again["candidates"][0]["reconciled"])
        self.assertEqual(again["candidates"][0]["commit"], landed)
        self.assertEqual(f.head("dev"), landed)
        self.assertEqual(git(f.authority, "rev-list", "--merges", "--count", f"{dev}..dev"), "2")
        state = ok(self, f.call("status", "b1"))
        self.assertEqual([c["state"] for c in state["candidates"]], ["staged"])
        self.assertEqual({t["state"] for t in state["tasks"]}, {"staged"})
        self.assertEqual(state["lanes"][0]["staged_candidate"], state["candidates"][0]["id"])

    def test_externally_landed_lane_is_refused(self):
        f = self.fixture
        self.single_lane(f)
        state = ok(self, f.call("status", "b1"))
        git(Path(state["mirror"]), "push", "--quiet", str(f.authority), f"{state['lanes'][0]['frozen']}:refs/heads/dev")
        dev = f.head("dev")
        refused = f.call("stage", "b1", ["ledger"])
        self.assertEqual((refused[0], refused[1].code), ("blocked", "already-landed"))
        self.assertEqual(f.head("dev"), dev)
        self.assertEqual(ok(self, f.call("status", "b1"))["candidates"], [])

    def test_cli_exit_statuses(self):
        f = self.fixture

        def call(*args):
            out = io.StringIO()
            with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                code = main([*args, "--json", "--directory", str(f.project)])
            return code, json.loads(out.getvalue())
        self.assertEqual(call("batch", "prepare", "b1", "--lanes", "ledger", "--authority", str(f.authority))[0], 2)
        self.assertEqual(call("batch", "prepare", "b1", "--lanes", "ledger", "--authority", str(f.seed), "--state-dir", str(f.state))[0], 2)
        self.assertEqual(call("batch", "prepare", "b1", "--lanes", "ledger", "--authority", "relative.git", "--state-dir", str(f.state))[0], 2)
        code, result = call("batch", "prepare", "b1", "--lanes", "ledger", "--authority", str(f.authority), "--state-dir", str(f.state))
        self.assertEqual((code, result["status"], result["data"]["base"]), (0, "ok", f.head("dev")))
        self.assertEqual(call("batch", "prepare", "b1", "--lanes", "ledger,reports", "--authority", str(f.authority), "--state-dir", str(f.state))[0], 3)
        self.assertEqual(call("stage", "b1", "--lanes", "ledger", "--state-dir", str(f.state))[0], 5)
        with patch.dict("os.environ", {"STARLINGS_STATE_DIR": str(f.state)}):
            code, result = call("status", "b1")
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["lanes"][0]["lane"], "ledger")
        self.assertEqual(call("status", "b1", "--state-dir", str(f.root / "absent"))[0], 2)
        self.assertEqual(batches.EXIT, {"ok": 0, "invalid": 2, "conflict": 3, "pending": 4, "parked": 5, "blocked": 5, "stale": 6, "failed": 7})


if __name__ == "__main__":
    unittest.main()
