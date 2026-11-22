from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import ClassVar


def nonempty(value, name):
    if type(value) is not str or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name}: expected a nonempty string without control characters")


def digest(value, name, sizes=(64,)):
    if type(value) is not str or len(value) not in sizes or not re.fullmatch("[0-9a-f]+", value):
        raise ValueError(f"{name}: expected a lowercase hexadecimal digest of length {sizes}")


class Record:
    schema: ClassVar[int] = 1
    kind: ClassVar[str]

    @property
    def id(self):
        body = {"schema": self.schema, "kind": self.kind, **asdict(self)}
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        return self.kind + ":" + hashlib.sha256(encoded).hexdigest()

    def to_dict(self):
        return json.loads(json.dumps({"schema": self.schema, "kind": self.kind, "id": self.id, **asdict(self)}))


def reference(value, kind):
    if type(value) is not str or not value.startswith(kind + ":"):
        raise ValueError(f"expected a {kind} content ID")
    digest(value[len(kind) + 1:], kind)


@dataclass(frozen=True)
class Candidate(Record):
    repository: str
    authority: str
    target: str
    base: str
    parents: tuple[str, ...]
    tree: str
    commit: str
    kind: ClassVar[str] = "candidate"

    def __post_init__(self):
        for name in ("repository", "authority", "target"):
            nonempty(getattr(self, name), name)
        branch = self.target.removeprefix("refs/heads/")
        if not self.target.startswith("refs/heads/") or not branch or branch.startswith(("-", "/", ".")) or branch.endswith(("/", ".")) or any(x in branch for x in ("..", "@{", "//")) or re.search(r"[\s~^:?*\[\\\x7f]", branch) or any(x.endswith(".lock") or x.startswith(".") for x in branch.split("/")) or branch == "@":
            raise ValueError("target: expected a valid fully qualified branch ref")
        for name in ("base", "tree", "commit"):
            digest(getattr(self, name), name, (40, 64))
        if type(self.parents) is not tuple or not self.parents:
            raise ValueError("parents: expected a nonempty ordered tuple of Git object IDs")
        for parent in self.parents:
            digest(parent, "parent", (len(self.commit),))
        if len({len(self.base), len(self.tree), len(self.commit)}) != 1:
            raise ValueError("Git object ID lengths must agree")


@dataclass(frozen=True)
class Plan(Record):
    candidate_id: str
    policy_digest: str
    gate: str
    checks: tuple[str, ...]
    executor: str
    environment: str
    kind: ClassVar[str] = "plan"

    def __post_init__(self):
        reference(self.candidate_id, "candidate")
        digest(self.policy_digest, "policy_digest")
        if self.gate not in ("task", "staging", "release") or self.executor not in ("local", "github") or self.environment not in ("native", "guest"):
            raise ValueError("plan: invalid gate, executor or environment")
        if type(self.checks) is not tuple or not self.checks:
            raise ValueError("checks: expected a nonempty ordered tuple")
        for check in self.checks:
            nonempty(check, "check")
        if len(set(self.checks)) != len(self.checks):
            raise ValueError("checks: duplicates are not allowed")


@dataclass(frozen=True)
class Evidence(Record):
    plan_id: str
    check: str
    exit_code: int
    artifact_digest: str
    kind: ClassVar[str] = "evidence"

    def __post_init__(self):
        reference(self.plan_id, "plan")
        nonempty(self.check, "check")
        if type(self.exit_code) is not int or not -255 <= self.exit_code <= 255:
            raise ValueError("exit_code: expected an integer between -255 and 255")
        digest(self.artifact_digest, "artifact_digest")


@dataclass(frozen=True)
class Adapter(Record):
    name: str
    version: str
    capabilities: tuple[str, ...]
    kind: ClassVar[str] = "adapter"

    def __post_init__(self):
        nonempty(self.name, "name")
        nonempty(self.version, "version")
        if type(self.capabilities) is not tuple:
            raise ValueError("capabilities: expected a tuple")
        for capability in self.capabilities:
            nonempty(capability, "capability")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities: duplicates are not allowed")


def name(value, label):
    if type(value) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", value):
        raise ValueError(f"{label}: expected a lowercase name of letters, digits, hyphens and underscores")


@dataclass(frozen=True)
class Assembly(Record):
    candidate_id: str
    batch: str
    lanes: tuple[str, ...]
    entries: tuple[tuple, ...]
    resolutions: tuple[tuple, ...]
    kind: ClassVar[str] = "assembly"

    def __post_init__(self):
        reference(self.candidate_id, "candidate")
        name(self.batch, "batch")
        if type(self.lanes) is not tuple or not self.lanes or len(set(self.lanes)) != len(self.lanes):
            raise ValueError("lanes: expected a nonempty ordered tuple without duplicates")
        for lane in self.lanes:
            name(lane, "lane")
        if type(self.entries) is not tuple or type(self.resolutions) is not tuple:
            raise ValueError("entries and resolutions: expected ordered tuples")
        tasks = []
        for entry in self.entries:
            if type(entry) is not tuple or len(entry) != 5:
                raise ValueError("entry: expected (task, lane, head, review, prerequisites)")
            task, lane, head, review, prerequisites = entry
            name(task, "task")
            if lane not in self.lanes:
                raise ValueError("entry: lane is not part of this assembly")
            digest(head, "head", (40, 64))
            nonempty(review, "review")
            if type(prerequisites) is not tuple:
                raise ValueError("prerequisites: expected an ordered tuple of (task, head)")
            for prerequisite in prerequisites:
                if type(prerequisite) is not tuple or len(prerequisite) != 2:
                    raise ValueError("prerequisite: expected (task, head)")
                name(prerequisite[0], "prerequisite")
                digest(prerequisite[1], "prerequisite head", (40, 64))
            tasks.append(task)
        if len(set(tasks)) != len(tasks):
            raise ValueError("entries: duplicate task")
        for resolution in self.resolutions:
            if type(resolution) is not tuple or len(resolution) != 4:
                raise ValueError("resolution: expected (lane, task, head, review)")
            lane, task, head, review = resolution
            if lane == "@stage":
                if task != ",".join(self.lanes):
                    raise ValueError("resolution: a staging resolution must name the comma-joined assembly lanes")
            elif lane not in self.lanes or task not in tasks:
                raise ValueError("resolution: lane and task must be part of this assembly")
            digest(head, "resolution head", (40, 64))
            nonempty(review, "resolution review")
