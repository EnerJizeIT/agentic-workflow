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
