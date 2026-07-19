#!/usr/bin/env bash
# status.sh — Show current workflow state
set -euo pipefail

if [[ -z "${LIB_DIR:-}" ]]; then
    LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
# shellcheck source=yaml.sh
source "$LIB_DIR/yaml.sh"
# shellcheck source=todos.sh
source "$LIB_DIR/todos.sh"

AGENTIC_DIR=".agentic"
INBOX="$AGENTIC_DIR/inbox"
OUTBOX="$AGENTIC_DIR/outbox"
CONFIG="$AGENTIC_DIR/config.yaml"

if [[ ! -d "$AGENTIC_DIR" ]]; then
    echo "No .agentic/ found. Run 'awf init' first."
    exit 1
fi

PROJECT_NAME="Project"
if [[ -f "$CONFIG" ]]; then
    NAME=$(yaml_get "project.name" "$CONFIG" "Project")
    [[ -n "$NAME" ]] && PROJECT_NAME="$NAME"
fi

echo "=== Agentic Workflow Status ==="
echo "Project: $PROJECT_NAME"
echo ""

# Collect ALL active TODOs (not just the first one). When >1 is active, that is
# a conflict the user must see — the orchestrator will pick the newest, but
# the older ones are still "live" and need explicit closure or rollback.
# Closure rules are shared with orchestrator via lib/todos.sh.
ACTIVE_IDS=()
NEWEST_TODO=""
if [[ -d "$INBOX" ]]; then
    while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        ACTIVE_IDS+=("$line")
        [[ -z "$NEWEST_TODO" ]] && NEWEST_TODO="$line"   # list_active_todos returns highest first
    done < <(list_active_todos)
fi

# Count tasks
DONE_COUNT=0
BLOCKED_COUNT=0
ACTIVE_COUNT=${#ACTIVE_IDS[@]}

for ready_file in "$INBOX"/TODO-*.ready; do
    [[ -f "$ready_file" ]] || continue
    ID=$(basename "$ready_file" .ready)
    if [[ -f "$OUTBOX/DONE-$ID.ready" ]]; then
        ((DONE_COUNT++)) || true
    elif [[ -f "$OUTBOX/BLOCKED-$ID.ready" ]]; then
        ((BLOCKED_COUNT++)) || true
        echo "Blocked: $ID"
    fi
done

# Active TODOs — print each with its progress snapshot
for ID in "${ACTIVE_IDS[@]}"; do
    echo "Active: $ID"
    [[ -f "$INBOX/ACK-$ID.ready" ]] && echo "  ACK: $(grep decision "$INBOX/ACK-$ID.ready" 2>/dev/null || echo 'none')"

    # Show progress if available
    PROGRESS_FILE="$OUTBOX/PROGRESS-${ID}.md"
    if [[ -f "$PROGRESS_FILE" ]]; then
        TOTAL_TASKS=$(grep -c '^## Task' "$PROGRESS_FILE" 2>/dev/null || echo 0)
        DONE_TASKS=$(grep '^## Task' "$PROGRESS_FILE" 2>/dev/null | grep -c '\[x\]' || echo 0)
        FAIL_TASKS=$(grep '^## Task' "$PROGRESS_FILE" 2>/dev/null | grep -c '\[!\]' || echo 0)
        echo "  Progress: $DONE_TASKS/$TOTAL_TASKS tasks done"
        if [[ $FAIL_TASKS -gt 0 ]]; then
            echo "  Failed:  $FAIL_TASKS task(s)"
        fi
        # Show last progress entry
        LAST_ENTRY=$(grep '^## Task' "$PROGRESS_FILE" 2>/dev/null | tail -1 || echo "")
        if [[ -n "$LAST_ENTRY" ]]; then
            echo "  Last:    $LAST_ENTRY"
        fi
    else
        echo "  Progress: (none — worker has not started)"
    fi
done

echo ""
echo "Summary:"
echo "  Completed: $DONE_COUNT"
echo "  Blocked:   $BLOCKED_COUNT"
echo "  Active:    $ACTIVE_COUNT"

if [[ $ACTIVE_COUNT -gt 1 ]]; then
    echo ""
    echo "⚠️  $ACTIVE_COUNT active TODOs detected."
    echo "   Orchestrator will run: $NEWEST_TODO (highest NNNN)."
    echo "   The other(s) are stale and should be closed explicitly:"
    echo "     - rollback:    awf rollback TODO-NNNN"
    echo "     - drop orphan: awf reset --orphans"
fi

if [[ $ACTIVE_COUNT -eq 0 ]] && [[ $BLOCKED_COUNT -eq 0 ]]; then
    echo ""
    echo "No active tasks. Supervisor should create the next TODO, then run: awf start"
fi
