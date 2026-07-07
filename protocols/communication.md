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
- `REVIEW_APPROVED` / `REVIEW_REJECTED` — Reviewer verdict (optional roles).
- `TEST_PASSED` / `TEST_FAILED` — Tester verdict (optional roles).

---

## 2. Directories

```
.agentic/
├── inbox/          # Incoming tasks from Supervisor
│   ├── TODO-0001.md
│   ├── TODO-0001.ready
│   └── ...
├── outbox/         # Reports from agents
│   ├── DONE-0001.md
│   ├── DONE-0001.ready
│   ├── BLOCKED-0002.md
│   ├── BLOCKED-0002.ready
│   └── ...
├── context/        # Context snapshots and baselines
│   └── BASELINE-0001.sha
├── logs/           # Iteration logs
│   └── orchestrator.log
└── reports/        # Summary reports for the user
    └── status.md
```

### Directory rules

- `inbox/` — only Supervisor writes, agents read.
- `outbox/` — only agents write, Supervisor reads.
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

**Files:**
- `.agentic/outbox/DONE-{NNNN}.md` — report.
- `.agentic/outbox/DONE-{NNNN}.ready` — signal file.

**Contents of `.ready`:**
```yaml
signal: TASK_DONE
task_id: TODO-0001
status: completed
created_by: worker
created_at: <ISO timestamp>
```

### 3.3 TASK_BLOCKED — Agent → Supervisor

**Files:**
- `.agentic/outbox/BLOCKED-{NNNN}.md` — blocker report.
- `.agentic/outbox/BLOCKED-{NNNN}.ready` — signal file.

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
- `.agentic/inbox/ACK-{NNNN}.ready` — signal file.

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

---

## 4. Task lifecycle

```
Supervisor:  [CREATE TODO-0001.md]
             [CREATE TODO-0001.ready] ──TASK_READY──▶
                                                     │
Agent:                                               ▼
                                          [READ TODO-0001.md]
                                          [EXECUTE tasks]
                                          [CREATE DONE-0001.md]
             ◀──────TASK_DONE──────────── [CREATE DONE-0001.ready]
             │
Supervisor:  [VERIFY result]
             [CREATE ACK-0001.ready]
             [CREATE TODO-0002.md]
             [CREATE TODO-0002.ready] ──TASK_READY──▶
```

---

## 5. Active task identification

An active task is one where:
- `.agentic/inbox/TODO-{NNNN}.ready` exists.
- No matching `.agentic/outbox/DONE-{NNNN}.ready` or `.agentic/outbox/BLOCKED-{NNNN}.ready` exists.

**An agent must not pick up a new task while an active one is open.**

---

## 6. Safety and atomicity

### 6.1 File creation order

1. **Supervisor:** writes `TODO-{NNNN}.md`, then `TODO-{NNNN}.ready`.
2. **Agent:** writes `DONE-{NNNN}.md` / `BLOCKED-{NNNN}.md`, then `.ready`.
3. **Supervisor:** writes `ACK-{NNNN}.ready` (optional).

### 6.2 Guardian checks

Agent before starting:
- `TODO-{NNNN}.md` exists and size > 0.
- No active `DONE-{NNNN}.ready` or `BLOCKED-{NNNN}.ready` for the same NNNN.

Supervisor before ACK:
- `DONE-{NNNN}.md` contains regression verify results.
- `git diff --stat` shows changes in source files.

### 6.3 Idempotency

Agent must not re-execute a TODO that already has `DONE-{NNNN}.ready` or `BLOCKED-{NNNN}.ready`.

If restarted, agent should:
1. Find the latest `TODO-{NNNN}.ready` without matching DONE/BLOCKED.
2. Verify `TODO-{NNNN}.md` hasn't changed since `.ready`.
3. Continue from that TODO.

---

## 7. Git status

Runtime files (`inbox/`, `outbox/`, `context/`, `logs/`, `reports/`) are **not committed** to git. Add to `.gitignore`:

```gitignore
.agentic/inbox/
.agentic/outbox/
.agentic/context/
.agentic/logs/
.agentic/reports/
```

Only static config and instructions are committed: `config.yaml`, `roles/`, `pipelines/`, `phases/`.
