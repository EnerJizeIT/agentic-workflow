"""API exception type. Importable via :mod:`awf.api`.

The class itself lives in :mod:`awf._errors` (top level, zero imports) so
lower-layer modules like ``awf.prove_red`` can raise it without triggering
the ``awf.api`` package init — which would be circular, because
``awf/api/__init__`` imports those modules back. Re-exported here for
compatibility; identity is the same class object.
"""
from .._errors import AwfApiError

__all__ = ["AwfApiError"]
