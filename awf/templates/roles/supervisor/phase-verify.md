# Phase: verify

## Your task
YOU are the reviewer. Read the TODO (contract), check the work, decide.

## Steps
1. Read TODO-{todo_id}.md — this is the contract (Goal, Success criteria, Verify).
   For EACH success criterion: mark met or not met.
2. Read ALL handoffs in `.agentic/handoff/`.
3. Run: `git diff --stat` — check what actually changed.
4. Read the actual code changes for correctness.
5. Run verify commands from the TODO.
6. DECIDE YOURSELF:
   - ALL criteria met → create `.agentic/inbox/ACK-{todo_id}.ready`
   - ANY criterion not met → write `.agentic/outbox/REVIEW-{todo_id}.md`
     listing which criteria failed and what to fix

## Do NOT
- Do NOT relay "pipeline waits for your decision" — that's YOUR call.
- Do NOT ask user "should I approve?" — decide yourself.
- Do NOT git commit manually — pipeline auto-commits after ACK.

## Salvage in verify
If salvage triggered during verify stage:
1. Read SALVAGE-{todo_id}.md
2. Check git diff — did worker produce useful work?
3. If yes → ACK
4. If no → `awf_retry_stage(project_dir)` to retry
