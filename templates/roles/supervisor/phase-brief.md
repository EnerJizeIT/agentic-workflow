# Phase: brief

## Your task
Study project context, write TODO for agent, dispatch + start pipeline.

## Step 1: Study project + pre-check

Read vision, BACKLOG, and pipeline config. **Before dispatching — check
which tasks are already implemented.** Use `read` and `grep` to verify
the code actually needs the changes described in BACKLOG.

**Skip tasks that are already done.** Don't dispatch a TODO for work
that's already in the codebase — that wastes an entire pipeline run.

## Step 2: Batch tasks into substantial TODOs

**Match TODO scope to pipeline depth.** Read `.agentic/pipelines/default.yaml`
to see how many stages will run:

- **1-2 stages**: small TODOs OK (single fix, single feature)
- **3-5 stages**: batch multiple tasks (group by area: "all storyboard fixes",
  "all UI improvements"). Aim for **10+ lines of real code change**.
- **6+ stages**: large increments (full feature, multiple files)

**Rule: don't run a pipeline for work that takes less than 1 minute to do.**
If a BACKLOG task is a one-line fix, combine it with neighbors into a
batch TODO.

## Step 3: Write TODO content

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

Do NOT: write instructions for stages 2+ (workers read role.md).
Do NOT: micro-manage the whole pipeline in one TODO.

## Step 4: Dispatch + start

1. `awf_dispatch_todo(project_dir, content=<TODO content>, role=<first role>)`
2. `awf_start(project_dir, background=True)` — dashboard opens automatically.
3. GO IDLE. User monitors dashboard.

## Before dispatch — verify role scope
Read `.agentic/roles/<next-role>.md` prohibitions. If task is out of scope,
rephrase or split into multiple TODOs.
