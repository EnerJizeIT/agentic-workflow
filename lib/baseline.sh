#!/usr/bin/env bash
# baseline.sh — Create a baseline snapshot before executing a task
set -euo pipefail

TODO_ID="${1:-}"
if [[ -z "$TODO_ID" ]]; then
    echo "Usage: awf baseline TODO-NNNN"
    exit 1
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
AGENTIC_DIR="$PROJECT_ROOT/.agentic"
CONTEXT_DIR="$AGENTIC_DIR/context"
CONFIG_FILE="$AGENTIC_DIR/config.yaml"

mkdir -p "$CONTEXT_DIR"

echo "Creating baseline for $TODO_ID..."

# 1. Git SHA
git rev-parse HEAD > "$CONTEXT_DIR/BASELINE-$TODO_ID.sha"

# 2. Git status
git status --short > "$CONTEXT_DIR/BASELINE-$TODO_ID.status"

# 3. Test baseline (if test_cmd configured)
if [[ -f "$CONFIG_FILE" ]]; then
    TEST_CMD=$(grep 'test_cmd:' "$CONFIG_FILE" | sed 's/.*: *"\(.*\)"/\1/' | tr -d '"')
    if [[ -n "$TEST_CMD" ]]; then
        eval "$TEST_CMD" > "$CONTEXT_DIR/BASELINE-$TODO_ID.tests.log" 2>&1 || true
        echo "Test baseline saved."
    else
        echo "No test_cmd configured, skipping test baseline." > "$CONTEXT_DIR/BASELINE-$TODO_ID.tests.log"
    fi
else
    echo "No config.yaml found, skipping test baseline." > "$CONTEXT_DIR/BASELINE-$TODO_ID.tests.log"
fi

# 4. Python environment (optional)
PYTHON_CMD="python3"
if ! command -v python3 &>/dev/null && command -v python &>/dev/null; then
    PYTHON_CMD="python"
fi
$PYTHON_CMD --version > "$CONTEXT_DIR/BASELINE-$TODO_ID.env.log" 2>&1 || true
$PYTHON_CMD -m pip list >> "$CONTEXT_DIR/BASELINE-$TODO_ID.env.log" 2>&1 || true

echo "Baseline files created:"
ls -la "$CONTEXT_DIR/BASELINE-$TODO_ID".*
