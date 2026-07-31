# Changelog

All notable changes to this project are documented here.
This project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

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
