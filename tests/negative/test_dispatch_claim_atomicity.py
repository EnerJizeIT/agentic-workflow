"""V-05-followup (TODO-0127): deterministic killer for the O_EXCL mutant.

The parallel test in test_dispatch_concurrency.py kills the mutant
``os.O_CREAT | os.O_EXCL | os.O_WRONLY`` -> ``os.O_CREAT | os.O_WRONLY``
only when the 8 forked dispatches happen to land on the same id — a
scheduler coin flip (CI run 36314566179: the mutant survived one of
two runs). This test removes the coin: two claims of ONE fixed name in
a single process, no race, no sleep, no scheduler.

With O_EXCL the second claim of the occupied name raises
FileExistsError, the retry loop re-pins the same name (the monkeypatched
``_next_todo_id``), and the dispatch is refused after 16 collisions —
the first writer's file stays intact. Without O_EXCL the second
``os.open`` silently truncates the file and the dispatch proceeds to
overwrite it. Both assertions below hold only with O_EXCL.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api
from awf.api._errors import AwfApiError
from awf.api.dispatch import dispatch_todo

OCCUPIED_ID = "TODO-0001"
FIRST_WRITER = "first-writer-content-do-not-touch\n"


class TestClaimAtomicity:
    """A repeat claim of an occupied name must not silently overwrite."""

    def test_repeat_claim_of_occupied_name_refuses_and_keeps_file(
        self, tmp_git_repo: Path, monkeypatch
    ):
        import awf.api.dispatch as dispatch_mod

        api.init_project(tmp_git_repo, project_name="ClaimAtom")
        proj = tmp_git_repo

        # "The other process" already claimed the name.
        occupied = tmp_git_repo / ".agentic" / "inbox" / f"{OCCUPIED_ID}.md"
        occupied.parent.mkdir(parents=True, exist_ok=True)
        occupied.write_text(FIRST_WRITER, encoding="utf-8")

        # Both "callers" compute the same next id — the race pinned to
        # one name so no scheduler is involved.
        monkeypatch.setattr(
            dispatch_mod, "_next_todo_id", lambda project_dir: OCCUPIED_ID
        )

        with pytest.raises(AwfApiError, match="could not reserve a free TODO id"):
            dispatch_todo(proj, "# second writer\n\nsecond writer content.\n")

        # The first writer's file must be byte-identical: no truncate,
        # no overwrite, no re-issue under the same name.
        assert occupied.read_text(encoding="utf-8") == FIRST_WRITER
