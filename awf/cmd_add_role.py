"""Port of lib/add-role.sh — ``awf add-role`` command."""
from __future__ import annotations

from pathlib import Path
from typing import Any

ROLE_TEMPLATE = '''# ROLE: {role_name}

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
'''


def run(args: Any) -> int:
    """Execute ``awf add-role`` and return exit code."""
    role_name = args.name
    description = getattr(args, "description", "") or ""
    model = getattr(args, "model", "") or ""

    if not model:
        model = input(f"Model id for role '{role_name}' (e.g. claude-sonnet-4-20250514, gpt-4.1): ").strip()
        if not model:
            model = "<set-me-in-.agentic/config.yaml>"

    agentic = Path(".agentic")
    if not agentic.is_dir():
        print("No .agentic/ found. Run 'awf init' first.")
        return 1

    roles_dir = agentic / "roles"
    roles_dir.mkdir(parents=True, exist_ok=True)

    content = ROLE_TEMPLATE.format(
        role_name=role_name,
        description=description or "new role",
        model=model,
    )
    (roles_dir / f"{role_name}.md").write_text(content, encoding="utf-8")

    print(f"Created: {roles_dir}/{role_name}.md")
    print()
    print("Next steps:")
    print(f"  1. Edit the role instructions in {roles_dir}/{role_name}.md")
    print("  2. Add model config to .agentic/config.yaml under models:" + role_name)
    print(f"  3. Add a stage to your pipeline YAML that uses role: {role_name}")
    return 0
