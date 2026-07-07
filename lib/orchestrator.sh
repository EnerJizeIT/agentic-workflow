#!/usr/bin/env bash
# orchestrator.sh — Pipeline execution engine
set -euo pipefail

MODE="${1:-start}"
shift || true

AGENTIC_DIR=".agentic"
INBOX="$AGENTIC_DIR/inbox"
OUTBOX="$AGENTIC_DIR/outbox"
CONTEXT="$AGENTIC_DIR/context"
LOGS="$AGENTIC_DIR/logs"
CONFIG="$AGENTIC_DIR/config.yaml"
PIPELINE_FILE="$AGENTIC_DIR/pipelines/default.yaml"

mkdir -p "$INBOX" "$OUTBOX" "$CONTEXT" "$LOGS"

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOGS/orchestrator.log"
}

if [[ ! -d "$AGENTIC_DIR" ]]; then
    echo "No .agentic/ found. Run 'awf init' first."
    exit 1
fi

# Parse pipeline config (simple YAML parser for our format)
get_pipeline_name() {
    grep 'name:' "$PIPELINE_FILE" | head -1 | sed 's/.*: *"\(.*\)"/\1/' | tr -d '"'
}

find_active_todo() {
    for ready_file in $(ls -1 "$INBOX"/TODO-*.ready 2>/dev/null | sort); do
        ID=$(basename "$ready_file" .ready)
        if [[ -f "$OUTBOX/DONE-$ID.ready" || -f "$OUTBOX/BLOCKED-$ID.ready" || -f "$INBOX/ACK-$ID.ready" ]]; then
            continue
        fi
        if [[ ! -s "$INBOX/$ID.md" ]]; then
            continue
        fi
        echo "$ID"
        return 0
    done
    echo ""
}

run_supervisor_stage() {
    local ACTION="$1"
    local PHASES_FILE=""
    
    if [[ -f "$CONFIG" ]]; then
        PHASES_FILE=$(grep 'current:' "$CONFIG" | sed 's/.*: *"\(.*\)"/\1/' | tr -d '"')
    fi
    
    echo ""
    echo "═══════════════════════════════════════════"
    echo "  SUPERVISOR STAGE: $ACTION"
    echo "═══════════════════════════════════════════"
    echo ""
    echo "Instructions: .agentic/roles/supervisor.md"
    [[ -n "$PHASES_FILE" ]] && echo "Phases file: $PHASES_FILE"
    echo ""
    
    case "$ACTION" in
        create_todo)
            echo "What to do:"
            echo "  1. Study the project state and phases file"
            echo "  2. Determine the next step"
            echo "  3. Create baseline: awf baseline TODO-{NNNN}"
            echo "  4. Write TODO to .agentic/inbox/TODO-{NNNN}.md"
            echo "  5. Create signal: .agentic/inbox/TODO-{NNNN}.ready"
            ;;
        verify_result|final_verify)
            echo "What to do:"
            echo "  1. Read report from .agentic/outbox/"
            echo "  2. Run verification commands independently"
            echo "  3. Check git diff — changes must be in source files"
            echo "  4. Decide: continue / fix / rollback"
            echo "  5. If approved: create .agentic/inbox/ACK-{NNNN}.ready"
            ;;
    esac
    
    echo ""
    echo "When done, press Enter to continue..."
    read -r
    log "Supervisor stage $ACTION completed by user"
}

run_agent_stage() {
    local ROLE="$1"
    local ACTION="$2"
    local TODO_ID="$3"
    
    local AGENT_NAME="$ROLE"
    local MODEL="vllm/llm"
    
    if [[ -f "$CONFIG" ]]; then
        # Try to extract agent_name from config
        AGENT_NAME=$(awk "/^  ${ROLE}:/{found=1} found && /agent_name:/{print; exit}" "$CONFIG" | sed 's/.*: *"\{0,1\}\([^"]*\)"\{0,1\}/\1/')
        [[ -z "$AGENT_NAME" ]] && AGENT_NAME="$ROLE"
    fi
    
    echo ""
    echo "═══════════════════════════════════════════"
    echo "  AGENT STAGE: $ROLE ($ACTION)"
    echo "  Task: $TODO_ID"
    echo "═══════════════════════════════════════════"
    echo ""
    
    local ROLE_FILE=".agentic/roles/${ROLE}.md"
    local TODO_FILE="$INBOX/$TODO_ID.md"
    
    if [[ ! -f "$ROLE_FILE" ]]; then
        echo "ERROR: Role file not found: $ROLE_FILE"
        log "ERROR: Role file missing $ROLE_FILE"
        return 1
    fi
    
    # Build prompt based on action
    local PROMPT=""
    case "$ACTION" in
        execute_todo)
            PROMPT="Execute all Tasks in $TODO_ID via edit tool. Run Verify after each task. Run regression verify before any commit. Write .agentic/outbox/DONE-${TODO_ID}.md or BLOCKED-${TODO_ID}.md and create matching .ready signal. Do not commit unless the TODO explicitly includes a final Git commit step."
            ;;
        review_code)
            PROMPT="Review the code changes for $TODO_ID. Compare TODO with actual git diff. Check quality and correctness. Write .agentic/outbox/REVIEW-APPROVED-${TODO_ID}.md or REVIEW-REJECTED-${TODO_ID}.md with .ready signal."
            ;;
        run_tests)
            PROMPT="Run the full test suite and verification commands. Compare results with baseline. Write .agentic/outbox/TEST-PASSED-${TODO_ID}.md or TEST-FAILED-${TODO_ID}.md with .ready signal."
            ;;
        *)
            PROMPT="Execute action '$ACTION' for $TODO_ID following role instructions. Write result to .agentic/outbox/ with .ready signal."
            ;;
    esac
    
    echo "Running agent: $AGENT_NAME"
    echo "Role file: $ROLE_FILE"
    echo "Task: $TODO_FILE"
    echo ""
    
    log "Agent stage started: $ROLE ($ACTION) for $TODO_ID"
    
    opencode run --auto \
        --agent "$AGENT_NAME" \
        --file "$ROLE_FILE" \
        --file "$TODO_FILE" \
        "$PROMPT"
    
    log "Agent stage finished: $ROLE ($ACTION) for $TODO_ID"
}

wait_for_signal() {
    local TODO_ID="$1"
    local TIMEOUT="${2:-3600}"
    local POLL_INTERVAL=5
    local START_TIME=$(date +%s)
    
    echo "Waiting for agent signal (timeout: ${TIMEOUT}s)..."
    log "Waiting for signal for $TODO_ID"
    
    while true; do
        local SIGNAL=""
        for f in "$OUTBOX"/*-"$TODO_ID".ready; do
            if [[ -f "$f" ]]; then
                SIGNAL=$(basename "$f")
                break
            fi
        done
        
        if [[ -n "$SIGNAL" ]]; then
            echo "Signal received: $SIGNAL"
            log "Signal received: $SIGNAL"
            echo "$SIGNAL"
            return 0
        fi
        
        sleep "$POLL_INTERVAL"
        
        local ELAPSED=$(( $(date +%s) - START_TIME ))
        if [[ $ELAPSED -gt $TIMEOUT ]]; then
            echo "TIMEOUT: Stage did not complete within ${TIMEOUT}s"
            log "TIMEOUT waiting for signal for $TODO_ID"
            return 1
        fi
    done
}

case "$MODE" in
    start)
        echo "=== Agentic Workflow: Starting Pipeline ==="
        log "Pipeline started"
        
        # Stage 1: Supervisor creates TODO
        run_supervisor_stage "create_todo"
        
        # Find active TODO
        TODO_ID=$(find_active_todo)
        if [[ -z "$TODO_ID" ]]; then
            echo "No active TODO found. Create one first."
            exit 1
        fi
        
        echo "Active TODO: $TODO_ID"
        
        # Stage 2: Worker executes
        run_agent_stage "worker" "execute_todo" "$TODO_ID"
        SIGNAL=$(wait_for_signal "$TODO_ID" 3600) || true
        
        # Check signal
        if echo "$SIGNAL" | grep -q "BLOCKED"; then
            echo ""
            echo "Worker returned BLOCKED. Check: $OUTBOX/BLOCKED-${TODO_ID}.md"
            echo "Supervisor should resolve and create a new TODO."
            log "Worker BLOCKED: $TODO_ID"
            exit 0
        elif echo "$SIGNAL" | grep -q "DONE"; then
            echo ""
            echo "Worker returned DONE. Proceeding to verification."
            log "Worker DONE: $TODO_ID"
        else
            echo "WARNING: No clear signal found. Check $OUTBOX/"
            exit 1
        fi
        
        # Stage 3: Supervisor verifies
        run_supervisor_stage "verify_result"
        
        echo ""
        echo "Pipeline iteration complete."
        echo "Run 'awf status' to check state."
        echo "Run 'awf start' again for the next iteration."
        log "Pipeline iteration complete"
        ;;
    
    continue)
        echo "=== Agentic Workflow: Continuing Pipeline ==="
        TODO_ID=$(find_active_todo)
        if [[ -z "$TODO_ID" ]]; then
            echo "No active TODO found."
            exit 0
        fi
        echo "Resuming with: $TODO_ID"
        run_agent_stage "worker" "execute_todo" "$TODO_ID"
        ;;
    
    *)
        echo "Unknown mode: $MODE"
        exit 1
        ;;
esac
