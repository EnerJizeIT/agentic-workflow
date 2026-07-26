# Changelog

All notable changes to this project are documented here.
This project follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.0] — 2026-07-25

### awf

**Bash → Python migration complete.**

All 9 CLI commands now route through Python (the `awf/` package). The bash
`lib/*.sh` modules (10 files, ~1700 lines) are removed. `bin/awf` is now a
36-line thin wrapper that delegates to `python3 -m awf`.

- **Closes #1**: `bin/awf` resolves symlinks via `os.path.realpath` — install via `ln -s` works natively.
- **`awf/` Python package**: 22 modules, ~2100 lines (cli, orchestrator, pipeline, signals, transitions, verify, git_utils, todos, cmd_*, opencode_agents).
- **163 tests**: 11 E2E (subprocess through bin/awf) + 152 unit (all awf/ modules).

### agent-workflow-ui

**v0.1.0 (MVP) — HTML forms plugin for opencode.**

Standalone MCP plugin that gives the supervisor agent tools for visual
interaction with users: HTML forms for structured input, dashboards for
monitoring.

- 5 MCP tools: `open_form`, `read_submit`, `cancel_form`, `list_pending_forms`, `list_templates`.
- HTTP endpoint for form submits (atomic YAML writes).
- Jinja2 rendering with YAML frontmatter.
- 5 default templates: `role-assignment`, `skill-picker`, `model-picker`, `pipeline-picker`, `conflict-resolver`.
- Cross-platform browser open (`xdg-open`/`open`/`explorer`).
- 89 tests, 99% coverage.

See [`vision/agent-ui-plugin.md`](vision/agent-ui-plugin.md) for product vision,
[`vision/architecture.md`](vision/architecture.md) for technical architecture.

## [0.3.x] — migration waves (2026-07-25)

- v0.3.4 — weak-spots closure (find_active_todo, status warns, reset --orphans).
- v0.3.5 — pytest E2E harness + mock opencode stub.
- v0.3.6 — Wave 4a: `awf status` ported to Python.
- v0.3.7 — Wave 4b: orchestrator ported (8 modules).
- v0.3.8 — Wave 4c: 6 remaining commands ported.
- v0.3.9 — Wave 4d-part1: 152 unit tests.
- v0.4.0 — Wave 4d-part2: bash retired.

## [0.3.0] — pipeline + signals + retry

- YAML pipeline parsing.
- Signal lifecycle (DONE/BLOCKED/REVIEW-APPROVED/...).
- Retry with supervisor escalation.
- `--pipeline`, `--from-stage`, `--auto`, `--timeout` flags.

## [0.2.0] — roles + templates

- supervisor/worker/reviewer/tester role templates.
- todo-template.md with Mode A/B/C.
- 3-Strike Error Protocol.
- Session recovery from PROGRESS-{NNNN}.md.

[Unreleased]: https://github.com/EnerJizeIT/agentic-workflow/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/EnerJizeIT/agentic-workflow/releases/tag/v0.4.0
