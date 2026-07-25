"""Port of lib/init.sh — ``awf init`` command."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import git_utils
from . import opencode_agents
from . import paths


def _find_framework_dir() -> Path:
    """Find the framework root (parent of awf/ package, sibling of templates/)."""
    return Path(__file__).resolve().parent.parent


def _find_reused_model() -> str | None:
    """Try to find an existing opencode agent's model to reuse as default."""
    oc_cfg = Path.home() / ".config" / "opencode" / "opencode.json"
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
  worker:
    agent_name: "worker"
    model: "{worker_model}"
    temperature: 0.1
  reviewer:
    agent_name: "reviewer"
    model: "{worker_model}"
    temperature: 0.1
  tester:
    agent_name: "tester"
    model: "{worker_model}"
    temperature: 0.1

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
    """Execute ``awf init`` and return exit code."""
    template = getattr(args, "template", "simple")
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
        print("  .agentic/roles/worker.md")
        if template == "full":
            print("  .agentic/roles/reviewer.md")
            print("  .agentic/roles/tester.md")
        print("  .agentic/pipelines/default.yaml")
        print("  .agentic/phases/")
        print("  .agentic/inbox/, outbox/, context/, logs/, reports/")
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

    # Copy role templates
    framework_dir = _find_framework_dir()
    templates_dir = framework_dir / "templates"

    shutil.copy2(templates_dir / "roles" / "supervisor.md", agentic / "roles" / "supervisor.md")
    shutil.copy2(templates_dir / "roles" / "worker.md", agentic / "roles" / "worker.md")

    if template == "full":
        shutil.copy2(templates_dir / "roles" / "reviewer.md", agentic / "roles" / "reviewer.md")
        shutil.copy2(templates_dir / "roles" / "tester.md", agentic / "roles" / "tester.md")

    # Copy pipeline template
    pipeline_src = templates_dir / "pipelines" / (template + ".yaml")
    shutil.copy2(pipeline_src, agentic / "pipelines" / "default.yaml")

    # Copy TODO template
    shutil.copy2(templates_dir / "todo-template.md", agentic / "todo-template.md")

    # Update .gitignore
    gitignore_block = ".agentic/inbox/\n.agentic/outbox/\n.agentic/context/\n.agentic/logs/\n.agentic/reports/"
    gitignore = Path(".gitignore")

    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if ".agentic/inbox/" not in content:
            gitignore.write_text(content + "\n# Agentic workflow runtime files\n" + gitignore_block + "\n", encoding="utf-8")
    else:
        gitignore.write_text("# Agentic workflow runtime files\n" + gitignore_block + "\n", encoding="utf-8")

    print()
    print(f"Created .agentic/ with {template} template")

    # Offer to create opencode agents
    offer_agents = ["worker"]
    if template == "full":
        offer_agents = ["worker", "reviewer", "tester"]

    oc_cfg = Path.home() / ".config" / "opencode" / "opencode.json"
    if oc_cfg.exists():
        proposal = opencode_agents.propose(str(oc_cfg), offer_agents, worker_model)

        if proposal.startswith("ERR:"):
            print()
            print(f"NOTE: cannot propose agent changes — {proposal}")
            print(f"      Add {', '.join(offer_agents)} manually to {oc_cfg}.")
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
                result = opencode_agents.apply(str(oc_cfg), offer_agents, worker_model)
                print(result)
        else:
            print()
            print(f"NOTE: unexpected proposal output: {proposal}")
    else:
        print()
        print(f"NOTE: no opencode config found at {oc_cfg}")
        print(f"      Create the opencode agents ({', '.join(offer_agents)}) manually (see README → Requirements).")

    print()
    print("Next steps:")
    print("  1. Edit .agentic/config.yaml if needed")
    print("  2. Create .agentic/phases/plan.md with your implementation plan")
    print("  3. Run: awf start --auto --background   (or: awf start for interactive)")
    return 0
