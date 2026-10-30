"""A11: the stage log names WHY a worker failed, not just "crashed"."""
from __future__ import annotations

from awf.pipeline_engine import _log_error_line, _stage_failure_reason


def test_log_error_line_takes_the_last_error():
    tail = (
        "level=INFO start\n"
        "level=ERROR cause=AgentNotFound\n"
        "level=INFO retry\n"
        "level=ERROR cause=ECONNREFUSED\n"
    )
    assert "ECONNREFUSED" in _log_error_line(tail)


def test_reason_prefers_the_worker_log_over_the_exception():
    assert _stage_failure_reason(
        RuntimeError("command failed"), "level=ERROR cause=AgentNotFound"
    ) == "level=ERROR cause=AgentNotFound"


def test_reason_falls_back_to_the_exception_first_line():
    assert _stage_failure_reason(
        RuntimeError("boom\nsecond line"), "no errors here"
    ) == "boom"


def test_reason_is_clipped_to_one_line_budget():
    line = _stage_failure_reason(RuntimeError("x"), "level=ERROR " + "a" * 500)
    assert len(line) == 300


def test_no_exception_and_no_error_line_is_empty():
    assert _stage_failure_reason(None, "level=INFO only") == ""
