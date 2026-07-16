#!/usr/bin/env bash
# orchestrator.sh — Pipeline execution engine
# Reads stages from YAML pipeline config, executes them sequentially,
# handles signals (DONE/BLOCKED/REVIEW/TEST), retries, and transitions.
set -euo pipefail

# yaml.sh lives next to this file; bin/awf exports LIB_DIR.
if [[ -z "${LIB_DIR:-}" ]]; then
    LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
# shellcheck source=yaml.sh
source "$LIB_DIR/yaml.sh"

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

if [[ ! -d "$AGENTIC_DIR" ]]; then
    echo "No .agentic/ found. Run 'awf init' first."
    exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
    echo "No .agentic/config.yaml found. Run 'awf init' first."
    exit 1
fi

mkdir -p "$INBOX" "$OUTBOX" "$CONTEXT" "$LOGS"

log() {
    mkdir -p "$LOGS"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOGS/orchestrator.log"
}

###############################################################################
# Pipeline resolution & parsing (via lib/yaml.sh)
###############################################################################

# Determine which pipeline file to use.
resolve_pipeline_file() {
    local default_pipe
    default_pipe=$(yaml_get "default_pipeline" "$CONFIG" "default")
    [[ -n "$PIPELINE_NAME" ]] && default_pipe="$PIPELINE_NAME"

    local candidate="$AGENTIC_DIR/pipelines/${default_pipe}.yaml"
    if [[ -f "$candidate" ]]; then
        echo "$candidate"
        return 0
    fi
    # Last-resort fallback to default.yaml so --pipeline typos still find something.
    if [[ "$default_pipe" != "default" && -f "$AGENTIC_DIR/pipelines/default.yaml" ]]; then
        echo "$AGENTIC_DIR/pipelines/default.yaml"
        return 0
    fi
    echo "ERROR: No pipeline file found. Expected $candidate" >&2
    exit 1
}

PIPELINE_FILE=$(resolve_pipeline_file)

# Parse stages from the pipeline YAML into parallel arrays using yaml_stages_dump
# (TSV rows). Columns: name role action description on_blocked on_approved
#                     on_rejected on_passed on_failed max_retries
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

    local row_count=0
    local IFS=$'\t'
    while read -r name role action desc on_b on_a on_r on_p on_f max; do
        [[ -z "$name" ]] && continue
        STAGE_NAMES+=("$name")
        STAGE_ROLES+=("$role")
        STAGE_ACTIONS+=("$action")
        STAGE_DESC+=("$desc")
        STAGE_ON_BLOCKED+=("${on_b:-escalate}")
        STAGE_ON_APPROVED+=("${on_a:-next}")
        STAGE_ON_REJECTED+=("${on_r:-rollback_to:implement}")
        STAGE_ON_PASSED+=("${on_p:-next}")
        STAGE_ON_FAILED+=("${on_f:-rollback_to:implement}")
        STAGE_MAX_RETRIES+=("${max:-1}")
        row_count=$((row_count + 1))
    done < <(yaml_stages_dump "$PIPELINE_FILE")
    unset IFS

    if [[ $row_count -eq 0 ]]; then
        echo "ERROR: No stages parsed from $PIPELINE_FILE (backend: $(yaml_backend))." >&2
        echo "       Check the YAML syntax or install yq / PyYAML." >&2
        exit 1
    fi
}

###############################################################################
# Config helpers (via lib/yaml.sh)
###############################################################################

get_config_value() {
    local key="$1"
    local default="${2:-}"
    yaml_get "$key" "$CONFIG" "$default"
}

# agent_name override for a role; defaults to the role name itself.
get_agent_name() {
    local role="$1"
    local name
    name=$(yaml_get "models.${role}.agent_name" "$CONFIG" "$role")
    echo "$name"
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
    local TODO_REF="${2:-}"
    local PHASES_FILE
    PHASES_FILE=$(get_config_value "phases.current" ".agentic/phases/plan.md")

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
        salvage)
            # No-signal recovery: worker finished but never signalled (timeout/crash).
            # The work may be complete and just unrecorded.
            local BASE_SHA=""
            [[ -n "$TODO_REF" && -f "$CONTEXT/BASELINE-${TODO_REF}.sha" ]] \
                && BASE_SHA=$(head -1 "$CONTEXT/BASELINE-${TODO_REF}.sha" 2>/dev/null)
            echo "Worker finished but wrote NO signal (timeout/crash). Work may be complete."
            echo ""
            echo "Investigate the orphaned work:"
            echo "  1. See what changed:  git diff ${BASE_SHA:+$BASE_SHA }--stat"
            echo "  2. Worker progress:   cat $OUTBOX/PROGRESS-${TODO_REF}.md"
            echo "  3. Verify changes independently (build, tests, review)."
            echo ""
            echo "Decide and act:"
            echo "  - good  -> salvage: write $OUTBOX/DONE-${TODO_REF}.md + $OUTBOX/DONE-${TODO_REF}.ready"
            echo "  - bad   -> rollback to baseline, then create a new TODO (replan)"
            echo "  - stuck -> leave as-is and stop"
            echo ""
            echo "After writing a signal (or a new TODO), press Enter to continue."
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

    # Drop stale signals this action could produce, so we never misread a
    # leftover from a previous run as the current stage's result.
    local STAGE_PREFIXES
    # shellcheck disable=SC2207
    STAGE_PREFIXES=($(expected_signal_prefixes "$ACTION"))
    clean_stage_signals "$TODO_ID" "${STAGE_PREFIXES[@]}"

    log "Agent stage started: $ROLE ($ACTION) for $TODO_ID"

    opencode run --auto \
        --agent "$AGENT_NAME" \
        --file "$ROLE_FILE" \
        --file "$TODO_FILE" \
        -- "$PROMPT"

    log "Agent stage finished: $ROLE ($ACTION) for $TODO_ID"
}

###############################################################################
# Signal handling
###############################################################################

wait_for_signal() {
    local TODO_ID="$1"
    shift
    local PREFIXES=("$@")
    [[ ${#PREFIXES[@]} -eq 0 ]] && PREFIXES=(DONE BLOCKED)
    local POLL_INTERVAL=5
    local START_TIME
    START_TIME=$(date +%s)

    # IMPORTANT: all diagnostics go to stderr. The caller captures stdout
    # (`SIGNAL=$(wait_for_signal ...)`) to get ONLY the signal name on success.
    # Echoing progress/timeout text to stdout contaminates that capture and caused
    # the orphan bug (multi-line garbage was classified as signal_type "unknown").
    echo "Waiting for agent signal (prefixes: ${PREFIXES[*]}; timeout: ${TIMEOUT}s)..." >&2
    log "Waiting for signal for $TODO_ID (prefixes: ${PREFIXES[*]})"

    while true; do
        local SIGNAL=""
        SIGNAL=$(read_signal_for_todo "$TODO_ID" "${PREFIXES[@]}" || true)

        if [[ -n "$SIGNAL" ]]; then
            echo "Signal received: $SIGNAL" >&2
            log "Signal received: $SIGNAL"
            printf '%s' "$SIGNAL"
            return 0
        fi

        # Show progress if available
        local PROGRESS_FILE="$OUTBOX/PROGRESS-${TODO_ID}.md"
        if [[ -f "$PROGRESS_FILE" ]]; then
            local TOTAL_TASKS DONE_TASKS
            TOTAL_TASKS=$(grep -c '^## Task' "$PROGRESS_FILE" 2>/dev/null || echo 0)
            DONE_TASKS=$(grep '^## Task' "$PROGRESS_FILE" 2>/dev/null | grep -c '\[x\]' || echo 0)
            echo "  Progress: $DONE_TASKS/$TOTAL_TASKS tasks completed" >&2
        fi

        sleep "$POLL_INTERVAL"

        local ELAPSED=$(( $(date +%s) - START_TIME ))
        if [[ $ELAPSED -gt $TIMEOUT ]]; then
            echo "TIMEOUT: Stage did not complete within ${TIMEOUT}s" >&2
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

# Signal prefixes an action is allowed to emit. Anything else is treated as
# stale (left over from a previous stage) and ignored.
expected_signal_prefixes() {
    case "$1" in
        execute_todo)                      echo "DONE BLOCKED" ;;
        review_code|audit_code)            echo "REVIEW-APPROVED REVIEW-REJECTED BLOCKED" ;;
        run_tests)                         echo "TEST-PASSED TEST-FAILED BLOCKED" ;;
        *)                                 echo "DONE BLOCKED REVIEW-APPROVED REVIEW-REJECTED TEST-PASSED TEST-FAILED" ;;
    esac
}

# Remove stale .ready + .md reports for a TODO that this action would produce.
# Keeps reports from earlier stages (e.g. worker's DONE survives into review).
clean_stage_signals() {
    local todo="$1"; shift
    local prefix
    for prefix in "$@"; do
        rm -f "$OUTBOX/${prefix}-${todo}.ready" "$OUTBOX/${prefix}-${todo}.md" 2>/dev/null || true
    done
}

# Echo the first existing signal (basename without .ready) for a TODO among the
# given prefixes, in order. Returns non-zero if none found.
read_signal_for_todo() {
    local todo="$1"; shift
    local prefix f
    for prefix in "$@"; do
        f="$OUTBOX/${prefix}-${todo}.ready"
        if [[ -f "$f" ]]; then
            basename "$f" .ready
            return 0
        fi
    done
    return 1
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
# Commit helpers
###############################################################################

# Commit the current increment if the stage policy demands it. The orchestrator
# NEVER pushes — push is the supervisor's explicit call (see supervisor.md Step 8).
# Args: stage_name  todo_id  policy
# Returns 0 if a commit was created, 1 otherwise. All human-facing messages -> stderr.
maybe_commit_on_policy() {
    local stage_name="$1" todo_id="$2" policy="$3"
    if [[ "$policy" != "commit_and_next" && "$policy" != "commit_and_report" ]]; then
        return 1
    fi
    if ! git rev-parse --git-dir &>/dev/null; then
        echo "Not a git repo — skipping auto-commit for '${stage_name}'." >&2
        log "No git repo; auto-commit skipped at ${stage_name}"
        return 1
    fi
    git add -A 2>/dev/null || true
    if git diff --cached --quiet 2>/dev/null; then
        echo "No changes to auto-commit at '${stage_name}'." >&2
        log "Nothing to auto-commit at ${stage_name}"
        return 1
    fi
    local msg="awf(${stage_name}): ${todo_id}"
    if git commit -m "$msg" >/dev/null 2>&1; then
        local sha
        sha=$(git rev-parse --short HEAD)
        echo "Auto-committed: ${todo_id} at '${stage_name}' ($sha)." >&2
        echo "Remember to push: git push origin HEAD" >&2
        log "Auto-committed ${todo_id} at ${stage_name} ($sha)"
        return 0
    fi
    echo "WARNING: git commit failed at '${stage_name}'; leaving changes staged." >&2
    log "git auto-commit failed at ${stage_name}"
    return 1
}

# Detect whether the worker left changes relative to the baseline snapshot for a TODO.
# Used by the no-signal salvage path: returns 0 (true) if there's evidence the worker
# did work despite not writing DONE/BLOCKED. Args: todo_id
detect_work_evidence() {
    local todo_id="$1"
    local sha_file="$CONTEXT/BASELINE-${todo_id}.sha"
    [[ -f "$sha_file" ]] || return 1
    git rev-parse --git-dir &>/dev/null || return 1
    local base_sha
    base_sha=$(head -1 "$sha_file" 2>/dev/null)
    [[ "$base_sha" =~ ^[0-9a-f]{7,} ]] || return 1
    git cat-file -e "${base_sha}^{commit}" 2>/dev/null || return 1
    # Tracked changes (committed-since + staged + unstaged) vs baseline.
    ! git diff --quiet "$base_sha" 2>/dev/null && return 0
    # New untracked (non-ignored) files also count as work — `git diff <commit>`
    # does NOT see them, so without this a worker that only creates new files
    # would be falsely classified as "no work" and orphaned.
    local untracked
    untracked=$(git ls-files --others --exclude-standard 2>/dev/null)
    [[ -n "$untracked" ]]
}

# Auto-generate a DONE signal when the worker finished without writing one, BUT the
# project's structured verify commands (config.yaml: typecheck_cmd/build_cmd/test_cmd)
# all pass AND there is work evidence vs baseline. This is the fix for the recurring
# "worker exhausts turn budget before writing DONE" failure mode: instead of orphaning
# good completed work, trust the objective verify + work presence and synthesize DONE.
# Gated by config automation.auto_done (default "true"). Returns 0 if DONE synthesized.
attempt_auto_done() {
    local todo_id="$1"

    # Opt-out via config: automation.auto_done == "false".
    local enabled
    enabled=$(yaml_get "automation.auto_done" "$CONFIG" "true")
    enabled="${enabled,,}"   # lowercase (yaml_get returns Python's "True"/"False")
    [[ "$enabled" == "true" || "$enabled" == "1" ]] || return 1

    # Must have actual work to confirm.
    detect_work_evidence "$todo_id" || { log "auto-DONE skip: no work evidence for $todo_id"; return 1; }

    # Collect structured verify commands from config.
    local cmds=()
    local tc bc tst cmd
    tc=$(yaml_get "verification.typecheck_cmd" "$CONFIG" "");  [[ -n "$tc" ]] && cmds+=("$tc")
    bc=$(yaml_get "verification.build_cmd"     "$CONFIG" "");  [[ -n "$bc" ]] && cmds+=("$bc")
    tst=$(yaml_get "verification.test_cmd"     "$CONFIG" "");  [[ -n "$tst" ]] && cmds+=("$tst")
    [[ ${#cmds[@]} -gt 0 ]] || { log "auto-DONE skip: no verify commands configured"; return 1; }

    # Every configured verify command must pass.
    for cmd in "${cmds[@]}"; do
        if ! bash -c "$cmd" >/dev/null 2>&1; then
            log "auto-DONE skip: verify failed for $todo_id — $cmd"
            echo "  auto-DONE: verify command failed: $cmd" >&2
            return 1
        fi
    done

    # Verify passed + work present -> synthesize DONE.
    cat > "$OUTBOX/DONE-${todo_id}.md" <<EOF
# DONE-${todo_id} (AUTO-GENERATED by orchestrator)

The worker finished the stage but did not write a DONE signal (most likely turn-budget
exhaustion before the report-writing step). The orchestrator synthesized this DONE
because ALL configured verify commands (typecheck/build/test) passed AND git changes
were detected vs the baseline. Supervisor review is still recommended; the worker's
own DONE report (design notes, decisions) is absent.
EOF
    touch "$OUTBOX/DONE-${todo_id}.ready"
    log "auto-DONE synthesized for $todo_id (verify passed + work present)"
    echo "  auto-DONE: verify passed + work detected -> synthesized DONE-${todo_id}" >&2
    return 0
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

            # After verify/finalize: the supervisor approved (by continuing past the
            # stage). Honour the stage's on_approved policy — commit the increment if
            # it says commit_and_next / commit_and_report. Push stays manual (Step 8).
            if [[ "$s_action" == "verify_result" || "$s_action" == "final_verify" ]]; then
                echo "Supervisor verification complete."
                maybe_commit_on_policy "$s_name" "$CURRENT_TODO" \
                    "${STAGE_ON_APPROVED[$stage_idx]:-next}" || true
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

        # Only accept signals this action is allowed to emit. Stale signals
        # from earlier stages (e.g. worker's DONE during a review stage) are
        # ignored — see expected_signal_prefixes().
        local STAGE_PREFIXES
        # shellcheck disable=SC2207
        STAGE_PREFIXES=($(expected_signal_prefixes "$s_action"))

        # opencode run is blocking; the .ready signal should already exist.
        local SIGNAL=""
        SIGNAL=$(read_signal_for_todo "$CURRENT_TODO" "${STAGE_PREFIXES[@]}" || true)

        if [[ -z "$SIGNAL" ]]; then
            # Brief fallback poll in case the signal write lags behind process exit.
            SIGNAL=$(TIMEOUT=30 wait_for_signal "$CURRENT_TODO" "${STAGE_PREFIXES[@]}") || true
        fi

        if [[ -z "$SIGNAL" ]]; then
            # No signal: the worker finished/crashed without writing DONE/BLOCKED.
            # First try to AUTO-generate DONE from structured verify (config) + work
            # evidence. This handles the common case where the worker finished the code
            # but exhausted its turn budget before writing DONE — no salvage needed.
            if attempt_auto_done "$CURRENT_TODO"; then
                SIGNAL=$(read_signal_for_todo "$CURRENT_TODO" "${STAGE_PREFIXES[@]}" || true)
            fi
        fi

        if [[ -z "$SIGNAL" ]]; then
            # Still no signal. Don't hard-stop and orphan potentially-completed work — escalate.
            echo "WARNING: No signal after agent stage '$s_name' (worker ran but didn't signal)." >&2
            log "No signal after $s_name — salvage path"
            if [[ $AUTO -eq 1 ]]; then
                if detect_work_evidence "$CURRENT_TODO"; then
                    echo "  Worker left changes vs baseline. TODO $CURRENT_TODO left ACTIVE for manual salvage." >&2
                    echo "  Inspect: git diff ; awf status ; then write DONE-${CURRENT_TODO}.ready or replan." >&2
                    log "Auto: work detected; $CURRENT_TODO left active for manual salvage"
                else
                    echo "  No worker changes and no signal — treating as failure." >&2
                    log "Auto: no work + no signal — stop at $s_name"
                fi
                exit 1
            fi
            # Interactive: supervisor salvage stage — review git diff, write DONE or replan.
            run_supervisor_stage "salvage" "$CURRENT_TODO"
            SIGNAL=$(read_signal_for_todo "$CURRENT_TODO" "${STAGE_PREFIXES[@]}" || true)
            if [[ -z "$SIGNAL" ]]; then
                echo "No signal after supervisor salvage. Stopping." >&2
                log "Stopped: salvage produced no signal at $s_name"
                exit 1
            fi
            echo "Salvaged signal: $SIGNAL" >&2
            log "Salvaged signal: $SIGNAL via supervisor"
        fi

        local SIG_TYPE
        SIG_TYPE=$(signal_type "$SIGNAL")
        echo "Signal classified as: $SIG_TYPE"

        resolve_transition "$stage_idx" "$SIG_TYPE"

        case "$TRANSITION_ACTION" in
            next|commit_and_next|commit_and_report)
                # Worker never commits; the orchestrator does it when policy says so.
                maybe_commit_on_policy "$s_name" "$CURRENT_TODO" "$TRANSITION_ACTION" || true
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
# Guarded so the file can be sourced by tests (AWF_NO_DISPATCH=1) to exercise
# individual functions without triggering a pipeline run.
###############################################################################

if [[ "${AWF_NO_DISPATCH:-0}" != "1" ]]; then
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
fi
