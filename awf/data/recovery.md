One recipe per situation. Check `awf_status` first.

- **Approved, where's the next step?** → the `done` event names the next command (see Rituals / Defaults).
- **Run stopped** (budget / stop-flag / queue) → read `RUN-REPORT-*.md`, replan, `awf_run_start`; never jump past a stop-flag.
- **Pipeline died / salvage** → infrastructure first (Rituals), then `awf_retry_stage`.
- **Stale BLOCKED holds a task** → `awf_unblock <id>`, or `awf_continue --ack <id>` (answer).
- **Unit archived without work** → `awf_restore <id>`.
- **Unit never started** → `awf_todo_remove <id>`.
- **Rejected unit stuck active** (DONE without `.ready`) → `awf todo-retire <id> --reason "…"`.
- **No checkpoint needed** → `no_checkpoints=True` (Defaults, above).
- **MCP / network hangs** → bash: `python3 -m awf <command> --project-dir <path>`.
- **Lost context / new session** → `awf brief`.
