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
mapfile -t files < <("$pybin" -m awf.test_selection \
  --project-dir "$project" ${base:+--base "$base"} $explain $full)

if [ "${#files[@]}" -eq 0 ]; then
  echo "affected-tests: empty selection — refusing to run nothing" >&2
  exit 2
fi

if [ "$run" -eq 0 ]; then
  printf '%s\n' "${files[@]}"
  exit 0
fi

cd "$project"
exec "$pybin" -m pytest "${files[@]}" "${pytest_args[@]}"
