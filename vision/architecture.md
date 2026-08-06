# Архитектура

**Версия:** 1.4 · **Дата:** 2026-08-06

## Обзор

`agent-workflow-ui` — MCP plugin для opencode. 24 typed tools (5 UI + 19 workflow). Plugin импортирует `awf` напрямую (Python import, без subprocess).

```
opencode (supervisor LLM)
  ↕ MCP stdio
agent-workflow-ui (MCP server)
  ↕ Python import
awf.api.* (synchronous functions)
  ↕ subprocess
opencode run (worker agents)
```

## Компоненты

### awf core

| Файл | Ответственность |
|---|---|
| `api/` | Public API (18 функций) — thin wrappers, не бизнес-логика |
| `orchestrator.py` | Pipeline dispatch loop: setup → stage loop → complete |
| `pipeline_engine.py` | Stage handlers: `execute_supervisor_stage`, `execute_agent_stage` |
| `supervisor.py` | Prompts (`build_prompt`), signal detection, snippet injection |
| `agent_stage.py` | Worker spawn (`opencode run`), handoff collection |
| `signal_watch.py` | File-based signal detection (`run_subprocess_until_signal`) |
| `plan_checkpoint.py` | BD-36 checkpoint (one-shot HTTP server + HTML form) |
| `commit_gate.py` | Auto-commit isolation (diff vs baseline) |
| `verify.py` | Test/lint execution, auto-DONE synthesis |
| `pipeline_state.py` | `.agentic/state/current.yaml` persistence |
| `todos.py` | TODO lifecycle: archive, ID generation, active detection |

### agent_workflow_ui plugin

| Файл | Ответственность |
|---|---|
| `server.py` | MCP server (stdio transport), 24 tools registered |
| `tools/awf.py` | 19 awf tool wrappers (async → `asyncio.to_thread`) |
| `tools/forms.py` | 5 UI tool wrappers (open_form, read_submit, ...) |
| `http_endpoint.py` | Long-lived HTTP server for form submits |
| `opencode_config.py` | Model discovery (opencode CLI + opencode.json + opencode.db) |
| `render/` | Template engine (Jinja2 + frontmatter) |

## Pipeline flow

```
1. awf_dispatch_todo → inbox/TODO-NNNN.ready + BASELINE
2. awf_start(background=True) → orchestrator subprocess
3. Stage loop:
   a. plan (supervisor) → finds TODO, checkpoint gate
   b. agent stages → opencode run → DONE/BLOCKED signal
   c. verify (supervisor) → ACK/REVIEW → commit → archive
4. awf_wait_for_event → polls state, returns events
5. Pipeline complete → state cleared, dashboard generated
```

## State management

- `.agentic/state/current.yaml` — structured pipeline state (stage, todo, PID)
- `.agentic/inbox/` — TODO files + signals (ACK, APPROVE)
- `.agentic/outbox/` — worker signals (DONE, BLOCKED, REVIEW)
- `.agentic/done/{todo_id}/` — archived TODOs (after verify approve)
- `.agentic/handoff/` — per-stage handoff files (BD-15)
- `.agentic/context/` — baselines (SHA, tests, env)
- `.agentic/logs/` — orchestrator.log, awf-start.out

## Key design decisions

1. **File-based signal bus.** Workers communicate via files, not IPC. Simple, debuggable.
2. **TODO lifecycle.** `dispatched → in_pipeline → done/`. Archive after verify (`archive_todo`).
3. **Reconcile before start.** `_reconcile()` cleans stale PIDs, deduplicates signals.
4. **Stage-specific snippets.** `build_prompt()` injects focused instructions per stage kind.
5. **Pipeline context.** Workers know their position (stage N of M, scope boundary).
6. **asyncio.to_thread.** All slow MCP tools run in thread pool — event loop stays free.
7. **PR_SET_PDEATHSIG.** Worker subprocesses die with orchestrator (Linux).

## Связанные документы

- [Product Vision](agent-ui-plugin.md)
- [File Bus Protocol](../protocols/communication.md)
- [BACKLOG](../BACKLOG.md)
