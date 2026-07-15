#!/usr/bin/env bash
# baseline.sh — Create a baseline snapshot before executing a task
set -euo pipefail

if [[ -z "${LIB_DIR:-}" ]]; then
    LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
# shellcheck source=yaml.sh
source "$LIB_DIR/yaml.sh"

TODO_ID="${1:-}"
if [[ -z "$TODO_ID" ]]; then
    echo "Usage: awf baseline TODO-NNNN"
    exit 1
fi

# baseline runs from the user's project root (where .agentic/ lives).
PROJECT_ROOT="$(pwd)"
AGENTIC_DIR="$PROJECT_ROOT/.agentic"
CONTEXT_DIR="$AGENTIC_DIR/context"
CONFIG_FILE="$AGENTIC_DIR/config.yaml"

if [[ ! -d "$AGENTIC_DIR" ]]; then
    echo "No .agentic/ found. Run 'awf init' first."
    exit 1
fi

mkdir -p "$CONTEXT_DIR"

echo "Creating baseline for $TODO_ID..."

# 1. Git SHA (best-effort — project may not be a git repo yet)
if git rev-parse --git-dir &>/dev/null; then
    git rev-parse HEAD > "$CONTEXT_DIR/BASELINE-$TODO_ID.sha"
    git status --short > "$CONTEXT_DIR/BASELINE-$TODO_ID.status"
else
    echo "(not a git repo)" > "$CONTEXT_DIR/BASELINE-$TODO_ID.sha"
    : > "$CONTEXT_DIR/BASELINE-$TODO_ID.status"
fi

# 2. Test baseline (if test_cmd configured)
if [[ -f "$CONFIG_FILE" ]]; then
    TEST_CMD=$(yaml_get "verification.test_cmd" "$CONFIG_FILE" "")
    if [[ -n "$TEST_CMD" ]]; then
        # Word-split intentionally so "pytest tests/" splits into cmd + arg.
        # shellcheck disable=SC2086
        if $TEST_CMD > "$CONTEXT_DIR/BASELINE-$TODO_ID.tests.log" 2>&1; then
            echo "Test baseline saved."
        else
            echo "Test baseline saved (command exited non-zero — recorded as-is)."
        fi
    else
        echo "No test_cmd configured, skipping test baseline." > "$CONTEXT_DIR/BASELINE-$TODO_ID.tests.log"
    fi
else
    echo "No config.yaml found, skipping test baseline." > "$CONTEXT_DIR/BASELINE-$TODO_ID.tests.log"
fi

# 3. Python environment (optional, best-effort)
PYTHON_CMD="python3"
if ! command -v python3 &>/dev/null && command -v python &>/dev/null; then
    PYTHON_CMD="python"
fi
{
    $PYTHON_CMD --version 2>&1 || true
    $PYTHON_CMD -m pip list 2>/dev/null || true
} > "$CONTEXT_DIR/BASELINE-$TODO_ID.env.log" 2>&1 || true

echo ""
echo "Baseline files created:"
ls -1 "$CONTEXT_DIR/BASELINE-$TODO_ID".* 2>/dev/null | sed 's|^|  |'
