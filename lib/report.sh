#!/usr/bin/env bash
# report.sh — Show summary report
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

echo "=============================================="
echo "  Agentic Workflow Report"
echo "  Project: $PROJECT_NAME"
echo "  Generated: $(date '+%Y-%m-%d %H:%M')"
echo "=============================================="
echo ""

DONE_COUNT=0
BLOCKED_COUNT=0

shopt -s nullglob
for ready_file in "$INBOX"/TODO-*.ready; do
    ID=$(basename "$ready_file" .ready)
    STEP=$(grep 'step:' "$ready_file" 2>/dev/null | cut -d' ' -f2- || echo "unknown")
    [[ -z "$STEP" ]] && STEP="unknown"

    if [[ -f "$OUTBOX/DONE-$ID.ready" ]]; then
        echo "  OK   $ID  $STEP"
        ((DONE_COUNT++)) || true
    elif [[ -f "$OUTBOX/BLOCKED-$ID.ready" ]]; then
        echo "  BLK  $ID  $STEP"
        ((BLOCKED_COUNT++)) || true
    else
        echo "  ...  $ID  $STEP  (in progress)"
    fi
done
shopt -u nullglob

echo ""
echo "Completed: $DONE_COUNT | Blocked: $BLOCKED_COUNT"

# Git diff stats
echo ""
echo "Files changed:"
git diff --stat 2>/dev/null || echo "  (no changes or not a git repo)"

# Test results from latest log
LATEST_LOG=$(ls -t "$OUTBOX"/TEST-RESULTS-*.log 2>/dev/null | head -1 || true)
if [[ -n "$LATEST_LOG" && -f "$LATEST_LOG" ]]; then
    echo ""
    echo "Latest test results:"
    tail -5 "$LATEST_LOG"
fi
