"""A12d (incident 31.10): ``awf status`` shows THIS run's tail, not yesterday's.

``awf-start.out`` is append-only across launches. A fresh run that wrote
three lines answered with those three lines plus the previous run's progress
report under them — the operator read the old run as the current one.
"""
from __future__ import annotations

from pathlib import Path

from awf.api._helpers import RUN_BANNERS, read_log_tail


def _log(tmp_path: Path) -> Path:
    path = tmp_path / "awf-start.out"
    path.write_text(
        "\n".join(
            [
                "=== Agentic Workflow: Starting Pipeline ===",
                "old stage 1",
                "PROGRESS REPORT",
                "Run 'awf start' for the next iteration.",
                "=== Agentic Workflow: Continuing Pipeline ===",
                "new stage 1",
                "new stage 2",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_tail_is_cut_at_the_last_run_banner(tmp_path):
    tail = read_log_tail(_log(tmp_path), 20, markers=RUN_BANNERS)
    assert tail is not None
    assert "old stage 1" not in tail
    assert "PROGRESS REPORT" not in tail
    assert tail.startswith("=== Agentic Workflow: Continuing Pipeline ===")
    assert "new stage 2" in tail


def test_without_markers_the_plain_tail_is_kept(tmp_path):
    tail = read_log_tail(_log(tmp_path), 20)
    assert tail is not None
    assert "old stage 1" in tail
    assert "new stage 2" in tail


def test_a_log_without_a_banner_keeps_the_plain_tail(tmp_path):
    path = tmp_path / "awf-start.out"
    path.write_text("refused: pinned runner drift\n", encoding="utf-8")
    tail = read_log_tail(path, 20, markers=RUN_BANNERS)
    assert tail == "refused: pinned runner drift"
