#!/usr/bin/env bash
# report.sh — Show summary report
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

echo "=============================================="
echo "  Agentic Workflow Report"
echo "  Project: $PROJECT_NAME"
echo "  Generated: $(date '+%Y-%m-%d %H:%M')"
echo "=============================================="
echo ""

DONE_COUNT=0
BLOCKED_COUNT=0

for ready_file in "$INBOX"/TODO-*.ready 2>/dev/null; do
    [[ -f "$ready_file" ]] || continue
    ID=$(basename "$ready_file" .ready)
    
    STEP="unknown"
    if [[ -f "$ready_file" ]]; then
        STEP=$(grep 'step:' "$ready_file" 2>/dev/null | cut -d' ' -f2- || echo "unknown")
    fi
    
    if [[ -f "$OUTBOX/DONE-$ID.ready" ]]; then
        echo "  ✅ $ID  $STEP"
        ((DONE_COUNT++)) || true
    elif [[ -f "$OUTBOX/BLOCKED-$ID.ready" ]]; then
        echo "  🚫 $ID  $STEP"
        ((BLOCKED_COUNT++)) || true
    else
        echo "  🔄 $ID  $STEP  (in progress)"
    fi
done

echo ""
echo "Completed: $DONE_COUNT | Blocked: $BLOCKED_COUNT"

# Git diff stats
echo ""
echo "Files changed:"
git diff --stat 2>/dev/null || echo "  (no changes or not a git repo)"

# Test results from latest log
LATEST_LOG=$(ls -t "$OUTBOX"/TEST-RESULTS-*.log 2>/dev/null | head -1)
if [[ -n "$LATEST_LOG" && -f "$LATEST_LOG" ]]; then
    echo ""
    echo "Latest test results:"
    tail -5 "$LATEST_LOG"
fi
