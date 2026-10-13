"""Coverage unit: the remaining branches of awf/run_plan_read.py.

PR #46 (run 10) was green on tests but red on the coverage ratchet:
``awf/run_plan_read.py`` fell from baseline 92.00 to 91.57 (CI measures
``tests/unit tests/agent_workflow_ui tests/negative`` via
``scripts/coverage.sh``). Lowering the baseline is refused by design —
instead the missing branches get targeted tests:

- ``_sources``: a ``report_file`` in the state lands in ``sources`` when
  the file is inside the project; outside the project
  (``relative_to`` -> ValueError) or absent (not a file) it is skipped
  silently — a link must not point at nothing;
- ``_evidence_plan_for``: ``current`` is set but no plan entry matches it
  -> the surface shows the LAST recorded plan, not nothing;
- ``read_run_record``: an OSError while reading the state degrades to an
  EMPTY record, not a traceback (the surfaces are read-only info); a
  garbage ``index`` in the finished-current correction block degrades to
  0 (defensive — ``read_run`` itself rejects a garbage index via its
  shape check, the guard covers an unsanitized state source);
- ``evidence_plan_lines(compact=True)``: more than three verify commands
  or prove_red ids -> the card keeps the first three and adds the
  "+N more" lines (the full context carries everything unclipped).

The state is written raw into run.yaml (``_raw_write``, the same pattern
as test_run_evidence_plan.py) or injected via a monkeypatched
``read_run`` — hermetic, no pipeline spawn.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from awf import run_state
from awf.run_plan_read import evidence_plan_lines, read_run_record

T = "TODO-0001"
T2 = "TODO-0002"


def _raw_write(proj: Path, state: dict) -> None:
    """Write run.yaml exactly as given — a valid-shape state the
    sanitized reader accepts as-is (or, for the monkeypatched tests, the
    raw input ``read_run_record`` must survive)."""
    f = run_state.run_file(proj)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump(state, allow_unicode=True), encoding="utf-8")


class TestSourcesReportFile:
    """``_sources``: the RUN-REPORT of a closed run joins ``sources`` as a
    project-relative link — only when the file really is inside the
    project (a broken link in the card would be worse than no link)."""

    def test_report_file_in_project_lands_in_sources(
        self, tmp_git_repo: Path, monkeypatch
    ) -> None:
        proj = tmp_git_repo
        # the reader checks the project-relative link against the CWD
        # (rel.is_file()), so run from inside the project
        monkeypatch.chdir(proj)
        report = proj / "outbox" / "SERVICE-RUN-REPORT-x.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("report\n", encoding="utf-8")
        _raw_write(
            proj,
            {
                "active": False,
                "queue": [{"todo_id": T, "pipeline": ""}],
                "index": 1,
                "stop_reason": "queue exhausted",
                "report_file": str(report),
            },
        )
        record = read_run_record(proj)
        assert record.sources == [
            ".agentic/state/run.yaml",
            "outbox/SERVICE-RUN-REPORT-x.md",
        ]

    def test_report_file_outside_project_is_skipped(
        self, tmp_git_repo: Path, tmp_path: Path
    ) -> None:
        proj = tmp_git_repo
        outside = tmp_path / "outside.md"  # a sibling of the project root
        outside.write_text("x\n", encoding="utf-8")
        _raw_write(
            proj,
            {
                "active": False,
                "queue": [{"todo_id": T, "pipeline": ""}],
                "index": 1,
                "report_file": str(outside),
            },
        )
        record = read_run_record(proj)
        assert record.sources == [".agentic/state/run.yaml"]

    def test_report_file_missing_inside_project_is_skipped(
        self, tmp_git_repo: Path, monkeypatch
    ) -> None:
        proj = tmp_git_repo
        monkeypatch.chdir(proj)
        _raw_write(
            proj,
            {
                "active": False,
                "queue": [{"todo_id": T, "pipeline": ""}],
                "index": 1,
                "report_file": str(proj / "outbox" / "GONE.md"),
            },
        )
        record = read_run_record(proj)
        assert record.sources == [".agentic/state/run.yaml"]


class TestEvidencePlanFallback:
    """``_evidence_plan_for``: the reader shows the CURRENT element's
    snapshot; when there is none for the current element it falls back
    to the LAST recorded entry (a closed run keeps its last element's
    plan, a re-launched item is matched by id, not by position)."""

    def test_current_without_plan_falls_back_to_last_entry(
        self, tmp_git_repo: Path
    ) -> None:
        proj = tmp_git_repo
        _raw_write(
            proj,
            {
                "active": True,
                "queue": [
                    {"todo_id": T, "pipeline": ""},
                    {"todo_id": T2, "pipeline": ""},
                ],
                "index": 1,
                "current": T2,
                "evidence_plans": [
                    {"ts": "t1", "todo_id": T, "todo_sha": "a" * 64,
                     "verify": ["cmd1"], "gates": [], "prove_red": [], "note": ""},
                ],
            },
        )
        record = read_run_record(proj)
        assert record.evidence_plan is not None
        assert record.evidence_plan["todo_id"] == T
        assert record.evidence_plan["verify"] == ["cmd1"]


class TestDegradation:
    """``read_run_record`` never raises on state it cannot trust — the
    surfaces are read-only info tools; a broken state degrades to "no
    run" or a safe default, the file/state is kept for inspection."""

    def test_unreadable_state_degrades_to_empty_record(
        self, tmp_git_repo: Path, monkeypatch
    ) -> None:
        """An OSError while reading the state -> an EMPTY record (the
        surfaces stay usable), not a traceback."""
        proj = tmp_git_repo

        def fake_read_run(project_dir, **kw):
            raise OSError("simulated: state unreadable")

        monkeypatch.setattr(run_state, "read_run", fake_read_run)
        record = read_run_record(proj)
        assert record.active is False
        assert record.goal == ""
        assert record.sources == []
        assert record.state is None

    def test_garbage_index_in_finished_current_degrades_to_zero(
        self, tmp_git_repo: Path, monkeypatch
    ) -> None:
        """Defensive branch: a garbage ``index`` in the finished-current
        correction block degrades to 0. ``read_run`` itself rejects a
        garbage index (shape check -> no run), so the state is injected
        via a monkeypatched ``read_run`` — the guard covers an
        unsanitized source. The queue must be empty: ``position()``
        skips the ``int()`` on an empty queue, leaving the ``except``
        as the only survivor."""
        proj = tmp_git_repo
        (proj / ".agentic" / "done" / T).mkdir(parents=True)  # current finished

        def fake_read_run(project_dir, **kw):
            return {
                "active": True,
                "queue": [],
                "index": "corrupt",
                "current": T,
            }

        monkeypatch.setattr(run_state, "read_run", fake_read_run)
        record = read_run_record(proj)
        assert record.active is True
        assert record.current == ""  # finished current, queue exhausted
        assert record.position == "0/0"


class TestCompactEvidencePlanOverflow:
    """``evidence_plan_lines(compact=True)`` (the brief card's word
    budget): at most ``_PLAN_MAX_ITEMS`` commands/ids per section, the
    rest collapsed into the "+N more" line."""

    def test_overflow_beyond_three_items_adds_more_lines(
        self, tmp_git_repo: Path
    ) -> None:
        proj = tmp_git_repo
        _raw_write(
            proj,
            {
                "active": True,
                "queue": [{"todo_id": T, "pipeline": ""}],
                "index": 0,
                "current": T,
                "evidence_plans": [
                    {
                        "ts": "t1",
                        "todo_id": T,
                        "todo_sha": "c" * 64,
                        "verify": [f"verify-{i}" for i in range(4)],
                        "gates": ["contracts"],
                        "prove_red": [f"proof-{i}" for i in range(4)],
                        "note": "",
                    },
                ],
            },
        )
        record = read_run_record(proj)
        lines = evidence_plan_lines(record, compact=True)
        assert sum(
            line.startswith("  - verify: verify-") for line in lines
        ) == 3
        assert "  - … +1 more verify command(s)" in lines
        assert sum(
            line.startswith("  - prove_red: proof-") for line in lines
        ) == 3
        assert "  - … +1 more prove_red id(s)" in lines
