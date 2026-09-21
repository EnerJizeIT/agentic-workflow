# agent-workflow-ui

MCP plugin для opencode. 37 typed tools (5 UI + 32 workflow).

## Установка

```bash
pip install agent-workflow-ui
```

Plugin автоматически регистрируется в opencode через `awf init`.

## Tools

### UI (5)
`open_form`, `read_submit`, `cancel_form`, `list_pending_forms`, `list_templates`

### Workflow (32)
`awf_init`, `awf_status`, `awf_start`, `awf_continue`, `awf_kill`, `awf_retry_stage`, `awf_baseline`, `awf_rollback`, `awf_approve`, `awf_reject`, `awf_report`, `awf_reset`, `awf_add_role`, `awf_analyze_roles`, `awf_dispatch_todo`, `awf_load_supervisor_context`, `awf_open_project_setup_form`, `awf_open_increment_planning_form`, `awf_open_pipeline_dashboard`, `awf_wait_for_event`, `awf_check_model_config`, `awf_current_step`, `awf_set_goal`, `awf_confirm_normalized`, `awf_run_start`, `awf_run_status`, `awf_run_next`, `awf_run_finish`, `awf_run_note`, `awf_restore`, `awf_prove_red`, `awf_verify_pack`

Все workflow tools возвращают `next_action` — guide для supervisor.

## Структура

```
src/agent_workflow_ui/
├── server.py              # MCP server, 37 tools
├── tools/awf.py           # 32 awf wrappers (asyncio.to_thread + next_action)
├── tools/forms.py         # 5 UI wrappers
├── http_endpoint.py       # Form HTTP server
├── opencode_config.py     # Model discovery
├── roles_processor.py     # Role setup materialization
├── state.py               # Form state persistence
└── render/                # Jinja2 templates
```

Все workflow tools — thin async wrappers над `awf.api.*()`. Бизнес-логика в `awf`.
