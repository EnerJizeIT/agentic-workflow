# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.2.0] — 2026-09-22

Three autonomous work programs (RUN3–RUN5) on top of 1.1.1: named pipelines,
state hygiene, the supervisor onboarding card, the feedback contour, the
leak-gate, and TODO retirement. Eight new MCP tools — a minor bump.

### Added
- **Named pipelines** (RUN3 #1) — `awf pipeline-write <name> --role R1 [--role R2] [--force]` (MCP `awf_write_pipeline`) writes only `.agentic/pipelines/<name>.yaml` (config.yaml and supervisor.md untouched); `awf pipelines` (MCP `awf_pipelines`) lists them and marks the active one; `awf start --pipeline <name>` runs the named pipeline
- **Per-item pipelines in a run queue** (RUN3 #2) — the queue accepts `{"todo_id": "TODO-0023", "pipeline": "audit-llm"}` objects alongside plain id strings; `awf_dispatch_todo(pipeline=...)` writes the name into the TODO's front-matter and `awf run-next` reads it for items without a queue-level pipeline
- **Role from a skill** (RUN3 #3) — `awf add-role <name> --from-skill <slug>` (MCP `awf_add_role(from_skill=...)`) creates the role with the SKILL.md body (front-matter stripped) instead of the empty template; project skills win over global ones
- **State hygiene** (RUN3 #4/#5) — `awf unblock <id>` (MCP `awf_unblock`) moves stale BLOCKED/ACK closure signals to a `context/` trace directory so a re-issued TODO is visible again; `awf todo-remove <id>` (MCP `awf_todo_remove`) removes a TODO that never started, trace in `done/<id>/removed-<ts>.md`
- **`awf brief`** (MCP `awf_brief`, `--json` for the machine variant; RUN4 #1) — the supervisor's onboarding/recovery card, assembled from the LIVE project state: header (version, project, phase, date), what's next, state (run, active TODOs, blocked/salvage, last signal), a tool map by situation (every tool of the registry, checked both ways by tests), rituals, recovery recipes (`awf/data/recovery.md`) plus project doctrine, what's new (latest CHANGELOG section), and the feedback line. Deterministic for the same state; a new project gets the setup-chain hint instead of the working cycle
- **`awf feedback`** (MCP `awf_feedback`; RUN4 #2) — friction with awf becomes a structured bug/feature report on the owner's desktop: `awf feedback --type bug|feature --title "…" --body "…"`; awf assembles the header facts, the report skeleton, and the newest log tail itself; the report never reads the environment
- **Leak-gate** (RUN5 #1) — `awf reject` (and the engine REVIEW path) snapshot the unit's untracked new files to `REJECT-<todo>.files`; `awf_dispatch_todo(carry_over_from=<id>)` re-claims those paths for the retry's baseline so the retry commit includes them; `awf verify-pack` warns when a rejected unit's files are still leaking
- **`awf todo-retire`** (MCP `awf_todo_retire`; RUN5 #2) — `awf todo-retire <id> --reason "…"` moves a rejected/abandoned TODO that stays "active" (DONE files without the `.ready` signal) to `done/<id>/` with a RETIRED note — no fake closure signal, `awf restore` still brings it back

### Changed
- An unknown explicit `--pipeline` / `awf_start(pipeline=...)` name is now a clear error with the list of available pipelines (previously it silently fell back to `default.yaml`)
- `awf_start` / `awf_continue` accept `no_checkpoints=true` — the BD-36 plan form is skipped for that one launch only (process-scoped: not written to config or state, the next launch asks again); the run-level `awf_run_start(no_checkpoints=...)` is unchanged
- `awf_current_step` recognizes a live project: an active pipeline, roles, or stale state means phase `run` (with a setup note when no goal is stored), and `awf set-goal` refuses on a live project
- `awf_status` / `awf_run_brief` show the active pipeline and the number of available ones
- Tool set: 45 MCP tools (5 UI + 40 workflow); README/USAGE counters and reference tables updated to match `server.py`

### Fixed
- `awf run-next` re-baselining swallowed the carry-over exclusion, so the leak came back in run mode — the exclusion now survives the re-baseline (RUN5 #1 QA)

## [1.1.1] — 2026-09-22

Post-audit additions (U8–U11) plus fixes from the first real autonomous run
(RUN2, topic-trainer bug report). The published 1.1.0 artifact predates them.

### Added
- **`awf metrics`** (MCP `awf_metrics`) — on-demand work-program metrics from `opencode.db`: worker and supervisor tokens, compactions, code lines per unit, and the "if workers ran on model X" cost conversion; markdown report to the desktop with an optional archive mirror (`metrics.mirror_dir`)
- **Subscriptions section** in the metrics report — GPT / Claude / GLM plans with limits, the exact fractional share ("need 2.9 → buy 3") and a pessimistic cache-inclusive line; GLM follows the vendor credits model; `--refresh-subscriptions` refreshes from a live source with cached snapshots and a built-in table as fallback
- **Project doctrine** — `.agentic/doctrine/*.md` is injected into every role's prompt (deterministic assembly, covered by the instruction budget); a new lesson needs no role-template edits
- **`awf tree-sha`** + **`awf approve --verified-sha <hash>`** — "what was verified = what is committed": approve refuses when the tree moved after verification
- **`awf mutations`** — mutation smoke over a quiet tree (once per wave / before release); **`awf todo-draft`** — a task-file skeleton from an audit registry
- **Model-price and subscription caches** — live source → awf cache → built-in table, with the source and date recorded in the report
- **Run checkpoints** — `awf_run_start(no_checkpoints=True)` skips the BD-36 plan form for the whole run; a submitted checkpoint decision survives a died pipeline (`.agentic/context/CHECKPOINT-<todo>.json`) and is applied on the next start without re-asking
- **Productive run budget** — the run budget counts productive minutes (wall clock minus recorded downtime: checkpoint waits, salvage handling, net-backoff pauses); status/brief and RUN-REPORT show both numbers
- **Wait cap** — every `awf_wait_for_event` response states the single-wait transport cap (55s) and how to raise it (`mcp` timeout in `opencode.json`)

### Changed
- `awf_approve` (MCP) accepts `verified_sha`; `awf_metrics` (MCP) accepts `refresh_subscriptions` and `mirror`
- Supervisor doctrine: replan / spec rewrite / task split need no owner approval — escalation only on the stop-list (role templates + USAGE)

## [1.1.0] — 2026-09-21

Release after the 2026-09 audit and the 2026-09-20
session-loss incident. The 1.0.0 PyPI build predates several data-safety and
security fixes — this release includes them.

### Added
- **Autonomous run (забег)** — `awf_run_start` / `awf_run_status` / `awf_run_next` / `awf_run_finish` / `awf_run_note`: a queue of TODOs with mechanical gates; the owner is pinged only on stop conditions
- **`awf_prove_red`** — machine proof that declared tests are red on the baseline sha
- **`awf_verify_pack`** — one deterministic verify report (GATES file)
- **`awf_restore`** — restore an archived TODO back to the inbox
- **Quality gates** — `bash scripts/run-all.sh`: contracts, ratchets, instruction budget, tests, lint
- **CI `package` job** — builds the real wheels, checks package-data contents, smoke-installs in a clean venv (package-data drift now fails CI)

### Changed
- `awf/data/role_zones.yaml` (BD-31 zone table) is now shipped in the wheel — PyPI installs no longer silently degrade to the 5-key fallback
- Tool set: 37 MCP tools (5 UI + 32 workflow); docs reference updated to match `server.py`
- Version constants (`awf.__version__`, plugin `__version__`) pinned to pyproject by a drift test; dead `AWF_VERSION` in `bin/awf` removed
- USAGE pipeline examples fixed to the real stage schema (`on_approved`, no computed `kind`); "Where things live" tree matches `awf init` output

### Fixed
- **A-run safety** — queue pinning, non-destructive reconcile, restore net (the run/reconcile data-destruction incident)
- **Dashboard** — XSS (Jinja2 autoescape), port reuse, chat hygiene
- **Network auto-retry** — worker death on API unreachability now retries with backoff instead of hard-stopping
- **`awf_init` R1 warning** — re-init on a live project deletes runtime dirs; docs no longer recommend it as a first-aid measure
- **`killpg` safety** — `pid > 1` guard + tripwire + regression test after the 2026-09-20 incident (a fake pid=1 killed the owner's session three times)
- USAGE troubleshooting brought to fact (orphan cleanup, phase stuck, supervisor timeout `AWF_SUPERVISOR_TIMEOUT` / `awf start --timeout`)
- `protocols/communication.md` v1.2 — handoff naming and signal table match the engine

### Security
- Origin/Referer check on the form server (A2) on top of the 127.0.0.1 binding

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

[1.1.0]: https://github.com/EnerJizeIT/agentic-workflow/releases/tag/v1.1.0
[1.0.0]: https://github.com/EnerJizeIT/agentic-workflow/releases/tag/v1.0.0
[0.4.0]: https://github.com/EnerJizeIT/agentic-workflow/releases/tag/v0.4.0
