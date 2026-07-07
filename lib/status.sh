#!/usr/bin/env bash
# status.sh — Show current workflow state
set -euo pipefail

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
    NAME=$(grep 'name:' "$CONFIG" | head -1 | sed 's/.*: *"\(.*\)"/\1/' | tr -d '"')
    [[ -n "$NAME" ]] && PROJECT_NAME="$NAME"
fi

echo "=== Agentic Workflow Status ==="
echo "Project: $PROJECT_NAME"
echo ""

# Count tasks
DONE_COUNT=0
BLOCKED_COUNT=0
ACTIVE_COUNT=0

for ready_file in "$INBOX"/TODO-*.ready 2>/dev/null; do
    [[ -f "$ready_file" ]] || continue
    ID=$(basename "$ready_file" .ready)
    
    if [[ -f "$OUTBOX/DONE-$ID.ready" ]]; then
        ((DONE_COUNT++)) || true
    elif [[ -f "$OUTBOX/BLOCKED-$ID.ready" ]]; then
        ((BLOCKED_COUNT++)) || true
    else
        ((ACTIVE_COUNT++)) || true
        echo "Active: $ID"
        [[ -f "$INBOX/ACK-$ID.ready" ]] && echo "  ACK: $(grep decision "$INBOX/ACK-$ID.ready" 2>/dev/null || echo 'none')"
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
