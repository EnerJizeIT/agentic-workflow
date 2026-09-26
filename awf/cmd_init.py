"""Port of lib/init.sh — ``awf init`` command.

Thin CLI wrapper around :func:`awf.api.init_project`. Retains interactive
prompts for human CLI use; opencode agents should call ``awf_init`` MCP tool
instead (no prompts, deterministic stack detection).
"""
from __future__ import annotations

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
    non_interactive = getattr(args, "non_interactive", False)
    project_dir_arg = getattr(args, "project_dir", ".")

    project_dir_path = Path(project_dir_arg).resolve()
    # A-11: awf init without force is non-destructive — an existing
    # .agentic/ (TODOs, done/ archive, state, logs, config) is preserved
    # as-is; only missing skeleton directories are created. --force is
    # the only destructive path (refused while a pipeline is live).
    # Error only if not a git repo.

    if not git_utils.is_git_repo(project_dir_path):
        print("ERROR: Not a git repository. Run 'git init' first.")
        return 1

    # MCP-6: --non-interactive skips all prompts, delegates fully to api.
    if non_interactive:
        try:
            result = api.init_project(project_dir_path, force=force, dry_run=dry_run)
        except api.AwfApiError as e:
            print(f"ERROR: {e}")
            return 1
        if dry_run:
            # AUD07-07: dry-run writes nothing — the old "Would create" message
            # lied when .agentic/ already existed (R1 branch).
            print("[DRY RUN] No files written — .agentic/ left as-is.")
            return 0
        print(f"Created .agentic/ skeleton for: {result.project_name}")
        print(f"  Stack: {result.stack}")
        print(f"  Vision: {result.vision_path or '(not found)'}")
        print()
        print("Next: open project-setup form (MCP tool open_form) to pick roles.")
        return 0

    print("=== Agentic Workflow Init ===")
    print()

    if dry_run:
        # AUD07-07: dry-run writes nothing and must not ask a single prompt —
        # the five input() calls used to run before this check and blocked
        # on stdin (or swallowed real answers) on `awf init --dry-run`.
        # FU-19 (backlog tail): "Would create:" lied when .agentic/ already
        # existed (R1 branch) — the message must match the branch taken.
        if (project_dir_path / ".agentic").is_dir():
            print("[DRY RUN] .agentic/ already exists — the existing project "
                  "and its runtime are left untouched.")
        else:
            print("[DRY RUN] Would create:")
            print("  .agentic/config.yaml (models added by project-setup form)")
            print("  .agentic/roles/supervisor.md")
            print("  .agentic/phases/plan.md (stub)")
            print("  .agentic/{pipelines,phases,inbox,outbox,context,logs}/")
        print("[DRY RUN] No files written.")
        return 0

    # Interactive prompts — order MUST match lib/init.sh for E2E compatibility
    # Use project_dir_path instead of cwd for git checks (MCP-6 compat).
    project_name = input("Project name: ").strip()
    print("(Verify commands below are load-bearing: when set, awf auto-confirms completed")
    print(" work whose verify passes — no manual salvage. Leave blank only if none apply.)")
    test_cmd = input("Test command (e.g. pytest tests/, bun test): ").strip()
    lint_cmd = input("Lint command (e.g. ruff check .): ").strip()
    typecheck_cmd = input("Typecheck command (e.g. mypy src/, tsc --noEmit): ").strip()
    build_cmd = input("Build command (optional, e.g. docker compose config): ").strip()

    # M5 fix: the worker-model prompt was asked but never saved (no
    # placeholder in CONFIG_TEMPLATE). Project-setup form handles per-role
    # models now (BD-32). AUD16-10: the leftover "" variable went with it —
    # propose/apply are called with an empty model.

    # Delegate skeleton creation to api.init_project
    try:
        result = api.init_project(
            project_dir=project_dir_path,
            force=force,
            # AUD07-07: blank answer → None → api derives name from dir name
            # (an empty string used to land in config.yaml as `name: ''`).
            project_name=project_name or None,
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
    _offer_opencode_agent_setup()

    _offer_plugin_install(result.project_name)

    print()
    print("Next steps:")
    print("  1. Edit .agentic/phases/plan.md with your project plan")
    print("  2. Open project-setup form (via opencode agent) to pick skills + roles")
    print("  3. Run: awf start --auto --background   (or: awf start for interactive)")
    return 0


def _offer_opencode_agent_setup() -> None:
    """Offer to add 'worker' agent to opencode.json. No-op if already there.

    AUD16-10: no model — the init flow never collected one (the prompt was
    removed in M5); per-role models come from the project-setup form.
    """
    oc_cfg = opencode_config_file()
    if not oc_cfg.exists():
        print()
        print(f"NOTE: no opencode config found at {oc_cfg}")
        print("      Create the 'worker' opencode agent manually (see README → Requirements).")
        return

    proposal = opencode_agents.propose(str(oc_cfg), ["worker"], "")
    # T2.8: typed Proposal instead of stringly-typed startswith checks.
    # __str__ keeps legacy format for prints, but branching on .kind is
    # exhaustiveness-checked and self-documenting.
    if proposal.kind is opencode_agents.ProposalKind.ERR:
        print()
        print(f"NOTE: cannot propose agent changes — {proposal}")
        print(f"      Add 'worker' manually to {oc_cfg}.")
        return
    if proposal.kind is opencode_agents.ProposalKind.NOTHING:
        print()
        print(proposal)
        return
    if proposal.kind is opencode_agents.ProposalKind.PROPOSE:
        print()
        print(f"Proposed change to {oc_cfg}:")
        print(proposal.detail)
        print(f"  (a timestamped backup {oc_cfg.name}.bak-<ts> will be created before writing)")
        ans = input("Apply this change to opencode config? [Y/n] ").strip().lower()
        if ans and ans not in ("y", "yes"):
            print("Skipping agent creation (create them manually if needed).")
            return
        result = opencode_agents.apply(str(oc_cfg), ["worker"], "")
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
    """Add agent-workflow-ui MCP block to opencode.json.

    AUD07-06: thin delegation — the backup/parse/merge/write logic lives in
    :func:`awf.opencode_agents.ensure_mcp_block` (atomic write, backup only
    after a successful parse, ERR results instead of tracebacks).
    """
    result = opencode_agents.ensure_mcp_block(str(opencode_config_file()))
    print(f"  {result}")
