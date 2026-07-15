# TEMPLATE-TODO: TODO Template

> Template for TODO lists passed to the Worker agent.
> Each task is self-contained with exact match/replace.

---

## Prohibitions (DO NOT do this)

Adapt this section for your project:

- DO NOT fix pre-existing errors unless the task explicitly requires it.
- DO NOT bump version between tasks — only once in Final Task.
- DO NOT use `sed`/`awk`/`cat`/`echo >` for code edits — only `edit` tool.
- DO NOT `git commit` until ALL Tasks 1..N are implemented via `edit` tool.
- DO NOT refactor neighboring code. DO NOT add comments beyond Replace blocks.
- If `Find` string is not found verbatim — STOP, write `BLOCKED-{NNNN}.md`.

---

## Meta

- **TODO ID:** TODO-{NNNN}
- **Step:** <step name from phases>
- **Target version:** <version>
- **Source:** <reference document or previous TODO>
- **Goal:** <one line>

---

## Context

> Brief context for the Worker. Only what's needed for these tasks.

- Project: <tech stack>
- Current focus: <phase step>
- Constraints: <any relevant limits>

## Architecture Notes

> Which public interfaces this TODO touches and why it's safe.

- Changed: `<file/interface>`
- Unchanged: `<what stays the same>`
- New dependencies: `<if any>`

---

## Tasks

### [ ] Task 1 · <title>

**Files:** `path/to/file.ext:line`

**Find:**
```
<exact existing code to match — copy verbatim from file>
```

**Replace with:**
```
<exact new code>
```

**Why:** <one line>

**Verify:**
```bash
<command>  # expect: <expected result>
```

**Done when:** <criterion>

---

### [ ] Task 2 · <title>

...

---

### [ ] Final · Regression Verify (required before Bump and Commit)

> Run after all Tasks 1..N, before version bump and commit.

```bash
{verification.test_cmd}
{verification.lint_cmd}
{verification.typecheck_cmd}
{verification.build_cmd}
```

Compare with baseline. No new errors allowed.

---

### [ ] Final · Bump version (if required)

> Version bump happens ONCE at the very end.

| # | File | Old | New |
|---|---|---|---|
| 1 | `VERSION` | `0.1.0` | `0.2.0` |

---

### [ ] Final · Git commit + push (LAST step, if required)

> **CRITICAL:** This is the LAST step. All Tasks 1..N + Bump must be done first.
> **If `git diff --stat` shows only `.md` files — you didn't implement code.**

**Checklist before commit:**

- [ ] All Tasks 1..N implemented via `edit` tool
- [ ] `git diff --stat` shows source file changes (NOT just `.md`)
- [ ] Regression verify passed
- [ ] Docs updated (if required)

```bash
git add -A
git commit -m "vX.Y.Z: <brief description>"
git push
```

---

## Rules for Worker

1. Do exactly what's written. If `Find` not found — STOP, write BLOCKED.
2. Create baseline before any changes: `awf baseline TODO-{NNNN}`.
3. Read prohibitions section FIRST.
4. Regression verify is mandatory before bump and commit.
5. Bump version ONCE at the very end.
6. After each task — run the specified Verify command.
7. Git commit + push — LAST step, not first.
8. Don't use sed/awk/cat/echo for code edits — use edit tool.
9. Don't do more than the TODO says. Don't refactor. Don't add comments.
10. After each task — append entry to `.agentic/outbox/PROGRESS-{NNNN}.md`.
11. If verify fails — use the 3-Strike Error Protocol before escalating.

## Progress Tracking

Write `.agentic/outbox/PROGRESS-{NNNN}.md` as you work. Append-only, never rewrite.

```markdown
## Task {N} · {title} — [x] complete / [~] in-progress / [!] failed
- **Completed at:** {ISO timestamp}
- **Files changed:** `file1`, `file2`
- **Verify:** {command} — green|red
- **Notes:** {brief note}
```

## Error Attempt Log

If a task fails, track attempts:

| Attempt | Approach | Error | Resolution |
|---------|----------|-------|------------|
| 1 | {what you tried} | {error} | {result} |
| 2 | {different approach} | {error} | {result} |
| 3 | {broader rethink} | {error} | escalated |

After 3 failures — write BLOCKED with the full table.
