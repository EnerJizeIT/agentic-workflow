---
custom-width: 75
---
# Communication Protocol

**Version:** 1.0  
**Purpose:** Define how agents exchange tasks, reports, and status signals through the filesystem.

---

## 1. Principle

Agents communicate via **files** in the `.agentic/` directory. This is simple, reliable, and requires no additional infrastructure.

**Signal types:**
- `TASK_READY` — Supervisor has created a TODO for an agent.
- `TASK_DONE` — Agent successfully completed the task.
- `TASK_BLOCKED` — Agent cannot proceed and needs supervisor intervention.
- `TASK_ACK` — Supervisor acknowledged the agent's report.
- `TASK_PROGRESS` — Agent reports intermediate progress (append-only).
- `REVIEW_APPROVED` / `REVIEW_REJECTED` — Reviewer verdict (optional roles).
- `TEST_PASSED` / `TEST_FAILED` — Tester verdict (optional roles).
- `APPROVE` — Supervisor authorizes auto-commit in `--auto`/`--background` mode.

---

## 2. Directories

```
.agentic/
├── inbox/          # Incoming tasks from Supervisor
│   ├── TODO-0001.md
│   ├── TODO-0001.ready
│   └── ...
├── outbox/         # Reports from agents
│   ├── DONE-TODO-0001.md
│   ├── DONE-TODO-0001.ready
│   ├── BLOCKED-TODO-0002.md
│   ├── BLOCKED-TODO-0002.ready
│   ├── PROGRESS-TODO-0001.md   # Append-only progress log
│   └── ...
├── context/        # Context snapshots and baselines
│   └── BASELINE-TODO-0001.sha
├── logs/           # Iteration logs
│   └── orchestrator.log
└── reports/        # Summary reports for the user
    └── status.md
```

### Directory rules

- `inbox/` — only Supervisor writes, agents read.
- `outbox/` — only agents write, Supervisor reads.
- `outbox/PROGRESS-{NNNN}.md` — agent appends after each task; Supervisor reads for monitoring.
- `context/` — Supervisor writes context snapshots, agents read.
- `logs/` — both agents and orchestrator may append.
- `reports/` — orchestrator writes, user reads.

---

## 3. Signal format

### 3.1 TASK_READY — Supervisor → Agent

**Files:**
- `.agentic/inbox/TODO-{NNNN}.md` — the TODO list.
- `.agentic/inbox/TODO-{NNNN}.ready` — signal file.

**Contents of `.ready`:**
```yaml
signal: TASK_READY
task_id: TODO-0001
step: "<step name from phases>"
priority: high|medium|low
created_by: supervisor
created_at: <ISO timestamp>
```

**Rules:**
- `{NNNN}` — four-digit number with leading zeros (0001, 0002, ...).
- `.md` file is created **before** `.ready`.
- Agent must not start until `.ready` exists.
- Supervisor must not create a new TODO until the previous one is closed (DONE or BLOCKED).

### 3.2 TASK_DONE — Agent → Supervisor

**Files (canonical — `{PREFIX}-TODO-{NNNN}`):**
- `.agentic/outbox/DONE-TODO-{NNNN}.md` — report.
- `.agentic/outbox/DONE-TODO-{NNNN}.ready` — signal file.

**Contents of `.ready`:**
```yaml
signal: TASK_DONE
task_id: TODO-0001
status: completed
created_by: worker
created_at: <ISO timestamp>
```

> **Naming:** the canonical signal filename is `DONE-TODO-{NNNN}` — i.e. the
> `{PREFIX}` (`DONE`) followed by the **full task id** (`TODO-0001`).
> The legacy short form `DONE-{NNNN}` (without the `TODO-` infix) is also
> accepted by the orchestrator for backwards compatibility — see §3.7.

### 3.3 TASK_BLOCKED — Agent → Supervisor

**Files (canonical):**
- `.agentic/outbox/BLOCKED-TODO-{NNNN}.md` — blocker report.
- `.agentic/outbox/BLOCKED-TODO-{NNNN}.ready` — signal file.

**Contents of `.ready`:**
```yaml
signal: TASK_BLOCKED
task_id: TODO-0001
blocked_task: "<task description>"
created_by: worker
created_at: <ISO timestamp>
```

### 3.4 TASK_ACK — Supervisor → Agent

**Files:**
- `.agentic/inbox/ACK-TODO-{NNNN}.ready` — signal file.

**Contents:**
```yaml
signal: TASK_ACK
ack_type: DONE|BLOCKED
decision: continue|fix|rollback|ask_user
referenced_task_id: TODO-0001
next_task_id: TODO-0002
notes: "<optional note>"
created_by: supervisor
created_at: <ISO timestamp>
```

### 3.5 TASK_PROGRESS — Agent → Supervisor (append-only)

**Files:**
- `.agentic/outbox/PROGRESS-TODO-{NNNN}.md` — append-only progress log.

**Format:** Markdown, one entry per task. Agent appends after completing each task.

```markdown
## Task {N} · {title} — [x] complete / [~] in-progress / [!] failed
- **Completed at:** {ISO timestamp}
- **Files changed:** `file1`, `file2`
- **Verify:** {command} — green|red
- **Notes:** {brief note if anything unexpected}
```

**Rules:**
- File is **append-only** — never rewrite or delete previous entries.
- Created by agent on first task completion.
- Supervisor may read at any time to monitor progress.
- If agent crashes and restarts, it reads this file to resume from last checkpoint.
- Timestamps use `T00:00:00Z` format for KV-cache stability (normalized hours).

### 3.6 APPROVE — Supervisor (auto-commit authorization)

**Files:**
- `.agentic/inbox/APPROVE-TODO-{NNNN}.ready` — signal file.

**Purpose:** When the pipeline runs in `--auto` mode (e.g. `--background`), the
`commit_and_next` and `commit_and_report` policies at the verify stage require
an explicit APPROVE signal before committing. Use `awf approve <TODO-ID>` to
authorize. Without it, the pipeline blocks indefinitely.

**Creation:**
```bash
awf approve TODO-0001
# or manually:
touch .agentic/inbox/APPROVE-TODO-0001.ready
```

### 3.7 Signal naming — canonical vs legacy

All signal filenames embed the **task id**. Two naming conventions exist:

| Convention | Example | Status |
|---|---|---|
| **Canonical** `{PREFIX}-TODO-{NNNN}` | `DONE-TODO-0001`, `BLOCKED-TODO-0002` | Preferred. What `awf` itself writes and what role templates instruct. |
| **Legacy short** `{PREFIX}-{NNNN}` | `DONE-0001`, `BLOCKED-0002` | Accepted for backwards compatibility. Older worker templates produced these. |

**Resolution rules (implemented in `read_signal_for_todo` / `find_active_todo`):**

1. When polling for a signal for `TODO-{NNNN}`, the orchestrator checks **both** forms, canonical first.
2. A TODO is considered closed if **either** form of `DONE`/`BLOCKED`/`ACK` exists for its `{NNNN}`.
3. New role templates and new supervisor-written signals MUST use the canonical form. The legacy form is read-only tolerance — do not write it.

**Why both:** the short form caused a silent bug (`worker.md` instructed it, orchestrator searched canonical → TODO "hung" and was re-run). Tolerance is a safety net during the migration period; new code paths rely on the canonical form only.

---

## 4. Task lifecycle

```
Supervisor:  [CREATE TODO-0001.md]
             [CREATE TODO-0001.ready] ──TASK_READY──▶
                                                     │
Agent:                                               ▼
                                           [READ TODO-0001.md]
                                           [EXECUTE tasks]
                                           [CREATE DONE-TODO-0001.md]
              ◀──────TASK_DONE──────────── [CREATE DONE-TODO-0001.ready]
              │
Supervisor:  [VERIFY result]
              [CREATE ACK-TODO-0001.ready]
              [CREATE TODO-0002.md]
              [CREATE TODO-0002.ready] ──TASK_READY──▶
```

---

## 5. Active task identification

An active task is one where:
- `.agentic/inbox/TODO-{NNNN}.ready` exists.
- No matching `.agentic/outbox/DONE-TODO-{NNNN}.ready` (or legacy `DONE-{NNNN}.ready`) exists.
- No matching `.agentic/outbox/BLOCKED-TODO-{NNNN}.ready` (or legacy `BLOCKED-{NNNN}.ready`) exists.

**An agent must not pick up a new task while an active one is open.**

> When more than one TODO is simultaneously active, the orchestrator picks the
> **highest-numbered** one (the most recently created) — see `find_active_todo()`.
> `awf status` lists all active TODOs and warns about conflicts.

---

## 6. Safety and atomicity

### 6.1 File creation order

1. **Supervisor:** writes `TODO-{NNNN}.md`, then `TODO-{NNNN}.ready`.
2. **Agent:** writes `DONE-TODO-{NNNN}.md` / `BLOCKED-TODO-{NNNN}.md`, then `.ready`.
3. **Supervisor:** writes `ACK-TODO-{NNNN}.ready` (optional).

### 6.2 Guardian checks

Agent before starting:
- `TODO-{NNNN}.md` exists and size > 0.
- No active `DONE-TODO-{NNNN}.ready` (or legacy `DONE-{NNNN}.ready`) for the same NNNN.

Supervisor before ACK:
- `DONE-TODO-{NNNN}.md` contains regression verify results.
- `git diff --stat` shows changes in source files.

### 6.3 Idempotency

Agent must not re-execute a TODO that already has `DONE-TODO-{NNNN}.ready` (or legacy `DONE-{NNNN}.ready`) / `BLOCKED-*` for the same NNNN.

If restarted, agent should:
1. Find the latest `TODO-{NNNN}.ready` without matching DONE/BLOCKED.
2. Verify `TODO-{NNNN}.md` hasn't changed since `.ready`.
3. Continue from that TODO.

### 6.4 Session Recovery

If the agent process crashes or is interrupted, it can resume from `PROGRESS-TODO-{NNNN}.md`:

1. Agent starts, finds active `TODO-{NNNN}.ready`.
2. Checks for `.agentic/outbox/PROGRESS-TODO-{NNNN}.md`.
3. If exists — reads it, identifies last completed task.
4. Skips tasks marked `[x]`, continues from first `[~]` or next unmarked task.
5. If no progress file — starts from Task 1.

**Supervisor replan invalidates progress:** if Supervisor creates a new TODO (e.g., `TODO-0002`) to replace a blocked `TODO-0001`, the new TODO starts fresh with no progress file.

---

## 7. Form inputs and templates (agent-workflow-ui plugin)

When the `agent-workflow-ui` plugin is installed, two additional directories
appear under `.agentic/`:

### 7.1 inputs/

Form submits from the browser to the agent. Each submit is a separate YAML file.

**Path:** `.agentic/inputs/<form_id>.yaml` (e.g. `.agentic/inputs/FORM-001.yaml`)

**Gitignored.** Transient runtime state, like inbox/outbox.

**Schema:**
- `form_id` (string, required) — form ID.
- `template` (string, required) — which template was used.
- `submitted_at` (ISO 8601 UTC, required) — when the user pressed Submit.
- `data` (object, required) — payload from the form.

See [`vision/architecture.md`](../vision/architecture.md) §7.2 for details.

### 7.2 templates/

Jinja2 templates for HTML forms. Project-scoped; override the plugin's default
templates by name.

**Path:** `.agentic/templates/<name>.html.j2`

**Committed to git.** Part of the project design, like `pipelines/`.

**Format:** YAML frontmatter + Jinja2 template body.

See [`vision/architecture.md`](../vision/architecture.md) §7.3 for details.

---

## 8. Role skills (BD-27 + BD-31)

### Single-layer model (BD-27)

Form writes skill content **directly into** `.agentic/roles/<role>.md`.
No separate `.agentic/skills/` layer (was BD-10/13 legacy, removed).
Agent receives role.md via `--file` arg.

### Optional: skill-aware analysis (BD-31)

`awf analyze-roles` reads all role.md files, detects zone overlaps
(e.g. qa-review + project-auditor both "verify"), and appends a
"BD-31: Pipeline-specific disambiguation" section to each role.md
clarifying the unique contribution. Idempotent — re-running replaces
existing patches.

```bash
awf analyze-roles              # apply patches
awf analyze-roles --dry-run    # preview only
```

Not mandatory — pipeline works without it.

---

## 9. Git status

Runtime files (`inbox/`, `outbox/`, `context/`, `logs/`, `reports/`) are **not committed** to git. Add to `.gitignore`:

```gitignore
.agentic/inbox/
.agentic/outbox/
.agentic/context/
.agentic/logs/
.agentic/reports/
```

Only static config and instructions are committed: `config.yaml`, `roles/`, `pipelines/`, `phases/`.
