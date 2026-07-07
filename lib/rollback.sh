#!/usr/bin/env bash
# rollback.sh — Rollback changes to baseline
set -euo pipefail

TODO_ID="${1:-}"
if [[ -z "$TODO_ID" ]]; then
    echo "Usage: awf rollback TODO-NNNN [--hard|--soft|--dry-run]"
    exit 1
fi

MODE="hard"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --hard) MODE="hard"; shift ;;
        --soft) MODE="soft"; shift ;;
        --dry-run) MODE="dry-run"; shift ;;
        *) shift ;;
    esac
done

AGENTIC_DIR=".agentic"
CONTEXT_DIR="$AGENTIC_DIR/context"
INBOX_DIR="$AGENTIC_DIR/inbox"

BASELINE_SHA_FILE="$CONTEXT_DIR/BASELINE-$TODO_ID.sha"
if [[ ! -f "$BASELINE_SHA_FILE" ]]; then
    echo "ERROR: Baseline SHA not found: $BASELINE_SHA_FILE"
    echo "Cannot rollback without baseline."
    exit 1
fi

BASELINE_SHA=$(cat "$BASELINE_SHA_FILE")

echo "Rolling back $TODO_ID to baseline: $BASELINE_SHA"
echo "Mode: $MODE"

if [[ "$MODE" == "dry-run" ]]; then
    echo "[DRY RUN] Would run: git reset --${MODE#dry-} $BASELINE_SHA"
    echo ""
    echo "Changes since baseline:"
    git diff "$BASELINE_SHA" --stat
    exit 0
fi

if [[ "$MODE" == "hard" ]]; then
    git reset --hard "$BASELINE_SHA"
else
    git reset "$BASELINE_SHA"
fi

# Create ACK with rollback decision
mkdir -p "$INBOX_DIR"
cat > "$INBOX_DIR/ACK-$TODO_ID.ready" <<EOF
signal: TASK_ACK
ack_type: DONE
decision: rollback
referenced_task_id: $TODO_ID
baseline_sha: $BASELINE_SHA
created_by: supervisor
created_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

echo "Rolled back to $BASELINE_SHA"
echo "ACK file: $INBOX_DIR/ACK-$TODO_ID.ready"
