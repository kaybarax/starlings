# How starlings was qualified

`starlings examples init` writes a deterministic example project outside any checkout: a `starlings_example` source package, four `unittest` check entry points (`static`, `unit`, `service`, `journey`, each runnable as `python3 -m unittest tests.test_<name>`), and a `starlings.toml` for the chosen preset. Every byte is a fixed function of the preset and flags — no timestamps, randomness or network — and an existing nonempty target or file is never overwritten.

```sh
starlings examples init /path/outside/checkout/project --preset release-only --with-qualification --json
```

`--with-qualification` adds the `[assembly] excluded`, `[selection]`, `[artifacts]` and `[exclusive]` tables the qualification fixture uses; without it the manifest carries only gates, checks and limits.

`python3 -m starlings.qualification` is the qualification driver. `run` builds a disposable bare authority, project checkout, state directory and evidence root under `--root` (which must be outside any checkout), assigns four lanes of eight tasks, runs one lane worker OS process per lane behind a file start barrier (the simulated harnesses), then drives assembly, freezing, staging, planning, runs, receipt checks and promotion for the chosen preset using only public `starlings` CLI argv. `--preset` accepts one preset or `all`; `--lanes`/`--tasks` resize the shape (the qualification shape is the default 4×8). `bench` records the release-gate benchmark: `--cold N` fresh authority/state/project trials, `--warm N` trials sharing authority, state (mirror, database, signer) and project with a new batch each, plus one staging-conflict trial and one late-failure trial (a check that fails only in combination, then a fix-forward recovery batch). Evidence lands in `<root>/<preset>/evidence/index.json` + `index.md` for `run`, and `<root>/bench.json` + `bench.md` (with per-trial directories) for `bench`; nothing is written inside the source checkout.

## Fixture shape

The qualification shape covers a producer-to-consumer dependency (`api-producer` → `api-client`, cross-lane, declared with `--after` and a prerequisite merge), an independent same-lane producer/consumer pair (`gamma-producer` → `gamma-consumer`), a same-lane merge conflict resolved through `lane resolve` with a reviewed resolution (`alpha-conflict-b`), an excluded-path task that parks (`delta-excluded` writes `secrets/deploy.txt`), and otherwise independent feature tasks — 32 tasks on lanes `alpha`, `beta`, `gamma`, `delta`. Gate counts below are executions of plan+run+promote cycles and check executions (4 checks per enabled gate — `static`, `unit`, `service`, `journey` — scope `full` everywhere):

| Preset | Task gates/checks | Staging gates/checks | Release gates/checks | Total | Expected table |
|---|---|---|---|---|---|
| `release-only` | 0/0 | 0/0 | 1/4 | 1 gate, 4 checks | matches |
| `combined-lanes-release` | 0/0 | 2/8 | 1/4 | 3 gates, 12 checks | 2 gates, 8 checks — **deviation** |
| `each-lane-release` | 0/0 | 4/16 | 1/4 | 5 gates, 20 checks | matches |
| `every-task-lane-release` | 31/124 | 4/16 | 1/4 | 36 gates, 144 checks | 37 gates, 148 checks — **deviation** |

Two honest deviations from the expected gate-count table, both forced by the required fixture shape rather than a defect. `combined-lanes-release` cannot stage all lanes in one wave: the cross-lane `--after api-producer` declaration requires the consumer's lane to assemble only after the producer's lane is staged, so delta necessarily waits for a second staging candidate (2 staging gates, not 1). `every-task-lane-release` parks `delta-excluded` during assembly — an excluded-path task never produces a task candidate — so 31 task gates run instead of 32. Every other count matches the table exactly.

## Traceability

Each preset's `evidence/index.json` covers all 32 task heads: each retained task ref still equals the handed-off head, each task appears in its Assembly record(s) (the single-task task candidate under the task gate plus the lane staging candidates), each landed head is an ancestor of the final `dev` and `main` commits, and the parked task carries its recorded reason (`touches excluded paths: secrets/deploy.txt`).

## Benchmarks

Benchmarks were recorded on this machine — Mac15,9 (Apple M3 Max), 16 CPUs, 48 GiB, macOS 27.2.1 (arm64), Python 3.14.4, Git 2.54.0, starlings 0.1.0 — against the generated fixture's four unittest checks. Cold means a fresh authority, state directory and project per trial; warm means the same authority, state (mirror, database, signer caches) and project with a new batch per trial. Raw ranges are preserved in `bench.json`; per-check command timings are under `trials[].release.run_checks[].metrics.duration_seconds` and admission waits under `run_checks[].admission_waited` (the runner records the wait as a boolean, not a duration). No thresholds are asserted — they remain pending an owner-agreed workload.

| Trial | Release gate wall (s) | Outcome |
|---|---|---|
| cold ×3 | 1.835–1.839 | passed |
| warm ×3 | 1.841–1.882 | passed |
| staging conflict | 1.841 | passed (reviewed `--resolution`) |
| late failure (journey only) | 1.801 | failed; `promote` refused `receipt-failed` |
| late-failure fix batch | 1.854 | passed |

At this workload size cold and warm are indistinguishable — the release gate is dominated by the four sequential unittest processes (~1.2 s of the ~1.85 s) rather than mirror or cache warmth; assembly and orchestration overhead is recorded separately under `phases` and per-call seconds under `calls`. Assembly of one lane costs roughly 0.5–0.7 s per lane in these runs.

## Supported profiles

| Profile | Status |
|---|---|
| Native, macOS 27.2.1 (arm64), Python 3.14.4, Git 2.54.0 | Passes the full suite. |
| Qualification driver, Mac15,9 (M3 Max, 16 CPUs, 48 GiB), macOS 27.2.1 (arm64), Python 3.14.4, Git 2.54.0 | All four policy presets run end-to-end (4 lanes × 8 tasks) with full traceability; cold/warm release-gate benchmarks and conflict/late-failure trials recorded (this document). |
| Guest: Colima 0.10.1 (vz, virtiofs, Ubuntu 24.04.4 aarch64), docker client 29.4.3 / server 29.2.1, image `python@sha256:9ef1a599f863231c3d91e941389bfdc4a2d6070557243d54df1a4e57015b960e` (official `python:3.13-alpine` index, arm64, Python 3.13.18) | Parity passed: the same passing and failing journey check gives the same outcome, exit code and artifact digest natively and in the guest (`GuestParityTests`). |
| Any other docker host or image | Not qualified. Unit tests cover the adapter with a fake docker binary. |

The real parity test runs only when `STARLINGS_TEST_GUEST_IMAGE` names a locally present image and `STARLINGS_TEST_GUEST_ROOT` names a directory shared with the docker VM; otherwise it is skipped with that reason.

## Reproducing the qualification from the wheel

The steps below were executed verbatim against `starlings-0.1.0-py3-none-any.whl` installed in a fresh venv outside the checkout; the script and JSON envelopes are retained as evidence. Run everything from a directory outside any checkout — any external directory works for `ROOT`.

```sh
python3 -m venv /path/outside/checkout/venv
/path/outside/checkout/venv/bin/python -m pip install /path/to/starlings-0.1.0-py3-none-any.whl
/path/outside/checkout/venv/bin/starlings --version
```

```sh
ROOT=/path/outside/checkout/repro
VENV=/path/outside/checkout/venv
STARLINGS=$VENV/bin/starlings
PY=$VENV/bin/python
WORK=$ROOT/work
AUTH=$ROOT/authority.git
STATE=$ROOT/state
mkdir -p "$ROOT" && cd "$ROOT"

$STARLINGS examples init "$WORK" --preset release-only --with-qualification --json > init.json
cd "$WORK"
git init -q -b dev . && git add -A
git -c user.name=operator -c user.email=operator@localhost commit -qm "seed: example project"
git init -q --bare "$AUTH"
git push -q "$AUTH" HEAD:refs/heads/dev HEAD:refs/heads/main
export STARLINGS_STATE_DIR="$STATE"

$STARLINGS policy trust --authority "$AUTH" --commit "$(git -C "$AUTH" rev-parse refs/heads/dev)" --review review:policy --json > trust.json
$STARLINGS signer init --json > signer.json
$STARLINGS batch prepare tiny --lanes work --authority "$AUTH" --json > prepare.json

$STARLINGS task assign tiny feat-a --lane work --owner operator --json > assign-a.json
$STARLINGS task assign tiny feat-b --lane work --owner operator --json > assign-b.json
WT_A=$($PY -c "import json,sys; print(json.load(open('assign-a.json'))['data']['worktree'])")
WT_B=$($PY -c "import json,sys; print(json.load(open('assign-b.json'))['data']['worktree'])")

cat > "$WT_A/src/starlings_example/features/feat_a.py" <<'EOF'
"""Feature added by task feat-a."""


def feat_a():
    return "feat-a"
EOF
cat > "$WT_B/src/starlings_example/features/feat_b.py" <<'EOF'
"""Feature added by task feat-b."""


def feat_b():
    return "feat-b"
EOF
git -C "$WT_A" add -A && git -C "$WT_A" -c user.name=operator -c user.email=operator@localhost commit -qm "feat-a"
git -C "$WT_B" add -A && git -C "$WT_B" -c user.name=operator -c user.email=operator@localhost commit -qm "feat-b"
HEAD_A=$(git -C "$WT_A" rev-parse HEAD)
HEAD_B=$(git -C "$WT_B" rev-parse HEAD)

$STARLINGS task handoff tiny feat-a --head "$HEAD_A" --review review:feat-a --json > handoff-a.json
$STARLINGS task handoff tiny feat-b --head "$HEAD_B" --review review:feat-b --json > handoff-b.json
$STARLINGS lane assemble tiny work --json > assemble.json
$STARLINGS lane freeze tiny work --json > freeze.json
$STARLINGS stage tiny --lanes work --json > stage.json

$STARLINGS plan tiny --gate release --json > plan-release.json
PLAN=$($PY -c "import json,sys; print(json.load(open('plan-release.json'))['data']['plan_id'])")
$STARLINGS run "$PLAN" --json > run-release.json
RECEIPT=$($PY -c "import json,sys; print(json.load(open('run-release.json'))['data']['receipt_id'])")
$STARLINGS check "$RECEIPT" --json > check-release.json
$STARLINGS promote "$PLAN" --receipt "$RECEIPT" --json > promote-release.json
git -C "$AUTH" rev-parse refs/heads/main
```

`stage` writes `dev` directly under the release-only preset; the release gate then lands `main` only through `promote` with the passed receipt (`check-release.json` reports `status: valid`, `authorizes: true`).

For the failing variant, prepare a `broken` batch whose single task changes `return sum(values)` to `return sum(values) + 1` in `core.py`, then run the same assemble/freeze/stage/plan/run flow. The release `run` exits 7 with `outcome: failed` (only the `unit` check fails; `check` reports `status: failed`, `authorizes: false`), and `promote` refuses with error `receipt-failed` while `main` stays at the previous commit:

```sh
$STARLINGS batch prepare broken --lanes work --authority "$AUTH" --json > prepare-broken.json
$STARLINGS task assign broken break-total --lane work --owner operator --json > assign-broken.json
WT_BROKEN=$($PY -c "import json,sys; print(json.load(open('assign-broken.json'))['data']['worktree'])")
$PY - "$WT_BROKEN/src/starlings_example/core.py" <<'EOF'
import sys
path = sys.argv[1]
text = open(path).read()
assert "return sum(values)" in text
open(path, "w").write(text.replace("return sum(values)", "return sum(values) + 1", 1))
EOF
git -C "$WT_BROKEN" add -A && git -C "$WT_BROKEN" -c user.name=operator -c user.email=operator@localhost commit -qm "break compute_total"
HEAD_BROKEN=$(git -C "$WT_BROKEN" rev-parse HEAD)
$STARLINGS task handoff broken break-total --head "$HEAD_BROKEN" --review review:break-total --json > handoff-broken.json
$STARLINGS lane assemble broken work --json > assemble-broken.json
$STARLINGS lane freeze broken work --json > freeze-broken.json
$STARLINGS stage broken --lanes work --json > stage-broken.json
$STARLINGS plan broken --gate release --json > plan-broken.json
PLAN_B=$($PY -c "import json,sys; print(json.load(open('plan-broken.json'))['data']['plan_id'])")
$STARLINGS run "$PLAN_B" --json > run-broken.json || true
RECEIPT_B=$($PY -c "import json,sys; print(json.load(open('run-broken.json'))['data']['receipt_id'])")
$STARLINGS check "$RECEIPT_B" --json > check-broken.json || true
$STARLINGS promote "$PLAN_B" --receipt "$RECEIPT_B" --json > promote-broken.json || true
git -C "$AUTH" rev-parse refs/heads/main   # unchanged: the failed receipt authorizes nothing
```

Recovery is fix-forward: a new `fix` batch whose task restores the line stages the corrected `core.py` to `dev`, and its release gate passes and advances `main`:

```sh
$STARLINGS batch prepare fix --lanes work --authority "$AUTH" --json > prepare-fix.json
$STARLINGS task assign fix repair-total --lane work --owner operator --json > assign-fix.json
WT_FIX=$($PY -c "import json,sys; print(json.load(open('assign-fix.json'))['data']['worktree'])")
$PY - "$WT_FIX/src/starlings_example/core.py" <<'EOF'
import sys
path = sys.argv[1]
text = open(path).read()
assert "return sum(values) + 1" in text
open(path, "w").write(text.replace("return sum(values) + 1", "return sum(values)", 1))
EOF
git -C "$WT_FIX" add -A && git -C "$WT_FIX" -c user.name=operator -c user.email=operator@localhost commit -qm "repair compute_total"
HEAD_FIX=$(git -C "$WT_FIX" rev-parse HEAD)
$STARLINGS task handoff fix repair-total --head "$HEAD_FIX" --review review:repair-total --json > handoff-fix.json
$STARLINGS lane assemble fix work --json > assemble-fix.json
$STARLINGS lane freeze fix work --json > freeze-fix.json
$STARLINGS stage fix --lanes work --json > stage-fix.json
$STARLINGS plan fix --gate release --json > plan-fix.json
PLAN_F=$($PY -c "import json,sys; print(json.load(open('plan-fix.json'))['data']['plan_id'])")
$STARLINGS run "$PLAN_F" --json > run-fix.json
RECEIPT_F=$($PY -c "import json,sys; print(json.load(open('run-fix.json'))['data']['receipt_id'])")
$STARLINGS promote "$PLAN_F" --receipt "$RECEIPT_F" --json > promote-fix.json
git -C "$AUTH" rev-parse refs/heads/main   # advanced: the repaired release landed
```

The retained JSON envelopes for both paths are part of the evidence.

## Regenerating the evidence

To regenerate the full evidence, run `python3 -m starlings.qualification run --root /path/outside/checkout/evidence --preset all` (or `bench --root ...`) — from a source checkout use `PYTHONPATH=src` in front; from an installed package the module runs directly. Each preset writes `evidence/index.json` (machine-readable: gate counts, per-run check metrics, calls, phases, worker results, full traceability) and `evidence/index.md` (human summary); `bench` writes `bench.json`/`bench.md`.

## Qualification limitations

Qualification covers local execution on the recorded profile only — hosted GitHub execution is unsupported and other platforms are unqualified; native memory/disk limits are recorded but not enforced; a check descendant that calls `setsid`/`setpgid` escapes process-group tracking; same-user trust is unenveloped (native checks run as the operator and can read the unencrypted signer key); performance numbers are honest local measurements on this machine with no asserted thresholds; the two gate-count deviations above are fixture-driven, not implementation defects.
