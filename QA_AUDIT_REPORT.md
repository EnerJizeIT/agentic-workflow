# QA Audit Report — agentic-workflow

**Date:** 2026-07-31
**Scope:** `awf/` (orchestrator, 32 modules, ~3848 LOC) + `agent_workflow_ui/` (MCP plugin, 5 tools)
**Tests:** 628 passed (README claims 510 — outdated)

---

## Summary

| Severity | Count | Description |
|---|---|---|
| CRITICAL | 1 | Auto-DONE race condition — `.ready` before `.md` |
| HIGH | 3 | Signal parsing edge case, rollback silent failure, HTTP double-submit |
| MEDIUM | 6 | Various state machine, config, and UI issues |
| LOW | 4 | Documentation, minor robustness |

---

## CRITICAL

### C-1: Auto-DONE writes `.ready` before `.md` — race condition

**File:** `awf/verify.py:86-97`

```python
done_md = outbox / f"DONE-{todo_id}.md"
done_md.write_text(...)              # line 87: writes .md
(outbox / f"DONE-{todo_id}.ready").touch()  # line 97: creates .ready
```

**Problem:** Between `write_text()` and `.touch()`, the orchestrator's `read_signal_for_todo()` (signals.py:82-83) checks:
```python
if md_file.exists() and md_file.stat().st_size == 0:
    continue  # treat as not-ready
```

If the `.md` write is still buffered/flushing when `.ready` appears, and another polling thread (or the orchestrator's own `wait_for_signal` at orchestrator.py:332) reads the signal, the `.md` file could appear empty or partially written. The signal will be accepted (`.md` exists and is non-empty), BUT the content could be truncated.

**Worse:** if `write_text()` fails silently (disk full, permission error), `.touch()` still runs, creating a `.ready` without a valid `.md`. The orchestrator will see the signal, classify it as "done", and proceed — with a missing or corrupt DONE report.

**Fix:** Write `.md` first, verify it's non-empty, THEN create `.ready`. Or use atomic rename pattern like `_atomic_write_text` from `state.py`.

**Severity:** CRITICAL — silent data corruption in auto-DONE reports.

---

## HIGH

### H-1: `read_signal_for_todo` returns "unknown" type for `.md.ready` signals

**File:** `awf/signals.py:89`

```python
return sig_file.stem.rsplit(".md", 1)[0] if sig_file.stem.endswith(".md") else sig_file.stem
```

For `DONE-TODO-0001.md.ready`, `stem` = `DONE-TODO-0001.md` → rsplit → `DONE-TODO-0001`. Correct.

BUT for `REVIEW-APPROVED-TODO-0001.md.ready`, `stem` = `REVIEW-APPROVED-TODO-0001.md` → rsplit → `REVIEW-APPROVED-TODO-0001`. Correct.

However, `signal_type()` at line 8-22 expects the filename WITHOUT the `.md` suffix. The `rsplit(".md", 1)[0]` correctly strips it. **This is actually correct.** Let me re-check...

Actually, the code is correct. Moving to MEDIUM.

### H-1 (revised): Rollback silently continues when supervisor doesn't create new TODO

**File:** `awf/orchestrator.py:196-200`

```python
def _handle_rollback(...):
    ...
    _run_supervisor_stage(replan_stage, current_todo, auto, project_dir, logs_dir)
    new_todo = _find_active_todo(project_dir)
    if new_todo:
        new_todo = new_todo  # no-op
    return target_idx, new_todo, 0  # ← new_todo can be ""!
```

**Problem:** Unlike `_handle_escalate()` (line 168-170) which returns `exit_code=1` when no new TODO is found, `_handle_rollback()` returns `exit_code=0` even when `new_todo == ""`. The pipeline will then proceed to the target stage with an empty `current_todo`, causing the agent stage to fail with "No active TODO for agent stage" — but the error message is misleading because the real issue was the supervisor's failure to create a TODO during rollback.

**Fix:** Add the same guard as `_handle_escalate`:
```python
if not new_todo:
    print("Supervisor did not create a new TODO during rollback. Stopping.")
    return -1, current_todo, 1
```

**Severity:** HIGH — confusing error messages, wasted compute.

### H-2: HTTP endpoint double-submit vulnerability

**File:** `agent_workflow_ui/src/agent_workflow_ui/http_endpoint.py:99-105`

```python
if record.status == "submitted":
    self._send_html(200, _ack_page(form_id, already_submitted=True))
    return

if record.status == "cancelled":
    self._send_text(410, f"Form {form_id} was cancelled.")
    return
```

**Problem:** There's a TOCTOU (Time-of-Check-Time-of-Use) race between checking `record.status` and calling `self.registry.update_status(form_id, "submitted")` at line 163. Two concurrent POST requests could both pass the `status == "submitted"` check (since neither has updated yet), both process the form, and both write to the same YAML file. The second `_atomic_write_yaml` would silently overwrite the first.

**Mitigation:** The `FormRegistry` uses a threading lock for `update_status`, but the check (`record.status == "submitted"`) and the update (`update_status`) are NOT atomic — they're two separate operations outside the lock.

**Fix:** Move the status check INTO the locked `update_status` method, or acquire the lock before checking.

**Severity:** HIGH — double form submission could overwrite pipeline config.

### H-3: `run_verify_commands` silently returns False on `FileNotFoundError`

**File:** `awf/verify.py:46-51`

```python
try:
    result = subprocess.run(parts, capture_output=True, check=False, cwd=cwd)
except (FileNotFoundError, OSError):
    return False  # ← silent failure
if result.returncode != 0:
    return False
```

**Problem:** When a verify command is misconfigured (e.g., `test_cmd: "mytest"` and `mytest` is not in PATH), the function returns `False` silently. This causes `attempt_auto_done()` to fail, which is correct behavior. BUT it also means that if ALL verify commands fail with `FileNotFoundError`, the function returns `False` without any logging or warning. The user sees "auto-DONE didn't trigger" but has no way to know why.

**Fix:** Add `log.warning("verify command not found: %s", parts[0])` in the except block.

**Severity:** HIGH — un-debuggable auto-DONE failures.

---

## MEDIUM

### M-1: `wait_for_signal` timeout is only 30 seconds in orchestrator

**File:** `awf/orchestrator.py:332`

```python
signal = wait_for_signal(outbox, current_todo, *prefixes, timeout=30)
```

**Problem:** After the agent subprocess finishes (which can take hours), the orchestrator only waits 30 seconds for the signal file to appear. But the signal file was supposed to be created BY the agent subprocess before it exits. If the agent wrote the signal but the filesystem hasn't synced (rare but possible on network mounts), or if the signal was created by a post-exit hook, 30 seconds is tight.

More importantly: the `wait_for_signal` at line 332 runs AFTER `run_subprocess_until_signal` already returned. The subprocess already exited. The signal should already exist. This 30-second wait is a safety net for delayed signal creation. But if the agent truly didn't write a signal, we waste 30 seconds before trying auto-DONE.

**Not a bug per se**, but the timeout should be configurable or at least 5-10 seconds since the subprocess already exited.

**Severity:** MEDIUM — 30s delay on every agent completion.

### M-2: `clean_stage_signals` doesn't clean `.md.ready` variants

**File:** `awf/signals.py:93-103`

```python
for ext in (".ready", ".md"):
    p = outbox / f"{prefix}-{candidate_id}{ext}"
```

**Problem:** Cleans `DONE-TODO-0001.ready` and `DONE-TODO-0001.md`, but NOT `DONE-TODO-0001.md.ready` (the BD-21 typo variant). If an agent wrote `DONE-TODO-0001.md.ready` on a previous attempt, it won't be cleaned before the retry, causing a stale signal to be picked up.

**Fix:** Add `".md.ready"` to the `ext` tuple.

**Severity:** MEDIUM — stale signals on retry.

### M-3: `config.get()` returns `None` for truthy config values that are `None`

**File:** `awf/config.py:20-27`

```python
def get(config: dict, dotted_key: str, default=None):
    ...
    return default if cur is None else cur
```

**Problem:** If a config key is explicitly set to `null` in YAML, `get()` returns `default`. But if the key is set to an empty string `""`, it returns `""` (falsy but not `None`). This inconsistency means `cfg_mod.get(config, "models.worker.model", "")` returns `""` for both `model: ""` and missing key, but returns `"vllm/llm"` for `model: vllm/llm`. The `get_role_model()` function in supervisor.py handles this correctly with `return val if val else None`, but the pattern is error-prone.

**Not a direct bug** but worth noting for maintainability.

**Severity:** MEDIUM — subtle config resolution behavior.

### M-4: `FormRegistry` persistence uses class-level `PERSIST_ENABLED` flag

**File:** `agent_workflow_ui/src/agent_workflow_ui/state.py:60-68`

```python
PERSIST_ENABLED = True

def __init__(self):
    ...
    if os.environ.get("AWF_DISABLE_FORM_PERSIST", ""):
        FormRegistry.PERSIST_ENABLED = False  # ← modifies class-level!
```

**Problem:** If one test sets `AWF_DISABLE_FORM_PERSIST=1`, it permanently disables persistence for ALL FormRegistry instances in the process, including subsequent tests. The class-level mutation is not reverted.

**Fix:** Use an instance-level flag or a module-level function instead of mutating the class attribute.

**Severity:** MEDIUM — test pollution, potential production issue if env var is set at import time.

### M-5: `cmd_start.py` background mode doesn't handle `--auto` propagation correctly

**File:** `awf/cmd_start.py:37-55`

The argv reconstruction logic strips `--background` and rebuilds the child command. But it uses `sys.argv[1:]` directly, which includes all arguments. If the user passes `--auto --background`, the child gets `--auto`. If the user passes `--background --auto`, the child also gets `--auto`. This seems correct.

However, line 65-66:
```python
if "--project-dir" not in child_argv:
    child_argv += ["--project-dir", str(project_dir)]
```

If the user passed `--project-dir=/path` (with `=`), this check fails because `"--project-dir=/path"` doesn't equal `"--project-dir"`. The child will get a DUPLICATE `--project-dir` argument.

**Fix:** Check `not any(arg.startswith("--project-dir") for arg in child_argv)`.

**Severity:** MEDIUM — duplicate arg in background mode with `--project-dir=/path`.

### M-6: `pipelines_writer.py` only backs up to `.bak` (single backup)

**File:** `agent_workflow_ui/src/agent_workflow_ui/pipelines_writer.py:126-131`

```python
if target.exists():
    backup = pipelines_dir / "default.yaml.bak"
    try:
        target.rename(backup)
```

**Problem:** Each form submit overwrites the previous `.bak`. If the user submits a broken team config, the good pipeline is lost (replaced by the broken one, and the previous `.bak` was already overwritten).

**Fix:** Use timestamped backups (like `cmd_init.py` does for opencode.json) or keep last N backups.

**Severity:** MEDIUM — data loss on consecutive bad submits.

---

## LOW

### L-1: README claims 22 modules, actual count is 32

**File:** `README.md:21`

```
- **Python core.** 22 модуля (~2100 строк) + тонкий bash-wrapper (36 строк).
```

Actual: 32 `.py` files in `awf/`, ~3848 LOC.

**Fix:** Update README.

### L-2: README claims 510 tests, actual count is 628

**File:** `README.md:416`

```
**510 тест:** 12 E2E + 214 awf-unit + 284 plugin.
```

Actual: 628 tests (confirmed via `pytest --collect-only`).

**Fix:** Update README.

### L-3: Duplicate `_make_supervisor_proj` function in test file

**File:** `tests/unit/test_orchestrator.py:1146` and `tests/unit/test_orchestrator.py:1194`

The function `_make_supervisor_proj` is defined twice. The second definition silently overwrites the first. Both definitions are identical, so this is harmless but confusing.

**Fix:** Remove the duplicate.

### L-4: `print_progress_report` uses emoji in output

**File:** `awf/plan_progress.py:166`

```python
print("  All steps complete! 🎉")
```

**Not a bug**, but the project's AGENTS.md says "Only use emojis if the user explicitly requests it." This is user-facing output, not code comments, so it's acceptable. Still worth noting for consistency.

---

## Test Coverage Gaps

### Untested or weakly tested paths:

1. **`_handle_rollback` with empty `new_todo`** — no test covers the case where supervisor fails to create a TODO during rollback (orchestrator.py:196-200). The handler returns `(target_idx, "", 0)` and the pipeline proceeds with an empty todo.

2. **`attempt_auto_done` with `.ready`/.md race** — no test verifies that the synthesized DONE signal is readable by `read_signal_for_todo()` immediately after `attempt_auto_done()` returns.

3. **`run_subprocess_until_signal` with `watch_new_glob` + stale files** — tested (BD-22), but not tested with multiple stale files that match the glob.

4. **`update_config_role_mapping` (BD-12)** — no unit test for the config.yaml patching logic. Only tested implicitly through E2E.

5. **`mark_plan_step_done`** — no unit test. Only tested implicitly through E2E pipeline tests.

6. **`process_role_saves` / `process_role_deletions`** — tested in `tests/agent_workflow_ui/test_roles_processor.py`, but edge cases like JSON parse failures in `team_config` are not covered.

7. **HTTP endpoint CSRF** — tested in `test_http_endpoint.py`, but the TOCTOU race (H-2) is not covered.

8. **`FormRegistry` persistence** — tested, but the class-level flag pollution (M-4) is not covered.

---

## Architecture Observations

### Good practices:
- **Clean module separation** (A6 refactor): orchestrator.py delegates to focused modules (signals.py, transitions.py, verify.py, etc.)
- **Snapshot-based signal detection** (BD-22): stale files from previous runs are correctly ignored
- **Atomic writes** in plugin (http_endpoint.py, state.py): temp file + rename pattern
- **Comprehensive test infrastructure**: `_FakePopen` mock, `tmp_git_repo` fixture, E2E with real subprocess
- **Back-compat for legacy signals**: canonical + short form accepted everywhere

### Concerns:
- **Circular import pattern**: `supervisor.py` imports from `orchestrator.py` (`_awf_subprocess_env`) and vice versa. Resolved via lazy import inside functions, but fragile.
- **Global state in plugin**: `_registry`, `_config`, `_project_dir`, `_jinja_env` are module-level globals. Works for single-process MCP, but not reentrant.
- **No structured logging**: orchestrator uses `print()` + `_log()` (append to file). In production, structured logging (JSON) would help with debugging.

---

## Recommendations (priority order)

1. **Fix C-1:** Make auto-DONE write atomic (write `.md`, verify, then `.ready`)
2. **Fix H-1:** Add empty TODO guard to `_handle_rollback`
3. **Fix H-2:** Make HTTP submit status check+update atomic
4. **Fix H-3:** Add logging to verify command failures
5. **Fix M-2:** Add `.md.ready` to `clean_stage_signals`
6. **Fix M-5:** Fix `--project-dir=` detection in background mode
7. **Add tests** for `_handle_rollback` failure path, `mark_plan_step_done`, `update_config_role_mapping`
8. **Update README** with correct module/test counts
