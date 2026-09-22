# Phase: run

Two modes — check which one you are in: `awf_run_status(project_dir)`.

- `active: false` (or no run state) → **REACTIVE mode** (below).
- `active: true` → **RUN mode / забег** (section at the bottom).

## REACTIVE mode — pipeline is running, YOU ARE IDLE

Dashboard opens inside `awf_start`. Only if its response has
`dashboard_opened: false` — call `awf_open_pipeline_dashboard(project_dir)`
once.

## ABSOLUTE RULES (violation = wasted tokens + annoyed user)
1. **Do NOT call `awf_wait_for_event`.** It blocks 30s for nothing.
2. **Do NOT call `awf_status` in a loop.** One check is enough, then stop.
3. **Do NOT call any awf tool unless the user writes you first.**
4. Tell user: "Pipeline running. Dashboard open. Write me when done or if issues."
5. **STOP.** Do nothing else. Wait for user message.

## When user writes you
- "Pipeline finished" / "Done" / "verify" → check `awf_status` ONCE, proceed to verify.
- "Salvage" / "Blocked" → check `awf_status`, act on event.
- Any question → answer, then go idle again.

## RUN mode (забег) — active autonomous run

You drive the loop yourself; the engine only enforces the stop gates.

One cycle:
1. `awf_run_next(project_dir)` — launches the next TODO from the queue.
2. `awf_wait_for_event(project_dir, timeout=<suggested_timeout>, actionable_only=True)`
   — wait for an event; on `timeout`/`stage_changed` just call it again
   (`suggested_timeout` comes from the response — sleep in chunks).
3. On `verify` — run your own probes (the TODO's verify commands, `git diff`),
   then approve with `awf_approve(todo_id, evidence=...)` — in a run the
   evidence is REQUIRED (the commands you actually ran + your verdict) —
   or reject with `awf_reject(todo_id, reason)`.
   Do NOT touch `ACK-{todo}.ready` by hand: while the run is active the
   engine ignores a file-based approve without
   `.agentic/context/RUN-EVIDENCE-{todo}.md`.
4. On every stage change — `awf_run_note(project_dir, text=...)`: the owner
   reads this on the dashboard.
5. Back to `awf_run_next`.

Stop gates (the engine stops the run itself and writes `RUN-REPORT-*.md`
to the outbox): budget exhausted, stop-flag on the next TODO, queue
exhausted, two rejections of one TODO. After a stop — hand the report to
the owner; do NOT start the next TODO past a stop-flag.

Replanning (rewriting the spec, splitting a task) is your standard option —
no owner approval required. Escalate to the owner only on the stop-list:
twice-rejected iteration, BLOCKED without a resolution, budget, audit point.
Record the reason for every replan/split in the run report/note.
