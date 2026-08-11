# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-08-11

### Added
- **SMO (State-Machine Orchestration)** — phase system: `init → goal → form → normalize → brief → run → verify → done`
- **29 MCP tools** (5 UI + 24 workflow), all with `next_action` guidance
- **Dashboard v2** — HTTP server with live `/api/state` polling, two-panel layout, chat-style handoffs, TODO timeline, browser notifications
- **`awf_reject`** tool — symmetric to `awf_approve` for verify stage
- **`awf_retry_stage`** tool — kill + retry from salvage stage in one call
- **`awf_current_step`**, **`awf_set_goal`**, **`awf_confirm_normalized`** — SMO phase tools
- **Pre-dispatch check** — `awf_dispatch_todo` greps codebase for already-implemented patterns
- **Form auto-advance** — form submission advances phase form→normalize automatically
- **Batch guidance** — phase-brief.md instructs supervisor to batch tasks by pipeline depth
- **TODO rollback** — failed dispatch cleans up orphan TODO files
- **Handoff sort** — pipeline order (not alphabetical)
- **Per-stage live timer** — JS ticker for current running agent
- **Markdown rendering** — handoffs and TODO content rendered via `markdown` library

### Changed
- `awf_init` returns compact phase prompt (not full 600-line supervisor.md)
- `awf_load_supervisor_context` returns `phase` + `phase_prompt`
- `next_action` is phase-aware in all SMO flow tools
- `awf_approve` next_action: "Wait for user" (was: "dispatch next TODO" — caused auto-dispatch without user consent)
- `awf_start` message: "GO IDLE" (was: "call awf_wait_for_event" — caused polling)
- Dashboard auto-refresh pauses on verify
- `requires-python` = `>=3.10` (PEP 604 syntax)

### Fixed
- Circular dependency orchestrator↔pipeline_engine eliminated
- Jinja2 XSS via `from_string` (autoescape=True)
- `OPENCODE_CONFIG_CONTENT` leaking API keys (only permission field serialized)
- `git commit` without reset on failure (staged files remain)
- Rollback path traversal (regex validation on todo_id)
- Dashboard `location.reload()` race condition (replaced with HTTP server)
- Handoff role extraction (split on `-TODO-`, not `-`)

### Security
- `inputs/*.yaml` chmod 0o600
- Worker log chmod 0o600
- Path validation on `roles_processor._copy_to_project`
- `yaml.safe_load` try/except in config.py and pipeline.py

## [0.4.0] — 2026-07-25

### Added
- Python migration (bash → Python core)
- Pipeline engine with stage handlers
- Commit gate with baseline isolation
- Signal-based worker communication
- BD-36 plan checkpoint
- Salvage path for crashed workers

[1.0.0]: https://github.com/EnerJizeIT/agentic-workflow/releases/tag/v1.0.0
[0.4.0]: https://github.com/EnerJizeIT/agentic-workflow/releases/tag/v0.4.0
