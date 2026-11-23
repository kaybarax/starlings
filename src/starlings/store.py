from contextlib import contextmanager
import hashlib
import json
import secrets
import sqlite3
import time

SCHEMA_VERSION = 5
TASKS = """CREATE TABLE IF NOT EXISTS {name}(
    batch TEXT NOT NULL, name TEXT NOT NULL, lane TEXT NOT NULL, owner TEXT NOT NULL, seq INTEGER NOT NULL,
    worktree TEXT NOT NULL, branch TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('assigned', 'handed-off', 'parked', 'pending', 'included', 'staged')),
    head TEXT, review TEXT, reason TEXT, park_kind TEXT, included_commit TEXT, included_position INTEGER, candidate TEXT,
    PRIMARY KEY(batch, name), UNIQUE(batch, seq), FOREIGN KEY(batch, lane) REFERENCES lanes(batch, name))"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS requests(scope TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(scope, key));
CREATE TABLE IF NOT EXISTS leases(resource TEXT PRIMARY KEY, owner TEXT NOT NULL, token TEXT NOT NULL, acquired REAL NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS batches(
    name TEXT PRIMARY KEY, project TEXT NOT NULL, authority TEXT NOT NULL, mirror TEXT NOT NULL,
    staging TEXT NOT NULL, release TEXT NOT NULL, policy_digest TEXT NOT NULL, policy TEXT NOT NULL,
    base TEXT NOT NULL, release_base TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS lanes(
    batch TEXT NOT NULL REFERENCES batches(name), name TEXT NOT NULL, position INTEGER NOT NULL,
    tip TEXT NOT NULL, frozen TEXT, staged_candidate TEXT, PRIMARY KEY(batch, name), UNIQUE(batch, position));
{tasks};
CREATE TABLE IF NOT EXISTS dependencies(
    batch TEXT NOT NULL, task TEXT NOT NULL, prerequisite TEXT NOT NULL, position INTEGER NOT NULL, head TEXT,
    PRIMARY KEY(batch, task, prerequisite),
    FOREIGN KEY(batch, task) REFERENCES tasks(batch, name), FOREIGN KEY(batch, prerequisite) REFERENCES tasks(batch, name));
CREATE TABLE IF NOT EXISTS resolutions(
    batch TEXT NOT NULL, lane TEXT NOT NULL, task TEXT NOT NULL, head TEXT NOT NULL, review TEXT NOT NULL, lane_parent TEXT NOT NULL, ref TEXT NOT NULL,
    PRIMARY KEY(batch, lane, task), FOREIGN KEY(batch, task) REFERENCES tasks(batch, name), FOREIGN KEY(batch, lane) REFERENCES lanes(batch, name));
CREATE TABLE IF NOT EXISTS candidates(
    id TEXT PRIMARY KEY, batch TEXT NOT NULL REFERENCES batches(name), lanes TEXT NOT NULL, base TEXT NOT NULL,
    commit_sha TEXT NOT NULL, ref TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('prepared', 'pending', 'staged', 'stale')),
    record TEXT NOT NULL, assembly_id TEXT NOT NULL, assembly TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS stage_resolutions(
    candidate TEXT PRIMARY KEY REFERENCES candidates(id), batch TEXT NOT NULL REFERENCES batches(name), lanes TEXT NOT NULL,
    head TEXT NOT NULL, review TEXT NOT NULL, base TEXT NOT NULL, ref TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trusted_policies(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, authority TEXT NOT NULL, digest TEXT NOT NULL, commit_sha TEXT NOT NULL,
    review TEXT NOT NULL, previous_digest TEXT, trusted_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS release_candidates(
    id TEXT PRIMARY KEY, batch TEXT NOT NULL REFERENCES batches(name), staging_head TEXT NOT NULL, release_head TEXT NOT NULL,
    commit_sha TEXT NOT NULL, tree TEXT NOT NULL, ref TEXT NOT NULL, record TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS plans(
    id TEXT PRIMARY KEY, batch TEXT NOT NULL REFERENCES batches(name), gate TEXT NOT NULL, candidate_id TEXT NOT NULL,
    candidate TEXT NOT NULL, record TEXT NOT NULL, policy TEXT NOT NULL, authority TEXT NOT NULL, target TEXT NOT NULL,
    changed TEXT NOT NULL, unmapped TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
    id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES plans(id), owner TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('running', 'completed', 'cancelled', 'interrupted', 'failed')),
    cancel_requested INTEGER NOT NULL DEFAULT 0, reason TEXT, receipt_id TEXT, started REAL NOT NULL, finished REAL);
CREATE TABLE IF NOT EXISTS evidence(
    run_id TEXT NOT NULL REFERENCES runs(id), check_id TEXT NOT NULL, position INTEGER NOT NULL, evidence_id TEXT NOT NULL,
    status TEXT NOT NULL, exit_code INTEGER NOT NULL, record TEXT NOT NULL, files TEXT NOT NULL, metrics TEXT NOT NULL,
    PRIMARY KEY(run_id, check_id));
CREATE TABLE IF NOT EXISTS receipts(
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), plan_id TEXT NOT NULL REFERENCES plans(id),
    authority TEXT NOT NULL, outcome TEXT NOT NULL, record TEXT NOT NULL, signature TEXT NOT NULL, issued_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS run_processes(
    run_id TEXT NOT NULL REFERENCES runs(id), check_id TEXT NOT NULL, pgid INTEGER NOT NULL, job TEXT NOT NULL, worktree TEXT NOT NULL,
    PRIMARY KEY(run_id, check_id));
CREATE TABLE IF NOT EXISTS task_candidates(
    id TEXT PRIMARY KEY, batch TEXT NOT NULL REFERENCES batches(name), lane TEXT NOT NULL, task TEXT NOT NULL, base TEXT NOT NULL,
    commit_sha TEXT NOT NULL, ref TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending', 'included', 'stale')),
    resolution TEXT, record TEXT NOT NULL, assembly_id TEXT NOT NULL, assembly TEXT NOT NULL, created REAL NOT NULL,
    FOREIGN KEY(batch, task) REFERENCES tasks(batch, name));
CREATE TABLE IF NOT EXISTS approvals(
    id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES plans(id), candidate_id TEXT NOT NULL, by_identity TEXT NOT NULL,
    review TEXT, granted_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL, record TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS promotions(
    id TEXT PRIMARY KEY, batch TEXT NOT NULL REFERENCES batches(name), gate TEXT NOT NULL, plan_id TEXT NOT NULL REFERENCES plans(id),
    receipt_id TEXT NOT NULL REFERENCES receipts(id), candidate_id TEXT NOT NULL, approvals TEXT NOT NULL, authority TEXT NOT NULL,
    repository TEXT NOT NULL, target TEXT NOT NULL, old TEXT NOT NULL, new TEXT NOT NULL, tree TEXT NOT NULL, parents TEXT NOT NULL,
    sources TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('intent', 'landed', 'not-applied', 'ambiguous')),
    observed TEXT, resolution TEXT, record_id TEXT, record TEXT, created REAL NOT NULL, finished REAL);
CREATE TABLE IF NOT EXISTS include_intents(
    batch TEXT NOT NULL, lane TEXT NOT NULL, task TEXT NOT NULL, old TEXT NOT NULL, new TEXT NOT NULL, resolution TEXT, created REAL NOT NULL,
    PRIMARY KEY(batch, lane), FOREIGN KEY(batch, task) REFERENCES tasks(batch, name));
CREATE TABLE IF NOT EXISTS diagnostic_runs(
    id TEXT PRIMARY KEY, batch TEXT NOT NULL REFERENCES batches(name), plan_id TEXT NOT NULL REFERENCES plans(id),
    receipt_id TEXT NOT NULL, gate TEXT NOT NULL, position INTEGER NOT NULL,
    basis TEXT NOT NULL, commit_sha TEXT, ref TEXT, conflict TEXT,
    checks TEXT NOT NULL, results TEXT NOT NULL, processes TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS authorities(
    authority TEXT PRIMARY KEY, hook_sha256 TEXT NOT NULL, protected_refs TEXT NOT NULL, installed_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS dispositions(
    batch TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, repo TEXT NOT NULL, source TEXT NOT NULL,
    pr INTEGER, state TEXT NOT NULL, link TEXT, head TEXT, observed_at REAL NOT NULL,
    PRIMARY KEY(batch, kind, name));
""".format(tasks=TASKS.format(name="tasks"))


class LeaseHeld(RuntimeError):
    pass


class LeaseLost(RuntimeError):
    pass


class RequestConflict(RuntimeError):
    pass


def request_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()


class Store:
    def __init__(self, path, clock=None):
        self.clock = clock or time.time
        self.connection = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA foreign_keys = OFF")
        with self.transaction() as c:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    c.execute(statement)
            versions = [row[0] for row in c.execute("SELECT version FROM schema_version")]
            if not versions:
                c.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            elif versions in ([1], [2]):
                c.execute(TASKS.format(name="tasks_v3"))
                c.execute("INSERT INTO tasks_v3 SELECT * FROM tasks")
                c.execute("DROP TABLE tasks")
                c.execute("ALTER TABLE tasks_v3 RENAME TO tasks")
                if c.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("state database migration found foreign key violations")
                c.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            elif versions in ([3], [4]):
                c.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            elif versions != [SCHEMA_VERSION]:
                raise ValueError(f"unsupported state database schema version {versions}")
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @contextmanager
    def transaction(self):
        c = self.connection
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")

    def request(self, c, scope, key, value):
        digest = request_digest(value)
        row = c.execute("SELECT digest FROM requests WHERE scope = ? AND key = ?", (scope, key)).fetchone()
        if row is None:
            c.execute("INSERT INTO requests(scope, key, digest) VALUES (?, ?, ?)", (scope, key, digest))
            return False
        if row["digest"] != digest:
            raise RequestConflict(f"{scope} {key} was already recorded with different arguments")
        return True

    def acquire(self, resource, owner, seconds):
        if not 0 < seconds <= 86400:
            raise ValueError("lease seconds must be between 1 and 86400")
        with self.transaction() as c:
            now = self.clock()
            row = c.execute("SELECT owner, expires FROM leases WHERE resource = ?", (resource,)).fetchone()
            if row is not None and row["expires"] > now:
                raise LeaseHeld(f"lease {resource} is held by {row['owner']} until {row['expires']}")
            token = secrets.token_hex(16)
            c.execute("INSERT OR REPLACE INTO leases(resource, owner, token, acquired, expires) VALUES (?, ?, ?, ?, ?)", (resource, owner, token, now, now + seconds))
            return {"resource": resource, "owner": owner, "token": token, "expires": now + seconds, "taken_over": row is not None}

    def check(self, c, lease):
        row = c.execute("SELECT token, expires FROM leases WHERE resource = ?", (lease["resource"],)).fetchone()
        if row is None or row["token"] != lease["token"] or row["expires"] <= self.clock():
            raise LeaseLost(f"lease {lease['resource']} is no longer owned by {lease['owner']}")

    def release(self, lease):
        with self.transaction() as c:
            deleted = c.execute("DELETE FROM leases WHERE resource = ? AND token = ?", (lease["resource"], lease["token"])).rowcount
        if not deleted:
            raise LeaseLost(f"lease {lease['resource']} is no longer owned by {lease['owner']}; release refused")

    def acquire_set(self, groups, owner, seconds):
        if not 0 < seconds <= 86400:
            raise ValueError("lease seconds must be between 1 and 86400")
        with self.transaction() as c:
            now = self.clock()
            chosen = []
            for group in groups:
                for resource in group:
                    row = c.execute("SELECT expires FROM leases WHERE resource = ?", (resource,)).fetchone()
                    if row is None or row["expires"] <= now:
                        chosen.append(resource)
                        break
                else:
                    return None
            leases = []
            for resource in chosen:
                token = secrets.token_hex(16)
                c.execute("INSERT OR REPLACE INTO leases(resource, owner, token, acquired, expires) VALUES (?, ?, ?, ?, ?)", (resource, owner, token, now, now + seconds))
                leases.append({"resource": resource, "owner": owner, "token": token, "expires": now + seconds, "taken_over": False})
            return leases
