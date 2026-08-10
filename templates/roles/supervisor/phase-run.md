# Phase: run

## Pipeline is running — YOU ARE IDLE

Dashboard auto-opens inside `awf_start`. Do NOT call `awf_open_pipeline_dashboard`.

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
