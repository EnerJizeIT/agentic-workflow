# agentic-workflow

Pipeline-оркестратор для AI-агентов в opencode. Два пакета: `awf` (Python core) + `agent-workflow-ui` (MCP plugin).

## Что это

Supervisor (LLM) планирует → worker-агенты выполняют → supervisor проверяет → коммит. Pipeline управляется через MCP tools — без shell-команд.

```
User → opencode (supervisor LLM)
           ↓ MCP tools (29 typed tools)
     agent-workflow-ui plugin
           ↓ Python import
     awf orchestrator → opencode run (worker agents)
           ↓ HTTP server
     Dashboard (live polling, chat handoffs)
```

## Структура

```
awf/                           # Python core
├── api/                       # Public API
│   ├── dashboard.py           # Dashboard generation + state JSON
│   ├── dashboard_server.py    # HTTP server (live polling)
│   ├── lifecycle.py           # init, status, report
│   ├── pipeline.py            # start, continue, kill, retry, approve, reject
│   ├── dispatch.py            # TODO dispatch + pre-check grep
│   ├── setup.py               # Project setup materialization
│   └── context.py             # Supervisor context aggregate
├── orchestrator.py            # Pipeline dispatch loop (thin entry point)
├── pipeline_engine.py         # Stage handlers + transition logic
├── phase.py                   # SMO: detect_phase, get_phase_prompt, advance_phase
├── supervisor.py              # Supervisor prompts + signals
├── agent_stage.py             # Worker spawn + handoff collection
├── signal_watch.py            # File-based signal detection
├── signals.py                 # Signal classification + file detection
├── transitions.py             # Signal → action resolution
├── plan_checkpoint.py         # BD-36 checkpoint (HTML form)
├── commit_gate.py             # Auto-commit isolation
├── verify.py                  # Test/lint verification
├── pipeline_state.py          # State persistence
├── todos.py                   # TODO lifecycle
└── templates/                 # Jinja2 (dashboard v2, supervisor phases)
agent_workflow_ui/             # MCP plugin
├── src/agent_workflow_ui/
│   ├── server.py              # MCP server (29 tools)
│   ├── tools/awf.py           # 24 awf wrappers + next_action guidance
│   ├── tools/forms.py         # 5 UI wrappers
│   ├── http_endpoint.py       # HTML form server
│   ├── opencode_config.py     # Model discovery
│   ├── roles_processor.py     # Role setup materialization
│   ├── state.py               # Form state persistence
│   └── render/                # Template engine (Jinja2 + frontmatter)
templates/roles/supervisor/    # Phase templates (_core, phase-{init,goal,form,normalize,brief,run,verify})
tests/                         # unit + integration + e2e (1000+ tests)
```

## 29 MCP tools

**UI (5):** `open_form`, `read_submit`, `cancel_form`, `list_pending_forms`, `list_templates`

**Workflow (24):**
- **Lifecycle:** `awf_init`, `awf_status`, `awf_report`, `awf_reset`
- **Pipeline:** `awf_start`, `awf_continue`, `awf_kill`, `awf_retry_stage`
- **TODO:** `awf_dispatch_todo` (pre-check grep), `awf_baseline`, `awf_rollback`
- **Verify:** `awf_approve`, `awf_reject`, `awf_wait_for_event`
- **SMO:** `awf_current_step`, `awf_set_goal`, `awf_confirm_normalized`
- **Setup:** `awf_open_project_setup_form`, `awf_open_increment_planning_form`
- **Context:** `awf_load_supervisor_context`, `awf_open_pipeline_dashboard`
- **Roles:** `awf_add_role`, `awf_analyze_roles`
- **Config:** `awf_check_model_config`

Все workflow tools возвращают `next_action` — компактную инструкцию для supervisor (даже слабые модели следуют за ней).

## SMO (State-Machine Orchestration)

Awf ведёт supervisor по фазам через детерминированные переходы:

```
init → goal → form → normalize → brief → run → verify → done
```

Каждая фаза: compact prompt (~50 строк) + phase-aware `next_action` в каждом tool result. Supervisor не читает 600-строчный промт — он идёт за `next_action`.

## Установка

Editable-only (`-e`). Wheel-дистрибуция не поддерживается — package-data и пути к шаблонам рассчитаны на исходную структуру каталогов.

```bash
pip install -e ".[dev]"
pip install -e "./agent_workflow_ui[dev]"
```

## Команды

```bash
ruff check awf/ tests/ agent_workflow_ui/src/agent_workflow_ui/   # lint
python -m pytest tests/                                            # тесты
```

## Dashboard v2

HTTP server на случайном порту (стартует оркестратором):
- `GET /` — HTML dashboard
- `GET /api/state` — JSON (stages, handoffs, TODO content, worker, events)
- JS polling каждые 3с — **без перезагрузки страницы**
- Two-panel layout: pipeline sidebar + content tabs (chat / TODO / events)
- Chat-style handoffs с chain visualization
- TODO timeline
- Browser notification на verify

## Документы

- [BACKLOG.md](BACKLOG.md) — открытые задачи
- [vision/architecture.md](vision/architecture.md) — архитектура
- [vision/agent-ui-plugin.md](vision/agent-ui-plugin.md) — product vision
- [vision/supervisor-flow.md](vision/supervisor-flow.md) — SMO flow (implemented)
- [protocols/communication.md](protocols/communication.md) — file bus protocol
