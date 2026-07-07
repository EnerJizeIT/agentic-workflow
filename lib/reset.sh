#!/usr/bin/env bash
# reset.sh — Clean runtime data
set -euo pipefail

AGENTIC_DIR=".agentic"
TASKS_ONLY=0
FULL=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tasks-only) TASKS_ONLY=1; shift ;;
        --full) FULL=1; shift ;;
        *) shift ;;
    esac
done

if [[ ! -d "$AGENTIC_DIR" ]]; then
    echo "No .agentic/ found."
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
