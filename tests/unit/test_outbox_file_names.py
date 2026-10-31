"""A12/QA-4: outbox names belong to a unit only at a real prefix boundary.

The diagnostic line lists files "for this unit"; a foreign name that merely
starts with a known prefix (``DONEFOO-…``) must not be shown as the worker's
artifact. Kept apart from ``test_done_signal_diagnostics.py`` so that file
stays under the 400-line ratchet line.
"""
from __future__ import annotations

from awf import pipeline_engine


def test_unit_file_names_requires_a_prefix_boundary(tmp_path):
    """QA-4: ``DONEFOO-...`` — не префикс ``DONE``, чужое имя.

    Префикс обязан кончаться на ``-``/``.``: иначе диагностика выдаёт
    посторонний файл за артефакт юнита.
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    for name in ("DONEFOO-TODO-0001.md", "PROGRESSIVE-TODO-0001.md"):
        (outbox / name).write_text("x", encoding="utf-8")
    (outbox / "DONE-TODO-0001.md").write_text("# done\n", encoding="utf-8")
    (outbox / "TEST-RESULTS-TODO-0001.log").write_text("ok\n", encoding="utf-8")

    assert pipeline_engine._unit_file_names(outbox, "TODO-0001") == [
        "DONE-TODO-0001.md",
        "TEST-RESULTS-TODO-0001.log",
    ]
