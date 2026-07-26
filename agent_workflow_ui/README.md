# agent-workflow-ui

MCP plugin for [opencode](https://opencode.ai/) that gives agents tools for
visual interaction with users: HTML forms for structured input, dashboards
for monitoring.

## Status

**v0.1.0** — MVP development. See [vision/](../vision/) for product docs.

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

## Tools

| Tool | Description |
|---|---|
| `open_form` | Open HTML form in browser |
| `read_submit` | Read user's submit for a form |
| `cancel_form` | Cancel a pending form |
| `list_pending_forms` | List forms awaiting submit |
| `list_templates` | List available form templates |

See [architecture.md](../vision/architecture.md) §6 for full reference.

## SKILL.md setup

**Automatic.** On first plugin start (every opencode session), the plugin copies
its bundled `SKILL.md` to `~/.config/opencode/skills/agent-workflow-ui/SKILL.md`.
The copy is idempotent — overwrites only if the bundled version differs (so
`pip install --upgrade` refreshes the skill automatically).

No manual steps required after `pip install agent-workflow-ui`.

## Coverage

```bash
pip install -e "./agent_workflow_ui[dev]"
python3 -m pytest tests/awf_ui_plugin/ --cov=agent_workflow_ui --cov-report=term-missing
```

Target: ≥80% на plugin code (исключая `__main__.py` и `__init__.py`). Текущее: **93%**.

## Development

```bash
# Clone + install both packages
pip install -e ./awf -e ./agent_workflow_ui

# Run all tests
python3 -m pytest tests/

# Run only plugin tests
python3 -m pytest tests/awf_ui_plugin/
```
