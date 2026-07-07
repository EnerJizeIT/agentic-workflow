#!/usr/bin/env bash
# orchestrator.sh — Pipeline execution engine
# Reads stages from YAML pipeline config, executes them sequentially,
# handles signals (DONE/BLOCKED/REVIEW/TEST), retries, and transitions.
set -euo pipefail

MODE="${1:-start}"
shift || true

PIPELINE_NAME=""
FROM_STAGE=""
AUTO=0
TIMEOUT=3600

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pipeline)   PIPELINE_NAME="$2"; shift 2 ;;
        --from-stage) FROM_STAGE="$2"; shift 2 ;;
        --auto)       AUTO=1; shift ;;
        --timeout)    TIMEOUT="$2"; shift 2 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

AGENTIC_DIR=".agentic"
INBOX="$AGENTIC_DIR/inbox"
OUTBOX="$AGENTIC_DIR/outbox"
CONTEXT="$AGENTIC_DIR/context"
LOGS="$AGENTIC_DIR/logs"
CONFIG="$AGENTIC_DIR/config.yaml"

mkdir -p "$INBOX" "$OUTBOX" "$CONTEXT" "$LOGS"

log() {
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOGS/orchestrator.log"
}

if [[ ! -d "$AGENTIC_DIR" ]]; then
    echo "No .agentic/ found. Run 'awf init' first."
    exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
    echo "No .agentic/config.yaml found. Run 'awf init' first."
    exit 1
fi

###############################################################################
# YAML parser helpers
###############################################################################

# Determine which pipeline file to use
resolve_pipeline_file() {
    local DEFAULT_PIPE="default"
    if [[ -f "$CONFIG" ]]; then
        local CFG=$(grep 'default_pipeline:' "$CONFIG" 2>/dev/null | sed 's/.*: *"\{0,1\}\([^"]*\)"\{0,1\}/\1/' | tr -d ' ')
        [[ -n "$CFG" ]] && DEFAULT_PIPE="$CFG"
    fi
    [[ -n "$PIPELINE_NAME" ]] && DEFAULT_PIPE="$PIPELINE_NAME"

    # Try pipelines/<name>.yaml
    if [[ -f "$AGENTIC_DIR/pipelines/${DEFAULT_PIPE}.yaml" ]]; then
        echo "$AGENTIC_DIR/pipelines/${DEFAULT_PIPE}.yaml"
    elif [[ -f "$AGENTIC_DIR/pipelines/default.yaml" ]]; then
        echo "$AGENTIC_DIR/pipelines/default.yaml"
    else
        echo "ERROR: No pipeline file found. Expected .agentic/pipelines/${DEFAULT_PIPE}.yaml or default.yaml" >&2
        exit 1
    fi
}

PIPELINE_FILE=$(resolve_pipeline_file)

# Parse stages from YAML into parallel arrays.
# Populates: STAGE_NAMES[], STAGE_ROLES[], STAGE_ACTIONS[], STAGE_DESC[],
#            STAGE_ON_BLOCKED[], STAGE_ON_APPROVED[], STAGE_ON_REJECTED[],
#            STAGE_ON_PASSED[], STAGE_ON_FAILED[], STAGE_MAX_RETRIES[]
parse_stages() {
    STAGE_NAMES=()
    STAGE_ROLES=()
    STAGE_ACTIONS=()
    STAGE_DESC=()
    STAGE_ON_BLOCKED=()
    STAGE_ON_APPROVED=()
    STAGE_ON_REJECTED=()
    STAGE_ON_PASSED=()
    STAGE_ON_FAILED=()
    STAGE_MAX_RETRIES=()

    local current_idx=-1
    local in_stages=0

    while IFS= read -r line; do
        # Detect stages: section
        if [[ "$line" =~ ^stages: ]]; then
            in_stages=1
            continue
        fi

        # If we hit a top-level key outside stages, stop
        if [[ $in_stages -eq 1 ]] && [[ "$line" =~ ^[a-zA-Z] ]] && [[ ! "$line" =~ ^[[:space:]] ]]; then
            in_stages=0
            continue
        fi

        [[ $in_stages -eq 0 ]] && continue

        # New stage entry: "  - name:"
        if [[ "$line" =~ ^[[:space:]]*-[[:space:]]+name:[[:space:]]*\"?([^\"]+)\"? ]]; then
            ((current_idx++)) || true
            STAGE_NAMES+=("${BASH_REMATCH[1]}")
            STAGE_ROLES+=("")
            STAGE_ACTIONS+=("")
            STAGE_DESC+=("")
            STAGE_ON_BLOCKED+=("escalate")
            STAGE_ON_APPROVED+=("next")
            STAGE_ON_REJECTED+=("rollback_to:implement")
            STAGE_ON_PASSED+=("next")
            STAGE_ON_FAILED+=("rollback_to:implement")
            STAGE_MAX_RETRIES+=("1")
            continue
        fi

        [[ $current_idx -lt 0 ]] && continue

        # Parse fields of current stage
        local value
        value=$(echo "$line" | sed 's/.*:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}/\1/' | tr -d '[:space:]')

        if [[ "$line" =~ role: ]]; then
            STAGE_ROLES[$current_idx]="$value"
        elif [[ "$line" =~ action: ]]; then
            STAGE_ACTIONS[$current_idx]="$value"
        elif [[ "$line" =~ description: ]]; then
            STAGE_DESC[$current_idx]=$(echo "$line" | sed 's/.*:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}/\1/')
        elif [[ "$line" =~ on_blocked: ]]; then
            STAGE_ON_BLOCKED[$current_idx]="$value"
        elif [[ "$line" =~ on_approved: ]]; then
            STAGE_ON_APPROVED[$current_idx]="$value"
        elif [[ "$line" =~ on_rejected: ]]; then
            STAGE_ON_REJECTED[$current_idx]="$value"
        elif [[ "$line" =~ on_passed: ]]; then
            STAGE_ON_PASSED[$current_idx]="$value"
        elif [[ "$line" =~ on_failed: ]]; then
            STAGE_ON_FAILED[$current_idx]="$value"
        elif [[ "$line" =~ max_retries: ]]; then
            STAGE_MAX_RETRIES[$current_idx]="$value"
        fi
    done < "$PIPELINE_FILE"
}

###############################################################################
# Config helpers
###############################################################################

get_config_value() {
    local key="$1"
    local default="${2:-}"
    if [[ -f "$CONFIG" ]]; then
        local val
        val=$(grep "$key:" "$CONFIG" 2>/dev/null | head -1 | sed 's/.*:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}/\1/' | tr -d '[:space:]')
        [[ -n "$val" ]] && echo "$val" || echo "$default"
    else
        echo "$default"
    fi
}

get_agent_name() {
    local role="$1"
    local name
    name=$(awk "/^  ${role}:/{found=1} found && /agent_name:/{print; exit}" "$CONFIG" 2>/dev/null | sed 's/.*:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}/\1/')
    echo "${name:-$role}"
}

###############################################################################
# TODO helpers
###############################################################################

find_active_todo() {
    for ready_file in $(ls -1 "$INBOX"/TODO-*.ready 2>/dev/null | sort); do
        local ID
        ID=$(basename "$ready_file" .ready)
        if [[ -f "$OUTBOX/DONE-${ID}.ready" || -f "$OUTBOX/BLOCKED-${ID}.ready" || -f "$INBOX/ACK-${ID}.ready" ]]; then
            continue
        fi
        if [[ ! -s "$INBOX/${ID}.md" ]]; then
            continue
        fi
        echo "$ID"
        return 0
    done
    echo ""
}

###############################################################################
# Stage executors
###############################################################################

run_supervisor_stage() {
    local ACTION="$1"
    local PHASES_FILE
    PHASES_FILE=$(get_config_value "current:" ".agentic/phases/plan.md")

    echo ""
    echo "═══════════════════════════════════════════"
    echo "  SUPERVISOR STAGE: $ACTION"
    echo "═══════════════════════════════════════════"
    echo ""
    echo "Instructions: .agentic/roles/supervisor.md"
    echo "Phases file: $PHASES_FILE"
    echo ""

    case "$ACTION" in
        create_todo)
            echo "What to do:"
            echo "  1. Study the project state and phases file"
            echo "  2. Determine the next step"
            echo "  3. Create baseline: awf baseline TODO-{NNNN}"
            echo "  4. Write task to .agentic/inbox/TODO-{NNNN}.md"
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
        replan)
            echo "Worker returned BLOCKED. Resolve the issue:"
            echo "  1. Read .agentic/outbox/BLOCKED-*.md"
            echo "  2. Analyze the problem"
            echo "  3. Create a new TODO with refined instructions"
            echo "  4. Create .agentic/inbox/TODO-{NNNN}.ready"
            ;;
    esac

    echo ""
    if [[ $AUTO -eq 1 ]]; then
        echo "[auto mode] Skipping supervisor wait."
        log "Supervisor stage $ACTION auto-skipped"
    else
        echo "When done, press Enter to continue..."
        read -r
        log "Supervisor stage $ACTION completed by user"
    fi
}

run_agent_stage() {
    local ROLE="$1"
    local ACTION="$2"
    local TODO_ID="$3"

    local AGENT_NAME
    AGENT_NAME=$(get_agent_name "$ROLE")

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
        audit_code)
            PROMPT="Audit the code changes for $TODO_ID for security issues. Write .agentic/outbox/REVIEW-APPROVED-${TODO_ID}.md or REVIEW-REJECTED-${TODO_ID}.md with .ready signal."
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

###############################################################################
# Signal handling
###############################################################################

wait_for_signal() {
    local TODO_ID="$1"
    local POLL_INTERVAL=5
    local START_TIME
    START_TIME=$(date +%s)

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

# Classify the signal type from filename
signal_type() {
    local sig="$1"
    if [[ "$sig" == DONE-* ]]; then echo "done"; return; fi
    if [[ "$sig" == BLOCKED-* ]]; then echo "blocked"; return; fi
    if [[ "$sig" == REVIEW-APPROVED-* ]]; then echo "approved"; return; fi
    if [[ "$sig" == REVIEW-REJECTED-* ]]; then echo "rejected"; return; fi
    if [[ "$sig" == TEST-PASSED-* ]]; then echo "passed"; return; fi
    if [[ "$sig" == TEST-FAILED-* ]]; then echo "failed"; return; fi
    echo "unknown"
}

###############################################################################
# Transition resolver
###############################################################################

# Given a stage index and signal type, determine what to do next.
# Sets TRANSITION_ACTION and TRANSITION_TARGET (stage index or keyword).
resolve_transition() {
    local stage_idx="$1"
    local sig_type="$2"

    TRANSITION_ACTION=""
    TRANSITION_TARGET=""

    case "$sig_type" in
        blocked)
            local policy="${STAGE_ON_BLOCKED[$stage_idx]:-escalate}"
            if [[ "$policy" == "stop" ]]; then
                TRANSITION_ACTION="stop"
            elif [[ "$policy" == escalate ]]; then
                TRANSITION_ACTION="escalate"
            elif [[ "$policy" =~ ^rollback_to:(.+)$ ]]; then
                TRANSITION_ACTION="rollback"
                TRANSITION_TARGET="${BASH_REMATCH[1]}"
            else
                TRANSITION_ACTION="escalate"
            fi
            ;;
        done|approved|passed)
            local policy
            case "$sig_type" in
                done)     policy="${STAGE_ON_APPROVED[$stage_idx]:-next}" ;;
                approved) policy="${STAGE_ON_APPROVED[$stage_idx]:-next}" ;;
                passed)   policy="${STAGE_ON_PASSED[$stage_idx]:-next}" ;;
            esac
            if [[ "$policy" == "next" ]]; then
                TRANSITION_ACTION="next"
            elif [[ "$policy" == commit_and_next ]]; then
                TRANSITION_ACTION="commit_and_next"
            elif [[ "$policy" == commit_and_report ]]; then
                TRANSITION_ACTION="commit_and_report"
            else
                TRANSITION_ACTION="next"
            fi
            ;;
        rejected|failed)
            local policy
            case "$sig_type" in
                rejected) policy="${STAGE_ON_REJECTED[$stage_idx]:-rollback_to:implement}" ;;
                failed)   policy="${STAGE_ON_FAILED[$stage_idx]:-rollback_to:implement}" ;;
            esac
            if [[ "$policy" =~ ^rollback_to:(.+)$ ]]; then
                TRANSITION_ACTION="rollback"
                TRANSITION_TARGET="${BASH_REMATCH[1]}"
            elif [[ "$policy" == "replan" ]]; then
                TRANSITION_ACTION="escalate"
            else
                TRANSITION_ACTION="escalate"
            fi
            ;;
        *)
            TRANSITION_ACTION="stop"
            ;;
    esac

    log "Transition: stage=$stage_idx signal=$sig_type → action=$TRANSITION_ACTION target=$TRANSITION_TARGET"
}

# Find stage index by name
find_stage_index() {
    local target_name="$1"
    for i in "${!STAGE_NAMES[@]}"; do
        if [[ "${STAGE_NAMES[$i]}" == "$target_name" ]]; then
            echo "$i"
            return 0
        fi
    done
    echo "-1"
}

###############################################################################
# Retry tracking
###############################################################################

RETRY_COUNTS=()

init_retry_counts() {
    RETRY_COUNTS=()
    for _ in "${!STAGE_NAMES[@]}"; do
        RETRY_COUNTS+=(0)
    done
}

increment_retry() {
    local idx="$1"
    RETRY_COUNTS[$idx]=$(( ${RETRY_COUNTS[$idx]} + 1 ))
}

should_retry() {
    local idx="$1"
    local max="${STAGE_MAX_RETRIES[$idx]:-1}"
    [[ ${RETRY_COUNTS[$idx]} -lt $max ]]
}

###############################################################################
# Main pipeline loop
###############################################################################

run_pipeline() {
    parse_stages

    local total=${#STAGE_NAMES[@]}
    if [[ $total -eq 0 ]]; then
        echo "ERROR: No stages found in $PIPELINE_FILE"
        exit 1
    fi

    echo "=== Agentic Workflow: Starting Pipeline ==="
    echo "Pipeline: $PIPELINE_FILE"
    echo "Stages: ${STAGE_NAMES[*]}"
    echo ""
    log "Pipeline started with $total stages: ${STAGE_NAMES[*]}"

    init_retry_counts

    # Determine starting stage
    local stage_idx=0
    if [[ -n "$FROM_STAGE" ]]; then
        stage_idx=$(find_stage_index "$FROM_STAGE")
        if [[ $stage_idx -eq -1 ]]; then
            echo "ERROR: Stage '$FROM_STAGE' not found in pipeline"
            exit 1
        fi
        log "Starting from stage: $FROM_STAGE (index $stage_idx)"
    fi

    # Track current TODO across stages
    local CURRENT_TODO=""

    while [[ $stage_idx -ge 0 ]] && [[ $stage_idx -lt $total ]]; do
        local s_name="${STAGE_NAMES[$stage_idx]}"
        local s_role="${STAGE_ROLES[$stage_idx]}"
        local s_action="${STAGE_ACTIONS[$stage_idx]}"
        local s_desc="${STAGE_DESC[$stage_idx]:-}"

        echo ""
        echo "───────────────────────────────────────────"
        echo "  Stage $((stage_idx + 1))/$total: $s_name ($s_role :: $s_action)"
        [[ -n "$s_desc" ]] && echo "  $s_desc"
        echo "───────────────────────────────────────────"
        log "Stage $stage_idx: $s_name ($s_role :: $s_action)"

        # Supervisor stage
        if [[ "$s_role" == "supervisor" ]]; then
            run_supervisor_stage "$s_action"

            # After create_todo or replan, find active TODO
            if [[ "$s_action" == "create_todo" || "$s_action" == "replan" ]]; then
                CURRENT_TODO=$(find_active_todo)
                if [[ -z "$CURRENT_TODO" ]]; then
                    echo "No active TODO found. Create one first, then continue."
                    log "No active TODO after supervisor stage"
                    exit 1
                fi
                echo "Active TODO: $CURRENT_TODO"
            fi

            # After verify/finalize, move to next stage
            if [[ "$s_action" == "verify_result" || "$s_action" == "final_verify" ]]; then
                echo "Supervisor verification complete."
                ((stage_idx++)) || true
                continue
            fi

            ((stage_idx++)) || true
            continue
        fi

        # Agent stage
        if [[ -z "$CURRENT_TODO" ]]; then
            CURRENT_TODO=$(find_active_todo)
            if [[ -z "$CURRENT_TODO" ]]; then
                echo "No active TODO for agent stage '$s_name'. Run supervisor stage first."
                exit 1
            fi
        fi

        run_agent_stage "$s_role" "$s_action" "$CURRENT_TODO"

        # Wait for signal (opencode run is blocking, but double-check outbox)
        local SIGNAL=""
        for f in "$OUTBOX"/*-"$CURRENT_TODO".ready; do
            if [[ -f "$f" ]]; then
                SIGNAL=$(basename "$f")
                break
            fi
        done

        if [[ -z "$SIGNAL" ]]; then
            # Agent may have exited without writing signal; try polling briefly
            SIGNAL=$(wait_for_signal "$CURRENT_TODO" 60) || true
        fi

        if [[ -z "$SIGNAL" ]]; then
            echo "WARNING: No signal found after agent stage '$s_name'. Check $OUTBOX/"
            log "WARNING: No signal after stage $s_name"
            exit 1
        fi

        local SIG_TYPE
        SIG_TYPE=$(signal_type "$SIGNAL")
        echo "Signal classified as: $SIG_TYPE"

        resolve_transition "$stage_idx" "$SIG_TYPE"

        case "$TRANSITION_ACTION" in
            next|commit_and_next|commit_and_report)
                if [[ "$TRANSITION_ACTION" == commit_and_next || "$TRANSITION_ACTION" == commit_and_report ]]; then
                    echo "Approving changes. (Commit handled by supervisor.)"
                    log "Auto-approve for $s_name"
                fi
                echo "Moving to next stage."
                RETRY_COUNTS[$stage_idx]=0
                ((stage_idx++)) || true
                ;;
            escalate)
                if should_retry "$stage_idx"; then
                    increment_retry "$stage_idx"
                    echo "BLOCKED — escalating to supervisor (attempt $(( ${RETRY_COUNTS[$stage_idx]} ))/${STAGE_MAX_RETRIES[$stage_idx]})"
                    log "Escalating to supervisor for retry"

                    # Supervisor replan stage
                    run_supervisor_stage "replan"

                    # Find new TODO created by supervisor
                    local NEW_TODO
                    NEW_TODO=$(find_active_todo)
                    if [[ -n "$NEW_TODO" ]]; then
                        CURRENT_TODO="$NEW_TODO"
                        echo "New TODO: $CURRENT_TODO — retrying stage '$s_name'"
                        # Stay on same stage index
                    else
                        echo "Supervisor did not create a new TODO. Stopping."
                        exit 1
                    fi
                else
                    echo "BLOCKED — max retries reached (${STAGE_MAX_RETRIES[$stage_idx]}). Pipeline stopped."
                    log "Max retries reached for stage $s_name"
                    exit 1
                fi
                ;;
            rollback)
                local target_stage
                target_stage=$(find_stage_index "$TRANSITION_TARGET")
                if [[ $target_stage -ge 0 ]]; then
                    echo "Rolling back to stage: ${STAGE_NAMES[$target_stage]}"
                    log "Rollback to stage ${STAGE_NAMES[$target_stage]} (index $target_stage)"
                    stage_idx=$target_stage

                    # Before retrying, supervisor replan
                    run_supervisor_stage "replan"
                    local NEW_TODO
                    NEW_TODO=$(find_active_todo)
                    if [[ -n "$NEW_TODO" ]]; then
                        CURRENT_TODO="$NEW_TODO"
                    fi
                else
                    echo "ERROR: Rollback target '$TRANSITION_TARGET' not found in pipeline"
                    exit 1
                fi
                ;;
            stop)
                echo "Pipeline stopped by policy."
                log "Pipeline stopped by policy at stage $s_name"
                exit 0
                ;;
            *)
                echo "Unknown transition: $TRANSITION_ACTION"
                exit 1
                ;;
        esac
    done

    # All stages completed
    echo ""
    echo "═══════════════════════════════════════════"
    echo "  Pipeline complete!"
    echo "═══════════════════════════════════════════"
    echo ""
    echo "Run 'awf status' to check state."
    echo "Run 'awf report' for summary."
    echo "Run 'awf start' for the next iteration."
    log "Pipeline complete"
}

###############################################################################
# Mode dispatch
###############################################################################

case "$MODE" in
    start)
        run_pipeline
        ;;
    continue)
        echo "=== Agentic Workflow: Continuing Pipeline ==="
        parse_stages
        CURRENT_TODO=$(find_active_todo)
        if [[ -z "$CURRENT_TODO" ]]; then
            echo "No active TODO found."
            exit 0
        fi
        echo "Resuming with: $CURRENT_TODO"
        log "Continuing pipeline with $CURRENT_TODO"

        # Find first agent stage and resume there
        for i in "${!STAGE_NAMES[@]}"; do
            if [[ "${STAGE_ROLES[$i]}" != "supervisor" ]]; then
                run_agent_stage "${STAGE_ROLES[$i]}" "${STAGE_ACTIONS[$i]}" "$CURRENT_TODO"
                break
            fi
        done
        ;;
    *)
        echo "Unknown mode: $MODE"
        echo "Use 'awf start' or 'awf continue'"
        exit 1
        ;;
esac
