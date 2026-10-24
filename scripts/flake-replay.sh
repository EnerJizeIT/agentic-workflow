#!/usr/bin/env bash
# flake-replay.sh — repeat a test file N times and fail on the first red
# (TODO-0103: 8 sequential runs prove the A-13 flake did not come back).
#
#   bash scripts/flake-replay.sh <test-file> <runs> [pytest args...]
#
# The loop lives HERE because a `verify:` command may not carry shell
# operators (A4, docs/unit-contract.md): the contract names one argv-safe
# command, the semantics stay the same.
set -u

file=${1:?usage: flake-replay.sh <test-file> <runs> [pytest args...]}
runs=${2:?usage: flake-replay.sh <test-file> <runs> [pytest args...]}
shift 2

for _ in $(seq 1 "$runs"); do
    python3 -m pytest "$file" -q "$@" || exit 1
done
