# Changelog

All notable changes to this project are documented here.
This project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### DASH — Pipeline dashboard + supervisor wake-up

Scenario 4 (long-running monitoring) — complete. Three phases:

**Phase 1: Prototype** — standalone HTML mock (approved design):
dark theme (VS Code palette), CSS animations (breathe, pulse-ring,
slideIn, spin-border), progressive disclosure (header → pipeline flow →
expandable cards: events stream, handoffs, task progress).

**Phase 2: Dashboard integration** (`37e5870`):
- `awf/templates/dashboard.html.j2` — Jinja2 template (auto-refresh 5s)
- `awf/api/dashboard.py` — `generate_dashboard(project_dir)` reads T4.1
  state + pipeline.yaml + handoffs + log events + task progress → renders
- Orchestrator hook: regenerate after each `write_state()` (stage transition)
- MCP tool `awf_open_pipeline_dashboard(project_dir)` — supervisor asks
  user before opening

**Phase 3: Supervisor wake-up** (`0d9d99c`):
- `awf/api/wait_event.py` — `wait_for_event(project_dir, timeout=120)`
  Single blocking call replaces sleep+status polling loops. Returns
  immediately on: verify stage reached, BLOCKED signal, checkpoint
  pending, pipeline done, or timeout.
- MCP tool `awf_wait_for_event(project_dir?, timeout=120)` — ONE tool
  call with ONE response vs N sleep+status cycles.
- Token savings: supervisor burns tokens on verify work, not idle polling.

**Also in this release:**
- T4.1 Pipeline state persistence — `.agentic/state/current.yaml` replaces
  regex log parsing (structured source of truth, regex as fallback).
- Dogfood-9 structural triggers: `increment_planning_needed` flag,
  `final_stage_commit_policy` visibility, REVIEW restart guidance,
  foreground+BD-36 incompatibility warning.
- Dogfood-8 A1 commit isolation: untracked files now included in auto-commit.
- Dogfood-7 Increment planning: `awf_open_increment_planning_form` — user
  picks decomposition variant, persisted to plan.md.
- Quick wins: T1.6 _reset_orphans removed, T2.8 propose() → Enum,
  T3.4 _ZONES extracted to data file.
- QA fixes: atomic writes (T2.6), DB connection leak, test false-positives
  documented.

23 MCP tools total (5 UI + 18 awf). 1067 tests. ruff clean.

## [Unreleased]

### DF5/DF6 — Dogfood v5/v6 fixes: lifecycle, reliability, supervisor autonomy

**DF6-1..4: TODO lifecycle management**
- `archive_todo()`: after verify approve, moves inbox/outbox/handoff → `done/{id}/`
- `_reconcile()`: before each background start — clears stale PID, dedup ACK+APPROVE, supersede old TODOs
- BD-30 orphan pickup checks `done/` — no false positive on archived TODOs
- `done_count` counts from `done/` directory

**DF6-5..8: pipeline reliability**
- `AWF_BACKGROUND_CHILD=1` env: checkpoint works in background mode (no noop crash)
- Dashboard `_determine_status`: detects dead PID → shows "Pipeline process dead"
- `PR_SET_PDEATHSIG`: worker subprocess dies with orchestrator (Linux)
- Salvage path: `SALVAGE-{todo_id}.md` in inbox + `salvage_needed` in state + `wait_for_event` event_type="salvage"

**DF5-12 fix: MCP event loop blocking**
- `wait_for_event` and `start_pipeline` wrapped in `asyncio.to_thread()`
- Event loop free during 30s wait — other MCP tools respond normally

**Stage-specific snippet injection**
- `build_prompt()` appends focused instructions per stage (plan/verify/salvage)
- `_SNIPPET_ALWAYS` + stage snippet at END of prompt (recency bias)
- `kind="salvage"` separate from `kind="verify"` (different context)

**Other improvements**
- `wait_for_event` poll interval: 10s → 3s
- `awf_status`: `salvage_needed`, `salvage_stage`, `expected_action` fields
- Dashboard: `completed_todos` from `done/` directory
- `supervisor.md`: Quick Reference (5 imperatives at top)
- Concurrency regression test (3 tests)
- 61 new tests: lifecycle (16), reconcile (10), integration (4), snippets (28), concurrency (3)

### MCP-MIGRATION — awf как pure MCP toolkit под opencode

Architectural pivot: awf больше НЕ позиционируется как standalone CLI.
Все команды awf доступны opencode-агенту как MCP tools в plugin'е
`agent-workflow-ui`. Plugin импортирует `awf` напрямую (без subprocess).

**MCP-1: `awf/api/` package** (refactor of 1446-line god module):
- Split на 10 focused submodules: `_errors`, `_results`, `_stack`,
  `_templates`, `_helpers`, `_background`, `lifecycle`, `pipeline`, `roles`.
- 11 typed Result dataclasses с `as_dict()` для MCP JSON serialization.
- 11 public functions, все принимают `str | Path` для project_dir.
- `detect_stack()` — auto-detect test/lint/typecheck/build из package.json,
  pyproject.toml, Cargo.toml, go.mod, file-heuristic.
- `derive_project_name()` — Title Case из имени директории.
- Public surface неизменен: `from awf import api; api.init_project(...)`.

**MCP-2: plugin depends on awf** — `agent_workflow_ui/pyproject.toml`
добавлен `awf>=0.4.0` в dependencies. Plugin импортирует `awf.api` напрямую.

**MCP-3: 11 MCP tools** в `tools/awf.py` (thin async wrappers над api):
`awf_init`, `awf_status`, `awf_start`, `awf_continue`, `awf_baseline`,
`awf_rollback`, `awf_approve`, `awf_report`, `awf_reset`, `awf_add_role`,
`awf_analyze_roles`. Uniform contract: `{status: "ok"|"error", ...}`.

**MCP-4: long-running pipeline coordination**:
- `awf_start(background=True)` → PID файл (`.agentic/logs/awf-start.pid`).
- `awf_status` детектит running pipeline: `pipeline_running`, `pipeline_pid`,
  `log_tail` (последние 20 строк).
- Stale PID files автоматически очищаются.

**MCP-5: global AGENTS.md** — `~/.config/opencode/AGENTS.md` расширен
блоком "Agentic Workflow (awf) — MCP Toolkit" с описанием всех 16 tools
и workflow recipes.

**MCP-6: `awf init --non-interactive`** — детерминированный init без
prompts (stack-detect + name-from-dir). Интерактивный путь сохранён для
human CLI users.

**MCP-audit cleanup (post-review):**
- `analyze_roles` refactor: extracted `analyze_roles_core()` pure function
  (no stdout capture, no emoji parsing). CLI/MCP wrappers use structured data.
- Two-step orphan protocol: `list_orphans()` (read-only) +
  `remove_orphans(ids)` (explicit). Replaces double computation in cmd_reset.
- `cmd_start._run_in_background` unified with `api.start_pipeline` —
  single source of truth for background path.
- `init_project` plan.md write now atomic (H5 invariant restored).
- `_CONFIG_TEMPLATE` switched to single quotes (YAML safety for commands
  containing double quotes, e.g. `pytest -k "not slow"`).
- `approve_commit` now requires `.agentic/` (consistency with other api fns).
- `_read_ack` returns `None` instead of literal `"none"` string.

### awf — architectural debt sprint (A1-A10, BD-25..35)

**A6 refactor:** `orchestrator.py` split from 1432 → 362 lines into focused modules:
`_log.py`, `_env.py`, `signal_watch.py`, `supervisor.py`, `agent_stage.py`,
`commit_gate.py`, `plan_progress.py`, `xdg.py`.

**Pipeline improvements:**
- **BD-29:** kind-based pipeline (plan/execute/verify computed from position,
  not action field). Removed hardcoded role registry (skills_contract.py).
- **BD-30:** interactive supervisor = current opencode (not subprocess).
  `--background` no longer forces `--auto`. awf prints instructions and
  waits for signal file.
- **BD-31:** `awf analyze-roles` — finds role overlaps, adds disambiguation
  patches.
- **BD-32:** per-role model selection in `project-setup` form — выбор
  сохраняется в `config.yaml` как `models.<role>.model`.
- **BD-33:** auto-mark `[x]` in `phases/plan.md` after verify.
- **BD-34:** auto progress report after pipeline completes.

**Architectural debt (A-series, all CLOSED):**
- **A1:** git commit isolation — только файлы из baseline diff (was `git add -A`).
- **A2:** CSRF protection — Origin/Referer whitelist.
- **A3:** `_ack_page` → Jinja2 template (`ack.html.j2`).
- **A4:** temp HTML cleanup — `cleanup_temp_files()` removes stale files.
- **A5:** auto-DONE preserves baseline gate (via A1 isolation).
- **A6:** orchestrator.py split into focused modules.
- **A7:** removed legacy `_check_skill_drift` / `cmd_normalize`.
- **A8:** plugin detection via `find_spec` (was `import_module`).
- **A9:** XDG_CONFIG_HOME support everywhere.
- **A10:** FormRegistry persists to `forms_registry.yaml`.

**Bug fixes:**
- **BD-22:** snapshot-based signal detection — stale signals ignored.
- **BD-25:** strip `OPENCODE_SERVER*` env vars (no attach to running serve).
- **BD-27:** skills dropdown replaces role-name guessing — skill content
  goes directly into `.agentic/roles/<role>.md`.
- **BD-28:** removed `templates/pipelines/` and `templates/roles/{worker,reviewer,tester}.md`.

**Tests:** 628 (was 510). Coverage 90%+ на plugin. ruff clean.

## [0.4.0] — 2026-07-25

### awf

**Bash → Python migration complete.**

All 9 CLI commands now route through Python (the `awf/` package). The bash
`lib/*.sh` modules (10 files, ~1700 lines) are removed. `bin/awf` is now a
36-line thin wrapper that delegates to `python3 -m awf`.

- **Closes #1**: `bin/awf` resolves symlinks via `os.path.realpath`.
- **`awf/` Python package**: 22 modules, ~2100 lines.
- **163 tests**: 11 E2E + 152 unit.

### agent-workflow-ui

**v0.1.0 (MVP) — HTML forms plugin for opencode.**

- 5 MCP tools: `open_form`, `read_submit`, `cancel_form`, `list_pending_forms`, `list_templates`.
- HTTP endpoint for form submits (atomic YAML writes).
- Jinja2 rendering with YAML frontmatter.
- Composite template `project-setup` (primary for MVP).
- Cross-platform browser open.
- 89 tests, 99% coverage.

## [0.3.x] — migration waves (2026-07-25)

- v0.3.4 — weak-spots closure.
- v0.3.5 — pytest E2E harness + mock opencode stub.
- v0.3.6 — Wave 4a: `awf status` ported.
- v0.3.7 — Wave 4b: orchestrator ported.
- v0.3.8 — Wave 4c: 6 remaining commands ported.
- v0.3.9 — Wave 4d-part1: 152 unit tests.
- v0.4.0 — Wave 4d-part2: bash retired.

## [0.3.0] — pipeline + signals + retry

- YAML pipeline parsing.
- Signal lifecycle (DONE/BLOCKED/REVIEW-APPROVED/...).
- Retry with supervisor escalation.

## [0.2.0] — roles + templates

- supervisor/worker/reviewer/tester role templates.
- todo-template.md with Mode A/B/C.
- 3-Strike Error Protocol.
- Session recovery from PROGRESS-{NNNN}.md.

[Unreleased]: https://github.com/EnerJizeIT/agentic-workflow/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/EnerJizeIT/agentic-workflow/releases/tag/v0.4.0
