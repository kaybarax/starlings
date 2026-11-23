# Starlings

Choose when and where CI runs for a software engineering agent swarm.

## Status

**Under active development, not yet released.** Local milestones 1–5 of 6 are complete; the optional GitHub milestone is offline-qualified against a deterministic local fake host and its live adapter is implemented, but a passing hosted run has not been recorded — hosted execution stays unsupported until it is qualified. Commands, the policy schema and file formats can still change, so do not rely on starlings for real projects yet.

| Milestone | What it delivers | Status |
| --- | --- | --- |
| 1. Contracts and package | Installable wheel, CLI, policy schema and presets | Done |
| 2. Local assembly | Batches, task worktrees, handoff, lane assembly and freeze | Done |
| 3. Verifier and runner | Gate planning, check execution, signed receipts, stale-result checks | Done |
| 4. Promotion and recovery | Guarded landing on staging and release, recovery after an interruption | Done |
| 5. Local qualification | Four lanes and 32 tasks under every preset, with benchmarks | Done (local) |
| 6. Optional GitHub operation | Hosted checks on GitHub Actions and pull request publishing | Done (offline; live adapter unqualified) |

This table is updated as each milestone is completed. Everything below describes the local path: a local bare Git repository as the authority, with checks run on your own machine. The one unfinished piece of the optional milestone — a passing hosted gate against a real GitHub remote — is noted where it appears.

## Why starlings

Coding agents make it cheap to run many tasks at once. Each task produces a branch, and if every branch triggers a full CI run, cost and waiting time multiply with every task and every lane of work.

Starlings lets you decide which steps are verified. Work moves from a task, to a lane, to a staging branch, to a release branch. You pick which of those three transitions run checks, and starlings makes sure nothing lands on a gated branch without a passing result for that exact change.

It is built for a solo engineer or small team coordinating several agents, and it works just as well for a person at a terminal.

## How it works

```text
task worktrees ──► lane branch ──► dev (staging) ──► main (release)
              task gate      staging gate       release gate
```

Each task gets its own Git worktree. Finished tasks are collected into a lane. Finished lanes are combined onto the staging branch. The staging branch is promoted to the release branch.

A gate is a set of checks that must pass before a transition happens. A policy file in your project says which gates are on. With every gate off except release, a batch of 32 tasks needs one CI run. With every gate on, the same batch needs 37.

## Concepts

| Term | Meaning |
| --- | --- |
| Authority | The bare Git repository whose branches are the source of truth. |
| Batch | One round of parallel work, started from a pinned staging commit. |
| Lane | A branch that collects related tasks. One writer assembles it. |
| Task | One unit of work, done in its own worktree and handed off as a reviewed commit. |
| Candidate | The exact commit a gate plans checks against, bound to its target branch. |
| Gate | The checks required at a transition: task, staging or release. |
| Policy | `starlings.toml`, the file that defines gates, checks and limits. |
| Trusted policy | The policy version you have reviewed and registered. Only it can plan or run checks. |
| Plan | The exact candidate and the checks that will run against it. |
| Receipt | A signed record that a plan's checks passed. |
| Promote | The only command that lands a verified candidate on a gated branch. |

## Install

Starlings needs Python 3.11 or later and has no third-party runtime dependencies. It also needs Git 2.38 or later and OpenSSH `ssh-keygen` with `-Y` signing support. Docker is optional and only used for guest checks.

From a source checkout, `PYTHONPATH=src python3 -m starlings` runs it without installing. To install, build a wheel and put it in a fresh environment outside any checkout:

```sh
uv build --offline --wheel --out-dir <wheel-dir>
python3 -m venv <venv-dir>
<venv-dir>/bin/python -m pip install <wheel-dir>/starlings-0.1.0-py3-none-any.whl
<venv-dir>/bin/starlings --version
```

## Quick start

`starlings demo` runs the whole release-only flow end to end in a disposable directory: it generates the example project, seeds a bare authority and drives one lane with two tasks through every step. It works offline, needs no edits and prints a short summary:

```sh
starlings demo --directory <dir>
```

The directory must be absent or empty; everything the demo writes stays inside it. A complete copy-and-paste version, including a failing variant, is in [docs/qualification.md](docs/qualification.md).

The same steps by hand look like this. Set up once:

```sh
starlings examples init <project-dir> --preset release-only
# commit the project, create a bare repository, and push dev and main to it
cd <project-dir>

export STARLINGS_STATE_DIR=<private-state-dir>
starlings policy trust --authority <authority-path> --commit <policy-sha> --review <review-ref>
starlings signer init
starlings authority protect --authority <authority-path>   # optional: refuse direct writes to staging/release
starlings doctor --authority <authority-path>
```

Then run a batch:

```sh
starlings batch prepare demo --lanes work --authority <authority-path>
starlings task assign demo feat-a --lane work --owner you
# make and commit a change in the worktree that was returned

starlings task handoff demo feat-a --head <task-sha> --review <review-ref>
starlings lane assemble demo work
starlings lane freeze demo work
starlings stage demo --lanes work

starlings plan demo --gate release
starlings run <plan-id>
starlings check <receipt-id>
starlings promote <plan-id> --receipt <receipt-id>
```

The authority path must be an absolute path to an existing local bare repository. The example project ships real `unittest` checks. A manifest created with `starlings init` contains a placeholder check that fails on purpose, so replace it with your project's own commands before use.

## Presets

`starlings init --preset <name> --preview` shows a complete `starlings.toml`. Add `--write` to create it; an existing file is never replaced. `starlings examples init --preset <name>` generates a working example project for the same preset.

| Preset | Task gate | Staging gate | Release gate | Choose it when |
| --- | --- | --- | --- | --- |
| `release-only` | off | off | full | You want the fewest runs and accept that staging can be broken until release. |
| `combined-lanes-release` | off | full, all ready lanes together | full | You want one early signal that the lanes work together. |
| `each-lane-release` | off | full, one per lane | full | You want to know which lane broke staging. |
| `every-task-lane-release` | full | full, one per lane | full | You have machine capacity to spare and want the earliest feedback. |

## Using starlings with coding agents

Agents use the same commands as a person. Add `--json` to any command for a structured result with stable identifiers, and rely on the exit status to tell success from pending, blocked, stale or failed work.

A typical split of roles:

- A **coordinator** prepares the batch, assigns each task to a worker with the repository, batch, lane, task, worktree and policy revision, then assembles and freezes lanes, stages and promotes.
- A **worker** receives a worktree from `task assign`, commits its change there, and reports the exact commit for `task handoff`; it never plans, runs or promotes.

`starlings authority protect --authority <authority-path>` installs a generated update hook into the authority that refuses writes to the recorded staging and release refs unless they match an intent starlings journaled first — a raw `git push` or `git update-ref` to those branches fails while `stage` and `promote` keep working. This guards against drift and accidents: a same-login agent that deliberately edits, deletes or bypasses the hook can still write the refs directly, so keep telling your agents in your project's instruction files to use starlings for every merge and never to write to lane, staging or release branches directly. `starlings doctor --authority <authority-path>` reports whether the hook is installed, unmodified and recorded.

**Planned:** ready-made instruction templates for specific agent tools, and a hosted qualification that records a passing gate on real GitHub Actions (the offline fake-host qualification is done and the live adapter is implemented, but unqualified).

## Commands

| Group | Commands | Purpose |
| --- | --- | --- |
| Setup and policy | `init`, `policy show`, `policy validate`, `policy trust`, `signer init`, `examples init`, `demo`, `authority protect`, `doctor` | Create project files, register the policy, manage the signer, run the demo, harden the authority and diagnose the setup. |
| Batch, tasks and lanes | `batch prepare`, `task assign`, `task handoff`, `lane assemble`, `lane resolve`, `lane freeze`, `stage`, `status` | Move work from task worktrees into lanes and onto staging. |
| Verification | `plan`, `run`, `cancel`, `check` | Plan a gate, run its checks, and validate the receipt. |
| Diagnostics | `diagnose` | Rebuild frozen lane subsets of a failed release candidate and re-run the failed checks to name the responsible task or lane pair; results never authorize a promotion. |
| Publishing | `templates github` | Preview or exclusively write the optional GitHub caller and reusable check workflows into `.github/workflows`; nothing touches `.github` unless asked. |
| Landing and recovery | `approve`, `revoke`, `promote`, `recover` | Record approvals, land a verified candidate, and reconcile after an interruption. |

Exit statuses are stable and documented in [docs/policy.md](docs/policy.md). Full options and semantics are in [docs/operations.md](docs/operations.md).

## Policy

`starlings.toml` lives in your project and is reviewed like code. A minimal release-only policy looks like this:

```toml
schema = 1
project = "sample-ledger"
staging = "dev"                # staging branch
release = "main"               # release branch; only promote advances it
fully_verified_release = true  # requires release scope "full"

[authority]
mode = "local-bare"            # the only supported authority

[gates.task]
scope = "none"                 # none | affected | full

[gates.staging]
mode = "each-lane"             # each-lane | combined-ready
scope = "none"

[gates.release]
scope = "full"
executor = "local"
environment = "native"         # or "guest" with a pinned [guest] image
checks = ["unit", "service"]

[checks]                       # check ID -> argv array, never a shell string
unit    = ["python3", "-m", "unittest", "tests.test_unit"]
service = ["python3", "-m", "unittest", "tests.test_service"]

[limits]
cpu_slots = 4
memory_mib = 8192
disk_mib = 20480
max_guests = 0
timeout_seconds = 1800
```

Checks are argument lists, never shell strings. Validation checks the structure of the file only. It does not run your commands or judge whether they give enough coverage. Use `starlings init --preview` for the authoritative rendered example, and see [docs/policy.md](docs/policy.md) for the full schema.

## State and trust

Starlings keeps its database, worktrees, logs, trusted policies, receipts and signing key in a private state directory, separate from your checkout. Set it with `--state-dir` or `STARLINGS_STATE_DIR`; `doctor` inspects the default `~/.local/state/starlings` without creating it.

Three things carry trust: the policy version you registered with `policy trust`, the signing key created by `signer init`, and the receipts that key signs. `check` and `promote` verify all of them again before anything lands.

The signing key is stored unencrypted and is protected only by the state directory's permissions.

## Limitations

- **Local only.** In v1 the authority must be a local bare repository. Remote authorities and deployment are unsupported. Staging and release gates may name executor `github` with an optional `[github]` table. The default `fake` adapter is a deterministic local simulation, offline-qualified for routing, receipt binding, dispositions and lost-response recovery only; it proves nothing about real event delivery, protections or runner parity. `adapter = "github"` is implemented and submits real checks through the `gh` CLI, but it is unqualified. A rehearsal against a private repository proved dispatch, routing, result-artifact binding and pull-request dispositions; a passing hosted gate could not be exercised during rehearsal. Hosted execution stays unsupported until someone qualifies it — issues are welcome. The task gate stays local-only (`plan` blocks `unsupported`), and hosted checks require environment `native`. See [docs/policy.md](docs/policy.md) and the hosted-execution section of [docs/operations.md](docs/operations.md).
- **Hook-based enforcement only.** `authority protect` refuses unrecorded writes to staging and release refs via a generated authority hook, but a same-login user or agent can still edit or delete the hook, run git with hooks disabled, or use an unprotected authority. It guards drift and accidents, not credentials.
- **One coordinator machine.** Checks within a run execute one after another; task worktrees are not cleaned up automatically; assembly leases do not supervise processes.
- **Native checks are trusted.** They run as you and can read the state directory, including the signing key. `memory_mib` and `disk_mib` are recorded, not enforced, and a descendant that calls `setsid`/`setpgid` escapes process-group tracking. Use native execution only for contributors you trust.
- **Guest checks are not a security boundary.** They run in a pinned Docker container (`--network none`, `name@sha256:` image), qualified on one recorded Colima profile; other docker hosts and images are unqualified.
- **Recovery has gaps.** Lane-ref/journal divergence that no include intent explains is reported as stale rather than reconciled, and ungated staging recovery covers only the lost-acknowledgement and externally-landed cases.
- **Validation is structural.** `policy validate` checks the file's shape, not command safety, availability or coverage. Qualification covers local execution on the recorded profile only; results are honest local measurements with no asserted thresholds — see [docs/qualification.md](docs/qualification.md).

## Contributing

Bug reports, documentation, checks and host-adapter work are welcome; qualifying the hosted GitHub adapter is the known open area. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, conventions and the pull-request process.

## License

Starlings is licensed under the Apache License 2.0. See `LICENSE`. Attribution must be retained as described in `NOTICE`.
