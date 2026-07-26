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
      "command": "python",
      "args": ["-m", "agent_workflow_ui"]
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

## SKILL.md setup (for opencode)

After installing the plugin, copy SKILL.md to opencode's skills directory so
the supervisor agent knows when and how to use forms:

```bash
mkdir -p ~/.config/opencode/skills/agent-workflow-ui
cp agent_workflow_ui/SKILL.md ~/.config/opencode/skills/agent-workflow-ui/SKILL.md
```

Opencode will automatically load the skill at session start.

## Coverage

```bash
pip install -e "./agent_workflow_ui[dev]"
python3 -m pytest tests/awf_ui_plugin/ --cov=agent_workflow_ui --cov-report=term-missing
```

Target: ≥95% on plugin code (excluding `__main__.py` and `__init__.py`).

## Development

```bash
# Clone + install both packages
pip install -e ./awf -e ./agent_workflow_ui

# Run all tests
python3 -m pytest tests/

# Run only plugin tests
python3 -m pytest tests/awf_ui_plugin/
```
