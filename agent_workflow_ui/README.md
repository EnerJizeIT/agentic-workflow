# agent-workflow-ui

MCP plugin для opencode. 48 typed tools (5 UI + 43 workflow).

## Установка

```bash
pip install agent-workflow-ui
```

Plugin автоматически регистрируется в opencode через `awf init`.

## Tools

### UI (5)
`open_form`, `read_submit`, `cancel_form`, `list_pending_forms`, `list_templates`

### Workflow (43)
`awf_add_role`, `awf_analyze_roles`, `awf_approve`, `awf_baseline`, `awf_brief`, `awf_check_model_config`, `awf_confirm_normalized`, `awf_continue`, `awf_current_step`, `awf_dispatch_todo`, `awf_feedback`, `awf_init`, `awf_kill`, `awf_load_supervisor_context`, `awf_metrics`, `awf_open_increment_planning_form`, `awf_open_pipeline_dashboard`, `awf_open_project_setup_form`, `awf_pipelines`, `awf_prove_red`, `awf_reject`, `awf_report`, `awf_reset`, `awf_restore`, `awf_retry_stage`, `awf_rollback`, `awf_run_finish`, `awf_run_next`, `awf_run_note`, `awf_run_revise`, `awf_run_start`, `awf_run_status`, `awf_set_goal`, `awf_start`, `awf_status`, `awf_todo_remove`, `awf_todo_retire`, `awf_todo_update`, `awf_tree_sha`, `awf_unblock`, `awf_verify_pack`, `awf_wait_for_event`, `awf_write_pipeline`

Все workflow tools возвращают `next_action` — guide для supervisor.

## Структура

```
src/agent_workflow_ui/
├── server.py              # MCP server, 48 tools
├── tools/registry.py      # единый реестр registry.TOOLS (48 = 5 UI + 43 workflow)
├── tools/awf.py           # 42 awf wrappers (asyncio.to_thread + next_action)
├── tools/forms.py         # 5 UI wrappers
├── http_endpoint.py       # Form HTTP server
├── opencode_config.py     # Model discovery
├── roles_processor.py     # Role setup materialization
├── state.py               # Form state persistence
└── render/                # Jinja2 templates
```

Все workflow tools — thin async wrappers над `awf.api.*()`. Бизнес-логика в `awf`.
