# Phase: brief

## Your task
Study project context, write TODO for agent, dispatch + start pipeline.

## Step 1: Study project

Read vision document and BACKLOG. Understand what already exists.
Decide what THIS increment accomplishes (based on session goal + increment plan).

## Step 2: Write TODO content

Prepare a TODO for the FIRST agent stage. Structure:

```markdown
# TODO-NNNN — <title>

## Goal
What this increment achieves (1-3 sentences).

## Tasks
- [ ] Specific, testable task 1
- [ ] Specific, testable task 2

## Context
<What the agent needs to know about existing code/architecture>

## Verify
- `<command>` — must pass after changes
```

**Multi-stage pipeline** (when 2+ roles share one TODO):
```markdown
## Context
This iteration goes through: <role-1> → <role-2> → <role-3>.

## Your task
<Specific task for role-1 only>

## What follows you
<Role-2> will <action>. <Role-3> will <action>.
```

Do NOT: write instructions for stages 2+ (workers read role.md).
Do NOT: micro-manage the whole pipeline in one TODO.

## Step 3: Dispatch + start

1. `awf_dispatch_todo(project_dir, content=<TODO content>, role=<first role>)`
2. `awf_start(project_dir, background=True)` — dashboard opens automatically.
3. GO IDLE. User monitors dashboard.

## Before dispatch — verify role scope
Read `.agentic/roles/<next-role>.md` prohibitions. If task is out of scope,
rephrase or split into multiple TODOs.
