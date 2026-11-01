"""A2: a stage report is archived before anything overwrites it.

Two seams write over history: the handoff file per stage (a retry reuses the
name) and the unit's DONE/BLOCKED reports + PROGRESS note (the next stage
consumes and rewrites them). Both archive first, into
``.agentic/context/reports/``, with names that cannot collide.
"""
from __future__ import annotations

from pathlib import Path

from awf import reports_archive


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".agentic" / "outbox").mkdir(parents=True)
    (project / ".agentic" / "context").mkdir(parents=True)
    return project


def test_archive_copies_and_keeps_the_source(tmp_path):
    project = _project(tmp_path)
    source = project / ".agentic" / "outbox" / "DONE-TODO-0001.md"
    source.write_text("# done\n", encoding="utf-8")

    archived = reports_archive.archive_report(
        project, source, stage="implement", attempt=1
    )

    assert archived is not None and archived.is_file()
    assert archived.parent == project / ".agentic" / "context" / "reports"
    assert archived.read_text(encoding="utf-8") == "# done\n"
    assert "implement" in archived.name and "a1" in archived.name
    assert source.is_file()  # the caller owns the removal


def test_two_archives_of_the_same_name_never_collide(tmp_path):
    project = _project(tmp_path)
    source = project / ".agentic" / "outbox" / "DONE-TODO-0001.md"
    source.write_text("one", encoding="utf-8")
    first = reports_archive.archive_report(project, source, stage="implement", attempt=1)
    source.write_text("two", encoding="utf-8")
    second = reports_archive.archive_report(project, source, stage="implement", attempt=2)

    assert first != second
    assert first.read_text(encoding="utf-8") == "one"
    assert second.read_text(encoding="utf-8") == "two"


def test_a_missing_source_is_a_noop(tmp_path):
    project = _project(tmp_path)
    assert (
        reports_archive.archive_report(
            project, project / ".agentic" / "outbox" / "DONE-TODO-0009.md"
        )
        is None
    )
    assert not (project / ".agentic" / "context" / "reports").exists()


def test_stage_entry_archives_the_previous_report(tmp_path, monkeypatch):
    """The engine archives DONE/PROGRESS of the stage it is replacing."""
    from awf import agent_stage

    project = _project(tmp_path)
    outbox = project / ".agentic" / "outbox"
    (outbox / "DONE-TODO-0001.md").write_text("first stage", encoding="utf-8")
    (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")
    (outbox / "PROGRESS-TODO-0001.md").write_text("notes", encoding="utf-8")

    # Stop the stage right after the entry cleanup: the archive must be there.
    def stop(*args, **kwargs):
        raise RuntimeError("stop after the entry cleanup")

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", stop
    )
    (project / ".agentic" / "roles").mkdir(parents=True, exist_ok=True)
    (project / ".agentic" / "roles" / "worker.md").write_text("# w\n")
    (project / ".agentic" / "inbox").mkdir(exist_ok=True)
    (project / ".agentic" / "inbox" / "TODO-0001.md").write_text("# TODO\n")
    (project / ".agentic" / "config.yaml").write_text("project:\n  name: x\n")

    from awf.pipeline import Stage

    try:
        agent_stage.run_agent_stage(
            Stage(name="qa", role="worker", kind="execute"),
            "TODO-0001",
            project,
            {},
            project / ".agentic" / "logs",
        )
    except RuntimeError:
        pass

    archived = sorted((project / ".agentic" / "context" / "reports").iterdir())
    names = [p.name for p in archived]
    assert any(name.startswith("DONE-TODO-0001-") for name in names), names
    assert any(name.startswith("PROGRESS-TODO-0001-") for name in names), names
    assert not (outbox / "DONE-TODO-0001.ready").exists()
