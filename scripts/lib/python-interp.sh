#!/bin/sh
# Which interpreter runs the project's checks — one answer for the shell gates.
#
# `Gate:` commands in docs/contracts/ say `python3`, and `python3` from an
# arbitrary PATH is often not the project's interpreter (no pytest-timeout, no
# project plugins). The shell gates ask the project instead: the first token of
# `awf suite-command` is the interpreter `verification.test_cmd` names.
#
# Sourced, not executed. Caller contract:
#
#   awf_find_cli <root>            -> prints the awf CLI to use, or nothing
#   awf_python_resolve <project-dir> [<awf-cli>...]
#
# Sets AWF_PY (the interpreter) and AWF_PY_SRC (where the answer came from:
# AWF_PYTHON / verification.test_cmd / PATH). Exits 2 with a message when the
# named interpreter does not exist — a silent `python3` fallback would turn
# "wrong interpreter" into a red gate that blames the code.
#
# No `awf` CLI passed and none runnable: the answer is plain `python3`, which
# is right only for a project that never configured a test command.

# Which `awf` answers the questions below: the source tree first, then a
# working one on PATH. "Working" is checked, not assumed: `bin/awf` runs under
# `python3` from PATH, so a hostile first `python3` can silence the wrapper
# while the installed console script (absolute interpreter in its shebang)
# still answers. A broken entry point (`ModuleNotFoundError` on `--help`) is
# skipped for the same reason — the caller has a `python -m awf` fallback.
awf_find_cli() {
  if [ -x "$1/bin/awf" ] && "$1/bin/awf" --help >/dev/null 2>&1; then
    printf '%s\n' "$1/bin/awf"
    return 0
  fi
  if command -v awf >/dev/null 2>&1 \
    && "$(command -v awf)" --help >/dev/null 2>&1; then
    command -v awf
  fi
  # Always 0: callers run this inside `set -e`, and "no CLI" is a normal
  # answer (the caller then asks `python -m awf`). A non-zero return here
  # would abort the script instead of falling back.
  return 0
}

awf_python_resolve() {
  awf_proj=$1
  shift

  AWF_PY=${AWF_PYTHON:-python3}
  AWF_PY_SRC=AWF_PYTHON
  [ -n "${AWF_PYTHON:-}" ] || AWF_PY_SRC=PATH

  if [ -z "${AWF_PYTHON:-}" ] && [ "$#" -gt 0 ]; then
    awf_suite_cmd=$("$@" suite-command --project-dir "$awf_proj" 2>/dev/null || true)
    awf_first=$(printf '%s\n' "$awf_suite_cmd" | awk '{print $1; exit}')
    awf_second=$(printf '%s\n' "$awf_suite_cmd" | awk '{print $2; exit}')
    # The interpreter is the token before `-m`; a command starting with a
    # bare `pytest` or a wrapper names no interpreter and leaves the default.
    if [ "${awf_second:-}" = "-m" ] && [ -n "${awf_first:-}" ]; then
      case "$awf_first" in
        ./*) awf_first=${awf_first#./} ;;
      esac
      case "$awf_first" in
        # A relative interpreter in test_cmd means "relative to the project":
        # the caller may run from anywhere. A bare name stays a PATH lookup.
        /*) AWF_PY=$awf_first ;;
        */*) AWF_PY=$awf_proj/$awf_first ;;
        *) AWF_PY=$awf_first ;;
      esac
      AWF_PY_SRC=verification.test_cmd
    fi
  fi

  case "$AWF_PY" in
    */*)
      # `-x` alone passes for a directory: a config naming the venv's `bin`
      # folder instead of its python reached the gates and painted them red
      # with `Permission denied`. A named interpreter is a file you can run.
      if [ ! -f "$AWF_PY" ] || [ ! -x "$AWF_PY" ]; then
        echo "интерпретатор из $AWF_PY_SRC не найден: $AWF_PY" >&2
        exit 2
      fi
      ;;
    *)
      if ! command -v "$AWF_PY" >/dev/null 2>&1; then
        echo "интерпретатор из $AWF_PY_SRC не найден в PATH: $AWF_PY" >&2
        exit 2
      fi
      ;;
  esac
}
