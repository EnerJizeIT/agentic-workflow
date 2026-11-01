#!/usr/bin/env bash
# affected-tests.sh — A21: the tests a change actually needs.
#
# The full suite is ~6350 tests and ~10 minutes; a unit touches a handful of
# files. This wrapper prints (or runs) the selection from
# `awf.test_selection`, which is deliberately conservative: core files force
# the full suite, and a selection above 40% of the suite degrades to it too.
#
#   scripts/affected-tests.sh                      # print the file list
#   scripts/affected-tests.sh --explain            # + why, on stderr
#   scripts/affected-tests.sh --run -- -q          # run pytest on it
#   scripts/affected-tests.sh --base <sha> [--full]
#
# Exit codes: 0 — selection printed/run passed; pytest's own code when --run;
# 2 — bad arguments.
set -euo pipefail

if [ -z "${BASH_VERSION:-}" ]; then
  command -v bash >/dev/null 2>&1 || { echo "affected-tests: нужен bash" >&2; exit 2; }
  exec bash "$0" "$@"
fi

project=$(pwd)
base=""
explain=""
full=""
run=0
pytest_args=()

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2
root=$(CDPATH= cd -- "$script_dir/.." && pwd) || exit 2

while [ "$#" -gt 0 ]; do
  case "$1" in
    --project)
      shift
      [ "$#" -gt 0 ] || { echo "affected-tests: --project needs a path" >&2; exit 2; }
      project=$1
      ;;
    --base)
      shift
      [ "$#" -gt 0 ] || { echo "affected-tests: --base needs a sha" >&2; exit 2; }
      base=$1
      ;;
    --explain) explain="--explain" ;;
    --full) full="--full" ;;
    --run) run=1 ;;
    --)
      shift
      pytest_args=("$@")
      break
      ;;
    -h|--help)
      sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "affected-tests: unknown argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

pybin=${AWF_PYTHON:-python3}
pybin_src="AWF_PYTHON"
if [ -z "${AWF_PYTHON:-}" ]; then
  pybin_src="PATH"
fi

# AUD8: a configured interpreter that does not exist must say so. Without
# this check the selector prints nothing and the script blames an "empty
# selection" — the real reason (a typo, a removed venv) stays hidden.
# Called before every use of $pybin: the CLI probe below runs the interpreter
# too, so a missing one has to be named before anything blames `awf`.
require_interpreter() {
  case "$pybin" in
    */*)
      if [ ! -x "$pybin" ]; then
        echo "affected-tests: интерпретатор из $pybin_src не найден: $pybin" >&2
        exit 2
      fi
      ;;
    *)
      if ! command -v "$pybin" >/dev/null 2>&1; then
        echo "affected-tests: интерпретатор из $pybin_src не найден в PATH: $pybin" >&2
        exit 2
      fi
      ;;
  esac
}

require_interpreter

# CLI: тот же порядок, что в scripts/suite-canonical.sh — дерево исходников
# ($root/bin/awf), работающий `awf` на PATH, затем установленный пакет через
# `$pybin -m awf`. Общего файла нет намеренно: suite-canonical.sh уезжает в
# шаблон проекта один, без соседней библиотеки.
awf=""
if [ -x "$root/bin/awf" ]; then
  awf="$root/bin/awf"
elif command -v awf >/dev/null 2>&1 \
  && "$(command -v awf)" --help >/dev/null 2>&1; then
  awf=$(command -v awf)
fi

run_awf() {
  if [ -n "$awf" ]; then
    "$awf" "$@"
  else
    "$pybin" -m awf "$@"
  fi
}

# The interpreter is the project's, not the shell's: `verification.test_cmd`
# names it (`awf suite-command` prints that command). `python3` is right only
# for a project that never configured one — and it is often the wrong
# interpreter for pytest plugins (`--timeout` lives in the venv). The
# question is asked about $project, not the current directory: --project
# exists precisely because they differ.
if [ -z "${AWF_PYTHON:-}" ]; then
  suite_cmd=$(run_awf suite-command --project-dir "$project" 2>/dev/null || true)
  read -r first second _rest <<<"${suite_cmd:-}"
  if [ "${second:-}" = "-m" ] && [ -n "${first:-}" ]; then
    case "$first" in
      # A relative interpreter in test_cmd means "relative to the project":
      # suite-canonical.sh runs that command from the project root, while
      # this script may be invoked from anywhere. Without the prefix
      # ``.venv/bin/python`` would be looked up in the caller's directory.
      /*) pybin=$first ;;
      */*) pybin="$project/$first" ;;
      *) pybin=$first ;;
    esac
    pybin_src="verification.test_cmd"
  fi
  require_interpreter
fi

if [ -z "$awf" ] && ! "$pybin" -c 'import awf' >/dev/null 2>&1; then
  echo "affected-tests: awf CLI not found (tried \$root/bin/awf, a working 'awf' on PATH, '$pybin -m awf')" >&2
  exit 2
fi

selection_args=(--project-dir "$project")
[ -n "$base" ] && selection_args+=(--base "$base")
[ -n "$explain" ] && selection_args+=(--explain)
[ -n "$full" ] && selection_args+=(--full)

selection_err=$(mktemp "${TMPDIR:-/tmp}/awf-affected.XXXXXX") || exit 2
trap 'rm -f "$selection_err"' EXIT
if ! selection=$("$pybin" -m awf.test_selection "${selection_args[@]}" 2>"$selection_err"); then
  cat "$selection_err" >&2
  echo "affected-tests: селекция не посчитана (интерпретатор $pybin)" >&2
  exit 2
fi
cat "$selection_err" >&2
# The --run path execs pytest, where the EXIT trap never fires: the scratch
# file is read once, so it is removed here rather than left in TMPDIR.
rm -f "$selection_err"

if [ -z "$selection" ]; then
  echo "affected-tests: empty selection — refusing to run nothing" >&2
  exit 2
fi
mapfile -t files <<<"$selection"

if [ "$run" -eq 0 ]; then
  printf '%s\n' "${files[@]}"
  exit 0
fi

# A21: the full suite in one thread is ~25 minutes and overruns the 600 s
# budget; parallel by default, and the caller's own ``-n`` still wins.
has_parallelism=0
for arg in ${pytest_args[@]+"${pytest_args[@]}"}; do
  case "$arg" in
    -n|--numprocesses|-n*|--numprocesses=*) has_parallelism=1 ;;
  esac
done
if [ "$has_parallelism" -eq 0 ]; then
  pytest_args+=(-n auto)
fi

cd "$project"
exec "$pybin" -m pytest "${files[@]}" ${pytest_args[@]+"${pytest_args[@]}"}
