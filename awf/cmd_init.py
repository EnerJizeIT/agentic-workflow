"""Port of lib/init.sh — ``awf init`` command."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from . import git_utils, opencode_agents
from .xdg import opencode_config_file


def _find_framework_dir() -> Path:
    """Find the framework root (parent of awf/ package, sibling of templates/)."""
    return Path(__file__).resolve().parent.parent


def _find_reused_model() -> str | None:
    """Try to find an existing opencode agent's model to reuse as default."""
    oc_cfg = opencode_config_file()
    if not oc_cfg.exists():
        return None
    try:
        import json
        with open(oc_cfg, encoding="utf-8") as f:
            d = json.load(f)
        agents = d.get("agent") or {}
        if isinstance(agents, dict):
            for a in agents.values():
                if isinstance(a, dict) and a.get("model"):
                    return a["model"]
    except Exception:
        pass
    return None


CONFIG_TEMPLATE = '''project:
  name: "{project_name}"
  root: "."

models:
  supervisor:
    description: "Current session model"
# Agent roles (system-analyst, developer, qa, project-auditor, ...) are added
# dynamically by the project-setup form on first submit (BD-12 auto-maps each
# role to agent_name="worker"). Don't pre-populate here.

verification:
  test_cmd: "{test_cmd}"
  lint_cmd: "{lint_cmd}"
  typecheck_cmd: "{typecheck_cmd}"
  build_cmd: "{build_cmd}"
  coverage_cmd: ""

phases:
  current: ".agentic/phases/plan.md"

default_pipeline: "default"

retry:
  max_attempts: 3
  backoff_seconds: 0
'''


def run(args: Any) -> int:
    """Execute ``awf init`` and return exit code.

    BD-28: simplified — UI form (project-setup) is now the only way to
    configure pipeline + roles. ``awf init`` creates only the bare .agentic/
    skeleton + minimal config.yaml + supervisor.md (used by current opencode
    session). Pipeline.yaml and worker/reviewer/tester roles are populated
    later by the form submit.
    """
    force = getattr(args, "force", False)
    dry_run = getattr(args, "dry_run", False)

    agentic = Path(".agentic")
    if agentic.exists() and not force:
        print("ERROR: .agentic/ already exists. Use --force to overwrite.")
        return 1

    if not git_utils.is_git_repo("."):
        print("ERROR: Not a git repository. Run 'git init' first.")
        return 1

    print("=== Agentic Workflow Init ===")
    print()

    # Interactive prompts — order MUST match lib/init.sh for E2E compatibility
    project_name = input("Project name: ").strip()
    print("(Verify commands below are load-bearing: when set, awf auto-confirms completed")
    print(" work whose verify passes — no manual salvage. Leave blank only if none apply.)")
    test_cmd = input("Test command (e.g. pytest tests/, bun test): ").strip()
    lint_cmd = input("Lint command (e.g. ruff check .): ").strip()
    typecheck_cmd = input("Typecheck command (e.g. mypy src/, tsc --noEmit): ").strip()
    build_cmd = input("Build command (optional, e.g. docker compose config): ").strip()

    # Pick worker model
    reused_model = _find_reused_model()
    print()
    if reused_model:
        ans = input(f"Model for worker/reviewer/tester agents [default: {reused_model}]: ").strip()
        worker_model = ans or reused_model
    else:
        worker_model = input("Model id for worker/reviewer/tester agents (e.g. claude-sonnet-4-20250514, gpt-4.1): ").strip()
        if not worker_model:
            print("  (left blank — edit .agentic/config.yaml before `awf start`)")

    # Dry run
    if dry_run:
        print("[DRY RUN] Would create:")
        print(f"  .agentic/config.yaml (worker model: {worker_model or '<blank>'})")
        print("  .agentic/roles/supervisor.md")
        print("  .agentic/phases/plan.md (stub)")
        print("  .agentic/{pipelines,phases,inbox,outbox,context,logs,reports}/")
        return 0

    # Create directories
    for d in ["roles", "pipelines", "phases", "inbox", "outbox", "context", "logs", "reports"]:
        (agentic / d).mkdir(parents=True, exist_ok=True)

    # Generate config.yaml
    config_content = CONFIG_TEMPLATE.format(
        project_name=project_name,
        worker_model=worker_model,
        test_cmd=test_cmd,
        lint_cmd=lint_cmd,
        typecheck_cmd=typecheck_cmd,
        build_cmd=build_cmd,
    )
    (agentic / "config.yaml").write_text(config_content, encoding="utf-8")

    # Copy supervisor.md template (used by current opencode session — the
    # form will add worker/reviewer/tester roles later based on user selection).
    framework_dir = _find_framework_dir()
    templates_dir = framework_dir / "templates"
    shutil.copy2(templates_dir / "roles" / "supervisor.md", agentic / "roles" / "supervisor.md")

    # Stub plan.md — supervisor will fill it in during plan stage
    (agentic / "phases" / "plan.md").write_text(
        f"# {project_name} — Plan\n\n"
        "Steps:\n"
        "1. [ ] TODO\n",
        encoding="utf-8",
    )

    # Update .gitignore
    # inputs/ and dashboards/ — runtime state from agent-workflow-ui plugin (submits, rendered dashboards).
    gitignore_block = (
        ".agentic/inbox/\n.agentic/outbox/\n.agentic/context/\n.agentic/logs/\n.agentic/reports/\n"
        ".agentic/inputs/\n.agentic/dashboards/"
    )
    gitignore = Path(".gitignore")

    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if ".agentic/inbox/" not in content:
            gitignore.write_text(content + "\n# Agentic workflow runtime files\n" + gitignore_block + "\n", encoding="utf-8")
        elif ".agentic/inputs/" not in content:
            # Pre-existing gitignore from older awf — append plugin runtime dirs.
            gitignore.write_text(content + "\n# agent-workflow-ui runtime\n.agentic/inputs/\n.agentic/dashboards/\n", encoding="utf-8")
    else:
        gitignore.write_text("# Agentic workflow runtime files\n" + gitignore_block + "\n", encoding="utf-8")

    print()
    print("Created .agentic/ skeleton (supervisor.md + minimal config.yaml)")
    print("Pipeline + team roles will be configured via project-setup form on first run.")

    # Offer to create opencode agents — only `worker` (the universal agent
    # that loads role .md as instruction). Other roles come from skills.
    oc_cfg = opencode_config_file()
    if oc_cfg.exists():
        proposal = opencode_agents.propose(str(oc_cfg), ["worker"], worker_model)

        if proposal.startswith("ERR:"):
            print()
            print(f"NOTE: cannot propose agent changes — {proposal}")
            print(f"      Add 'worker' manually to {oc_cfg}.")
        elif proposal.startswith("NOTHING:"):
            print()
            print(proposal)
        elif proposal.startswith("PROPOSE:"):
            detail = proposal[len("PROPOSE:"):]
            print()
            print(f"Proposed change to {oc_cfg}:")
            print(detail)
            print("  (a timestamped backup ${cfg}.bak-<ts> will be created before writing)")
            ans = input("Apply this change to opencode config? [Y/n] ").strip().lower()
            if ans and ans not in ("y", "yes"):
                print("Skipping agent creation (create them manually if needed).")
            else:
                result = opencode_agents.apply(str(oc_cfg), ["worker"], worker_model)
                print(result)
        else:
            print()
            print(f"NOTE: unexpected proposal output: {proposal}")
    else:
        print()
        print(f"NOTE: no opencode config found at {oc_cfg}")
        print("      Create the 'worker' opencode agent manually (see README → Requirements).")

    _offer_plugin_install(project_name)

    print()
    print("Next steps:")
    print("  1. Edit .agentic/phases/plan.md with your project plan")
    print("  2. Open project-setup form (via opencode agent) to pick skills + roles")
    print("  3. Run: awf start --auto --background   (or: awf start for interactive)")
    return 0


def _offer_plugin_install(project_name):
    """Offer to configure agent-workflow-ui plugin."""
    print()
    print("agent-workflow-ui plugin (optional):")
    print("  HTML forms + dashboards for opencode agents.")
    print("  See: vision/agent-ui-plugin.md")

    try:
        import importlib
        importlib.import_module("agent_workflow_ui")
        plugin_installed = True
    except ImportError:
        plugin_installed = False

    if plugin_installed:
        print("  ✓ agent_workflow_ui detected.")
        ans = input("  Add MCP config to opencode.json? [y/N]: ").strip().lower()
        if ans in ("y", "yes"):
            _add_mcp_config_to_opencode()
    else:
        print("  Plugin not installed. Install from source:")
        print("    pip install -e ./agent_workflow_ui")
        print("  (PyPI publish pending — `pip install agent-workflow-ui` not yet available.)")
        print("  After install, re-run `awf init` to configure automatically.")


def _add_mcp_config_to_opencode():
    """Add agent-workflow-ui MCP block to opencode.json."""
    import json
    from datetime import datetime

    cfg_path = opencode_config_file()
    if not cfg_path.exists():
        print(f"  NOTE: {cfg_path} not found. Skipping.")
        return

    backup = cfg_path.with_suffix(f".json.bak-{datetime.now().strftime('%Y%m%d%H%M%S')}")
    shutil.copy2(cfg_path, backup)

    try:
        with cfg_path.open() as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  ERROR: cannot parse {cfg_path}: {e}")
        return

    mcp = cfg.setdefault("mcp", {})
    if "agent-workflow-ui" in mcp:
        print("  ✓ agent-workflow-ui already in opencode.json.")
        return

    mcp["agent-workflow-ui"] = {
        "type": "local",
        "command": ["python3", "-m", "agent_workflow_ui"],
    }

    with cfg_path.open("w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"  ✓ Added agent-workflow-ui to {cfg_path}")
    print(f"  Backup: {backup}")
