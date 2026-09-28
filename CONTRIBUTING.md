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

# Quality gates — exactly what CI runs: contracts, ratchet, instruction
# budget, the full suite and ruff.
bash scripts/run-all.sh
```

## Coverage

Coverage is ratcheted: it can rise or hold, a drop below the baseline
fails. The gate (`scripts/coverage.sh`) measures TOTAL + one number per
M1-M5 module and compares against `.coverage-baseline`:

```bash
bash scripts/coverage.sh                # check (what CI runs)
bash scripts/coverage.sh show           # current vs baseline, no verdict
bash scripts/coverage.sh --update "why" # re-measure and raise the baseline
```

It runs the fast subset — `tests/unit` + `tests/agent_workflow_ui` +
`tests/negative` (the directories that carry the tracked modules'
coverage). A second full-suite run would add ~6.5 min to CI; the full
suite still runs uninstrumented in the Quality gates step, so the subset
only measures — it does not replace any functional check.

`--update` only raises. A measured drop is refused (fix the code first),
and the reason goes into the baseline file itself. In CI the gate is the
"Coverage ratchet" step of the test job.

## Code style

- **Linter:** ruff (`ruff check tests/ awf/ agent_workflow_ui/src/agent_workflow_ui/`)
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
3. Register in `tools/registry.py` — the single source `create_server()` reads
4. Add to `expected` in `test_server_smoke.py` (the smoke pins the registry)
5. Document in `USAGE.md` (the docs counters test checks the numbers)

## Adding a new SMO phase

1. Add phase to `ALL_PHASES` in `awf/phase.py`
2. Create template `awf/templates/roles/supervisor/phase-{name}.md`
3. Add detection logic to `detect_phase()`
4. Add to `_NEXT_ACTIONS` in relevant tools
