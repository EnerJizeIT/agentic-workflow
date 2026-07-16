# TESTER: Instructions

**Role:** tester  
**Runs as:** `opencode run --agent tester` (or bash script)

---

## 1. What to do

Run the full verification suite and compare results with baseline.

### Step 1 · Run checks

```bash
# Tests
{verification.test_cmd} > .agentic/outbox/TEST-RESULTS-TODO-{NNNN}.log 2>&1

# Linter
{verification.lint_cmd} >> .agentic/outbox/TEST-RESULTS-TODO-{NNNN}.log 2>&1

# Type checker
{verification.typecheck_cmd} >> .agentic/outbox/TEST-RESULTS-TODO-{NNNN}.log 2>&1

# Build (if configured)
{verification.build_cmd} >> .agentic/outbox/TEST-RESULTS-TODO-{NNNN}.log 2>&1
```

### Step 2 · Compare with baseline

Check `.agentic/context/BASELINE-{NNNN}.tests.log`:
- New test failures = regression.
- New lint/typecheck errors = regression.
- Errors that were already in baseline = pre-existing (ignore).

### Step 3 · Coverage check (if configured)

```bash
{verification.coverage_cmd}
```

Coverage must not drop below baseline.

---

## 2. Report

### PASSED

All checks green. No new regressions.

Write `.agentic/outbox/TEST-PASSED-TODO-{NNNN}.md` + `.ready`:
```yaml
signal: TEST_PASSED
task_id: TODO-{NNNN}
created_by: tester
created_at: <ISO timestamp>
```

### FAILED

Checks failed. Describe:
- Which tests failed.
- Is it a regression or pre-existing.
- Error log excerpt.

Write `.agentic/outbox/TEST-FAILED-TODO-{NNNN}.md` + `.ready`:
```yaml
signal: TEST_FAILED
task_id: TODO-{NNNN}
created_by: tester
created_at: <ISO timestamp>
```
