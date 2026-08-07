# agentic-workflow

Pipeline-оркестратор для AI-агентов в opencode. Два пакета: `awf` (Python core) + `agent-workflow-ui` (MCP plugin).

## Что это

Supervisor (LLM) планирует → worker-агенты выполняют → supervisor проверяет → коммит. Pipeline управляется через MCP tools — без shell-команд.

```
User → opencode (supervisor LLM)
           ↓ MCP tools
     agent-workflow-ui plugin (24 tools)
           ↓ Python import
     awf orchestrator → opencode run (worker agents)
```

## Структура

```
awf/                        # Python core
├── api/                    # Public API (18 functions)
├── orchestrator.py         # Pipeline dispatch loop
├── pipeline_engine.py      # Stage handlers (supervisor/agent)
├── supervisor.py           # Supervisor prompts + signals
├── agent_stage.py          # Worker spawn + handoff
├── signal_watch.py         # File-based signal detection
├── plan_checkpoint.py      # BD-36 checkpoint (HTML form)
├── pipeline_state.py       # State persistence
├── commit_gate.py          # Auto-commit isolation
├── verify.py               # Test/lint verification
├── dashboard.py            # Dashboard generation
└── templates/              # Jinja2 (dashboard)
agent_workflow_ui/          # MCP plugin
├── tools/awf.py            # 19 awf MCP tool wrappers
├── tools/forms.py          # 5 UI MCP tool wrappers
├── http_endpoint.py        # HTML form server
├── opencode_config.py      # Model discovery
└── render/                 # Template engine
templates/roles/supervisor.md  # Supervisor role instructions
tests/                      # unit + integration + e2e
```

## 24 MCP tools

**UI (5):** `open_form`, `read_submit`, `cancel_form`, `list_pending_forms`, `list_templates`

**Workflow (19):** `awf_init`, `awf_status`, `awf_start`, `awf_continue`, `awf_kill`, `awf_baseline`, `awf_rollback`, `awf_approve`, `awf_report`, `awf_reset`, `awf_add_role`, `awf_analyze_roles`, `awf_dispatch_todo`, `awf_load_supervisor_context`, `awf_open_project_setup_form`, `awf_open_increment_planning_form`, `awf_open_pipeline_dashboard`, `awf_wait_for_event`, `awf_check_model_config`

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

## Документы

- [BACKLOG.md](BACKLOG.md) — открытые задачи
- [vision/architecture.md](vision/architecture.md) — архитектура
- [vision/agent-ui-plugin.md](vision/agent-ui-plugin.md) — product vision
- [protocols/communication.md](protocols/communication.md) — file bus protocol
