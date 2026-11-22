# Starlings

An offline, stdlib-only Python foundation for configurable verification policies in parallel Git development. Requires Python 3.11 or newer. Distribution, import and command name: `starlings`.

## Scope

Milestone 1 ships policy previews, validation, diagnostics and versioned data contracts. Milestone 2 adds local assembly against a local bare authority: batches, atomic task claims with worktrees, reviewed handoffs, single-writer lane assembly in dependency order, reviewed conflict resolutions, lane freezes and target-leased staging. This version does **not** execute checks, verify candidates, issue signatures, register trusted policy, enforce resources, promote to the release branch, reconcile crashes, install harness instructions or create hosted workflows. It makes no verification or enforcement claim.

Install a locally built wheel in an isolated environment:

```sh
python3 -m venv /path/outside/checkout/venv
/path/outside/checkout/venv/bin/python -m pip install /path/to/starlings-0.1.0-py3-none-any.whl
/path/outside/checkout/venv/bin/starlings --version
```

From an existing project directory:

```sh
starlings init
starlings init --preset release-only --preview --json
starlings init --preset combined-lanes-release --preview
starlings init --preset each-lane-release --preview
starlings init --preset every-task-lane-release --preview
starlings init --write
starlings policy show
starlings policy validate --json
starlings doctor --json
```

## Local assembly (milestone 2)

```sh
export STARLINGS_STATE_DIR=/absolute/private/state
starlings batch prepare demo --lanes ledger,reports --authority /absolute/authority.git
starlings task assign demo totals --lane ledger --owner worker-a
starlings task assign demo summary --lane reports --owner worker-b --after totals
starlings task handoff demo totals --head <sha> --review <review-ref>
starlings task handoff demo summary --head <sha> --review <review-ref> --prerequisite totals=<sha>
starlings lane assemble demo ledger
starlings lane resolve demo reports --task <task> --head <sha> --review <review-ref>
starlings lane freeze demo ledger
starlings stage demo --lanes ledger,reports
starlings stage demo --lanes reports --resolution <sha> --review <review-ref>
starlings status demo --json
```

Every assembly command accepts `--state-dir` (or `STARLINGS_STATE_DIR`), `--directory` and `--json`; lane and stage commands also accept `--owner` and `--lease-seconds` (default 600). The authority must be an absolute path to an existing local bare repository; remote authorities are unsupported. All Git work runs as argv subprocesses (never a shell) with hooks disabled (`core.hooksPath=/dev/null`), `GIT_TERMINAL_PROMPT=0` and only the `file` protocol allowed. Git 2.38 or newer is required for `merge-tree --write-tree`; older versions are diagnosed.

The state directory is created with mode 0700 if absent and validated with the rules below. It holds `starlings.db` (SQLite, WAL, foreign keys, schema-version table, short `BEGIN IMMEDIATE` transactions), `mirrors/<sha256 of authority path>.git` (a bare coordinator mirror used for all object work) and `worktrees/<batch>/<task>`.

- `batch prepare` reads `starlings.toml` from `--directory`, records the SHA-256 of the manifest bytes as an **untrusted** policy digest, snapshots the policy, records lane order and pins the authority staging branch as the batch base (`refs/starlings/<batch>/base` in the mirror). Identical re-runs are idempotent; different authority, lanes or manifest for the same batch is a conflict.
- `task assign` atomically claims a task (the same owner and arguments are idempotent; another claimant conflicts) and creates a worktree on `starlings/<batch>/<task>` at the base. `--after` names prerequisites that must already be assigned.
- `task handoff` requires a full commit ID present in the mirror object store and descending from the base, and retains it at `refs/starlings/<batch>/tasks/<task>`. Prerequisite SHAs must match the `--after` declarations and the prerequisites' handed-off heads; otherwise the task is parked with a reason. A parked or not-yet-included task may be handed off again.
- `lane assemble` takes the lane lease and includes handed-off tasks in assignment order (prerequisites are always assigned earlier) with `git merge-tree --write-tree` and `commit-tree`, advancing `refs/starlings/<batch>/lanes/<lane>` by compare-and-swap. A task parks if a same-lane prerequisite is not included, a cross-lane prerequisite is not yet staged, its diff from the base touches an `[assembly] excluded` glob, its head contains the handed-off head of another task in the batch that is not a declared prerequisite (directly or through the declared prerequisites' own `--after` chains), or it conflicts with the lane tip (conflicting paths are reported). Undeclared content parks with kind `dependency` and names the undeclared tasks; declared prerequisites need not be ancestors. Parked tasks other than handoff parks are retried on the next run.
- `lane resolve` accepts a resolution commit for a conflict-parked task only with `--review` and only if it contains the current lane tip and the task head; without `--review` the task stays parked. A resolution that adds an excluded path or another task's undeclared head (one not already in the lane tip) is refused and the task stays conflict-parked, so a corrected resolution can follow. The resolution is retained at `refs/starlings/<batch>/resolutions/<lane>/<task>` and becomes the lane tip.
- `lane freeze` records the tip at `refs/starlings/<batch>/frozen/<lane>`; further assembly or resolution on that lane is refused. The result lists `left_out` tasks (assigned, handed-off or parked, with state and reason) that the frozen tip does not contain; `status` shows the same per frozen lane.
- `stage` requires frozen lanes and an expiring target lease on the authority staging branch. Mode `each-lane` builds successive candidates in recorded lane order; `combined-ready` builds one candidate whose parents are the observed staging head followed by the lane tips. Each candidate is retained at `refs/starlings/<batch>/candidates/<digest>` and recorded with a `Candidate` and an `Assembly` provenance record. If `[gates.staging].scope` is not `none`, the first candidate is prepared and the command returns pending without advancing staging (verification is a later milestone). With scope `none`, objects are fetched into the authority and the staging branch advances with `git update-ref <ref> <new> <old>`; if staging moved after planning, nothing is written and the result is stale. Starlings never writes the release branch.
- If a round conflicts with the staging head, `stage` blocks with the conflicting paths. Supply `--resolution <sha> --review <ref>` (both together; `--resolution` alone is blocked as unreviewed, `--review` alone is invalid). The option applies only to a conflicting round; in `each-lane` mode `--lanes` must name exactly one not-yet-staged lane. The resolution must be in the mirror and contain every round lane's frozen tip (otherwise invalid) and the current staging head (otherwise stale: staging moved since it was made). Paths it changes beyond the lanes' own changes that match an excluded glob, and undeclared task heads not already in staging or the lane tips, block with no write. The candidate keeps the `Candidate` shape: the resolution's tree with parents `(staging head, *lane tips)`. The resolution is retained at `refs/starlings/<batch>/resolutions/@stage/<candidate digest>` and recorded in the `Assembly` resolutions as `("@stage", "<comma-joined lanes>", head, review)`; `status` lists `stage_resolutions`.
- Lost acknowledgements: a candidate is journalled as `prepared` before the authority write. At the start of each round, if a `prepared` candidate for exactly those lanes is already contained in the staging head, `stage` reconciles it (marks the candidate, lanes and tasks staged; entry `reconciled: true`) without writing. If a round lane's frozen tip is already in the staging head with no recorded landing, `stage` refuses with status `blocked`, code `already-landed`, and writes nothing.
- `status` lists tasks (assigned, handed-off, included, parked with reason, staged), heads, reviews, lane tips, frozen state, resolutions, candidates with provenance, current authority heads and, for every handed-off task, its retained ref and whether it still matches.

Leases store absolute expiry. An expired lease can be taken over; the previous owner's later write, finalize or release is refused. Commit timestamps and identity for commits created by Starlings are fixed per batch, so rebuilding the same inputs yields the same candidate.

Limitations: one coordinator machine; lane-ref/journal divergence after a crash during assembly is reported as stale rather than reconciled; staging recovery covers only the lost-acknowledgement and externally-landed cases above; excluded globs use `fnmatch` semantics where `*` also matches `/`; lane resolutions are checked against them only for their diff from the lane tip; undeclared-content detection sees only handed-off heads (not unrecorded commits); pending candidates are not verified or consumed; worktrees are not cleaned up; leases do not supervise processes. Anyone with direct write access to the authority can bypass these checks.

Every subcommand accepts `--directory PATH`. Initialization defaults to preview, is offline, launches no subprocesses, and only `--write` writes `starlings.toml`. It stages a complete UTF-8 file in the same directory, flushes it, then publishes by exclusive hard link. Existing files, directories and even dangling symlinks are never replaced. A crash can leave a `.starlings-*` temporary file; inspect before manual cleanup. No crash-durable directory journal is claimed. No instruction or workflow files are touched.

The generated check is a **synthetic, deliberately failing example**, not project coverage. Replace it with reviewed project commands. Presets select task, staging and release scopes; they do not claim four real check families or expected execution counts. Release-only staging may contain defects. `policy validate` validates structure, not command safety, command availability, meaningful test coverage, policy trust or successful execution.

## Policy schema 1

`starlings init --preview` is the complete schema example. All fields in that output are required except disabled gates may omit `executor`, `environment` and `checks`. Unknown fields are errors at every table. The optional `[assembly]` table accepts `excluded`, a duplicate-free array of relative path globs; tasks whose changes touch a matching path park during assembly. `project` is a nonempty name; `staging` and `release` are distinct Git branch names. `fully_verified_release = true` requires release scope `full`.

The gates are `task`, `staging`, `release`. Scopes: `none`, `affected`, `full`. Staging mode: `each-lane` or `combined-ready`. Enabled gates require a nonempty, duplicate-free list of existing check IDs plus executor (`local`, `github`) and environment (`native`, `guest`). Disabled gates remain fully structurally validated. Checks are nonempty argv arrays, never shell strings; empty non-executable arguments are allowed. Check IDs use lowercase letters, digits, hyphens and underscores, starting with a letter. Commands are never evaluated by this milestone. There is no check-dependency graph in schema 1; gate-to-check references are the only execution dependencies.

Positive integer limits: `cpu_slots` (up to 1024), `memory_mib` (up to 2^30), `disk_mib` (up to 2^40), `timeout_seconds` (up to 604800). `max_guests` is an integer 0–1024; enabled guest gates require at least 1. Booleans are not integers. Limits are declarative, not enforced here. GitHub, guests and affected planning are accepted configuration vocabulary but explicitly unsupported execution features in diagnostics. Future affected planning must expand unknown or ambiguous changes to full coverage.

`authority.mode` currently accepts only `local-bare`. The first authority design is a local bare Git repository with frozen source refs and expected-target updates in one ref transaction, under exclusive coordinator control. The authority path is supplied per batch to `batch prepare`; `doctor` does not register or inspect it. A direct Git writer can bypass a CLI; future enforcement requires actual exclusive credentials or equivalent server controls. Remote authorities and GitHub enforcement remain unsupported.

## Private state and contracts

`doctor --state-dir /absolute/private/path` describes, validates and never creates state. Default: `~/.local/state/starlings`. State must be separate from the checkout (neither ancestor nor descendant), with no symlink components. An existing root and known entries must be operator-owned and inaccessible to group/other; directories need mode 0700 or stricter and files 0600 or stricter. Known children: `starlings.db` (with its `-wal` and `-shm` files), `mirrors/` and `worktrees/` (created by assembly commands), plus planned `policy/`, `logs/`, `evidence/`, `artifacts/`, `signer-references/`, `journal.sqlite3`. These checks are not a race-proof file-opening authority or sandbox. No keys or signer references are created.

`starlings.contracts` supplies frozen `Candidate`, `Plan`, `Evidence`, `Adapter` and `Assembly` records. Schema is integer 1. IDs are `<kind>:<sha256>` over ASCII canonical JSON (sorted keys, compact separators, escaped Unicode) containing schema, kind and all typed fields, excluding ID. Tuple order is identity-significant. IDs are content identities, **not signatures**. Candidate binds repository/authority labels, target, base, ordered parents, tree and exact commit; plan binds candidate ID, policy digest, gate, checks and execution location; evidence records one check outcome and artifact digest; adapter describes name, version and capabilities; assembly binds a candidate ID to the batch, ordered lanes, ordered entries `(task, lane, head, review, ((prerequisite, head), ...))` and resolutions `(lane, task, head, review)`. These lean records are not complete promotion receipts: future verifier/environment/approval identity, retention, trust and signing contracts require explicit extensions and versioning. Candidate and assembly records are stored as JSON in the state database; general deserialization is not implemented. Never treat constructing a record as proof that it happened.

## Output and development

`--json` works before or after subcommands. JSON responses use `schema`, `ok`, and either `data` or `error` (`code`, `message`). Assembly commands also include `status` (`ok`, `pending`, `parked`, `blocked`, `stale`, `failed`); errors may carry `error.data`. Exit statuses: 0 ok; 2 usage, invalid input or invalid policy/state configuration (including unsupported Git); 3 filesystem/missing-file/conflict errors, including a conflicting re-prepare or a task claimed by another owner; 4 pending (candidate prepared, staging not advanced); 5 parked or blocked (tasks parked, lease held, lane frozen or not frozen, staging conflict, unreviewed or excluded or undeclared staging resolution, already landed); 6 stale (compare-and-swap failure, moved target or lost lease; nothing further written); 7 failed (Git or database failure). Unsupported later commands fail with status 2, never fake success. Human help/version use standard argparse formatting; `--version --json` returns the JSON envelope. `doctor` is read-only inventory, not a readiness certificate; a missing manifest is informational.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
uv build --offline --wheel --out-dir /path/outside/checkout/wheels
```

Runtime has no third-party dependencies. The isolated build uses pinned Hatchling 1.33.0. Build dependencies must already be cached for offline builds. Current tests demonstrate Python 3.14 on macOS; other Python/platform combinations are not qualified. The next milestones implement a real verifier, then promotion and recovery integration.

## License

Starlings is licensed under the Apache License 2.0; see `LICENSE`. Attribution to Kevin Barasa must be retained as described in `NOTICE`.
