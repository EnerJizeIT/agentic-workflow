#!/usr/bin/env bash
# tests/run.sh — bash test runner for awf internals.
# Zero dependencies beyond bash + the yaml backend (yq or python3+PyYAML).
#
# Usage: ./tests/run.sh
set -u

FRAMEWORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PASS=0
FAIL=0
FAILED_TESTS=()

# --- assertion helpers (no -e so failures get collected, not fatal) ---
assert_eq() {
    local exp="$1" act="$2" name="${3:-test}"
    if [[ "$exp" == "$act" ]]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILED_TESTS+=("$name")
        printf '  FAIL: %s\n    expected: <%s>\n    actual:   <%s>\n' \
            "$name" "$exp" "$act" >&2
    fi
}
assert_contains() {
    local hay="$1" needle="$2" name="${3:-test}"
    if [[ "$hay" == *"$needle"* ]]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILED_TESTS+=("$name")
        printf '  FAIL: %s\n    expected to contain: <%s>\n    in: <%s>\n' \
            "$name" "$needle" "$hay" >&2
    fi
}

echo "== awf test suite =="
echo "tmp: $TMP"
echo

###############################################################################
# Minimal project skeleton so orchestrator.sh can be sourced (it does some
# setup at load time: .agentic check, pipeline resolution).
###############################################################################
mkdir -p "$TMP/proj/.agentic/pipelines" \
         "$TMP/proj/.agentic/inbox" \
         "$TMP/proj/.agentic/outbox" \
         "$TMP/proj/.agentic/context" \
         "$TMP/proj/.agentic/logs"
cat > "$TMP/proj/.agentic/config.yaml" <<'YAML'
project:
  name: "test-project"
default_pipeline: "default"
phases:
  current: ".agentic/phases/plan.md"
verification:
  test_cmd: ""
models:
  worker:
    agent_name: "worker"
  reviewer:
    agent_name: "rev1"
YAML
cp "$FRAMEWORK_DIR/templates/pipelines/simple.yaml" "$TMP/proj/.agentic/pipelines/default.yaml"
cp "$FRAMEWORK_DIR/templates/pipelines/full.yaml"   "$TMP/proj/.agentic/pipelines/full.yaml"
cp "$FRAMEWORK_DIR/templates/pipelines/simple.yaml" "$TMP/proj/.agentic/pipelines/simple.yaml"

# shellcheck source=../lib/yaml.sh
source "$FRAMEWORK_DIR/lib/yaml.sh"

cd "$TMP/proj"

###############################################################################
# 1. yaml_get + yaml_stages_dump (lib/yaml.sh)
###############################################################################

CFG="$TMP/proj/.agentic/config.yaml"
assert_eq "test-project" "$(yaml_get project.name "$CFG")" "yaml_get.project.name"
assert_eq "default"       "$(yaml_get default_pipeline "$CFG")" "yaml_get.default_pipeline"
assert_eq ".agentic/phases/plan.md" "$(yaml_get phases.current "$CFG")" "yaml_get.phases.current"
assert_eq "FALLBACK" "$(yaml_get no.such.key "$CFG" FALLBACK)" "yaml_get.missing.returns.default"
assert_eq "worker"   "$(yaml_get models.worker.agent_name "$CFG" "?")" "yaml_get.nested.models.worker.agent_name"
assert_eq "rev1"     "$(yaml_get models.reviewer.agent_name "$CFG" "?")" "yaml_get.nested.models.reviewer.agent_name"

SIMPLE="$FRAMEWORK_DIR/templates/pipelines/simple.yaml"
FULL="$FRAMEWORK_DIR/templates/pipelines/full.yaml"
SIMPLE_ROWS=$(yaml_stages_dump "$SIMPLE" | wc -l | tr -d ' ')
FULL_ROWS=$(yaml_stages_dump "$FULL" | wc -l | tr -d ' ')
assert_eq "3" "$SIMPLE_ROWS" "yaml_stages_dump.simple.has.3.rows"
assert_eq "5" "$FULL_ROWS"   "yaml_stages_dump.full.has.5.rows"

# description with spaces survives intact (TSV field 4)
DESC_IMPLEMENT=$(yaml_stages_dump "$SIMPLE" | sed -n '2p' | cut -f4)
assert_eq "Worker executes the TODO" "$DESC_IMPLEMENT" "yaml_stages_dump.preserves.description.with.spaces"

# empty description stays empty, not mangled
DESC_PLAN_FULL=$(yaml_stages_dump "$FULL" | sed -n '1p' | cut -f4)
assert_eq "" "$DESC_PLAN_FULL" "yaml_stages_dump.empty.description"

###############################################################################
# 2. Source orchestrator.sh (functions only, no dispatch)
###############################################################################
# shellcheck disable=SC1090
AWF_NO_DISPATCH=1 source "$FRAMEWORK_DIR/lib/orchestrator.sh" start >/dev/null 2>&1
# orchestrator enables -euo pipefail on source; relax -e for test collection.
set +e

###############################################################################
# 3. parse_stages
###############################################################################
PIPELINE_FILE="$TMP/proj/.agentic/pipelines/simple.yaml"
parse_stages
assert_eq "3" "${#STAGE_NAMES[@]}" "parse_stages.simple.count"
assert_eq "plan"      "${STAGE_NAMES[0]}"   "parse_stages.simple.name[0]"
assert_eq "implement" "${STAGE_NAMES[1]}"   "parse_stages.simple.name[1]"
assert_eq "verify"    "${STAGE_NAMES[2]}"   "parse_stages.simple.name[2]"
assert_eq "supervisor" "${STAGE_ROLES[0]}"  "parse_stages.simple.role[0]=supervisor"
assert_eq "worker"     "${STAGE_ROLES[1]}"  "parse_stages.simple.role[1]=worker"
assert_eq "execute_todo"   "${STAGE_ACTIONS[1]}"  "parse_stages.simple.action[1]=execute_todo"
assert_eq "verify_result"  "${STAGE_ACTIONS[2]}"  "parse_stages.simple.action[2]=verify_result"
assert_eq "commit_and_next" "${STAGE_ON_APPROVED[2]}" "parse_stages.simple.on_approved[2]=commit_and_next"
assert_eq "replan"          "${STAGE_ON_REJECTED[2]}" "parse_stages.simple.on_rejected[2]=replan"
assert_eq "3" "${STAGE_MAX_RETRIES[1]}" "parse_stages.simple.max_retries[1]=3"

PIPELINE_FILE="$TMP/proj/.agentic/pipelines/full.yaml"
parse_stages
assert_eq "5" "${#STAGE_NAMES[@]}" "parse_stages.full.count"
assert_eq "review"   "${STAGE_NAMES[2]}"  "parse_stages.full.name[2]=review"
assert_eq "reviewer" "${STAGE_ROLES[2]}"  "parse_stages.full.role[2]=reviewer"
assert_eq "review_code" "${STAGE_ACTIONS[2]}" "parse_stages.full.action[2]=review_code"
assert_eq "tester"    "${STAGE_ROLES[3]}"  "parse_stages.full.role[3]=tester"
assert_eq "run_tests" "${STAGE_ACTIONS[3]}" "parse_stages.full.action[3]=run_tests"
assert_eq "rollback_to:implement" "${STAGE_ON_REJECTED[2]}" "parse_stages.full.on_rejected[2]"
assert_eq "2" "${STAGE_MAX_RETRIES[2]}" "parse_stages.full.max_retries[2]=2"

# reset to simple for subsequent tests
PIPELINE_FILE="$TMP/proj/.agentic/pipelines/simple.yaml"
parse_stages

###############################################################################
# 4. get_agent_name (config-driven)
###############################################################################
assert_eq "worker" "$(get_agent_name worker)"    "get_agent_name.worker"
assert_eq "rev1"   "$(get_agent_name reviewer)"  "get_agent_name.reviewer.uses.config"
# role without config entry falls back to role name
assert_eq "ghost"  "$(get_agent_name ghost)"     "get_agent_name.unknown.falls.back"

###############################################################################
# 5. signal_type
###############################################################################
assert_eq "done"     "$(signal_type DONE-TODO-0001.ready)"      "signal_type.done"
assert_eq "blocked"  "$(signal_type BLOCKED-TODO-0001.ready)"   "signal_type.blocked"
assert_eq "approved" "$(signal_type REVIEW-APPROVED-TODO-0001.ready)"  "signal_type.approved"
assert_eq "rejected" "$(signal_type REVIEW-REJECTED-TODO-0001.ready)"  "signal_type.rejected"
assert_eq "passed"   "$(signal_type TEST-PASSED-TODO-0001.ready)"      "signal_type.passed"
assert_eq "failed"   "$(signal_type TEST-FAILED-TODO-0001.ready)"      "signal_type.failed"
assert_eq "unknown"  "$(signal_type WEIRD-TODO-0001.ready)"    "signal_type.unknown"

###############################################################################
# 6. expected_signal_prefixes
###############################################################################
assert_eq "DONE BLOCKED" "$(expected_signal_prefixes execute_todo)" "prefixes.execute_todo"
assert_contains "$(expected_signal_prefixes review_code)" "REVIEW-APPROVED" "prefixes.review_code.has.approved"
assert_contains "$(expected_signal_prefixes review_code)" "REVIEW-REJECTED" "prefixes.review_code.has.rejected"
assert_contains "$(expected_signal_prefixes run_tests)" "TEST-PASSED" "prefixes.run_tests.has.passed"
assert_contains "$(expected_signal_prefixes run_tests)" "TEST-FAILED" "prefixes.run_tests.has.failed"

###############################################################################
# 7. read_signal_for_todo — prefix filtering (the stale-signal bug fix)
###############################################################################
# Simulate: worker already produced DONE-TODO-0001 in a prior stage. The review
# stage must NOT pick up that stale DONE when polling for REVIEW-* signals.
OUTBOX="$TMP/proj/.agentic/outbox"
mkdir -p "$OUTBOX"
touch "$OUTBOX/DONE-TODO-0001.ready"
touch "$OUTBOX/DONE-TODO-0001.md"

# (a) execute_todo polling for DONE/BLOCKED -> finds DONE (correct for its stage)
assert_eq "DONE-TODO-0001" \
    "$(read_signal_for_todo TODO-0001 DONE BLOCKED)" \
    "read_signal.execute_todo.sees.DONE"

# (b) review_code polling for REVIEW-*/BLOCKED -> MUST ignore stale DONE
REVIEW_RESULT=$(read_signal_for_todo TODO-0001 REVIEW-APPROVED REVIEW-REJECTED BLOCKED || true)
assert_eq "" "$REVIEW_RESULT" "read_signal.review.ignores.stale.DONE [BUG FIX]"

# (c) once reviewer writes its signal, it is found
touch "$OUTBOX/REVIEW-APPROVED-TODO-0001.ready"
assert_eq "REVIEW-APPROVED-TODO-0001" \
    "$(read_signal_for_todo TODO-0001 REVIEW-APPROVED REVIEW-REJECTED BLOCKED)" \
    "read_signal.review.sees.own.signal"

###############################################################################
# 8. clean_stage_signals — only removes the action's own signals
###############################################################################
clean_stage_signals TODO-0001 REVIEW-APPROVED REVIEW-REJECTED BLOCKED
assert_eq "no" "$([[ -e "$OUTBOX/REVIEW-APPROVED-TODO-0001.ready" ]] && echo yes || echo no)" "clean.removes.review.ready"
assert_eq "yes" "$([[ -e "$OUTBOX/DONE-TODO-0001.ready" ]] && echo yes || echo no)" "clean.preserves.DONE.from.earlier.stage"

###############################################################################
# 9. resolve_transition — stage policy -> action mapping
###############################################################################
# next on approved
STAGE_ON_APPROVED=("next"); STAGE_ON_REJECTED=("rollback_to:implement")
STAGE_ON_BLOCKED=("escalate"); STAGE_ON_PASSED=("next"); STAGE_ON_FAILED=("rollback_to:implement")
resolve_transition 0 done
assert_eq "next" "$TRANSITION_ACTION" "transition.done.next"
assert_eq "" "$TRANSITION_TARGET" "transition.done.next.no.target"

# escalate on blocked
resolve_transition 0 blocked
assert_eq "escalate" "$TRANSITION_ACTION" "transition.blocked.escalate"

# rollback_to on rejected
resolve_transition 0 rejected
assert_eq "rollback" "$TRANSITION_ACTION" "transition.rejected.rollback"
assert_eq "implement" "$TRANSITION_TARGET" "transition.rejected.rollback.target"

# commit_and_next on approved
STAGE_ON_APPROVED=("commit_and_next")
resolve_transition 0 done
assert_eq "commit_and_next" "$TRANSITION_ACTION" "transition.done.commit_and_next"

# stop policy on blocked
STAGE_ON_BLOCKED=("stop")
resolve_transition 0 blocked
assert_eq "stop" "$TRANSITION_ACTION" "transition.blocked.stop"

###############################################################################
# 10. max_retries / should_retry
###############################################################################
STAGE_MAX_RETRIES=("2")
RETRY_COUNTS=(0)
assert_eq "yes" "$(should_retry 0 && echo yes || echo no)" "should_retry.under.max"
RETRY_COUNTS=(2)
assert_eq "no" "$(should_retry 0 && echo yes || echo no)" "should_retry.at.max"

###############################################################################
# 11. maybe_commit_on_policy — orchestrator auto-commit (needs a real git repo)
###############################################################################
GITREPO="$TMP/commit-repo"
mkdir -p "$GITREPO"
cd "$GITREPO"
git init -q
git config user.email t@t.t
git config user.name t
printf '.agentic/\n' > .gitignore
echo "hello" > file.txt
git add -A && git commit -qm "init"
HEAD0=$(git rev-parse HEAD)

# policy "next" -> NO commit, HEAD unchanged
echo "change1" >> file.txt
assert_eq "no-commit" "$(maybe_commit_on_policy verify TODO-1 next >/dev/null 2>&1 && echo committed || echo no-commit)" \
    "commit.policy.next.does.not.commit"
assert_eq "$HEAD0" "$(git rev-parse HEAD)" "commit.policy.next.head.unchanged"

# policy "commit_and_next" + changes -> commits, HEAD advances
assert_eq "committed" "$(maybe_commit_on_policy verify TODO-1 commit_and_next >/dev/null 2>&1 && echo committed || echo no-commit)" \
    "commit.policy.commit_and_next.commits"
HEAD1=$(git rev-parse HEAD)
assert_eq "moved" "$([[ "$HEAD0" != "$HEAD1" ]] && echo moved || echo same)" \
    "commit.policy.commit_and_next.head.moved"
# the increment's source change landed in the commit
assert_eq "yes" "$(git cat-file -p HEAD:file.txt >/dev/null 2>&1 && echo yes || echo no)" \
    "commit.includes.file.txt"
assert_eq "hello
change1" "$(git cat-file -p HEAD:file.txt 2>/dev/null)" "commit.content.correct"

# policy "commit_and_next" + NO changes -> no commit, HEAD unchanged
assert_eq "no-commit" "$(maybe_commit_on_policy verify TODO-1 commit_and_next >/dev/null 2>&1 && echo committed || echo no-commit)" \
    "commit.policy.nothing.to.commit"
assert_eq "$HEAD1" "$(git rev-parse HEAD)" "commit.policy.no.changes.head.unchanged"

# not a git repo -> no commit
cd "$TMP"
mkdir -p notgit && cd notgit
echo "x" > f.txt
assert_eq "no-commit" "$(maybe_commit_on_policy verify TODO-1 commit_and_next >/dev/null 2>&1 && echo committed || echo no-commit)" \
    "commit.nongit.no.commit"

cd "$TMP/proj"   # restore cwd

###############################################################################
# 12. wait_for_signal — stdout carries ONLY the signal name (regression)
#     The orphan bug was caused by diagnostics polluting the captured stdout.
###############################################################################
OUTBOX="$TMP/proj/.agentic/outbox"
mkdir -p "$OUTBOX"
rm -f "$OUTBOX"/DONE-TODO-WFS.ready
touch "$OUTBOX"/DONE-TODO-WFS.ready
WFS_OUT=$(wait_for_signal TODO-WFS DONE BLOCKED 2>"$TMP/wfs.err")
assert_eq "DONE-TODO-WFS" "$WFS_OUT" "wait_for_signal.stdout.is.signal.only [REGRESSION]"
assert_contains "$(cat "$TMP/wfs.err")" "Waiting for agent signal" "wait_for_signal.diagnostics.on.stderr"
assert_contains "$(cat "$TMP/wfs.err")" "Signal received" "wait_for_signal.received.msg.on.stderr"
rm -f "$OUTBOX"/DONE-TODO-WFS.ready

###############################################################################
# 13. detect_work_evidence — orphan salvage heuristic
###############################################################################
cd "$GITREPO"
CONTEXT="$GITREPO/.agentic/context"
mkdir -p "$CONTEXT"
DET_BASE=$(git rev-parse HEAD)
echo "$DET_BASE" > "$CONTEXT/BASELINE-TODO-DET.sha"
# clean tree relative to baseline -> no evidence
assert_eq "no" "$(detect_work_evidence TODO-DET >/dev/null 2>&1 && echo yes || echo no)" "evidence.clean.no"
# uncommitted change vs baseline -> evidence present
echo "more" >> file.txt
assert_eq "yes" "$(detect_work_evidence TODO-DET >/dev/null 2>&1 && echo yes || echo no)" "evidence.changed.yes"
# NEW untracked file must count as work even with a clean tracked tree.
# (Bug case: a worker that only creates new files was falsely seen as "no work"
#  because git diff <commit> ignores untracked files.)
git add -A && git commit -qm "chg" >/dev/null
echo "$(git rev-parse HEAD)" > "$CONTEXT/BASELINE-TODO-UNTR.sha"
echo "docs body" > NEW-DOC.md
assert_eq "yes" "$(detect_work_evidence TODO-UNTR >/dev/null 2>&1 && echo yes || echo no)" "evidence.untracked.only.yes [BUG FIX]"
rm -f NEW-DOC.md
# missing baseline file -> no evidence
assert_eq "no" "$(detect_work_evidence TODO-NOSUCHSHA >/dev/null 2>&1 && echo yes || echo no)" "evidence.no.baseline.no"
cd "$TMP/proj"

###############################################################################
# 14. attempt_auto_done — synthesize DONE when verify passes + work present
###############################################################################
AUTODIR="$TMP/auto-repo"
mkdir -p "$AUTODIR/.agentic/inbox" "$AUTODIR/.agentic/outbox" "$AUTODIR/.agentic/context"
cd "$AUTODIR"
git init -q; git config user.email t@t.t; git config user.name t
printf '.agentic/\n' > .gitignore
echo "src" > file.txt
git add -A && git commit -qm init
# point awf globals at this repo
CONFIG="$AUTODIR/.agentic/config.yaml"
CONTEXT="$AUTODIR/.agentic/context"
INBOX="$AUTODIR/.agentic/inbox"
OUTBOX="$AUTODIR/.agentic/outbox"
AUTO_BASE=$(git rev-parse HEAD)
echo "$AUTO_BASE" > "$CONTEXT/BASELINE-TODO-AUTO.sha"

write_cfg() {  # $1 = typecheck_cmd, $2 = auto_done
  cat > "$CONFIG" <<YAML
verification:
  typecheck_cmd: "$1"
automation:
  auto_done: $2
YAML
}

# (a) verify passes + work present -> DONE synthesized
write_cfg "test -f file.txt" true
echo "change" >> file.txt
rm -f "$OUTBOX/DONE-TODO-AUTO.ready"
assert_eq "yes" "$(attempt_auto_done TODO-AUTO >/dev/null 2>&1 && echo yes || echo no)" "autodone.verify.pass.work.present"
assert_eq "yes" "$([[ -f "$OUTBOX/DONE-TODO-AUTO.ready" ]] && echo yes || echo no)" "autodone.writes.DONE.ready"

# (b) verify FAILS -> no DONE
git add -A && git commit -qm chg >/dev/null; echo "$(git rev-parse HEAD)" > "$CONTEXT/BASELINE-TODO-AUTO2.sha"
write_cfg "false" true
echo "more" >> file.txt
rm -f "$OUTBOX/DONE-TODO-AUTO2.ready"
assert_eq "no" "$(attempt_auto_done TODO-AUTO2 >/dev/null 2>&1 && echo yes || echo no)" "autodone.verify.fail.no.DONE"
assert_eq "no" "$([[ -f "$OUTBOX/DONE-TODO-AUTO2.ready" ]] && echo yes || echo no)" "autodone.no.DONE.on.fail"

# (c) verify passes but NO work -> no DONE
git add -A && git commit -qm chg2 >/dev/null; echo "$(git rev-parse HEAD)" > "$CONTEXT/BASELINE-TODO-AUTO3.sha"
write_cfg "true" true
# (clean tree, no changes vs baseline)
rm -f "$OUTBOX/DONE-TODO-AUTO3.ready"
assert_eq "no" "$(attempt_auto_done TODO-AUTO3 >/dev/null 2>&1 && echo yes || echo no)" "autodone.no.work.no.DONE"

# (d) auto_done disabled -> no DONE even if verify+work
echo "$(git rev-parse HEAD)" > "$CONTEXT/BASELINE-TODO-AUTO4.sha"
write_cfg "true" false
echo "x" >> file.txt
rm -f "$OUTBOX/DONE-TODO-AUTO4.ready"
assert_eq "no" "$(attempt_auto_done TODO-AUTO4 >/dev/null 2>&1 && echo yes || echo no)" "autodone.disabled.no.DONE"

# (e) no verify commands configured -> no DONE
echo "y" >> file.txt
printf 'automation:\n  auto_done: true\n' > "$CONFIG"
rm -f "$OUTBOX/DONE-TODO-AUTO5.ready"
assert_eq "no" "$(attempt_auto_done TODO-AUTO5 >/dev/null 2>&1 && echo yes || echo no)" "autodone.no.verify.cmds.no.DONE"

cd "$TMP/proj"

###############################################################################
# 15. signal-naming tolerance — canonical DONE-TODO-{NNNN} AND legacy DONE-{NNNN}
#     (without this, a worker writing the short form is invisible -> re-run)
###############################################################################
INBOX="$TMP/proj/.agentic/inbox"
OUTBOX="$TMP/proj/.agentic/outbox"
mkdir -p "$INBOX" "$OUTBOX"
# canonical form: DONE-TODO-0001 found when polling for TODO-0001
rm -f "$OUTBOX"/DONE-TODO-0001.ready "$OUTBOX"/DONE-0002.ready
touch "$OUTBOX/DONE-TODO-0001.ready"
assert_eq "DONE-TODO-0001" "$(read_signal_for_todo TODO-0001 DONE BLOCKED)" "signal.canonical.form.found"
rm -f "$OUTBOX/DONE-TODO-0001.ready"
# legacy short form: DONE-0002 (no TODO- prefix) must ALSO be found for TODO-0002
touch "$OUTBOX/DONE-0002.ready"
assert_eq "DONE-0002" "$(read_signal_for_todo TODO-0002 DONE BLOCKED)" "signal.short.form.found [BUG FIX]"
rm -f "$OUTBOX/DONE-0002.ready"

# find_active_todo must treat a TODO closed by EITHER form as done (not re-run it)
rm -f "$INBOX"/TODO-0099.* "$OUTBOX"/DONE-0099.ready "$OUTBOX"/DONE-TODO-0099.ready
printf 'x' > "$INBOX/TODO-0099.md"; touch "$INBOX/TODO-0099.ready"
touch "$OUTBOX/DONE-0099.ready"   # legacy short closure
assert_eq "" "$(find_active_todo)" "find_active_todo.skips.short.closed [BUG FIX]"
rm -f "$INBOX"/TODO-0099.* "$OUTBOX"/DONE-0099.ready

cd "$TMP/proj"

###############################################################################
# 16. find_active_todo picks HIGHEST-numbered active TODO (not lowest)
#     Bug case: supervisor created TODO-0002 while TODO-0001 was still active
#     (mid-salvage / after replan). Old code returned TODO-0001 (lowest via
#     lexical `sort`), silently re-running the stale one.
###############################################################################
INBOX="$TMP/proj/.agentic/inbox"
OUTBOX="$TMP/proj/.agentic/outbox"
mkdir -p "$INBOX" "$OUTBOX"
# Clean slate for this section
rm -f "$INBOX"/TODO-* "$OUTBOX"/DONE-* "$OUTBOX"/BLOCKED-* "$OUTBOX"/PROGRESS-*
printf 'a' > "$INBOX/TODO-0001.md"; touch "$INBOX/TODO-0001.ready"
printf 'b' > "$INBOX/TODO-0002.md"; touch "$INBOX/TODO-0002.ready"
assert_eq "TODO-0002" "$(find_active_todo)" "find_active_todo.picks.highest.NNNN [BUG FIX]"

# Numeric, not lexical: TODO-0010 must beat TODO-0009 (lexical would reverse them)
rm -f "$INBOX"/TODO-0002.md "$INBOX"/TODO-0002.ready
printf 'c' > "$INBOX/TODO-0009.md"; touch "$INBOX/TODO-0009.ready"
printf 'd' > "$INBOX/TODO-0010.md"; touch "$INBOX/TODO-0010.ready"
assert_eq "TODO-0010" "$(find_active_todo)" "find_active_todo.numeric.sort.10.beats.9 [BUG FIX]"

# Closed TODO does not get picked even if its NNNN is higher
touch "$OUTBOX/DONE-TODO-0010.ready"
assert_eq "TODO-0009" "$(find_active_todo)" "find_active_todo.skips.closed.even.if.highest"
rm -f "$OUTBOX/DONE-TODO-0010.ready"

# Empty .md is treated as not-yet-prepared (matches the old behaviour)
rm -f "$INBOX"/TODO-0009.md "$INBOX"/TODO-0009.ready "$INBOX"/TODO-0010.md "$INBOX"/TODO-0010.ready
: > "$INBOX/TODO-0005.md"; touch "$INBOX/TODO-0005.ready"   # zero-size .md
printf 'e' > "$INBOX/TODO-0006.md"; touch "$INBOX/TODO-0006.ready"
assert_eq "TODO-0006" "$(find_active_todo)" "find_active_todo.ignores.empty.md"

###############################################################################
# 17. list_active_todos — returns ALL active, highest first
###############################################################################
rm -f "$INBOX"/TODO-* "$OUTBOX"/DONE-* "$OUTBOX"/BLOCKED-* "$OUTBOX"/PROGRESS-*
printf 'x' > "$INBOX/TODO-0001.md"; touch "$INBOX/TODO-0001.ready"
printf 'x' > "$INBOX/TODO-0003.md"; touch "$INBOX/TODO-0003.ready"
printf 'x' > "$INBOX/TODO-0007.md"; touch "$INBOX/TODO-0007.ready"
# 0002 closed (canonical DONE), 0005 closed (legacy BLOCKED) -> neither appears
touch "$INBOX/TODO-0002.md"; touch "$INBOX/TODO-0002.ready"
touch "$OUTBOX/DONE-TODO-0002.ready"
touch "$INBOX/TODO-0005.md"; touch "$INBOX/TODO-0005.ready"
touch "$OUTBOX/BLOCKED-0005.ready"
LIST_OUT=$(list_active_todos)
assert_eq "3" "$(printf '%s\n' "$LIST_OUT" | grep -c . || echo 0)" "list_active_todos.count.excludes.closed"
assert_eq "TODO-0007" "$(printf '%s\n' "$LIST_OUT" | head -1)" "list_active_todos.first.is.highest"
assert_eq "TODO-0001" "$(printf '%s\n' "$LIST_OUT" | tail -1)" "list_active_todos.last.is.lowest"
# Empty inbox -> empty output, not an error
rm -f "$INBOX"/TODO-*
assert_eq "" "$(list_active_todos)" "list_active_todos.empty.inbox"

###############################################################################
# 18. todo_has_progress — distinguishes dispatched vs never-dispatched TODO
###############################################################################
rm -f "$OUTBOX"/PROGRESS-* "$OUTBOX"/DONE-* "$OUTBOX"/BLOCKED-*
assert_eq "no" "$(todo_has_progress TODO-0001 && echo yes || echo no)" "has_progress.no.file"
printf 'stub' > "$OUTBOX/PROGRESS-TODO-0001.md"
assert_eq "yes" "$(todo_has_progress TODO-0001 && echo yes || echo no)" "has_progress.canonical.form"
rm -f "$OUTBOX/PROGRESS-TODO-0001.md"
printf 'stub' > "$OUTBOX/PROGRESS-0042.md"
assert_eq "yes" "$(todo_has_progress TODO-0042 && echo yes || echo no)" "has_progress.legacy.short.form"
rm -f "$OUTBOX/PROGRESS-0042.md"

###############################################################################
# 19. awf reset --orphans — clears never-dispatched TODOs, preserves in-flight
###############################################################################
ORPHAN_DIR="$TMP/orphan-test"
rm -rf "$ORPHAN_DIR"
mkdir -p "$ORPHAN_DIR/.agentic/inbox" "$ORPHAN_DIR/.agentic/outbox"
cd "$ORPHAN_DIR"
# TODO-0001: orphan — has .ready + .md, no progress, no closure
printf 'x' > .agentic/inbox/TODO-0001.md; touch .agentic/inbox/TODO-0001.ready
# TODO-0002: in-flight — has progress
printf 'x' > .agentic/inbox/TODO-0002.md; touch .agentic/inbox/TODO-0002.ready
printf 'stub' > .agentic/outbox/PROGRESS-TODO-0002.md
# TODO-0003: closed — must NOT be touched by --orphans
printf 'x' > .agentic/inbox/TODO-0003.md; touch .agentic/inbox/TODO-0003.ready
touch .agentic/outbox/DONE-TODO-0003.ready

# reset.sh sources todos.sh with INBOX/OUTBOX relative to cwd
INBOX="$ORPHAN_DIR/.agentic/inbox" OUTBOX="$ORPHAN_DIR/.agentic/outbox" \
    bash "$FRAMEWORK_DIR/lib/reset.sh" --orphans --force >/dev/null 2>&1

assert_eq "no" "$([[ -e .agentic/inbox/TODO-0001.ready ]] && echo yes || echo no)" "orphans.removes.orphan.ready"
assert_eq "no" "$([[ -e .agentic/inbox/TODO-0001.md ]] && echo yes || echo no)"    "orphans.removes.orphan.md"
assert_eq "yes" "$([[ -e .agentic/inbox/TODO-0002.ready ]] && echo yes || echo no)" "orphans.preserves.in.flight.ready"
assert_eq "yes" "$([[ -e .agentic/inbox/TODO-0002.md ]] && echo yes || echo no)"    "orphans.preserves.in.flight.md"
assert_eq "yes" "$([[ -e .agentic/inbox/TODO-0003.ready ]] && echo yes || echo no)" "orphans.preserves.closed.ready"
assert_eq "yes" "$([[ -e .agentic/outbox/DONE-TODO-0003.ready ]] && echo yes || echo no)" "orphans.preserves.closed.outbox"

# No orphans → exit 0, no removal
rm -rf "$ORPHAN_DIR"
mkdir -p "$ORPHAN_DIR/.agentic/inbox" "$ORPHAN_DIR/.agentic/outbox"
cd "$ORPHAN_DIR"
printf 'x' > .agentic/inbox/TODO-0050.md; touch .agentic/inbox/TODO-0050.ready
printf 'stub' > .agentic/outbox/PROGRESS-TODO-0050.md
INBOX="$ORPHAN_DIR/.agentic/inbox" OUTBOX="$ORPHAN_DIR/.agentic/outbox" \
    bash "$FRAMEWORK_DIR/lib/reset.sh" --orphans --force >/dev/null 2>&1
assert_eq "yes" "$([[ -e .agentic/inbox/TODO-0050.ready ]] && echo yes || echo no)" "orphans.no.orphans.no.removal"

cd "$TMP/proj"

###############################################################################
# Summary
###############################################################################
echo
echo "----------------------------------------------------------------"
TOTAL=$((PASS + FAIL))
echo "passed: $PASS / $TOTAL"
if [[ $FAIL -gt 0 ]]; then
    echo "FAILED ($FAIL):"
    for t in "${FAILED_TESTS[@]}"; do echo "  - $t"; done
    echo "----------------------------------------------------------------"
    exit 1
fi
echo "----------------------------------------------------------------"
echo "All tests passed."
