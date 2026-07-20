# TEMPLATE-TODO: TODO Template

> Unified template for TODO lists passed to the Worker agent.
> Supports Mode A (high-level), Mode B (detailed with examples), and Mode C (exact Find/Replace).
> Delete sections marked `(optional)` when they don't apply.

---

## Prohibitions (DO NOT do this)

> Adapt to your project. Worker reads this section FIRST.

- DO NOT fix pre-existing errors unless the task explicitly requires it.
- DO NOT bump version between tasks — only once in the Final Task.
- DO NOT use `sed`/`awk`/`cat >`/`echo >` for code edits — only `edit` and `write` tools.
- DO NOT `git commit` or `git push` — that is the Supervisor's job.
- DO NOT refactor neighboring code. DO NOT add comments beyond what the TODO says.
- DO NOT change public interfaces without explicit instruction.
- If a `Find` string (Mode C) is not found verbatim — STOP, write `BLOCKED-{NNNN}.md`.

---

## Meta

- **TODO ID:** TODO-{NNNN}
- **Step:** <step name from phases>
- **Source:** <reference document or previous TODO>
- **Goal:** <one line>
- **Working directory:** `<absolute path>`
- **Done when:** <one-line testable criterion>

---

## Context

> Brief context for the Worker. The worker has ZERO context outside this file.

- **Project:** <what it is, tech stack>
- **Current state:** <what was just done, relevant version>
- **Why this TODO exists:** <one sentence on the problem being solved>
- **Constraints:** <any relevant limits: time, scope, dependencies>

---

## Architecture Notes (optional — Mode B/C)

> What changes, what stays the same. Include code blocks or file-tree snippets when helpful.
> Reference existing patterns: `See how X does it in path/to/file`.

- **Changed:** `<file/interface>` — <what changes and why>
- **Unchanged:** `<what stays the same>`
- **New dependencies:** `<if any>`
- **Reference pattern:** `<see existing code that demonstrates the approach>`

---

## Environment & Dependencies (optional)

> Env vars, tools on PATH, stubs/mocks, test infrastructure the task relies on.
> Skip if not applicable (e.g. Mode C with no env needs).

- **Env vars:** `VAR=value` — <purpose>
- **Tools required:** <commands that must be on PATH, e.g. `python3`, `pytest`, `git`>
- **Stubs/mocks:** <path to stub, how it behaves, env var that controls behavior>
- **Test infra:** <pytest fixtures, bash test runner, baseline files>
- **Fake HOME:** <if tests must not touch user's real config, set `HOME` to a tmp dir>

---

## Tasks

> 1–5 tasks per TODO (optimal). Each task is self-contained.
> Mode A: description + constraints. Mode B: description + examples. Mode C: Find/Replace blocks.

### Task 1 · <title>

**Files:** `path/to/file.ext:line`

**Description:**
<What to build. Mode A: imperative description of the feature. Mode B: description
+ example code or architecture notes. Mode C: skip, use Find/Replace blocks below.>

**Constraints:**
- <what NOT to touch, what must stay stable>
- <follow existing patterns in the codebase>

**Find:** (Mode C only)
```<lang>
<exact existing code to match — copy verbatim from file>
```

**Replace with:** (Mode C only)
```<lang>
<exact new code>
```

**Why:** (Mode C only) <one line — why this exact change>

**Verify:**
```bash
<command>  # expect: <expected output or exit code>
```

**Done when:** <testable criterion, e.g. "endpoint returns 400 for missing fields">

---

### Task 2 · <title>

> Repeat structure from Task 1.

---

### Final · Regression Verify

> Run after all Tasks 1..N, before reporting DONE.

```bash
<test command>      # expect: all pass
<lint command>      # expect: no new warnings
<typecheck command> # expect: no new errors
```

Compare with baseline. No new errors allowed.

---

## Verify (final regression)

> Full-suite commands that must pass before DONE.

```bash
<full test suite command>
```

Expected: <exact expected output, e.g. `passed: 102 / 102`>.

---

## Pre-DONE checklist

> Self-check before writing `DONE-TODO-{NNNN}.md`. All must be checked.

- [ ] All Tasks 1..N implemented
- [ ] `git diff --stat` shows source file changes (not just `.md`)
- [ ] Verify after each Task — passed
- [ ] Final regression verify — passed
- [ ] No production code modified outside the task's file scope
- [ ] No commit made (supervisor's job)
- [ ] `PROGRESS-{NNNN}.md` has entries for all Tasks
- [ ] Deviations documented in DONE report (if any)

---

## Read-vs-Write decision matrix

> When to re-read a file, when to write progress, when to ask.

| Situation | Action | Reason |
|---|---|---|
| Just read a file | DON'T re-read | Content still in context |
| Just wrote a file | DON'T re-read | Content still in context |
| Discovered something unexpected | Write to PROGRESS NOW | Prevents lost context |
| Starting new Task | Read TODO again | Re-orient if context stale |
| Error occurred | Read target file | Need current state to fix |
| After 2 file operations | Append to PROGRESS | 2-action rule: persist findings |

---

## Progress tracking

> After each Task, append to `.agentic/outbox/PROGRESS-{NNNN}.md` (append-only, never rewrite):

```markdown
## Task {N} · {title} — [x] complete / [~] in-progress / [!] failed
- **Completed at:** {ISO timestamp}
- **Files changed:** `file1`, `file2`
- **Verify:** {command} — green|red
- **Notes:** {brief note if anything unexpected}
```

If a task fails, track attempts in the same file:

| Attempt | Approach | Error | Resolution |
|---------|----------|-------|------------|
| 1 | {what you tried} | {error} | {result} |
| 2 | {different approach} | {error} | {result} |
| 3 | {broader rethink} | {error} | escalated |

After 3 failures — write BLOCKED with the full table.

---

## If BLOCKED

> Write `BLOCKED-TODO-{NNNN}.md` with:

- **Which task** failed
- **Exact error** (command output, not paraphrase)
- **Attempts** (what you tried, up to 3)
- **Root cause hypothesis**
- **Suggested next step** for the Supervisor
