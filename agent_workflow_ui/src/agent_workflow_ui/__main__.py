"""Entry point: python -m agent_workflow_ui"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from .config import load, ensure_directories
from .http_endpoint import start_http_server
from .opencode_config import read_opencode_models
from .render.engine import create_env
from .server import create_server
from .state import get_registry, set_config, set_http_port, set_jinja_env


DEFAULT_TEMPLATES_DIR = Path(__file__).parent / "render" / "default_templates"


def main() -> int:
    """Run the MCP server on stdio transport, with HTTP endpoint in background."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    config = load()
    ensure_directories(config)
    set_config(config)

    env = create_env([config.templates_dir, DEFAULT_TEMPLATES_DIR])
    env.globals["available_models"] = read_opencode_models()
    set_jinja_env(env)

    registry = get_registry()
    server, port = start_http_server(config, registry)
    set_http_port(port)
    logging.info("agent_workflow_ui started (HTTP on port %d)", port)

    mcp = create_server()
    mcp.run()  # blocks; stdio transport

    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
