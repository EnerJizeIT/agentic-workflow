"""RUN11: supervisor templates must not promise automatic metrics.

Metrics are on-demand — on the owner's request or when the supervisor
decides a summary is needed. The engine already works that way
(``run_finish`` in ``awf/api/run.py`` does not call metrics); this test
pins the prompt text to the same semantics so the templates cannot
drift back to "at the end of the run" / "after unit closure" wording.

Checks run on whitespace-collapsed text: the original end-of-run phrase
was line-wrapped across two lines, so a literal substring match would
never catch a drift back to it.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SUPERVISOR_MD = [
    REPO_ROOT / "awf" / "templates" / "roles" / "supervisor.md",
    REPO_ROOT / "templates" / "roles" / "supervisor.md",
]
PHASE_VERIFY_MD = [
    REPO_ROOT / "awf" / "templates" / "roles" / "supervisor" / "phase-verify.md",
    REPO_ROOT / "templates" / "roles" / "supervisor" / "phase-verify.md",
]


def _read(path: Path) -> str:
    assert path.is_file(), f"missing template: {path}"
    return path.read_text(encoding="utf-8")


def _flat(path: Path) -> str:
    return re.sub(r"\s+", " ", _read(path))


def test_supervisor_templates_do_not_promise_end_of_run_metrics():
    for path in SUPERVISOR_MD:
        text = _flat(path)
        assert "в конце забега" not in text, (
            f"{path}: end-of-run auto-metrics promised in the template"
        )


def test_phase_verify_templates_do_not_promise_post_closure_metrics():
    for path in PHASE_VERIFY_MD:
        text = _flat(path)
        assert "после закрытия юнита" not in text, (
            f"{path}: post-closure auto-metrics promised in the template"
        )


def test_templates_state_metrics_are_on_demand():
    for path in SUPERVISOR_MD + PHASE_VERIFY_MD:
        text = _flat(path)
        assert "по запросу" in text, (
            f"{path}: no on-demand metrics wording left in the template"
        )
