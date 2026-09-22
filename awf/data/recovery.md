One recipe per situation. Check `awf_status` first.

- **Run stopped** (budget / stop-flag / queue) → read `RUN-REPORT-*.md`, replan, `awf_run_start` with the new queue. Do NOT jump past a stop-flag.
- **Pipeline died / salvage** → infrastructure first: `opencode run --auto --agent <role> -- 'say hello'`. Then `awf_retry_stage` or `awf_continue --from-stage <stage>`.
- **Stale BLOCKED holds a task** → `awf_unblock TODO-NNNN`, or answer with `awf_continue --ack TODO-NNNN`.
- **TODO archived without work** → `awf_restore TODO-NNNN`.
- **TODO never started** → `awf_todo_remove TODO-NNNN`.
- **Rejected/abandoned TODO stuck active** (DONE without the `.ready` signal) → `awf todo-retire TODO-NNNN --reason "…"`; back via `awf_restore`.
- **No checkpoint needed** → `no_checkpoints=True` on `awf_start` or `awf_run_start`.
- **MCP / network hangs** → bash: `python3 -m awf <command> --project-dir <path>`.
- **Lost context / new session** → `awf brief`.
