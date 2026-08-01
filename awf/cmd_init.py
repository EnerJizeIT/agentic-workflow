"""Port of lib/init.sh — ``awf init`` command.

Thin CLI wrapper around :func:`awf.api.init_project`. Retains interactive
prompts for human CLI use; opencode agents should call ``awf_init`` MCP tool
instead (no prompts, deterministic stack detection).
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from . import api, git_utils, opencode_agents
from .xdg import opencode_config_file


def run(args: Any) -> int:
    """Execute ``awf init`` and return exit code.

    Flow:
    1. Pre-checks (git repo, .agentic existence)
    2. Interactive prompts (project_name, test/lint/typecheck/build commands)
       — when called from CLI. MCP path skips these via api.init_project().
    3. api.init_project() creates the .agentic/ skeleton
    4. Optional: opencode agents + agent-workflow-ui plugin setup
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

    # M5 fix: worker_model prompt was asked but never saved (no placeholder
    # in CONFIG_TEMPLATE). Project-setup form handles per-role models now
    # (BD-32). Removed the dead prompt — was misleading users.
    worker_model = ""  # kept for opencode_agents.propose() below (legacy compat)

    if dry_run:
        print("[DRY RUN] Would create:")
        print("  .agentic/config.yaml (models added by project-setup form)")
        print("  .agentic/roles/supervisor.md")
        print("  .agentic/phases/plan.md (stub)")
        print("  .agentic/{pipelines,phases,inbox,outbox,context,logs,reports}/")
        return 0

    # Delegate skeleton creation to api.init_project
    try:
        result = api.init_project(
            project_dir=Path("."),
            force=force,
            project_name=project_name,
            test_cmd=test_cmd,
            lint_cmd=lint_cmd,
            typecheck_cmd=typecheck_cmd,
            build_cmd=build_cmd,
        )
    except api.AwfApiError as e:
        print(f"ERROR: {e}")
        return 1

    print()
    print(f"Created .agentic/ skeleton for: {result.project_name}")
    print(f"  Stack detected: {result.stack}")
    print(f"  Vision: {result.vision_path or '(not found)'}")
    print("Pipeline + team roles will be configured via project-setup form on first run.")

    # Offer to create opencode agents — only `worker` (the universal agent
    # that loads role .md as instruction). Other roles come from skills.
    _offer_opencode_agent_setup(worker_model)

    _offer_plugin_install(result.project_name)

    print()
    print("Next steps:")
    print("  1. Edit .agentic/phases/plan.md with your project plan")
    print("  2. Open project-setup form (via opencode agent) to pick skills + roles")
    print("  3. Run: awf start --auto --background   (or: awf start for interactive)")
    return 0


def _offer_opencode_agent_setup(worker_model: str) -> None:
    """Offer to add 'worker' agent to opencode.json. No-op if already there."""
    oc_cfg = opencode_config_file()
    if not oc_cfg.exists():
        print()
        print(f"NOTE: no opencode config found at {oc_cfg}")
        print("      Create the 'worker' opencode agent manually (see README → Requirements).")
        return

    proposal = opencode_agents.propose(str(oc_cfg), ["worker"], worker_model)
    if proposal.startswith("ERR:"):
        print()
        print(f"NOTE: cannot propose agent changes — {proposal}")
        print(f"      Add 'worker' manually to {oc_cfg}.")
        return
    if proposal.startswith("NOTHING:"):
        print()
        print(proposal)
        return
    if proposal.startswith("PROPOSE:"):
        detail = proposal[len("PROPOSE:"):]
        print()
        print(f"Proposed change to {oc_cfg}:")
        print(detail)
        print("  (a timestamped backup ${cfg}.bak-<ts> will be created before writing)")
        ans = input("Apply this change to opencode config? [Y/n] ").strip().lower()
        if ans and ans not in ("y", "yes"):
            print("Skipping agent creation (create them manually if needed).")
            return
        result = opencode_agents.apply(str(oc_cfg), ["worker"], worker_model)
        print(result)
        return
    print()
    print(f"NOTE: unexpected proposal output: {proposal}")


def _offer_plugin_install(project_name: str) -> None:
    """Offer to configure agent-workflow-ui plugin."""
    print()
    print("agent-workflow-ui plugin (optional):")
    print("  HTML forms + dashboards for opencode agents.")
    print("  See: vision/agent-ui-plugin.md")

    try:
        import importlib.util

        plugin_installed = importlib.util.find_spec("agent_workflow_ui") is not None
    except (ImportError, ValueError):
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


def _add_mcp_config_to_opencode() -> None:
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
