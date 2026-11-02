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

# Which interpreter runs the project's checks is answered by one shared
# function (scripts/lib/python-interp.sh): the contract gate asks the same
# question, and a second copy would drift. Here `pybin` is only the bootstrap
# for the CLI probe below; the project's answer replaces it right after.
# The existence test comes first: a failed `.` aborts dash itself, so an
# `|| { echo …; exit 2; }` after it is dead code.
lib="$script_dir/lib/python-interp.sh"
if [ ! -f "$lib" ]; then
  echo "affected-tests: нет $lib" >&2
  exit 2
fi
. "$lib"
pybin=${AWF_PYTHON:-python3}

# CLI: тот же порядок, что в scripts/suite-canonical.sh — дерево исходников
# ($root/bin/awf), работающий `awf` на PATH, затем установленный пакет через
# `$pybin -m awf`. Поиск CLI — общий (scripts/lib/python-interp.sh): тот же
# вопрос задаёт check-contracts.sh, копия разъехалась бы. suite-canonical.sh
# остаётся самодостаточным намеренно: он уезжает в шаблон проекта один.
awf=$(awf_find_cli "$root")

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
if [ -n "$awf" ]; then
  awf_python_resolve "$project" "$awf"
elif "$pybin" -c 'import awf' >/dev/null 2>&1; then
  # No CLI, but the package is importable: ask the same question through
  # `python -m awf`, exactly as run_awf would. Dropping this branch silently
  # demoted a configured interpreter to `python3` from PATH.
  awf_python_resolve "$project" "$pybin" -m awf
else
  awf_python_resolve "$project"
fi
pybin=$AWF_PY

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
