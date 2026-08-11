# agent-workflow-ui

MCP plugin для opencode. 29 typed tools (5 UI + 24 workflow).

## Установка

```bash
pip install -e ".[dev]"
```

Plugin автоматически регистрируется в opencode через `awf init`.

## Tools

### UI (5)
`open_form`, `read_submit`, `cancel_form`, `list_pending_forms`, `list_templates`

### Workflow (24)
`awf_init`, `awf_status`, `awf_start`, `awf_continue`, `awf_kill`, `awf_retry_stage`, `awf_baseline`, `awf_rollback`, `awf_approve`, `awf_reject`, `awf_report`, `awf_reset`, `awf_add_role`, `awf_analyze_roles`, `awf_dispatch_todo`, `awf_load_supervisor_context`, `awf_open_project_setup_form`, `awf_open_increment_planning_form`, `awf_open_pipeline_dashboard`, `awf_wait_for_event`, `awf_check_model_config`, `awf_current_step`, `awf_set_goal`, `awf_confirm_normalized`

Все workflow tools возвращают `next_action` — guide для supervisor.

## Структура

```
src/agent_workflow_ui/
├── server.py              # MCP server, 29 tools
├── tools/awf.py           # 24 awf wrappers (asyncio.to_thread + next_action)
├── tools/forms.py         # 5 UI wrappers
├── http_endpoint.py       # Form HTTP server
├── opencode_config.py     # Model discovery
├── roles_processor.py     # Role setup materialization
├── state.py               # Form state persistence
└── render/                # Jinja2 templates
```

Все workflow tools — thin async wrappers над `awf.api.*()`. Бизнес-логика в `awf`.
