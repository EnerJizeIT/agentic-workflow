#!/usr/bin/env bash
# reset.sh — Clean runtime data
set -euo pipefail

if [[ -z "${LIB_DIR:-}" ]]; then
    LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
# shellcheck source=todos.sh
source "$LIB_DIR/todos.sh"

AGENTIC_DIR=".agentic"
INBOX="$AGENTIC_DIR/inbox"
OUTBOX="$AGENTIC_DIR/outbox"
TASKS_ONLY=0
FULL=0
ORPHANS=0
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tasks-only) TASKS_ONLY=1; shift ;;
        --full) FULL=1; shift ;;
        --orphans) ORPHANS=1; shift ;;
        --force) FORCE=1; shift ;;   # skip confirmation for --orphans
        *) shift ;;
    esac
done

if [[ ! -d "$AGENTIC_DIR" ]]; then
    echo "No .agentic/ found."
    exit 0
fi

# --orphans: drop TODO-{NNNN}.ready + .md for active TODOs the worker never
# touched (no PROGRESS-TODO-{NNNN}.md). This is the safe subset of --tasks-only:
# an orphan is provably never-dispatched, so removing it cannot lose work.
# Use case: status shows "⚠️ N active TODOs" and the stale ones need clearing.
if [[ $ORPHANS -eq 1 ]]; then
    echo "=== Looking for orphan TODOs (active, no progress) ==="
    orphan_ids=()
    while IFS= read -r id; do
        [[ -z "$id" ]] && continue
        if ! todo_has_progress "$id"; then
            orphan_ids+=("$id")
        fi
    done < <(list_active_todos)

    if [[ ${#orphan_ids[@]} -eq 0 ]]; then
        echo "No orphans found. Every active TODO has a PROGRESS file."
        exit 0
    fi

    echo "Found ${#orphan_ids[@]} orphan TODO(s):"
    for id in "${orphan_ids[@]}"; do
        ready_time="(unknown)"
        if [[ -f "$INBOX/$id.ready" ]]; then
            ts=$(stat -c %y "$INBOX/$id.ready" 2>/dev/null || stat -f %Sm "$INBOX/$id.ready" 2>/dev/null || echo "")
            [[ -n "$ts" ]] && ready_time="$ts"
        fi
        echo "  $id   created: $ready_time"
    done
    echo ""

    if [[ $FORCE -ne 1 ]]; then
        read -rp "Remove these ${#orphan_ids[@]} orphan TODO(s)? [y/N] " ans
        [[ "$ans" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
    fi

    for id in "${orphan_ids[@]}"; do
        rm -f "$INBOX/$id.ready" "$INBOX/$id.md"
        echo "  removed: $id"
    done
    echo "Done. Active TODOs with progress are untouched."
    exit 0
fi

echo "=== Reset agentic workflow ==="

if [[ $FULL -eq 1 ]]; then
    echo "Cleaning all runtime directories..."
    rm -rf "$AGENTIC_DIR/inbox/"*
    rm -rf "$AGENTIC_DIR/outbox/"*
    rm -rf "$AGENTIC_DIR/context/"*
    rm -rf "$AGENTIC_DIR/logs/"*
    rm -rf "$AGENTIC_DIR/reports/"*
elif [[ $TASKS_ONLY -eq 1 ]]; then
    echo "Cleaning inbox and outbox..."
    rm -rf "$AGENTIC_DIR/inbox/"*
    rm -rf "$AGENTIC_DIR/outbox/"*
else
    echo "Cleaning all runtime (keeping phases)..."
    rm -rf "$AGENTIC_DIR/inbox/"*
    rm -rf "$AGENTIC_DIR/outbox/"*
    rm -rf "$AGENTIC_DIR/context/"*
    rm -rf "$AGENTIC_DIR/logs/"*
    rm -rf "$AGENTIC_DIR/reports/"*
fi

echo "Done. Runtime cleaned."
