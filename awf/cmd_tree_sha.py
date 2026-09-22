"""Entry point for ``awf tree-sha``.

U11 (B5): prints the content-addressed fingerprint of the working tree —
the value the supervisor records at verify time and passes back to
``awf approve --verified-sha``. Same tree → same hash; any change (new
commit, tracked edit, untracked add/edit/delete) → different hash.
"""
from __future__ import annotations

from typing import Any

from . import git_utils
from .api._errors import AwfApiError


def run(args: Any) -> int:
    """Print the tree fingerprint; 1 with a clean message on failure."""
    try:
        print(git_utils.tree_fingerprint(getattr(args, "project_dir", ".")))
    except RuntimeError as e:
        msg = str(e).splitlines()[0] if str(e) else "git failure"
        raise AwfApiError(
            f"cannot compute the tree fingerprint: {msg}. Is the project a "
            "git repo with at least one commit?"
        ) from e
    return 0
