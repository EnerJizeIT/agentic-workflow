# agent-workflow-ui

MCP plugin для opencode. 24 typed tools (5 UI + 19 workflow).

## Установка

```bash
pip install -e ".[dev]"
```

Plugin автоматически регистрируется в opencode через `awf init`.

## Tools

### UI (5)
`open_form`, `read_submit`, `cancel_form`, `list_pending_forms`, `list_templates`

### Workflow (19)
`awf_init`, `awf_status`, `awf_start`, `awf_continue`, `awf_kill`, `awf_baseline`, `awf_rollback`, `awf_approve`, `awf_report`, `awf_reset`, `awf_add_role`, `awf_analyze_roles`, `awf_dispatch_todo`, `awf_load_supervisor_context`, `awf_open_project_setup_form`, `awf_open_increment_planning_form`, `awf_open_pipeline_dashboard`, `awf_wait_for_event`, `awf_check_model_config`

## Структура

```
src/agent_workflow_ui/
├── server.py          # MCP server, 24 tools
├── tools/awf.py       # 19 awf wrappers (asyncio.to_thread)
├── tools/forms.py     # 5 UI wrappers
├── http_endpoint.py   # Form HTTP server
├── opencode_config.py # Model discovery
└── render/            # Jinja2 templates
```

Все workflow tools — thin async wrappers над `awf.api.*()`. Бизнес-логика в `awf`.
