# Contributing

Bug reports, documentation fixes, new checks and host-adapter work are all welcome. The project is still pre-release, so open an issue before large or speculative changes.

## Development setup

Starlings needs Python 3.11 or later (developed and tested on 3.14) and has no third-party runtime dependencies. You also need Git 2.38 or later and OpenSSH `ssh-keygen` with `-Y` signing support. Development adds `ruff` for lint and `uv` for packaging (pip plus a PEP 517 frontend such as `build` works just as well).

No install is required: run everything with `PYTHONPATH=src`. An editable install (`pip install -e .`) or `uv sync` also works.

Keep build output, virtual environments and test fixtures outside the checkout.

## Testing

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
ruff check .
```

For packaging, build the wheel into a directory outside the checkout:

```sh
uv build --offline --wheel --out-dir <wheel-dir>
```

As a smoke check, `starlings demo --directory <dir>` runs the whole release-only flow end to end in a disposable directory. It works offline and needs no setup.

Most of the suite is hermetic: tests build disposable bare authorities in temporary directories, use a fake docker binary for guest checks, and inject clocks rather than sleeping. The one real guest-parity test is env-gated: it needs a running docker daemon (qualified on Colima), `STARLINGS_TEST_GUEST_IMAGE` set to an image pulled locally, and `STARLINGS_TEST_GUEST_ROOT` set to a directory shared with the docker VM. Without them it skips cleanly.

`tests/test_readme.py` parses every shell example in the README against the real CLI parser and resolves every Markdown link, so commands and links in the docs must be real. Documentation edits can fail tests.

## Conventions

- **Stdlib-only runtime.** The `dependencies` list in `pyproject.toml` is empty and stays that way. Propose any runtime dependency in an issue first.
- **Argv subprocesses only.** All Git and tool calls run as argv lists; `shell=True` is never acceptable.
- **State outside the checkout.** Everything starlings writes lives under the state directory (`--state-dir` or `STARLINGS_STATE_DIR`, default `~/.local/state/starlings`). Commands are preview-first (`init --preview`, `templates github`); writes happen only behind explicit flags.
- **Versioned records.** Records in `contracts.py` carry a `schema` field that feeds their content-derived IDs, and the database carries a `schema_version` with migrations in `store.py`. Changing a record shape or the database schema means bumping the version and writing the migration path; silent format changes are not accepted.
- **Security-sensitive verification.** `verifier.py`, `signer.py` and `promotion.py` hold the trust boundary: receipt signatures, evidence binding and journaled ref writes. Changes there need tests proving tampered input is rejected, not just that the happy path works.
- **Compact code, few comments.** Match the existing style: dense code with comments only where the reason is not visible in the code itself.
- **No private material.** `tests/test_no_private_terms.py` scans every tracked file for absolute home-directory paths, private-key blocks and workspace markers. Do not commit local paths, machine-identifying details or credentials. An optional operator denylist (`STARLINGS_DENYLIST`) extends the scan and itself stays outside the checkout. Extend `.gitignore` whenever a change can leave new untracked artifacts inside the checkout.

## The open qualification: hosted GitHub

The `github` host adapter (`src/starlings/adapters/github.py`) is implemented and submits real checks through the `gh` CLI, but it is unqualified. A rehearsal against a private repository proved dispatch, routing, result-artifact binding and pull-request dispositions, yet no real GitHub environment has produced a passing hosted gate, so hosted execution stays unsupported.

Qualifying it is a welcome contribution. That means running a hosted staging or release gate to green on a real repository and submitting the evidence: recorded run IDs, the receipts and their `starlings check` output, the environment used, and honest notes on what remains uncovered (event ordering, runner parity, protections). Pull requests that claim hosted support without that evidence will not merge.

## Pull requests

Work on a fork or a feature branch. Keep the test suite and `ruff check .` green. In the description, say what evidence backs any behavior claim — a test, a recorded run, a transcript — rather than asserting it. For bugs, open an issue with a reproduction before or alongside the fix. Maintainers review and merge.

## License

Contributions are licensed under the Apache License 2.0, the same as the project (see `LICENSE`).
