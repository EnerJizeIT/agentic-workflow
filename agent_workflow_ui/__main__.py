"""Entry point: python -m agent_workflow_ui"""
from __future__ import annotations

import sys

from .config import load, ensure_directories
from .server import create_server


def main() -> int:
    """Run the MCP server on stdio transport."""
    config = load()
    ensure_directories(config)
    server = create_server()
    server.run()  # blocks; stdio transport
    return 0


if __name__ == "__main__":
    sys.exit(main())
