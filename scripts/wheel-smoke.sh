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
#   * `create_server().list_tools()`      — non-empty registry from the wheel
#                                           (count checked >0, never hardcoded)
#   * the project-setup template renders  — the default template ships + works
#   * the plugin entry point starts       — under an isolated XDG/HOME, no
#                                           network, no writes into $HOME
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
# A-18 supplement (TODO-0111): the registry check imports
# `agent_workflow_ui.server`, which needs the plugin's `mcp` runtime dep that
# the --no-deps install above skipped. Install ONLY the third-party package
# (with the same pin as the plugin's pyproject), never the plugin wheel a
# second time — that is what would drag a PyPI `awf` over the local one.
# PyYAML/jinja2 already came in with the core wheel's deps.
pip_install "mcp>=1.0,<2"

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

# A-18 supplement (TODO-0111): the registry built from the INSTALLED wheel is
# non-empty. The count is asserted >0, never hardcoded against the registry
# (the live registry is the single source of truth — see R-06).
( cd "$smoke_cwd" && env -u PYTHONPATH "$venv/bin/python" -c \
  "import asyncio, agent_workflow_ui, pathlib; \
    f = pathlib.Path(agent_workflow_ui.__file__); \
    assert 'site-packages' in str(f), f'plugin resolved outside the wheel: {f}'; \
    from agent_workflow_ui.server import create_server; \
    tools = asyncio.run(create_server().list_tools()); \
    names = [t.name for t in tools]; \
    assert len(names) > 0, 'create_server().list_tools() returned an empty registry'; \
    print(f'  list_tools(): {len(names)} tools registered (non-empty; count not hardcoded)')" )

# A-18 supplement (TODO-0111): the standard project-setup template renders
# from the installed package data (a dropped/corrupted template stays green
# today — the wheel-contents grep only checks the file is IN the wheel).
( cd "$smoke_cwd" && env -u PYTHONPATH "$venv/bin/python" -c \
  "import agent_workflow_ui, pathlib; \
    pkg = pathlib.Path(agent_workflow_ui.__file__).parent; \
    from agent_workflow_ui.render.engine import create_env, render_template; \
    env = create_env([pkg / 'render' / 'default_templates']); \
    html = render_template(env, 'project-setup', { \
        'form_id': 'smoke-0001', \
        'submit_url': 'http://127.0.0.1:0/submit/smoke-0001', \
        'available_roles': [{'id': 'developer', 'title': 'Developer'}], \
    }); \
    assert 'id=\"setup-form\"' in html, 'project-setup render lost the form body'; \
    assert len(html) > 1000, f'project-setup render suspiciously small: {len(html)} chars'; \
    print('  render project-setup: OK (%d chars)' % len(html))" )

# A-18 supplement (TODO-0111): the declared console entry point starts under
# a fully isolated environment — XDG_CONFIG_HOME/XDG_DATA_HOME/
# XDG_STATE_HOME (and HOME) pointed at throwaway dirs, PATH restricted so
# the `opencode` CLI (and any other external command) cannot be reached:
# no network, no $HOME access. TODO-0183: XDG_STATE_HOME joined the
# isolation — the fault log (fault_log.py) falls back to ~/.local/state
# without it, writing into the isolated HOME and tripping the assertion.
# stdin is /dev/null so the stdio MCP server exits at EOF instead of blocking.
# The restricted PATH is what keeps `read_available_models` on its offline
# fallback (no `opencode models` subprocess).
entry_home="$workdir/entry-home"
entry_cfg="$workdir/entry-xdg-config"
entry_data="$workdir/entry-xdg-data"
entry_state="$workdir/entry-xdg-state"
entry_cwd="$workdir/entry-cwd"
mkdir -p "$entry_home" "$entry_cfg" "$entry_data" "$entry_state" "$entry_cwd"

if [ -x "$venv/bin/agent-workflow-ui" ]; then
  # Run from $entry_cwd (not the repo): the entry point creates .agentic/
  # runtime dirs against its cwd — a throwaway dir keeps the tree clean.
  ( cd "$entry_cwd" && timeout 30 env HOME="$entry_home" \
      XDG_CONFIG_HOME="$entry_cfg" XDG_DATA_HOME="$entry_data" \
      XDG_STATE_HOME="$entry_state" \
      PATH="$venv/bin:/usr/bin:/bin" \
      "$venv/bin/agent-workflow-ui" > "$workdir/entry.log" 2>&1 ) < /dev/null || {
      echo "wheel-smoke: plugin entry point exited non-zero (rc=$?)" >&2
      tail -n 5 "$workdir/entry.log" >&2
      exit 1
    }
  grep -q "agent_workflow_ui started" "$workdir/entry.log" || {
    echo "wheel-smoke: entry point did not reach startup:" >&2
    tail -n 5 "$workdir/entry.log" >&2
    exit 1
  }
  [ -f "$entry_cfg/opencode/skills/agent-workflow-ui/SKILL.md" ] || {
    echo "wheel-smoke: skill not installed into the isolated XDG_CONFIG_HOME" >&2
    exit 1
  }
  [ -z "$(ls -A "$entry_home" 2>/dev/null)" ] || {
    echo "wheel-smoke: entry point wrote into HOME ($entry_home):" >&2
    ls -A "$entry_home" | head -n 3 >&2
    exit 1
  }
  echo "  entry point agent-workflow-ui: OK (isolated XDG (config/data/state), HOME untouched)"
else
  # No console script declared — the fallback still proves the server module
  # imports from the wheel and registers tools, under the same isolation.
  ( cd "$smoke_cwd" && env -u PYTHONPATH \
      HOME="$entry_home" XDG_CONFIG_HOME="$entry_cfg" XDG_DATA_HOME="$entry_data" \
      XDG_STATE_HOME="$entry_state" \
      "$venv/bin/python" -c \
    "import asyncio, agent_workflow_ui, pathlib; \
      f = pathlib.Path(agent_workflow_ui.__file__); \
      assert 'site-packages' in str(f), f'plugin resolved outside the wheel: {f}'; \
      from agent_workflow_ui.server import create_server; \
      tools = asyncio.run(create_server().list_tools()); \
      assert len(tools) > 0, 'create_server().list_tools() returned an empty registry'; \
      print('  (no entry point declared) import + list_tools: OK (%d tools)' % len(tools))" )
fi

echo "wheel-smoke: OK — both wheels built, installed clean, import + CLI + registry + template + entry point verified"
