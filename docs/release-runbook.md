# Release runbook

Install, validate and recovery steps for a starlings wheel. Version `0.1.0` is a pre-release: commands, the policy schema and file formats can still change, and hosted (`github` executor) checks are implemented but unqualified — no passing hosted gate has been recorded.

## Required tools

- Python 3.11 or later; the wheel has no third-party runtime dependencies.
- Git 2.38 or later (`git --version`; `merge-tree --write-tree` is required).
- OpenSSH `ssh-keygen` with `-Y` signing support (receipts).
- Optional: docker on a VM whose mounts cover the checkout path (guest checks), `gh` (the unqualified live GitHub adapter), `uv` (offline wheel builds).

## Build, verify and install

Build offline from the source checkout, record the digest, and install into a fresh venv outside any checkout:

```sh
uv build --offline --wheel --out-dir <wheel-dir>
shasum -a 256 <wheel-dir>/starlings-0.2.0-py3-none-any.whl   # record this digest
python3 -m venv <venv-dir>
<venv-dir>/bin/python -m pip install <wheel-dir>/starlings-0.2.0-py3-none-any.whl
<venv-dir>/bin/starlings --version
```

Verify a wheel before installing it by comparing `shasum -a 256` output (or `sha256sum` on Linux) against the digest recorded at build time. There is no published checksum; the build is the reference.

## First-time setup

Choose a private state directory outside the checkout — starlings validates ownership and permissions (0700 directories, 0600 files) and `doctor` inspects without creating:

```sh
export STARLINGS_STATE_DIR=<private-state-dir>   # or pass --state-dir to every command
```

Create a project (`starlings examples init <project-dir> --preset <name>` for a working fixture, or write `starlings.toml` by hand and `starlings policy validate --directory <project-dir>`), commit it, create a bare authority repository and push the staging and release branches. Then:

```sh
starlings policy trust --authority <authority-path> --commit <policy-sha> --review <review-ref>
starlings signer init
starlings authority protect --authority <authority-path>   # optional hook enforcement
starlings doctor --authority <authority-path>
```

`signer init` creates an unencrypted ed25519 key under the state directory; its only protection is the directory's permissions. The full batch walkthrough is the [README quick start](../README.md#quick-start); the per-command semantics are in [operations.md](operations.md).

## Validating the install

```sh
starlings demo --directory <empty-dir>
```

`demo` drives the whole release-only flow end to end in a disposable directory — authority, policy trust, signer, batch, lane, run, receipt check and promote — and prints a short summary. It needs no edits and works offline.

## Recovery

- `starlings recover <batch>` reconciles every target and lane journal, finishes or drops interrupted promotions, marks lost runs `interrupted` (never signed) and releases their slots.
- If a landing cannot be resolved from the observed ref, the target parks as `ambiguous-landing`; inspect, then resolve explicitly with `starlings recover <batch> --resolve <promotion-id> --as landed|not-applied`.
- Leases expire: an expired lease can be taken over, and the previous owner's later write is refused. Recovery skips work whose lease is still live.
- Crash leftovers are inspectable: `.starlings-*` init tempfiles, run worktrees and job directories under the state root, and guest containers named `starlings-<run>-<check>`.

## Supported profiles

| Profile | Status |
|---|---|
| Native: macOS (arm64), Python 3.14, Git ≥2.38 | Full suite and all four presets qualified; the recorded profile is in [qualification.md](qualification.md). |
| Guest: Colima VM (vz, virtiofs), pinned `name@sha256:` image | Parity passed on one recorded profile; other docker hosts and images are unqualified. |
| Hosted: `adapter = "github"` via `gh` | Implemented, unqualified — dispatch, routing, binding and dispositions rehearsed; a passing hosted gate could not be exercised during rehearsal. |
| Other platforms, Pythons and docker setups | Not qualified. |

## Reproducing the fixtures

```sh
starlings examples init <project-dir> --preset <name> --with-qualification
PYTHONPATH=src python3 -m starlings.qualification run --root <evidence-root> --preset all
PYTHONPATH=src python3 -m starlings.qualification bench --root <evidence-root>
PYTHONPATH=src python3 -m unittest discover -s tests        # source checkout; includes the private-terms scan
```

`tests/test_no_private_terms.py` scans every tracked file for private markers and extends from `$STARLINGS_DENYLIST` or `~/.local/state/starlings/denylist.txt` — the denylist stays outside the checkout and is never committed.

## Release evidence

[qualification.md](qualification.md) is the evidence index: the qualification driver writes `evidence/index.json` + `index.md` per preset (gate counts, per-check metrics, calls, traceability) and `bench.json` + `bench.md` for benchmarks; the test suite is 125 tests on the recorded profile; supported profiles and qualification limitations are listed there. Evidence is generated under a caller-chosen root outside the checkout — nothing lands in the repository.
