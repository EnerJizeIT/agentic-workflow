"""Templates used by :func:`awf.api.init_project` and :func:`awf.api.add_role`.

Single quotes used for YAML command values so that commands containing
double quotes (e.g. ``pytest -k "not slow"``) don't break YAML parsing.
"""
from __future__ import annotations

from pathlib import Path

_CONFIG_TEMPLATE = '''project:
  name: '{project_name}'
  root: "."

models:
  supervisor:
    description: "Current session model"
# Agent roles (system-analyst, developer, qa, project-auditor, ...) are added
# dynamically by the project-setup form on first submit (BD-12 auto-maps each
# role to agent_name="worker"). Don't pre-populate here.

verification:
  test_cmd: '{test_cmd}'
  lint_cmd: '{lint_cmd}'
  typecheck_cmd: '{typecheck_cmd}'
  build_cmd: '{build_cmd}'
  coverage_cmd: ""

phases:
  current: ".agentic/phases/plan.md"

default_pipeline: "default"

retry:
  max_attempts: 3
  backoff_seconds: 0
'''


_ROLE_TEMPLATE = """# ROLE: {role_name}

**Role:** {description}
**Runs as:** `opencode run --agent {role_name}`
**Model:** {model}

## 1. Who you are

<describe the responsibility of this role>

## 2. Input

This role receives:
- <what comes from the previous stage>

## 3. Actions

<describe what to do with the input>

## 4. Output

When finished, create one of:
- `.agentic/outbox/APPROVED-{{NNNN}}.md` + `.ready` — success
- `.agentic/outbox/REJECTED-{{NNNN}}.md` + `.ready` — needs fixes
- `.agentic/outbox/BLOCKED-{{NNNN}}.md` + `.ready` — needs supervisor

## 5. Prohibitions

<list what this role must NOT do>
"""


def update_gitignore(project_dir: Path) -> None:
    """Idempotent: add .agentic runtime dirs to .gitignore.

    Two checks: first for the main runtime block (inbox/outbox/context/...),
    second for the plugin's inputs/dashboards dirs. Avoids duplicating either.
    """
    gitignore_block = (
        ".agentic/inbox/\n.agentic/outbox/\n.agentic/context/\n"
        ".agentic/logs/\n.agentic/reports/\n"
        ".agentic/inputs/\n.agentic/dashboards/\n"
    )
    gitignore = project_dir / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if ".agentic/inbox/" not in content:
            gitignore.write_text(
                content + "\n# Agentic workflow runtime files\n" + gitignore_block + "\n",
                encoding="utf-8",
            )
        elif ".agentic/inputs/" not in content:
            gitignore.write_text(
                content + "\n# agent-workflow-ui runtime\n.agentic/inputs/\n.agentic/dashboards/\n",
                encoding="utf-8",
            )
    else:
        gitignore.write_text(
            "# Agentic workflow runtime files\n" + gitignore_block + "\n",
            encoding="utf-8",
        )


__all__ = ["_CONFIG_TEMPLATE", "_ROLE_TEMPLATE", "update_gitignore"]
