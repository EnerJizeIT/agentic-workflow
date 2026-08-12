# Contributing

Thanks for your interest in contributing to agentic-workflow!

## Development setup

```bash
git clone https://github.com/EnerJizeIT/agentic-workflow.git
cd agentic-workflow
# Editable install for development (both packages)
pip install -e ".[dev]"
pip install -e "./agent_workflow_ui[dev]"
```

## Running tests

```bash
# All tests
python -m pytest tests/

# Unit only (fast)
python -m pytest tests/unit/

# With coverage
python -m pytest tests/ --cov=awf --cov=agent_workflow_ui --cov-report=term-missing
```

## Code style

- **Linter:** ruff (`ruff check awf/ tests/ agent_workflow_ui/src/`)
- **Python:** 3.10+ (PEP 604 union types)
- **Line length:** 100 chars
- **Imports:** sorted by isort (via ruff)

## Commit style

```
type(scope): description

type: feat, fix, docs, refactor, test, chore
scope: optional (e.g. dashboard, pipeline, phase)
```

Examples:
```
feat: awf_reject MCP tool
fix: dashboard handoff sort by pipeline order
docs: bilingual README
```

## Pull request process

1. Fork the repo, create a feature branch
2. Write tests for new functionality
3. Ensure `ruff check` and `pytest` pass
4. Create a PR with a clear description

## Adding a new MCP tool

1. Add the async wrapper in `agent_workflow_ui/src/agent_workflow_ui/tools/awf.py`
2. Include `next_action` in the return dict
3. Register in `server.py`
4. Add to the tool list in `test_server_smoke.py`
5. Document in `USAGE.md`

## Adding a new SMO phase

1. Add phase to `ALL_PHASES` in `awf/phase.py`
2. Create template `awf/templates/roles/supervisor/phase-{name}.md`
3. Add detection logic to `detect_phase()`
4. Add to `_NEXT_ACTIONS` in relevant tools
