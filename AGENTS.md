# Development conventions

- Python package uses `src/starlings`; runtime is stdlib-only, Python >=3.11.
- Run tests: `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
- Build offline from cached pinned build dependencies: `uv build --offline --wheel --out-dir /absolute/external/wheels`.
- Keep temporary environments and acceptance fixtures outside the checkout; build output and bytecode are ignored.
- Write regression tests before implementation where practical. Keep schema and content-ID changes explicit and versioned.
- Milestones 1-2 cover policy/configuration/contracts and local assembly only. Do not represent records as signatures, validation or the recorded policy digest as trusted policy, staging as verified, or diagnostics as enforcement. Never write the release branch. Initialization never edits harness instructions or workflows.
- Module ownership: `store.py` (SQLite schema, transactions, leases, request idempotency), `gitops.py` (all Git as argv subprocesses, hooks disabled, file protocol only), `batches.py` (batch/task/lane/stage logic); keep `cli.py` wiring thin. Exit statuses live in `batches.EXIT`.
- Assembly tests build disposable bare authorities in temp dirs via `tests/fixtures.py`; use barriers, hooks and injected clocks, never sleeps. Lease tests inject time through `Store(path, clock)` / `Context(..., clock=...)`. Test hook points: `lane-lease` (assemble), `target-lease`, `before-write`, `after-write` (stage).
- Lint: `ruff check .`. Check Git with `git --version` (>= 2.38 required).
- Licensed Apache-2.0. Keep `LICENSE` and `NOTICE` intact. Publication still requires owner authorization.
- At the end of each milestone, commit and push to `origin/main` only after the owner gives the go-ahead for that milestone; never push without it.
