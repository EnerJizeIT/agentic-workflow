"""REPORTS26 F5: dispatch_todo — a foreign TODO-NNNN in the content heading.

Source: awf-feature-20261007-dispatch-todo-zagolovok-kontenta-s-drugim-todo-nnnn.md.
The supervisor copies a previous unit's template; the ``# TODO-XXXX`` title
keeps the old number while dispatch issues a new one — the inbox holds a
unit whose title names someone else's number.

Four invariants:
(a) auto number + a foreign number in the first line's heading → the
    heading is renumbered to the issued id, the result carries
    ``renumbered: true``, the rest of the text is untouched;
(b) explicit ``todo_id`` + a heading with a DIFFERENT number → refused
    before any side effect (the message names both numbers and points to
    ``allow_mismatch``); nothing lands in the inbox;
(c) ``allow_mismatch=True`` → the content is written as-is,
    ``renumbered: false``;
(d) no heading / a heading without a number / a matching number → the
    old behavior, ``renumbered: false``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api, paths


@pytest.fixture
def project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="F5dispatch")
    return tmp_git_repo


def _inbox(project: Path) -> Path:
    return paths.inbox(project)


def _todo_files(project: Path, todo_id: str) -> list[Path]:
    """Every file that mentions the id under .agentic/ (md, ready, baseline)."""
    return [
        p for p in (project / ".agentic").rglob("*")
        if p.is_file() and todo_id in p.name
    ]


class TestAutoRenumberForeignHeading:
    """(a) auto number: the heading takes the issued id."""

    def test_heading_renumbered_to_issued_id(self, project):
        result = api.dispatch_todo(
            project, "# TODO-9999 fix the dispatch\n\nbody line\n"
        )

        assert result.todo_id == "TODO-0001"
        assert result.renumbered is True
        assert result.as_dict()["renumbered"] is True
        md = (_inbox(project) / "TODO-0001.md").read_text()
        assert md.splitlines()[0] == "# TODO-0001 fix the dispatch"
        assert "TODO-9999" not in md
        assert "body line" in md
        assert (_inbox(project) / "TODO-0001.ready").is_file()

    def test_role_hint_stays_above_renumbered_heading(self, project):
        result = api.dispatch_todo(
            project, "# TODO-9999 x\n\nbody\n", role="agent-implementer"
        )
        assert result.renumbered is True
        lines = (_inbox(project) / "TODO-0001.md").read_text().splitlines()
        assert lines[0] == "<!-- role_hint: agent-implementer -->"
        assert lines[1] == "# TODO-0001 x"


class TestExplicitIdForeignHeadingRefused:
    """(b) explicit id + a foreign heading number: refusal, zero side effects."""

    def test_refusal_names_both_numbers_and_the_flag(self, project):
        with pytest.raises(api.AwfApiError) as ei:
            api.dispatch_todo(
                project, "# TODO-9999 x\n\nbody\n", todo_id="TODO-0001"
            )

        msg = str(ei.value)
        assert "TODO-0001" in msg, "the explicit id must be named"
        assert "9999" in msg, "the heading number must be named"
        assert "allow_mismatch" in msg, "the escape flag must be named"
        # nothing was written: no TODO, no .ready, no baseline for the id
        assert _todo_files(project, "TODO-0001") == []

    def test_number_match_is_not_a_mismatch(self, project):
        """Explicit id, same number in the heading (padded differently): ok."""
        result = api.dispatch_todo(
            project, "# TODO-1 task\n\nbody\n", todo_id="TODO-0001"
        )
        assert result.todo_id == "TODO-0001"
        assert result.renumbered is False
        md = (_inbox(project) / "TODO-0001.md").read_text()
        assert md.splitlines()[0] == "# TODO-1 task"


class TestAllowMismatch:
    """(c) allow_mismatch=True: the content is written as-is."""

    def test_writes_foreign_heading_as_is(self, project):
        result = api.dispatch_todo(
            project, "# TODO-9999 x\n\nbody\n",
            todo_id="TODO-0001", allow_mismatch=True,
        )
        assert result.renumbered is False
        md = (_inbox(project) / "TODO-0001.md").read_text()
        assert md.splitlines()[0] == "# TODO-9999 x"
        assert (_inbox(project) / "TODO-0001.ready").is_file()


class TestNoHeadingOrMatchingNumber:
    """(d) no heading / heading without a number / matching number: as before."""

    def test_no_heading(self, project):
        result = api.dispatch_todo(project, "# Task\n\ndo work\n")
        assert result.renumbered is False
        md = (_inbox(project) / "TODO-0001.md").read_text()
        assert md.splitlines()[0] == "# Task"

    def test_heading_without_number(self, project):
        result = api.dispatch_todo(project, "# TODO — some title\n\nbody\n")
        assert result.renumbered is False
        md = (_inbox(project) / "TODO-0001.md").read_text()
        assert md.splitlines()[0] == "# TODO — some title"

    def test_matching_explicit_number(self, project):
        result = api.dispatch_todo(
            project, "# TODO-0001 task\n\nbody\n", todo_id="TODO-0001"
        )
        assert result.renumbered is False
        md = (_inbox(project) / "TODO-0001.md").read_text()
        assert md.splitlines()[0] == "# TODO-0001 task"

    def test_explicit_without_mismatch_flag_stays_default(self, project):
        """The parameter defaults to False — a foreign heading is refused."""
        result_before = api.dispatch_todo(project, "# plain\n")
        assert result_before.renumbered is False
        with pytest.raises(api.AwfApiError):
            api.dispatch_todo(
                project, "# TODO-7777 x\n", todo_id="TODO-0002"
            )
