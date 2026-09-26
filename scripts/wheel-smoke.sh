#!/usr/bin/env bash
# wheel-smoke.sh — A-18: build BOTH wheels, install them into a clean
# throwaway venv, and prove the installed packages import + the CLI runs —
# with the working tree kept OUT of sys.path.
#
# Why: editable installs (`pip install -e`) never validate the built wheel,
# so a package could ship broken (or with dropped package-data) and CI would
# stay green. This script is the external judge for the real wheel artefacts:
# it builds both distributions, installs them as wheels (not the source tree),
# and runs the checks from a neutral directory so `import awf` /
# `import agent_workflow_ui` resolve to the venv's site-packages, not the
# repo checkout.
#
# What it proves:
#   * `awf --help`                       — the console entry point runs
#   * `import awf.api`                    — the core API imports from the wheel
#   * `import agent_workflow_ui`          — the plugin wheel installs + imports
#   * the plugin's SKILL.md package-data  — survives the wheel build+install
#
# Wheels are written to the repo's dist/ (gitignored, the CI build output
# dir) so the "Check wheel contents" step can read them. The venv and the
# neutral smoke cwd live in a mktemp dir and are removed on exit.
#
# Exit codes: 0 — all checks passed; 1 — a check failed (build/install/import);
#             2 — environment problem (no bash / no python3 / bad root).

# The script is bash but may be invoked via sh — re-exec under bash so a
# non-bash shell does not die on an unclear "Syntax error" (run-all.sh style).
if [ -z "${BASH_VERSION:-}" ]; then
  command -v bash > /dev/null 2>&1 || { echo "wheel-smoke: нужен bash" >&2; exit 2; }
  exec bash "$0" "$@"
fi

set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2

# project root: git toplevel, else one level above the script
if root=$(git rev-parse --show-toplevel 2>/dev/null); then
  cd "$root" || exit 2
else
  cd "$script_dir/.." || exit 2
fi

command -v python3 > /dev/null 2>&1 || { echo "wheel-smoke: нужен python3" >&2; exit 2; }

# Wheel selection (F1, TODO-0106 attempt 2). The old line was
# `ls dist/awf-*.whl | head -n1` — alphabetical. A version bump leaves the
# previous release's wheel in dist/, so the smoke "proved" the STALE
# artefact: false green at the exact moment of a release. The build step
# below wipes dist/*.whl first; this function is the second line of defense
# and fails hard with a readable message instead of silently picking a
# winner.
#
# `--pick-wheel <wheel-dir> <glob>` runs ONLY this selection and exits —
# the offline test hook (the full smoke needs the build, the selection
# does not).
pick_wheel() {
  local dir="$1" glob="$2" matches count
  matches=$( { find "$dir" -maxdepth 1 -type f -name "$glob" 2>/dev/null || true; } | sort )
  if [ -n "$matches" ]; then
    count=$(printf '%s\n' "$matches" | wc -l)
  else
    count=0
  fi
  if [ "$count" -eq 1 ]; then
    printf '%s\n' "$matches"
  elif [ "$count" -eq 0 ]; then
    echo "wheel-smoke: no wheel matching '$glob' in $dir (did the build fail?)" >&2
    exit 1
  else
    echo "wheel-smoke: $count wheels match '$glob' in $dir — ambiguous, refusing to guess:" >&2
    printf '%s\n' "$matches" >&2
    echo "wheel-smoke: remove the stale wheel(s) from $dir and re-run" >&2
    exit 1
  fi
}

if [ "${1:-}" = "--pick-wheel" ]; then
  [ "$#" -eq 3 ] || { echo "usage: wheel-smoke.sh --pick-wheel <wheel-dir> <glob>" >&2; exit 2; }
  pick_wheel "$2" "$3"
  exit 0
fi

# A clean, self-contained work area: venv + neutral smoke cwd (NOT the repo).
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
venv="$workdir/venv"
smoke_cwd="$workdir/smoke"
wheels="$root/dist"
mkdir -p "$wheels" "$smoke_cwd"

echo "── build: both wheels (awf + agent-workflow-ui) -> dist/"
# F1 (TODO-0106 attempt 2): wipe stale wheels from a previous release so the
# artefacts built by THIS run are the only candidates (see pick_wheel above).
rm -f "$wheels"/*.whl
python3 -m pip wheel . --no-deps --wheel-dir "$wheels" > "$workdir/build.log" 2>&1
python3 -m pip wheel ./agent_workflow_ui --no-deps --wheel-dir "$wheels" >> "$workdir/build.log" 2>&1
core_whl=$(pick_wheel "$wheels" "awf-*.whl")
ui_whl=$(pick_wheel "$wheels" "agent_workflow_ui-*.whl")
echo "  built: $(basename "$core_whl"), $(basename "$ui_whl")"

echo "── venv: clean throwaway environment"
# Normal venv (with pip) is the CI path. Some minimal environments lack
# ensurepip, so `python3 -m venv` would fail noisily. Check up front instead:
# ensurepip present -> normal venv; absent -> --without-pip venv, and the
# install below is driven through the system pip targeted at the venv.
if python3 -m ensurepip --version > /dev/null 2>&1; then
  python3 -m venv "$venv"
else
  echo "  (no ensurepip — using --without-pip venv + system pip)"
  python3 -m venv --without-pip "$venv"
fi

# Use the venv's own pip when present, else the system pip pointed at the
# venv interpreter. Both create the `awf` console script in bin/.
if [ -x "$venv/bin/pip" ]; then
  pip_install() { "$venv/bin/pip" install --quiet "$@"; }
else
  pip_install() { python3 -m pip --quiet --python "$venv/bin/python" install "$@"; }
fi

# Install the core wheel WITH its declared runtime deps (PyYAML/jinja2/
# markdown) so `import awf.api` and the CLI resolve. Install the plugin wheel
# WITHOUT deps: its top-level package needs no third-party import, and this
# keeps pip from pulling a second (PyPI) copy of `awf` over the local wheel.
pip_install "$core_whl"
pip_install --no-deps "$ui_whl"

# Run every check from $smoke_cwd (a neutral dir, outside the repo) and with
# PYTHONPATH cleared, so sys.path[0] is not the checkout. The site-packages
# assertion below is the belt-and-braces proof the import came from the
# installed wheel, not the working tree.
echo "── smoke: checks (neutral cwd, working tree not in sys.path)"
"$venv/bin/awf" --help > "$workdir/help.log" 2>&1
echo "  awf --help: OK ($(wc -l < "$workdir/help.log") lines)"

( cd "$smoke_cwd" && env -u PYTHONPATH "$venv/bin/python" -c \
  "import awf.api, pathlib; f = pathlib.Path(awf.api.__file__); \
   assert 'site-packages' in str(f), f'awf.api resolved outside the wheel: {f}'; \
   print('  import awf.api ->', f)" )

( cd "$smoke_cwd" && env -u PYTHONPATH "$venv/bin/python" -c \
  "import agent_workflow_ui, pathlib; f = pathlib.Path(agent_workflow_ui.__file__); \
   assert 'site-packages' in str(f), f'plugin resolved outside the wheel: {f}'; \
   skill = pathlib.Path(f).parent / 'SKILL.md'; \
   assert skill.is_file(), f'package-data SKILL.md missing from the installed wheel: {skill}'; \
   print('  import agent_workflow_ui ->', f); \
   print('  package-data SKILL.md: present')" )

echo "wheel-smoke: OK — both wheels built, installed clean, import + CLI verified"
