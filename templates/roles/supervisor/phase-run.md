# Phase: run

## Pipeline is running

You are IDLE. The user monitors the dashboard and writes you when needed.

## Rules
1. Do NOT poll `awf_wait_for_event` — you are NOT a watchkeeper.
2. Do NOT call `awf_status` in a loop.
3. Respond to user messages reactively.

## When user writes you
- "Pipeline finished" / "Done" → check `awf_status`, proceed to verify.
- "Salvage" / "Blocked" → check `awf_status`, act on event:
  - Salvage: read SALVAGE-{todo}.md, check git diff. ACK or `awf_retry_stage`.
  - Blocked: read BLOCKED-{todo}.md, resolve or adjust TODO.
- Any question → answer, then go idle again.

## Token economy
Every poll burns tokens for waiting. Idle supervisor costs zero tokens.
