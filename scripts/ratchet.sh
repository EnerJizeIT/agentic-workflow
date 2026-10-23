#!/bin/sh
# Count ratchet (quality-gates). Ported from pliablepixels/gap-trap (MIT).
#
# Each counter is a shell command that prints one number. The baseline file
# records the last accepted count per name. A count may fall or hold; a rise
# fails, and a baseline more than SLACK above the real count fails too (a
# raised number nobody lowered back).
#
#   sh ratchet.sh            check
#   sh ratchet.sh --update   lower the baseline to the current counts
#   sh ratchet.sh show       print current counts next to the baseline
#
# --update only ever lowers. A count that rose, or a baseline name whose
# counter left the config, needs a hand edit, so the raise lands as a
# reviewable diff with a reason in the commit message rather than as a flag
# the agent can reach for. A counter command that fails is an error, never a
# zero: a broken command counted as 0 reads as a clean sweep. A counter that
# shows 0 the first time gets a warning — a zero can also mean the command
# looks in the wrong place.
#
# Counters live in .ratchet-counters, one per line: name<TAB>command.
#
# A comment in .ratchet-counters may carry a line-count annotation:
#   # @lines <path> (<count> lines)        — rewritten to the file's wc -l
#   # @lines <path> (<old> -> <count> lines) — only the right side is rewritten
# The suffix (lines / строк) is preserved. A missing file leaves the line
# alone. --update refreshes the annotations, so doc numbers stop drifting.
# Scope: ONLY annotations that name their path after @lines. A bare
# "(N lines)" elsewhere in a comment has no file to measure and stays a
# hand-written number.
#
# Exit codes: 0 within baseline, 1 a count grew or drifted, 2 the ratchet
# could not run (broken counter, missing counters or baseline file).
set -u
COUNTERS=${GT_RATCHET_COUNTERS:-.ratchet-counters}
BASELINE=${GT_RATCHET_BASELINE:-.ratchet-baseline}
SLACK=5
TAB=$(printf '\t')

[ -f "$COUNTERS" ] || { echo "ratchet: no $COUNTERS file"; exit 2; }

# The baseline is hand-edited (raises carry a reason in the commit message),
# so malformed lines must fail loudly instead of quietly breaking the
# numeric comparisons the loop below relies on.
if [ -f "$BASELINE" ]; then
  bad=$(awk 'NF != 2 || $2 !~ /^[0-9]+$/ { print "line "NR": "$0 }' "$BASELINE")
  if [ -n "$bad" ]; then
    printf '%s\n' "$bad" | head -5
    echo "ratchet: $BASELINE is malformed; expected one line per counter: name<space>number."
    exit 2
  fi
fi

# name<SPACE>count per line, or name<SPACE>ERROR<SPACE>why when the command
# did not produce exactly one number.
read_counts() {
  grep -v '^#' "$COUNTERS" | grep -v "^[[:space:]]*$" | while IFS="$TAB" read -r name cmd; do
    [ -n "$name" ] || continue
    if [ -z "${cmd:-}" ]; then
      echo "$name ERROR no command after the tab"
      continue
    fi
    out=$(sh -c "$cmd" 2>&1)
    status=$?
    printed=$(printf '%s\n' "$out" | grep -c .)
    value=$(printf '%s' "$out" | tr -d '[:space:]')
    if [ "$status" -ne 0 ]; then
      echo "$name ERROR command exited $status"
      printf '%s\n' "$out" | head -3 | sed 's/^/  | /' >&2
    elif [ "$printed" -ne 1 ]; then
      echo "$name ERROR command printed $printed lines, expected one number"
      printf '%s\n' "$out" | head -3 | sed 's/^/  | /' >&2
    else
      case "$value" in
        '' | *[!0-9]*) echo "$name ERROR command printed a non-number: $value" ;;
        *) echo "$name $value" ;;
      esac
    fi
  done
}

current=$(read_counts)

broken=$(printf '%s\n' "$current" | grep ' ERROR ' || true)
if [ -n "$broken" ]; then
  printf '%s\n' "$broken" | while IFS= read -r line; do echo "ratchet: counter $line"; done
  echo "ratchet: a counter that cannot run is not a zero. Fix the command in $COUNTERS."
  exit 2
fi

if [ -z "$current" ]; then
  echo "ratchet: no counters configured in $COUNTERS."
  echo "ratchet: a ratchet that measures nothing reports the same green as one that measures everything. Add at least one counter."
  exit 2
fi

count_of() { printf '%s\n' "$current" | awk -v n="$1" '$1==n{print $2; exit}'; }
allowed_of() { awk -v n="$1" '$1==n{print $2; exit}' "$BASELINE" 2>/dev/null; }

if [ "${1:-}" = "show" ]; then
  printf '%s\n' "$current" | while IFS= read -r line; do
    name=${line%% *}; count=${line#* }
    allowed=$(allowed_of "$name")
    echo "$name: сейчас $count, baseline ${allowed:-нет}"
  done
  exit 0
fi

# Rewrite one ``@lines <path> (<count>[ lines| строк])`` annotation from the
# file's current line count. An optional ``<old> -> <count>`` keeps the old
# anchor and refreshes only the right side. A missing file leaves the line
# untouched (a stale annotation must not invent a number).
refresh_annotation() {
  line=$1
  rest=${line#*@lines }
  path=${rest%% *}
  case "$rest" in
    *"("*")"*) ;;
    *) printf '%s\n' "$line"; return 0 ;;
  esac
  inside=${rest#*(}
  inside=${inside%%)*}
  # shellcheck disable=SC2086 — the words are the annotation's own fields
  set -- $inside
  first=${1:-}
  case "$first" in
    ''|*[!0-9]*) printf '%s\n' "$line"; return 0 ;;
  esac
  # W2: ``set -u`` treats an unset $2 as an error — a bare-count annotation
  # ``# @lines file.py (12)`` has no word after the count. Defaults, not a
  # rc=0 exit with a blanked line.
  if [ "${2:-}" = "->" ]; then
    third=${3:-}
    case "$third" in
      ''|*[!0-9]*) printf '%s\n' "$line"; return 0 ;;
    esac
    unit=${4:-lines}
    prefix="$first -> "
  else
    # W2: no unit word after the count — the bare form stays bare
    # (``(12)`` refreshes to ``(N)``, never to ``(N lines)``).
    unit=${2:-}
    prefix=""
  fi
  [ -f "$path" ] || { printf '%s\n' "$line"; return 0; }
  count=$(wc -l < "$path" 2>/dev/null | tr -d '[:space:]')
  case "$count" in
    ''|*[!0-9]*) printf '%s\n' "$line"; return 0 ;;
  esac
  # ``rest`` starts right after ``@lines ``: the annotation's own "(...)" is
  # the first one there, while prose parentheses before the annotation (unit
  # ids like ``(TODO-0209)``) stay untouched.
  head="${line%%@lines *}@lines ${rest%%(*}"
  tail=${rest#*)}
  if [ -n "$prefix" ]; then
    printf '%s(%s%s %s)%s\n' "$head" "$prefix" "$count" "$unit" "$tail"
  elif [ -n "$unit" ]; then
    printf '%s(%s %s)%s\n' "$head" "$count" "$unit" "$tail"
  else
    printf '%s(%s)%s\n' "$head" "$count" "$tail"
  fi
}

refresh_annotations() {
  [ -f "$COUNTERS" ] || return 0
  tmp="$COUNTERS.tmp.$$"
  : > "$tmp" || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      *"@lines "*) line=$(refresh_annotation "$line") ;;
    esac
    printf '%s\n' "$line" >> "$tmp" || { rm -f "$tmp"; return 1; }
  done < "$COUNTERS"
  mv "$tmp" "$COUNTERS"
}

if [ "${1:-}" = "--update" ]; then
  refusals=''
  if [ -f "$BASELINE" ]; then
    while IFS= read -r name; do
      [ -n "$name" ] || continue
      count=$(count_of "$name"); allowed=$(allowed_of "$name")
      if [ -z "$count" ]; then
        refusals="$refusals
$name: in the baseline but no counter is configured"
      elif [ -n "$allowed" ] && [ "$count" -gt "$allowed" ]; then
        refusals="$refusals
$name: $allowed -> $count is a rise"
      fi
    done <<EOF
$(awk '{print $1}' "$BASELINE")
EOF
  fi
  if [ -n "$refusals" ]; then
    printf '%s\n' "$refusals" | grep -v '^$'
    echo "--update only lowers. Edit $BASELINE by hand and say why in the commit message."
    exit 1
  fi
  printf '%s\n' "$current" | while IFS= read -r line; do
    name=${line%% *}; count=${line#* }
    allowed=$(allowed_of "$name")
    if [ "$count" -eq 0 ]; then
      if [ -z "$allowed" ]; then
        echo "ratchet: $name = 0 впервые — убедись, что счётчик правда ничего не находит: внеси одно нарушение и посмотри, что число выросло." >&2
      elif [ "$allowed" -ne 0 ]; then
        echo "ratchet: $name упал до 0 (было $allowed) — это уборка или команда смотрит не туда? Внеси одно нарушение и проверь рост." >&2
      fi
    fi
  done
  if ! refresh_annotations; then
    echo "ratchet: could not refresh @lines annotations in $COUNTERS" >&2
    exit 2
  fi
  printf '%s\n' "$current" > "$BASELINE"
  echo "Baseline written:"; cat "$BASELINE"; exit 0
fi
[ -f "$BASELINE" ] || { echo "ratchet: no $BASELINE; run --update once"; exit 2; }

# Every baseline name is checked too, so deleting a counter from $COUNTERS
# cannot retire the number it was holding.
names=$(printf '%s\n%s\n' "$(printf '%s\n' "$current" | awk '{print $1}')" "$(awk '{print $1}' "$BASELINE")" | grep -v '^$' | sort -u)
report=$(printf '%s\n' "$names" | while IFS= read -r name; do
  [ -n "$name" ] || continue
  count=$(count_of "$name"); allowed=$(allowed_of "$name")
  if [ -z "$count" ]; then
    echo "FAIL $name: in the baseline but no counter is configured; restore the counter, or delete the baseline entry and say why in the commit message"
  elif [ -z "$allowed" ]; then
    echo "FAIL $name: new counter, $count found, not ratified; run --update to accept the current count, or fix the debt first"
  elif [ "$count" -gt "$allowed" ]; then
    echo "FAIL $name: $allowed allowed, $count found (+$((count - allowed)))"
  elif [ $((allowed - count)) -gt "$SLACK" ]; then
    echo "FAIL $name: baseline $allowed is $((allowed - count)) above the $count found; run --update"
  elif [ "$count" -lt "$allowed" ]; then
    echo "improved $name: $allowed -> $count, run --update to lock the gain in"
  fi
done)
[ -z "$report" ] || printf '%s\n' "$report"

case "$report" in
  *FAIL*) echo "Fix the problems, or change the numbers in $BASELINE by hand and say why in the commit message."; exit 1 ;;
esac
echo "ratchet: counts within baseline"
exit 0
