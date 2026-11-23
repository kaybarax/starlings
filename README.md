# Starlings

Choose when and where CI runs for a software engineering agent swarm.

![A murmuration of starlings at sunset above a skyline blending New York, London, and Berlin.](docs/images/starlings-skyline.png)

Starlings facilitates running CI locally for a swarm of software engineering agents, instead of on paid hosted CI services such as GitHub Actions and the like. It is not a CI system itself. It is a workflow for CI runs and integration checks across many worktrees. As the swarm's work moves from task worktrees into lanes, onto a staging branch and finally onto a release branch, you decide at which of those steps your checks run, and Starlings schedules them on your machine within the CPU, memory and time it has. Nothing lands on a gated branch without a signed, passing result for that exact commit.

Version 0.1.0 is the first release, and it covers the complete local workflow. Coordinating gates on hosted CI (GitHub Actions) is included as an experimental option that has not been tested end to end yet. See [Hosted checks on GitHub](#hosted-checks-on-github-experimental).

## Why Starlings

An agent swarm produces a lot of parallel work. Whether it comes from one harness or several, and whether its subagents work side by side or one after another, it all ends up in worktrees whose changes must be merged into `dev` and eventually into `main`. If every one of those branches triggers a full run on a paid hosted CI service, the bill and the waiting time grow with every task and every lane.

Starlings makes it practical to run those checks on your own machine instead. Local runs carry no service cost, and Starlings keeps them efficient, robust and fast. You also choose where CI sits in the flow to match the compute you have: with CI only at release, a batch of 32 tasks across four lanes needs one CI run, and with CI at every step the same batch needs 37. Checks are admitted against the CPU slots, guest slots and exclusive resources you configure, so the swarm never asks more of the machine than it has.

The aim is local CI that is almost as fast as hosted CI, without giving up the guarantee that every landing on a gated branch was verified. If you still want some gates to run on hosted CI, Starlings can coordinate those too.

## Features

- **CI where you want it.** Place CI at any of three transitions: task to lane, lane to staging, and staging to release.
- **Resource-aware runs.** Checks are admitted against configured CPU slots, guest slots and exclusive resources such as ports, and wait rather than overload the machine.
- **Isolated task worktrees.** Each task gets its own worktree and is handed off as an exact, reviewed commit.
- **Deterministic assembly.** Lanes and staging candidates are built with `git merge-tree`, so the same inputs always produce the same commit. Conflicts, undeclared dependencies and changes to excluded paths park the task with a reason instead of landing.
- **Signed receipts.** Check results are recorded in receipts signed with an SSH key and bound to the candidate commit, the trusted policy and the retained logs and artifacts.
- **Guarded landing.** `promote` is the only command that advances a gated branch. It re-verifies the receipt, the trusted policy, required approvals and the branch heads, journals its intent, then lands in one atomic ref transaction.
- **Crash recovery.** `recover` reconciles interrupted promotions, assembly steps and lost runs from the journal.
- **Failure diagnosis.** `diagnose` re-runs a failed release gate over subsets of the frozen lanes to name the responsible task, lane or lane pair.
- **Built for agents.** Every command accepts `--json` and returns stable identifiers and exit statuses, so harnesses and subagents can drive it directly.
- **No dependencies.** Pure Python standard library, plus Git and OpenSSH. Works fully offline.

## How it works

```text
task worktrees ──► lane branch ──► dev (staging) ──► main (release)
              task gate      staging gate       release gate
```

1. A **batch** pins the current staging commit as the starting point for one round of swarm work.
2. Each **task** is assigned to an agent in its own worktree. When the work is done, its commit is handed off with a review reference.
3. Handed-off tasks are **assembled** into their **lane** in dependency order, and the lane is **frozen**.
4. Frozen lanes are **staged** onto the staging branch, one lane at a time or all ready lanes together.
5. The staging branch is **planned**, **run** and **promoted** to the release branch.

A **gate** is a CI run at one of those transitions: the set of checks that must pass before work moves on. The policy file in your project says where gates sit, which checks they run, and how much of the machine they may use.

### Concepts

| Term | Meaning |
| --- | --- |
| Authority | The bare Git repository whose branches are the source of truth. |
| Batch | One round of parallel work, started from a pinned staging commit. |
| Lane | A branch that collects related tasks, for example the work of one harness or one group of subagents. One writer assembles it. |
| Task | One unit of agent work, done in its own worktree and handed off as a reviewed commit. |
| Candidate | The exact commit a gate plans checks against, bound to its target branch. |
| Gate | The CI run required at a transition: task, staging or release. |
| Policy | `starlings.toml`, the file that defines gates, checks and limits. |
| Trusted policy | The policy version you have reviewed and registered. Only it can plan or run checks. |
| Plan | The exact candidate and the checks that will run against it. |
| Receipt | A signed record of a plan's check results. |
| Promote | The only command that lands a verified candidate on a gated branch. |

## Requirements

- Python 3.11 or later. There are no third-party runtime dependencies.
- Git 2.38 or later.
- OpenSSH `ssh-keygen` with `-Y` signing support, used to sign and verify receipts.
- Optional: Docker, for running checks in a pinned container image.
- Optional: the GitHub CLI `gh`, for the experimental GitHub executor.

## Installation

Build a wheel from a source checkout and install it into a fresh virtual environment outside the checkout:

```sh
uv build --offline --wheel --out-dir <wheel-dir>
python3 -m venv <venv-dir>
<venv-dir>/bin/python -m pip install <wheel-dir>/starlings-0.1.0-py3-none-any.whl
<venv-dir>/bin/starlings --version
```

To run from a source checkout without installing, use `PYTHONPATH=src python3 -m starlings`.

## Quick start

`starlings demo` runs the whole flow end to end in a disposable directory. It generates an example project, creates a bare authority repository, and drives one lane with two tasks through assembly, staging, a release gate and promotion. It works offline and needs no setup:

```sh
starlings demo --directory <empty-dir>
```

The directory must be absent or empty, and everything the demo writes stays inside it. The summary ends with the new release commit and a `starlings status` command you can use to inspect the result.

## Usage

The steps below walk through the same flow by hand, using the `release-only` preset.

### 1. Set up a project and authority

Generate an example project (or add a `starlings.toml` to your own), commit it, and push the staging and release branches to a local bare repository that acts as the authority:

```sh
starlings examples init <project-dir> --preset release-only
cd <project-dir>
git init -b dev . && git add -A && git commit -m "Add starlings policy"
git init --bare <authority-path>
git push <authority-path> HEAD:refs/heads/dev HEAD:refs/heads/main
```

The authority path must be an absolute path to a local bare repository.

### 2. Trust the policy and create a signer

Choose a private state directory outside the checkout, register the reviewed policy, and create the receipt signing key:

```sh
export STARLINGS_STATE_DIR=<private-state-dir>
starlings policy trust --authority <authority-path> --commit <policy-sha> --review <review-ref>
starlings signer init
starlings authority protect --authority <authority-path>   # optional: refuse direct writes to staging and release
starlings doctor --authority <authority-path>
```

`<policy-sha>` is the full commit ID on the authority that contains the reviewed `starlings.toml`, and `<review-ref>` is any reference to the review that approved it, such as a pull request URL.

### 3. Run a batch

Prepare a batch, assign tasks, and hand off their commits:

```sh
starlings batch prepare demo --lanes work --authority <authority-path>
starlings task assign demo feat-a --lane work --owner you
# make and commit a change in the worktree that task assign returned
starlings task handoff demo feat-a --head <task-sha> --review <review-ref>
```

Assemble and freeze the lane, then stage it:

```sh
starlings lane assemble demo work
starlings lane freeze demo work
starlings stage demo --lanes work
starlings status demo
```

### 4. Verify and release

Plan the release gate, run its checks, confirm the receipt, and promote:

```sh
starlings plan demo --gate release
starlings run <plan-id>
starlings check <receipt-id>
starlings promote <plan-id> --receipt <receipt-id>
```

If the run fails, `starlings diagnose demo --gate release --receipt <receipt-id>` narrows the failure to a task, lane or lane pair. If anything is interrupted, `starlings recover demo` reconciles the batch.

## Presets

Presets are ready-made policies. Preview one with `starlings init --preset <name> --preview`, and add `--write` to create `starlings.toml` (an existing file is never replaced). `starlings examples init <dir> --preset <name>` generates a complete working example project for the same preset.

| Preset | Task gate | Staging gate | Release gate | Choose it when |
| --- | --- | --- | --- | --- |
| `release-only` | off | off | full | Compute is tight and you want the fewest CI runs, accepting that staging can be broken until release. |
| `combined-lanes-release` | off | full, all ready lanes together | full | You can afford one extra run for an early signal that the lanes work together. |
| `each-lane-release` | off | full, one per lane | full | You can afford a run per lane and want to know which lane broke staging. |
| `every-task-lane-release` | full | full, one per lane | full | You have compute to spare and want the earliest feedback on every task. |

The policy written by `starlings init` contains a placeholder check that fails on purpose. Replace it with your project's own commands before use.

## Configuration

`starlings.toml` lives in your project and is reviewed like code. A minimal release-only policy looks like this:

```toml
schema = 1
project = "sample-ledger"
staging = "dev"                # staging branch
release = "main"               # release branch; only promote advances it
fully_verified_release = true  # requires release scope "full"

[authority]
mode = "local-bare"

[gates.task]
scope = "none"                 # none | affected | full

[gates.staging]
mode = "each-lane"             # each-lane | combined-ready
scope = "none"

[gates.release]
scope = "full"
executor = "local"             # local | github (experimental)
environment = "native"         # native | guest (pinned Docker image)
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

The `[limits]` table sizes CI to your machine: `cpu_slots` and `max_guests` bound how many checks run at once across the swarm, and `timeout_seconds` bounds each check. Optional tables add path-based check selection for scope `affected` (`[selection]`), required artifacts (`[artifacts]`), exclusive resources such as ports (`[exclusive]`), excluded paths for assembly (`[assembly]`), required approvals per gate, a pinned guest image (`[guest]`) and GitHub settings (`[github]`). Run `starlings policy validate` to check the file's structure. The full schema is in [docs/policy.md](docs/policy.md).

## State and trust

Starlings keeps its database, worktrees, logs, trusted policies, receipts and signing key in a private state directory, separate from your checkout. Set it with `--state-dir` or `STARLINGS_STATE_DIR`. The default is `~/.local/state/starlings`.

Three things carry trust: the policy version registered with `policy trust`, the signing key created by `signer init`, and the receipts that key signs. `check` and `promote` verify all of them again before anything lands. The signing key is stored unencrypted and is protected only by the state directory's permissions.

## Roles in the swarm

Agents drive Starlings through the CLI. Add `--json` to any command for a structured result, and use the exit status to tell success from pending, blocked, stale or failed work.

A typical split of roles:

- A **coordinator agent** prepares the batch and assigns each task to a worker, then assembles and freezes lanes, stages, and runs the gates and promotions.
- A **worker agent** receives a worktree from `task assign`, commits its change there, and reports the exact commit for `task handoff`. It never plans, runs or promotes.

Workers can come from one harness or several. Tell your agents in your project's instruction files to use Starlings for every merge and never to write to lane, staging or release branches directly. `starlings authority protect` installs a hook in the authority that refuses direct writes to the staging and release branches, which guards against mistakes and drift.

## Commands

| Group | Commands | Purpose |
| --- | --- | --- |
| Setup and policy | `init`, `policy show`, `policy validate`, `policy trust`, `signer init`, `examples init`, `demo`, `authority protect`, `doctor` | Create project files, register the policy, manage the signer, run the demo, protect the authority and check the setup. |
| Batch, tasks and lanes | `batch prepare`, `task assign`, `task handoff`, `lane assemble`, `lane resolve`, `lane freeze`, `stage`, `status` | Move work from task worktrees into lanes and onto staging. |
| Verification | `plan`, `run`, `cancel`, `check` | Plan a gate, run its checks and validate the receipt. |
| Landing and recovery | `approve`, `revoke`, `promote`, `recover` | Record approvals, land a verified candidate and reconcile after an interruption. |
| Diagnostics | `diagnose` | Find the task or lane responsible for a failed release gate. Results never authorize a promotion. |
| Publishing | `templates github` | Preview or write the GitHub workflow templates used by the GitHub executor. |

Run `starlings <command> --help` for options. Exit statuses are listed in [docs/policy.md](docs/policy.md#output-and-exit-statuses), and full command semantics are in [docs/operations.md](docs/operations.md).

## Hosted checks on GitHub (experimental)

Starlings can also coordinate gates that run on hosted CI, so you can keep some checks there while the rest run locally. The supported host today is GitHub Actions: staging and release gates can set `executor = "github"` to run their checks there through the `gh` CLI, with results bound into the same signed receipts. `starlings templates github` generates the workflow files this needs.

This executor is implemented but has not yet been tested end to end against GitHub, so treat it as unsupported in 0.1.0 and use the local executor for real work. By default a `[github]` policy uses a local simulated host (`adapter = "fake"`) that exercises the workflow without contacting GitHub. The task gate always runs locally. Details are in [docs/operations.md](docs/operations.md#hosted-execution).

## Limitations

- **Local authority only.** The authority must be a local bare Git repository. Remote authorities and deployment are out of scope.
- **One coordinator machine.** All CI runs on the machine that holds the state directory. Checks within one run execute one after another (separate runs share the configured slots), and task worktrees are not cleaned up automatically.
- **Hook protection is not access control.** `authority protect` refuses unrecorded writes to staging and release, but anyone with the same login can edit or bypass the hook.
- **Native checks are trusted.** They run as your user and can read the state directory, including the signing key. Memory and disk limits are recorded, not enforced. Use native checks only for agent work you trust to run as you.
- **Guest checks are not a security boundary.** They run in a pinned Docker image with no network, and have been verified on one Docker setup only.
- **Validation is structural.** `policy validate` checks the file's shape, not whether your commands are safe or give enough coverage.
- **Tested platform.** 0.1.0 has been tested on macOS (Apple Silicon). Other platforms have not been verified yet.

## Documentation

- [docs/operations.md](docs/operations.md): full behaviour of every command.
- [docs/policy.md](docs/policy.md): policy schema, record formats, state directory and exit statuses.
- [docs/release-runbook.md](docs/release-runbook.md): building, installing, validating and recovering.
- [docs/qualification.md](docs/qualification.md): how Starlings was tested, with benchmarks and supported profiles.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
ruff check .
```

Contributor conventions are in [AGENTS.md](AGENTS.md).

## License

Starlings is licensed under the Apache License 2.0. See [LICENSE](LICENSE). Attribution must be retained as described in [NOTICE](NOTICE).
