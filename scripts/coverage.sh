#!/usr/bin/env bash
# coverage.sh — coverage ratchet (ORCH M6.1).
#
# Measures coverage of the tracked names (TOTAL + one per M1-M5 module) on
# the fast test subset and ratchets the numbers: they may rise or hold; a
# drop below the baseline fails. The baseline is .coverage-baseline — the
# same naming class as .ratchet-baseline (dot = machine state, not docs).
#
#   bash scripts/coverage.sh                  check (what CI runs)
#   bash scripts/coverage.sh --update "why"   re-measure and raise the baseline
#   bash scripts/coverage.sh show             current vs baseline, no verdict
#
# --update only raises. A measured drop is refused, the same class of guard
# as the debt ratchet's "only lowers": a regression cannot be ratified into
# the baseline. The reason for the new numbers goes into the file itself
# (the "reason:" line), not just the commit message.
#
# Why a subset, not the full suite: a second full-suite run costs ~6.5 min
# on the 2-core CI runner (the Quality gates step alone is 6:13) against a
# +2 min budget. The subset is the three fast directories that carry the
# tracked modules' coverage (per-directory coverage matrix, M6.1 report:
# e2e is 127s for 28 tests and adds <=4 points to at most two tracked
# modules; integration is 48s and <=4 points to two). The full suite still
# runs uninstrumented in the Quality gates step — nothing goes unexercised
# in CI; the subset only measures.
#
# COVERAGE_PYTEST_EXTRA — extra pytest arguments, local debug/scratch only
# (e.g. --ignore=<file> to prove the gate goes red). Never set in CI.
#
# Exit codes: 0 within baseline, 1 a value dropped or is not ratified,
# 2 the gate could not run (no baseline, broken run, malformed baseline).

# The script is bash but may be called via sh — like the other gates.
if [ -z "${BASH_VERSION:-}" ]; then
  command -v bash > /dev/null 2>&1 || { echo "coverage: нужен bash" >&2; exit 2; }
  exec bash "$0" "$@"
fi

set -u

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2
if root=$(git rev-parse --show-toplevel 2>/dev/null); then
  cd "$root" || exit 2
else
  cd "$script_dir/.." || exit 2
fi

BASELINE=${COVERAGE_BASELINE:-.coverage-baseline}

# Tracked names: TOTAL + the production modules of the ORCH M1-M5 waves —
# the newest code, and the least covered of the recent additions
# (full-suite matrix: 74-92%; the older modules sit at 95-99%).
MODULES=(
  awf/api/run.py
  awf/run_state.py
  awf/run_plan_read.py
  awf/brief.py
  awf/stall_detect.py
  awf/api/roles.py
  awf/commit_plan.py
  awf/commit_gate.py
  awf/_env.py
  agent_workflow_ui/src/agent_workflow_ui/tools/awf.py
)

# CI-time-measured subset (M6.1, 2026-09-28): 35s + 24s + 71s = 130s local
# (8 cores) vs 269s for the full suite. Changing SELECTION invalidates the
# baseline's numbers — check mode refuses to compare a changed selection.
SELECTION=(tests/unit tests/agent_workflow_ui tests/negative)
SELECTION_STR=${SELECTION[*]}

# Measurement noise tolerance (points). The suite has timing-sensitive
# branches (timeout tests, multiprocessing) — back-to-back runs on the same
# tree jitter within TOLERANCE; a real regression (dropped tests, dead
# branch) moves a tracked value by more. Measured, not guessed: see the
# M6.1 report.
TOLERANCE=0.10

usage() {
  cat <<'EOF'
usage: coverage.sh [--update "reason" | show]
  (no args)   check the measured coverage against the baseline
  --update    re-measure and raise the baseline (reason is required)
  show        print current vs baseline, no verdict
EOF
  exit 2
}

mode=check
reason=""
if [ "$#" -gt 0 ]; then
  case "$1" in
    show) mode=show ;;
    --update)
      mode=update
      shift
      [ "$#" -gt 0 ] || usage
      reason=$*
      ;;
    *) usage ;;
  esac
fi

extra=()
if [ -n "${COVERAGE_PYTEST_EXTRA:-}" ]; then
  read -r -a extra <<< "$COVERAGE_PYTEST_EXTRA"
fi

tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/coverage.XXXXXX") || exit 2
trap 'rm -rf "$tmp_dir"' EXIT
json="$tmp_dir/coverage.json"

echo "coverage: measuring ${SELECTION_STR} ..."
if ! python3 -m pytest "${SELECTION[@]}" -q -n auto --timeout=300 \
      -p no:cacheprovider --cov=awf --cov=agent_workflow_ui \
      --cov-report=json:"$json" ${extra[@]+"${extra[@]}"}; then
  echo "coverage: the test run failed — nothing to measure (exit 2)" >&2
  exit 2
fi
[ -s "$json" ] || { echo "coverage: no coverage json produced (exit 2)" >&2; exit 2; }

python3 - "$mode" "$reason" "$json" "$BASELINE" "$SELECTION_STR" "$TOLERANCE" \
  "${MODULES[@]}" <<'PY'
import json
import os
import re
import sys

mode, reason, json_path, baseline_path, selection, tol = sys.argv[1:7]
tol = float(tol)
modules = sys.argv[7:]
eps = 1e-9
names = ["total"] + modules

data = json.load(open(json_path))
files = data.get("files", {})


def pct(path):
    f = files.get(path)
    if f is None:
        return None
    return round(float(f["summary"]["percent_covered"]), 2)


current = {"total": round(float(data["totals"]["percent_covered"]), 2)}
for m in modules:
    current[m] = pct(m)

baseline = {}
baseline_selection = None
if os.path.exists(baseline_path):
    for line in open(baseline_path, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        if line.startswith("#"):
            if line.startswith("# selection:"):
                baseline_selection = line[len("# selection:"):].strip()
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not re.fullmatch(r"\d+(\.\d+)?", parts[1]):
            print(f"coverage: {baseline_path} is malformed: {line!r}")
            print("coverage: expected one value per line: name<space>number (exit 2)")
            sys.exit(2)
        baseline[parts[0]] = float(parts[1])

missing = [n for n in names if current[n] is None]
if missing:
    print(f"coverage: not in the coverage output (module moved or never imported): {missing}")
    sys.exit(1)

if baseline_selection is not None and baseline_selection != selection and mode != "show":
    print(f"coverage: the baseline was measured on {baseline_selection!r},")
    print(f"coverage: the script now measures {selection!r} — the numbers are not comparable.")
    print("coverage: restore SELECTION, or run --update with a reason.")
    sys.exit(1)

if mode == "show":
    print(f"{'name':<52} {'now':>7} {'baseline':>9} {'delta':>7}")
    for name in names:
        cur = current[name]
        base = baseline.get(name)
        base_s = f"{base:.2f}" if base is not None else "-"
        delta_s = f"{cur - base:+.2f}" if base is not None else "-"
        print(f"{name:<52} {cur:>7.2f} {base_s:>9} {delta_s:>7}")
    sys.exit(0)

if mode == "update":
    refusals = []
    for name, old in baseline.items():
        new = current.get(name)
        if new is None:
            refusals.append(
                f"{name}: in the baseline but no longer measured — delete the entry by hand and say why"
            )
        elif new < old - tol - eps:
            refusals.append(f"{name}: {old:.2f} -> {new:.2f} is a drop; fix the code first")
    if refusals:
        print("coverage: --update only raises; refused:")
        for r in refusals:
            print(f"  - {r}")
        sys.exit(1)
    lines = [
        "# coverage ratchet baseline — scripts/coverage.sh (ORCH M6.1)",
        f"# selection: {selection}",
        f"# reason: {reason}",
    ]
    for name in names:
        lines.append(f"{name}\t{current[name]:.2f}")
    text = "\n".join(lines) + "\n"
    tmp = baseline_path + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, baseline_path)
    print("Baseline written:")
    print(text, end="")
    sys.exit(0)

# mode == "check"
if not baseline:
    print(f"coverage: no {baseline_path}; run --update once with a reason")
    sys.exit(2)
fail = False
for name in names:
    cur = current[name]
    base = baseline.get(name)
    if base is None:
        print(f"FAIL {name}: {cur:.2f} measured, not in the baseline — run --update to ratify")
        fail = True
    elif cur < base - tol - eps:
        print(
            f"FAIL {name}: baseline {base:.2f}, measured {cur:.2f} "
            f"(drop of {base - cur:.2f} > tolerance {tol})"
        )
        fail = True
    elif cur > base + eps:
        print(f"improved {name}: {base:.2f} -> {cur:.2f}, run --update to lock the gain in")
if fail:
    print("coverage: below baseline — restore the tests/branches that lost coverage.")
    sys.exit(1)
print(f"coverage: within baseline ({len(names)} names, tolerance {tol})")
sys.exit(0)
PY
