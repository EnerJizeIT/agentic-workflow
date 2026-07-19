#!/usr/bin/env bash
# todos.sh — shared helpers for enumerating active/closed TODOs.
#
# Used by orchestrator.sh, status.sh, reset.sh so they all agree on what
# "active" means. Lives outside orchestrator.sh because status/reset must not
# trigger a pipeline dispatch just to read state.
#
# Expected env (set by the caller before sourcing or calling):
#   INBOX  — .agentic/inbox
#   OUTBOX — .agentic/outbox
#
# Public functions:
#   todo_is_closed <TODO-NNNN>     — 0 if a closure signal exists (canonical OR legacy)
#   list_active_todos              — print every active TODO id, highest NNNN first
#   todo_has_progress <TODO-NNNN>  — 0 if PROGRESS-TODO-NNNN.md exists (worker started)

set -uo pipefail

# Closure check, shared across all callers. Tolerates BOTH signal conventions
# (canonical {PREFIX}-TODO-{NNNN} and legacy short {PREFIX}-{NNNN}) so an old
# worker's closure still counts.
todo_is_closed() {
    local id="$1"
    local short="${id#TODO-}"
    [[ -f "$OUTBOX/DONE-${id}.ready"    || -f "$OUTBOX/DONE-${short}.ready" \
    || -f "$OUTBOX/BLOCKED-${id}.ready" || -f "$OUTBOX/BLOCKED-${short}.ready" \
    || -f "$INBOX/ACK-${id}.ready"      || -f "$INBOX/ACK-${short}.ready" ]]
}

# 0 if the worker has produced any progress entry for this TODO.
# Used by `awf reset --orphans` to distinguish a TODO that was never dispatched
# from one that's in-flight.
todo_has_progress() {
    local id="$1"
    local short="${id#TODO-}"
    [[ -s "$OUTBOX/PROGRESS-${id}.ready" || -s "$OUTBOX/PROGRESS-${short}.ready" \
    || -s "$OUTBOX/PROGRESS-${id}.md"    || -s "$OUTBOX/PROGRESS-${short}.md" ]]
}

# Print every active TODO id (one per line), highest-numbered first.
# "Active" = .ready exists, .md non-empty, no closure signal.
# Numeric sort on NNNN (10#$short) so TODO-0010 sorts above TODO-0009.
list_active_todos() {
    local ready_file id short num
    local -a rows=()
    for ready_file in "$INBOX"/TODO-*.ready; do
        [[ -f "$ready_file" ]] || continue
        id=$(basename "$ready_file" .ready)
        short="${id#TODO-}"
        if [[ "$short" =~ ^[0-9]+$ ]]; then
            num=$((10#$short))
        else
            num=0
        fi
        todo_is_closed "$id" && continue
        [[ -s "$INBOX/${id}.md" ]] || continue
        # zero-padded numeric prefix so lexical sort == numeric sort
        rows+=("$(printf '%05d\t%s' "$num" "$id")")
    done
    if [[ ${#rows[@]} -eq 0 ]]; then
        return 0
    fi
    printf '%s\n' "${rows[@]}" | sort -rn -k1,1 | cut -f2
}
