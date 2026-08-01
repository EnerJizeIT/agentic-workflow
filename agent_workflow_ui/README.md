# agent-workflow-ui

MCP plugin for [opencode](https://opencode.ai/) that gives agents tools for
visual interaction with users (HTML forms, dashboards) AND for driving the
agentic-workflow pipeline (init, start, status, rollback, ...). Depends on
the `awf` package — imports `awf.api` directly (no subprocess).

## Status

**Stable.** MVP complete, used in production dogfooding. See [vision/](../vision/) for product docs and [CHANGELOG.md](../CHANGELOG.md) for history.

## Install (dev)

```bash
pip install -e ./agent_workflow_ui
```

## Run

The plugin is started automatically by opencode when configured in
`~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "agent-workflow-ui": {
      "type": "local",
      "command": ["python3", "-m", "agent_workflow_ui"]
    }
  }
}
```

For manual testing:

```bash
python -m agent_workflow_ui
```

## Tools (18 total: 5 UI + 13 awf)

### UI tools (form lifecycle)

| Tool | Description |
|---|---|
| `open_form` | Open HTML form in browser |
| `read_submit` | Read user's submit for a form |
| `cancel_form` | Cancel a pending form |
| `list_pending_forms` | List forms awaiting submit |
| `list_templates` | List available form templates |

### awf workflow tools (thin wrappers over `awf.api.*`)

| Tool | Description |
|---|---|
| `awf_init` | Create `.agentic/` + assume supervisor role (auto-detects stack) |
| `awf_status` | Current pipeline state + active TODOs + `pipeline_running` + stage visibility |
| `awf_start` | Launch pipeline (background by default, returns PID) |
| `awf_continue` | Resume interrupted pipeline |
| `awf_baseline` | Create git HEAD + tests + env snapshot |
| `awf_rollback` | `git reset` to baseline (hard/soft/dry-run) |
| `awf_approve` | Authorize auto-commit |
| `awf_report` | Summary: task statuses + git diff + test log tail |
| `awf_reset` | Clear runtime data (tasks_only/full/orphans) |
| `awf_add_role` | Generate role template at `.agentic/roles/{name}.md` |
| `awf_analyze_roles` | Detect role overlaps, write disambiguation patches |
| `awf_dispatch_todo` | **Atomic TODO + baseline + signal** (1 call = ready to run) |
| `awf_load_supervisor_context` | **One-shot aggregate** (vision + plan + roles + status) |

See [architecture.md](../vision/architecture.md) §6 for full reference and
[README.md](../README.md) §"MCP-MIGRATION" for the architectural rationale.

## SKILL.md setup

**Automatic.** On first plugin start (every opencode session), the plugin copies
its bundled `SKILL.md` to `~/.config/opencode/skills/agent-workflow-ui/SKILL.md`.
The copy is idempotent — overwrites only if the bundled version differs (so
`pip install --upgrade` refreshes the skill automatically).

No manual steps required after `pip install agent-workflow-ui`.

## Coverage

```bash
pip install -e "./agent_workflow_ui[dev]"
python3 -m pytest tests/agent_workflow_ui/ --cov=agent_workflow_ui --cov-report=term-missing
```

Target: ≥80% на plugin code (исключая `__main__.py` и `__init__.py`). Текущее: **90%**.

## Development

```bash
# Clone + install both packages
pip install -e ./awf -e ./agent_workflow_ui

# Run all tests
python3 -m pytest tests/

# Run only plugin tests
python3 -m pytest tests/agent_workflow_ui/
```
