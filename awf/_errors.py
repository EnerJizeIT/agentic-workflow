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


class LockUnavailable(AwfApiError):
    """The advisory lock cannot be taken on this platform (no ``fcntl`` /
    ``msvcrt``).

    R2.5 (R1 carry b): the typed refusal for "no flock" on the state RMW
    lock. The launch interlock raises the shared base ``AwfApiError`` for
    the same condition; a ``LockUnavailable`` IS one, so both stay under one
    contract. It lives here, next to the base, so consumers can name the
    condition precisely instead of catching every ``AwfApiError`` — which
    would also swallow :class:`OperationRecoveryRequired`, a signal that
    MUST NOT be degraded to "no state".
    """

    outcome = "lock_unavailable"


class OperationRecoveryRequired(AwfApiError):
    """R1.2 (TODO-0216): a retried operation whose previous attempt left no
    recorded outcome — the write may or may not have landed. Re-running it
    blindly risks a duplicate effect, so the caller must reconcile first.
    """

    outcome = "recovery_required"

    def __init__(self, operation_id: str, message: str = "") -> None:
        self.operation_id = operation_id
        super().__init__(
            message
            or (
                f"operation {operation_id}: outcome unknown (the attempt "
                "started but recorded no result) — recovery_required, do not "
                "retry blindly"
            )
        )
