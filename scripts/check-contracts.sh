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
# TODO-0240: the same run checks the units still in flight
# (.agentic/inbox/TODO-*.md): a "## Границы" block naming an FS
# destructive/write call (unlink/rmtree/move/replace/write_text/mkdir)
# must carry a "## Матрица замыкания" table. Only BACKTICKED tokens in the
# boundaries block trigger the lock — an FS word in prose ("replace the old
# check") is not an operation. Archived units (done/) are not read — the
# rule guards live plans only. The summary prints the denominator:
# "closure matrix: N triggered / M units".
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
# TODO-0240: unit-contract closure-matrix lock (inbox/*.md only).
# A unit whose boundaries block mentions an FS destructive/write call must
# carry a `## Матрица замыкания` section with a table (operation |
# surface | root/leaf | order). Archived contracts (done/) are not read —
# the rule guards units still in flight.
n_units=0
n_triggered=0
inbox=".agentic/inbox"
if [ -d "$inbox" ]; then
  unit_fail=0
  for uf in "$inbox"/TODO-*.md; do
    [ -f "$uf" ] || continue
    n_units=$((n_units + 1))
    bounds=$(awk '/^## (Границы|Boundaries)/{flag=1;next}/^## /{flag=0}flag' "$uf")
    # Trigger only the backticked tokens: prose mentioning an FS verb is not
    # an operation. A path token (`awf/x.py`) never matches the word list.
    tokens=$(printf '%s\n' "$bounds" | grep -o '`[^`]*`' | tr -d '`' || true)
    if printf '%s\n' "$tokens" | grep -qE '(^|[^A-Za-z0-9_])(unlink|rmtree|move|replace|write_text|mkdir)([^A-Za-z0-9_]|$)'; then
      n_triggered=$((n_triggered + 1))
      matrix=$(awk '/^## Матрица замыкания/{flag=1;next}/^## /{flag=0}flag' "$uf")
      # Z3: the table must be the real four-column shape, not a decorative
      # `| stub |` line. Header cells: операция | поверхность | корень/лист |
      # порядок; each data row needs four non-empty cells.
      matrix_problem=""
      if ! printf '%s' "$matrix" | grep -q '^[[:space:]]*|'; then
        matrix_problem="no table"
      else
        header=$(printf '%s\n' "$matrix" | grep -m1 '^[[:space:]]*|' || true)
        for col in 'операция' 'поверхность' 'корень/лист' 'порядок'; do
          printf '%s' "$header" | grep -qi -- "$col" \
            || matrix_problem="header has no '$col' column"
        done
        if [ -z "$matrix_problem" ]; then
          # TODO-0242 (B.11): a header-only table (no data row) proves
          # nothing, and a data row made of placeholders is decoration. The
          # matrix must carry >= 1 real row: rc 1 = no data, rc 2 = placeholder.
          printf '%s\n' "$matrix" | awk '
            /^[[:space:]]*\|/ {
              line=$0
              sub(/^[[:space:]]*\|/, "", line)
              sub(/\|[[:space:]]*$/, "", line)
              n=split(line, cell, "|")
              if (hdr == 0) { hdr=1; next }
              allsep=1; empty=0; placeholder=1
              for (i=1; i<=n; i++) {
                c=cell[i]
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", c)
                if (c !~ /^[-:]+$/) allsep=0
                if (c == "") empty=1
                if (c !~ /^(\.\.\.|…|[-—?]+|[Tt][Oo][Dd][Oo]|[Tt][Bb][Dd]|<[^>]*>)$/) placeholder=0
              }
              if (allsep) next
              if (empty || n < 4) bad=1
              data++
              if (placeholder) ph_bad=1
            }
            END {
              if (data == 0) exit 1
              if (ph_bad) exit 2
              exit bad ? 1 : 0
            }
          ' >/dev/null 2>&1
          arc=$?
          if [ "$arc" -eq 2 ]; then
            matrix_problem="a data row is only placeholders — fill the surface/order cells"
          elif [ "$arc" -ne 0 ]; then
            matrix_problem="no real data row (or a row has fewer than 4 columns / an empty cell)"
          fi
        fi
      fi
      if [ -n "$matrix_problem" ]; then
        echo "FAIL: $uf: boundaries touch the FS but the '## Матрица замыкания' table is invalid — $matrix_problem"
        unit_fail=1
      fi
    fi
  done
  if [ "$unit_fail" -eq 1 ]; then
    fail=1
  fi
fi

if [ "$fail" -eq 0 ]; then
  echo "check-contracts: ok ($n_contracts contracts, $n_units unit contracts, closure matrix: $n_triggered triggered / $n_units units, $n_tokens path tokens, $n_gates gates run, $n_review review-only)"
else
  echo "check-contracts: FAILED ($n_contracts contracts, $n_units unit contracts, closure matrix: $n_triggered triggered / $n_units units, $n_tokens path tokens, $n_gates gates run, $n_review review-only)"
fi
exit "$fail"
