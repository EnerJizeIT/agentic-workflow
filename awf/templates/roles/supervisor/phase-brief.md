# Phase: brief

## Your task
Write Increment Brief for user approval, then write TODO for agent.

## Step 1: Write Brief

Write `.agentic/inbox/BRIEF-TODO-NNNN.md`:

```markdown
# Brief: <increment title>

## Goal
What this increment achieves (1-3 sentences). Based on session goal.

## Success criteria
- Testable conditions that define "done"

## Out of scope
- What we explicitly don't do

## Verify
- Commands to check success
```

Create signal: `.agentic/inbox/BRIEF-TODO-NNNN.ready`

## Step 2: After Brief approval → write TODO

Once user approves Brief (checkpoint form), write `.agentic/inbox/TODO-NNNN.md` —
detailed task for the FIRST agent stage only.

Include: context, tasks, files, constraints, verify command, prohibitions.

**Multi-stage template** (when 2+ roles share one TODO):
```markdown
## Context
This iteration goes through: <role-1> (you) → <role-2> → <role-3>.

## Your task
<Specific task for role-1 only>

## What follows you
<Role-2> will <action>. <Role-3> will <action>.
```

Do NOT: write instructions for stages 2+ (workers read role.md).
Do NOT: micro-manage the whole pipeline in one TODO.

## Step 3: Dispatch + start

1. `awf_dispatch_todo(project_dir, content=<TODO content>, role=<first role>)`
2. `awf_start(project_dir, background=True)`
3. `awf_open_pipeline_dashboard(project_dir)` — MANDATORY.
4. Go IDLE. User monitors dashboard.

## Before writing TODO — verify role scope
Read `.agentic/roles/<next-role>.md` prohibitions. If task is out of scope,
rephrase or split into multiple TODOs.
