"""Entry point: python -m agent_workflow_ui"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from .agents_md import ensure_agents_md
from .config import detect_project_dir, ensure_directories, load
from .fault_log import setup_fault_log
from .http_endpoint import start_http_server
from .instance import boot
from .opencode_config import read_opencode_models, read_recent_models
from .render.engine import create_env
from .server import create_server
from .skill_installer import ensure_skill_installed, ensure_supervisor_skill_installed
from .state import (
    get_registry,
    set_config,
    set_http_port,
    set_instance_mode,
    set_jinja_env,
    set_project_dir,
)

log = logging.getLogger(__name__)

DEFAULT_TEMPLATES_DIR = Path(__file__).parent / "render" / "default_templates"


def main() -> int:
    """Run the MCP server on stdio transport, with HTTP endpoint in background."""
    # TODO-0181: fault log first — lifecycle + crash traces survive even a
    # hard kill of the MCP session (owner diagnosis 2026-10-09).
    setup_fault_log()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    try:
        # Lazy skill install: copy SKILL.md to ~/.config/opencode/skills/agent-workflow-ui/
        # Idempotent — overwrites if bundled version differs. No-op if already current.
        ensure_skill_installed()
        # RUN6 #5: the awf-supervisor doctrine skill (same mechanism).
        ensure_supervisor_skill_installed()
        # RUN6 #5: keep the plugin's AGENTS.md block (system prompt) in sync.
        ensure_agents_md()

        config = load()
        ensure_directories(config)
        set_config(config)
        project_dir = detect_project_dir()
        set_project_dir(project_dir)

        env = create_env([config.templates_dir, DEFAULT_TEMPLATES_DIR])
        env.globals["available_models"] = read_opencode_models()
        env.globals["recent_models"] = read_recent_models()
        set_jinja_env(env)

        registry = get_registry()
        result = boot(config, project_dir, registry, start_server=start_http_server)
        set_instance_mode(result.mode)
        if result.port:
            set_http_port(result.port)
        log.info("%s", result.message)
        log.info(
            "agent_workflow_ui mode=%s port=%s (single instance per project, A45)",
            result.mode,
            result.port,
        )

        mcp = create_server()
        mcp.run()  # blocks; stdio transport

        if result.server is not None:
            result.server.shutdown()
        if result.lock is not None:
            result.lock.release()
        return 0
    except KeyboardInterrupt:
        log.info("agent_workflow_ui interrupted by user")
        return 130
    except Exception:
        log.exception("agent_workflow_ui crashed during startup/runtime")
        print("FATAL: agent_workflow_ui failed to start", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
