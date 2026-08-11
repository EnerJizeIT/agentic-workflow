# Архитектура

**Версия:** 2.0 · **Дата:** 2026-08-11

## Обзор

`agent-workflow-ui` — MCP plugin для opencode. 29 typed tools (5 UI + 24 workflow). Plugin импортирует `awf` напрямую (Python import, без subprocess).

```
opencode (supervisor LLM)
  ↕ MCP stdio
agent-workflow-ui (MCP server, 29 tools)
  ↕ Python import
awf.api.* (synchronous functions)
  ↕ subprocess
opencode run (worker agents)
  ↕ HTTP server (daemon thread)
Dashboard (live polling /api/state)
```

## SMO (State-Machine Orchestration)

Awf ведёт supervisor через детерминированные фазы:

```
init → goal → form → normalize → brief → run → verify → done
```

- `awf/phase.py` — `detect_phase()`, `get_phase_prompt()`, `advance_phase()`
- Каждый tool возвращает `next_action` — компактную инструкцию для следующего шага
- `awf_init` и `awf_load_supervisor_context` возвращают compact phase prompt (не полный supervisor.md)
- Phase templates: `templates/roles/supervisor/phase-{init,goal,form,normalize,brief,run,verify}.md`

## Компоненты

### awf core

| Файл | Ответственность |
|---|---|
| `api/dashboard.py` | Dashboard HTML generation + `generate_state_dict()` JSON API |
| `api/dashboard_server.py` | HTTP server (ThreadingHTTPServer, live polling) |
| `api/lifecycle.py` | `init_project`, `get_status`, `get_report` |
| `api/pipeline.py` | `start_pipeline`, `continue_pipeline`, `kill`, `retry_stage`, `approve`, `reject` |
| `api/dispatch.py` | `dispatch_todo` (atomic: TODO + baseline + signal + pre-check grep) |
| `api/setup.py` | `apply_project_setup` (form materialization → pipeline.yaml + config) |
| `api/context.py` | `load_supervisor_context` (aggregate: vision + plan + phase + state) |
| `orchestrator.py` | Pipeline dispatch loop (thin entry point: setup → stage loop → complete) |
| `pipeline_engine.py` | Stage handlers + transition logic (`execute_supervisor_stage`, `execute_agent_stage`) |
| `phase.py` | SMO: `detect_phase`, `get_phase_prompt`, `advance_phase` |
| `supervisor.py` | Supervisor prompts (`build_prompt`), subprocess execution |
| `agent_stage.py` | Worker spawn (`opencode run`), handoff collection |
| `signal_watch.py` | File-based signal detection (`run_subprocess_until_signal`) |
| `signals.py` | Signal classification, file detection, expected prefixes |
| `transitions.py` | Signal → action resolution (`resolve_transition`) |
| `plan_checkpoint.py` | BD-36 checkpoint (one-shot HTTP server + HTML form) |
| `commit_gate.py` | Auto-commit isolation (diff vs baseline) |
| `verify.py` | Test/lint execution, auto-DONE synthesis |
| `pipeline_state.py` | `.agentic/state/current.yaml` persistence |

### agent_workflow_ui plugin

| Файл | Ответственность |
|---|---|
| `server.py` | MCP server (stdio transport), 29 tools registered |
| `tools/awf.py` | 24 awf tool wrappers (async → `asyncio.to_thread` + `next_action`) |
| `tools/forms.py` | 5 UI tool wrappers (open_form, read_submit, ...) |
| `http_endpoint.py` | Long-lived HTTP server for form submits |
| `opencode_config.py` | Model discovery (opencode CLI + opencode.json + opencode.db) |
| `roles_processor.py` | Role setup: form data → awf.api.apply_project_setup |
| `state.py` | Form state persistence (YAML + submitting TTL recovery) |
| `render/` | Template engine (Jinja2 + frontmatter) |

## Pipeline flow

```
1. awf_dispatch_todo → inbox/TODO-NNNN.ready + BASELINE + pre-check grep
2. awf_start(background=True) → orchestrator subprocess + dashboard HTTP server
3. Stage loop:
   a. plan (supervisor) → finds TODO, checkpoint gate
   b. agent stages → opencode run → DONE/BLOCKED signal
   c. verify (supervisor) → ACK/REVIEW → commit → archive
4. awf_approve → commit + archive + pipeline exits
5. For next TODO: awf_dispatch_todo → awf_start (new pipeline run)
```

## Dashboard v2

HTTP server (daemon thread in orchestrator):
- `GET /` → HTML dashboard (Jinja2 + embedded initial state JSON)
- `GET /api/state` → JSON with all pipeline data (stages, handoffs, TODO, worker, events)
- JS polls `/api/state` every 3s, patches DOM (no page reload)
- Two-panel: pipeline sidebar + content tabs (chat handoffs / TODO content / events)
- Chat handoffs: role icons, chain visualization, rendered markdown
- TODO timeline: `[✅ TODO-0001] ─ [🔄 TODO-0002]`
- Browser notification on verify

## State management

- `.agentic/state/current.yaml` — structured pipeline state (stage, todo, PID, phase)
- `.agentic/state/dashboard_port` — HTTP server port (separate file, survives state overwrites)
- `.agentic/inbox/` — TODO files + signals (ACK, APPROVE)
- `.agentic/outbox/` — worker signals (DONE, BLOCKED, REVIEW)
- `.agentic/done/{todo_id}/` — archived TODOs (after verify approve)
- `.agentic/handoff/` — per-stage handoff files
- `.agentic/context/` — baselines (SHA, tests, env)
- `.agentic/logs/` — orchestrator.log, awf-start.out, agent-*.out

## Key design decisions

1. **SMO (State-Machine Orchestration).** Awf ведёт supervisor по фазам, LLM исполняет шаги. Compact prompt + next_action на каждом шаге.
2. **next_action в каждом tool.** Даже слабые модели (Qwen) следуют короткой инструкции из tool result.
3. **File-based signal bus.** Workers communicate via files, not IPC. Simple, debuggable.
4. **HTTP dashboard.** Live polling через /api/state — без page reload, без file:// CORS.
5. **Pre-dispatch check.** `dispatch_todo` greps codebase для identifiers из TODO — warning если уже реализовано.
6. **TODO rollback.** Если baseline fail → TODO .md удаляется (нет orphan TODOs).
7. **PR_SET_PDEATHSIG.** Worker subprocesses die with orchestrator (Linux).

## Связанные документы

- [Product Vision](agent-ui-plugin.md)
- [Supervisor Flow (SMO)](supervisor-flow.md)
- [File Bus Protocol](../protocols/communication.md)
- [BACKLOG](../BACKLOG.md)
