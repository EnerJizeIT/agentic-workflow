"""M7, architect-review (TODO-0231): one vocabulary for review rounds.

The wave made the supervisor's appender (``review_append`` →
``_next_review_round``) and the RUN-REPORT counter (``_iteration_lines``)
ask the same question — "what round does this ``## REVIEW-фикс`` heading
declare" — and each answered with its own regexes. On a unit whose history
uses the legacy ``## REVIEW-фикс 2`` spelling the appender restarted at 1
while the report read 3: two facts with one name. The parser now lives in
``awf.todos`` for both; these tests pin the shared vocabulary, the
agreement on a mixed history, and the absence of a second TODO reader
next to the one.
"""
from __future__ import annotations

from pathlib import Path

T = "TODO-0001"


def test_review_round_reads_both_spellings_and_ignores_iter() -> None:
    from awf import todos

    assert todos.review_round("## REVIEW-фикс (супервизор, виток 3 — x)") == 3
    assert todos.review_round("## REVIEW-фикс 2 (verify, iter 4)") == 2
    assert todos.review_round("## REVIEW-фикс (verify, iter 17)") is None
    assert todos.review_round("not a heading") is None


def test_report_and_appender_agree_on_a_mixed_history(tmp_path: Path) -> None:
    from awf import paths
    from awf.api import run as run_mod
    from awf.api.hygiene import _next_review_round

    text = (
        "# Task\n\n"
        "## REVIEW-фикс 2 (verify, iter 4)\n\n"
        "## REVIEW-фикс 3 (verify, iter 5)\n\n"
        "## REVIEW-фикс (супервизор, виток 5 — закреплено)\n"
    )
    done = paths.done_dir(tmp_path) / T / "TODO.md"
    done.parent.mkdir(parents=True)
    done.write_text(text, encoding="utf-8")

    lines = run_mod._iteration_lines(tmp_path, [T])
    assert any(f"- {T}: iters: 5 (" in line for line in lines), lines
    assert _next_review_round(text) == 6, (
        "the next round must be past the legacy numbers too — the appender "
        "and the report read ONE vocabulary"
    )


def test_mixed_numbered_and_unnumbered_history_advances_past_the_number(
    tmp_path: Path,
) -> None:
    """AA1 (TODO-0231 виток 7): mixed history → ``max(declared) + 1``.

    An unnumbered section sits beside a numbered one; the declared numbers
    win — the report reads ``iters: 3`` (max of ``[3]``) and the appender
    must not restart at the position fallback (4 = 3 sections + 1).
    """
    from awf import paths
    from awf.api import run as run_mod
    from awf.api.hygiene import _next_review_round

    text = (
        "# Task\n\n"
        "## REVIEW-фикс (решение супервизора)\n\n"
        "## REVIEW-фикс (супервизор, виток 3 — закреплено)\n"
    )
    done = paths.done_dir(tmp_path) / T / "TODO.md"
    done.parent.mkdir(parents=True)
    done.write_text(text, encoding="utf-8")

    lines = run_mod._iteration_lines(tmp_path, [T])
    assert any(f"- {T}: iters: 3 (" in line for line in lines), lines
    assert _next_review_round(text) == 4, _next_review_round(text)


def test_no_second_todo_reader_next_to_the_one() -> None:
    from awf import candidate_verify

    assert not hasattr(candidate_verify, "read_todo_text"), (
        "the TODO reader has ONE owner (awf.todos.read_todo_text); a "
        "forwarding second name lets the two drift again"
    )
