#!/bin/sh
# Contract gate (quality-gates). Checks every contract in docs/contracts/:
# the four fields parse, every backticked token in Path: exists (a token
# with / is a file, otherwise a symbol grepped in the repo), and every
# Gate: command passes. A rule a script could check but names no gate is a
# defect; this gate is the one that finds the defect.
#
#   sh check-contracts.sh [contracts-dir]   (default: docs/contracts)
#
# Run it from the project root: Gate: one-liners use paths relative to it.
#
# M2: every run prints its denominator — contracts read, tokens resolved,
# gates executed, review-only rules. A gate that measured nothing must not
# report the same green as one that measured everything.
#
# Exit codes: 0 ok, 1 a violation, 2 nothing to check or unreadable.
set -u

DIR="${1:-docs/contracts}"
[ -d "$DIR" ] || { echo "check-contracts: no $DIR/ directory"; exit 2; }
files=$(ls "$DIR"/*.md 2>/dev/null || true)
[ -n "$files" ] || { echo "check-contracts: no contracts in $DIR/"; exit 2; }

fail=0
n_contracts=0
n_tokens=0
n_gates=0
n_review=0

symbol_exists() {
  token=$1
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    # --untracked: new files count before their first commit, and the
    # .ratchet files are excluded — their command lines quote code tokens.
    git grep -qwF --untracked -- "$token" -- ':(exclude)docs/' ':(exclude)*/node_modules/*' ':(exclude).ratchet*' 2>/dev/null
  else
    grep -rqwF --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=docs -- "$token" . 2>/dev/null
  fi
}

# A token without a slash is a symbol (`apiClient`, `os.environ`) unless it
# is a filename (`api.py` at the repo root). The extension decides: a bare
# name that exists is a file, a bare name with a code extension that does
# not exist is a renamed or deleted file, anything else is a symbol.
FILE_EXT_RE='\.(py|pyi|ts|tsx|js|jsx|mjs|cjs|go|rs|java|kt|kts|rb|php|cs|c|cc|cpp|h|hpp|sh|bash|zsh|sql|json|yaml|yml|toml|ini|cfg|conf|xml|html|css|scss|md|txt)$'
is_file_like() { printf '%s' "$1" | grep -qE "$FILE_EXT_RE"; }

for f in $files; do
  name=$(grep -m1 '^# Contract:' "$f" | sed 's/^# Contract: *//' || true)
  [ -n "$name" ] || name=$f
  n_contracts=$((n_contracts + 1))

  for field in 'Owns:' 'Path:' 'Never:' 'Gate:'; do
    grep -q "^$field" "$f" || { echo "FAIL: $name ($f): missing $field"; fail=1; }
  done

  path=$(grep -m1 '^Path:' "$f" || true)
  tokens=$(printf '%s\n' "$path" | grep -o '`[^`]*`' | tr -d '`' || true)
  for t in $tokens; do
    n_tokens=$((n_tokens + 1))
    case "$t" in
      */*)
        [ -e "$t" ] || { echo "FAIL: $name: path $t does not exist"; fail=1; }
        ;;
      *)
        if [ -e "$t" ]; then
          : # a bare filename that exists — fine
        elif is_file_like "$t"; then
          echo "FAIL: $name: file $t does not exist"
          fail=1
        else
          symbol_exists "$t" || { echo "FAIL: $name: symbol $t not found in the code (outside docs/)"; fail=1; }
        fi
        ;;
    esac
  done

  gate=$(grep -m1 '^Gate:' "$f" | sed 's/^Gate: *//' || true)
  if [ -z "$gate" ]; then
    echo "FAIL: $name: empty Gate: — write a command, or 'review' when only a human can check it"
    fail=1
  elif [ "$gate" = "review" ]; then
    n_review=$((n_review + 1))
    echo "note: $name: Gate: review — no script check, an incident must back this rule"
  else
    n_gates=$((n_gates + 1))
    if ! gate_out=$(sh -c "$gate" 2>&1); then
      echo "FAIL: $name: gate reports a violation: $gate"
      printf '%s\n' "$gate_out" | head -10
      fail=1
    fi
  fi
done

# M2: the denominator prints on every run, not only on success.
if [ "$fail" -eq 0 ]; then
  echo "check-contracts: ok ($n_contracts contracts, $n_tokens path tokens, $n_gates gates run, $n_review review-only)"
else
  echo "check-contracts: FAILED ($n_contracts contracts, $n_tokens path tokens, $n_gates gates run, $n_review review-only)"
fi
exit "$fail"
