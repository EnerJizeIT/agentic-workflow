"""Framework-level exception type.

Lives at the top of the awf/ package with zero imports so lower-layer
modules (e.g. prove_red) can raise it without triggering the awf.api
package initialization — importing awf.api first would be circular for
them (awf/api/__init__ imports those modules back).
"""
from __future__ import annotations


class AwfApiError(Exception):
    """Raised on any API-level failure (missing .agentic/, git error, etc.).

    The message is human-readable and can be shown directly to the user.
    MCP tools convert this to ``{"status": "error", "error": msg}``.
    """
