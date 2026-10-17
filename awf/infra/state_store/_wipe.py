"""R1.5 (REVIEW A5): the Store-owned wipe of the ``.agentic/state`` tree.

``init_project(force=True)`` (``lifecycle._clean_state_dir``) removed the
CONTENTS of ``.agentic/state`` (``control.json``, ``records/``,
``operations/``, the projections) with a raw ``unlink``/``rmtree`` loop from
the api layer — the same "single writer of state" leak as the projection
writers, but for DELETION. The wipe lives here so the destructive door names
the Store, not a local loop.

Synchronisation files (``.interlock``, ``.lock``) are PRESERVED by the
caller: removing their inode would orphan a live competitor's flock — the
new writer would open a fresh inode and step past the lock (QA F-B class).

Honest scope (REVIEW A5): the RESET loop in
``lifecycle._reset_runtime_impl`` keeps its raw unlink/rmtree. It wipes
runtime artifacts (``inbox``/``outbox``/``context``/``logs``) — NOT control
state — so it is outside the Store's remit; routing it here would put
non-state directories under a state-store API. The registry of what was and
was not routed through here is in the unit's DONE report.
"""
from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path


def delete_state_tree(state_dir: Path, *, preserve: Iterable[str] = ()) -> None:
    """Remove the CONTENTS of ``state_dir``, keeping ``preserve`` names.

    ``state_dir`` itself is kept (it is re-scaffolded by the caller). A
    symlinked entry is unlinked itself, never followed; a directory is
    removed recursively. A name in ``preserve`` is skipped entirely (no
    descent into it).
    """
    keep = set(preserve)
    for entry in state_dir.iterdir():
        if entry.name in keep:
            continue
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        elif entry.is_dir():
            shutil.rmtree(entry)


__all__ = ["delete_state_tree"]
