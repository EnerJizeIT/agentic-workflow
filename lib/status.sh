#!/usr/bin/env bash
# status.sh — Show current workflow state
set -euo pipefail

if [[ -z "${LIB_DIR:-}" ]]; then
    LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
# shellcheck source=yaml.sh
source "$LIB_DIR/yaml.sh"

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

# Count tasks
DONE_COUNT=0
BLOCKED_COUNT=0
ACTIVE_COUNT=0

for ready_file in "$INBOX"/TODO-*.ready; do
    [[ -f "$ready_file" ]] || continue
    ID=$(basename "$ready_file" .ready)

    if [[ -f "$OUTBOX/DONE-$ID.ready" ]]; then
        ((DONE_COUNT++)) || true
    elif [[ -f "$OUTBOX/BLOCKED-$ID.ready" ]]; then
        ((BLOCKED_COUNT++)) || true
        echo "Blocked: $ID"
    else
        ((ACTIVE_COUNT++)) || true
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
        fi
    fi
done

echo ""
echo "Summary:"
echo "  Completed: $DONE_COUNT"
echo "  Blocked:   $BLOCKED_COUNT"
echo "  Active:    $ACTIVE_COUNT"

if [[ $ACTIVE_COUNT -eq 0 ]] && [[ $BLOCKED_COUNT -eq 0 ]]; then
    echo ""
    echo "No active tasks. Supervisor should create the next TODO, then run: awf start"
fi
