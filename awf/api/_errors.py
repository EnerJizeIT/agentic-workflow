"""API exception type. Importable via :mod:`awf.api`."""
from __future__ import annotations


class AwfApiError(Exception):
    """Raised on any API-level failure (missing .agentic/, git error, etc.).

    The message is human-readable and can be shown directly to the user.
    MCP tools convert this to ``{"status": "error", "error": msg}``.
    """
