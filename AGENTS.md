# Development conventions

- Python package uses `src/starlings`; runtime is stdlib-only, Python >=3.11.
- Run tests: `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
- Build offline from cached pinned build dependencies: `uv build --offline --wheel --out-dir /absolute/external/wheels`.
- Keep temporary environments and acceptance fixtures outside the checkout; build output and bytecode are ignored.
- Write regression tests before implementation where practical. Keep schema and content-ID changes explicit and versioned.
- Milestones 1-3 cover policy/configuration/contracts, local assembly, trusted policy, planning, runs and signed receipts. Only `policy trust` makes a policy trusted (the batch's recorded digest is not trust); only `check` evaluates a receipt; a passed receipt does not advance staging or release until milestone 4. Do not represent diagnostics as enforcement. Never write the release branch. Initialization never edits harness instructions or workflows.
- Module ownership: `store.py` (SQLite schema, transactions, leases, request idempotency), `gitops.py` (all Git as argv subprocesses, hooks disabled, file protocol only), `batches.py` (batch/task/lane/stage logic), `verifier.py` (policy trust, planning and selection, receipt checks), `runner.py` (admission, process supervision, guest adapter, evidence and receipt issue), `signer.py` (ssh-keygen signing); keep `cli.py` wiring thin. Exit statuses live in `batches.EXIT`. Candidate processes never receive the signer path or state directory in their environment; never use `shell=True`.
- Runner tests use `tests/test_verifier.py` helpers: tiny poll loops on marker files and the runner hook points `run-started`, `admission-wait`, `admitted`, `process-started`. Guest tests use a fake docker binary; the real parity test needs `STARLINGS_TEST_GUEST_IMAGE` and `STARLINGS_TEST_GUEST_ROOT` (a directory shared with the docker VM).
- Assembly tests build disposable bare authorities in temp dirs via `tests/fixtures.py`; use barriers, hooks and injected clocks, never sleeps. Lease tests inject time through `Store(path, clock)` / `Context(..., clock=...)`. Test hook points: `lane-lease` (assemble), `target-lease`, `before-write`, `after-write` (stage).
- Lint: `ruff check .`. Check Git with `git --version` (>= 2.38 required).
- Licensed Apache-2.0. Keep `LICENSE` and `NOTICE` intact. Publication still requires owner authorization.
- Every milestone updates README.md in the established style (scope, commands, schema, behaviour, limitations, license) so it describes the current state of the project; when the project is complete the README is its final version.
- At the end of each milestone, commit and push to `origin/main` only after the owner gives the go-ahead for that milestone; never push without it.
