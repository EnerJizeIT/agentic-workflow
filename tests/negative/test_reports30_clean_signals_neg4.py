"""NEG-4 (reports30, TODO-0190): clean_stage_signals keeps the .md evidence.

The transition contract (pipeline_engine, the "NEG-4 (day-2 B1)" block):
the fired .ready is consumed, the .md report stays as evidence for the
handoff ("Keep the .md report as evidence"). clean_stage_signals — called
at every stage start (agent_stage), the net-death retry and the silent
retry — must match that contract. Before the fix it deleted the .md too:
the previous stage's DONE/BLOCKED report (the evidence) was wiped on
transition, contradicting NEG-4.

The .md without a .ready is inert for the engine: read_signal_for_todo
only reads .ready / .md.ready forms, so a surviving .md can never be
re-read as a signal by the next stage.
"""
from __future__ import annotations

from pathlib import Path

from awf.signals import (
    clean_stage_signals,
    expected_signal_prefixes,
    read_signal_for_todo,
)

T = "TODO-0001"


class TestCleanKeepsMdEvidence:
    def _outbox(self, tmp_path: Path) -> Path:
        outbox = tmp_path / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        return outbox

    def test_ready_and_md_ready_removed_md_kept(self, tmp_path: Path) -> None:
        """Both signal forms are consumed; the .md report survives."""
        outbox = self._outbox(tmp_path)
        for name in (
            "DONE-TODO-0001.ready",
            "DONE-TODO-0001.md.ready",  # BD-21 agent typo form
            "DONE-TODO-0001.md",
            "DONE-0001.md",  # legacy short form
        ):
            (outbox / name).write_text("evidence", encoding="utf-8")

        clean_stage_signals(outbox, T, *expected_signal_prefixes("execute"))

        assert not (outbox / "DONE-TODO-0001.ready").exists()
        assert not (outbox / "DONE-TODO-0001.md.ready").exists()
        # NEG-4: the evidence survives, in both id forms
        assert (outbox / "DONE-TODO-0001.md").exists()
        assert (outbox / "DONE-0001.md").exists()
        # no readable signal remains for the next stage
        assert read_signal_for_todo(outbox, T, *expected_signal_prefixes("execute")) is None

    def test_transition_scenario_keeps_done_evidence(self, tmp_path: Path) -> None:
        """Stage N wrote DONE.md + DONE.ready; the engine consumed the .ready
        at transition (the NEG-4 block in pipeline_engine); stage N+1 starts
        and cleans this kind's stale signals (agent_stage). The DONE.md
        evidence must survive for the handoff — and nothing readable must
        remain for the new stage."""
        outbox = self._outbox(tmp_path)
        (outbox / "DONE-TODO-0001.md").write_text("# Done: stage N report\n", encoding="utf-8")
        (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")

        # engine transition: consume the fired .ready (NEG-4 block)
        (outbox / "DONE-TODO-0001.ready").unlink()

        # stage N+1 start: agent_stage cleans this kind's signals
        clean_stage_signals(outbox, T, *expected_signal_prefixes("execute"))

        assert (outbox / "DONE-TODO-0001.md").exists(), (
            "NEG-4: the .md evidence must survive the transition"
        )
        assert read_signal_for_todo(outbox, T, *expected_signal_prefixes("execute")) is None

    def test_blocked_md_evidence_survives_clean(self, tmp_path: Path) -> None:
        """The BLOCKED cycle: the reason report is evidence too — it survives
        the clean after the .ready is consumed, so the supervisor can read
        why the worker stopped."""
        outbox = self._outbox(tmp_path)
        (outbox / "BLOCKED-TODO-0001.md").write_text("worker said: blocked\n", encoding="utf-8")
        (outbox / "BLOCKED-TODO-0001.ready").write_text("", encoding="utf-8")

        clean_stage_signals(outbox, T, *expected_signal_prefixes("execute"))

        assert not (outbox / "BLOCKED-TODO-0001.ready").exists()
        assert (outbox / "BLOCKED-TODO-0001.md").exists()
        assert read_signal_for_todo(outbox, T, *expected_signal_prefixes("execute")) is None

    def test_no_md_only_ready_consumed(self, tmp_path: Path) -> None:
        """Signal-only form (.ready without any .md): consumed as before —
        the fix must not resurrect a signal marker."""
        outbox = self._outbox(tmp_path)
        (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")

        clean_stage_signals(outbox, T, *expected_signal_prefixes("execute"))

        assert not (outbox / "DONE-TODO-0001.ready").exists()
        assert read_signal_for_todo(outbox, T, *expected_signal_prefixes("execute")) is None
