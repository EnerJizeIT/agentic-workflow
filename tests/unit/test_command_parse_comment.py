"""A66: a trailing `` # comment`` is prose, not a command argument.

The ``done_json`` rerun used to feed the comment words to pytest as paths
and call an honest green claim a contradiction (TODO-0385). The shared
parser owns the rule now: ``awf.command_parse`` strips the comment once.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from awf import command_parse, verify_pack

# --- the parser: a comment never becomes a word --------------------------


def test_a_trailing_comment_leaves_the_argv_unchanged():
    plain = command_parse.split_argv("python3 -c 'print(1)'")
    commented = command_parse.split_argv(
        "python3 -c 'print(1)'  # under 16 CPU busy loops"
    )
    assert commented == plain


def test_a_hash_inside_quotes_is_not_a_comment():
    argv = command_parse.split_argv("python -c \"print('# not a comment')\"")
    assert argv == ["python", "-c", "print('# not a comment')"]


def test_a_hash_glued_to_a_word_is_not_a_comment():
    assert command_parse.split_argv("echo a#b") == ["echo", "a#b"]


def test_a_line_without_a_comment_parses_exactly_as_before():
    """TODO п. 2: with no `` #`` the parser is the same ``shlex`` reader.

    The split moved this guard out of ``test_done_claims.py``; without it
    only the two hand-picked forms above would hold the promise.
    """
    for line in [
        "pytest tests/",
        "uv run --no-project pytest tests/",
        "python3 -m pytest tests/unit -q",
        "sudo /usr/bin/pytest tests/",
        "bash -c 'pytest tests/'",
        "PYTHONPATH=. poetry run pytest tests/",
        "python3 -c 'import sys; sys.exit(1)'",
        "pytest -k 'a or b' tests/",
        "echo a#b",
        "echo '# not a comment'",
        'python -c "print(\'# not a comment\')"',
    ]:
        assert command_parse.split_argv(line) == shlex.split(line), line


def test_a_comment_still_names_the_runner():
    """The gate's other readers (``detect_runner``, flake probe) see the
    command without the tail, so a note cannot hide a pytest run."""
    assert command_parse.detect_runner("pytest tests/  # a note") == "pytest"
    assert verify_pack._is_test_like("pytest tests/  # a note") is True


def test_an_escaped_quote_does_not_open_a_comment():
    """REVIEW R1: a backslash escapes the quote, so ``#`` stays inside it.

    Cutting at the hash left the quote open, ``shlex`` refused the line and
    the claim was skipped instead of rerun — a quiet loss of the check.
    """
    argv = command_parse.split_argv('python -c "print(\\"a # b\\")"')
    assert argv == ["python", "-c", 'print("a # b")']


def test_an_escaped_space_keeps_the_word_without_a_dangling_backslash():
    """REVIEW Q1/the cut is not trimmed, so ``a\\ `` stays a valid word."""
    assert command_parse.split_argv("echo a\\  # c") == ["echo", "a "]


def test_a_comment_does_not_hide_a_duplicate_option():
    """REVIEW R2: the doctor reads the same argv owner as the rerun."""
    assert command_parse.duplicate_option_values(
        "pytest --timeout=60 tests/  # was --timeout=300"
    ) == {}
    assert command_parse.duplicate_option_values(
        "pytest --timeout=60 tests/ --timeout=300"
    ) == {"--timeout": ["60", "300"]}
    # An apostrophe in the note used to make shlex raise, and the doctor
    # answered "no duplicates" about a command that has two.
    assert command_parse.duplicate_option_values(
        "pytest --timeout=60 --timeout=300  # don't"
    ) == {"--timeout": ["60", "300"]}


# --- the gate: the comment fixes a false contradiction, not the check -----


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".agentic" / "outbox").mkdir(parents=True)
    (project / ".agentic" / "context").mkdir(parents=True)
    return project


def _done(project: Path, tests_run: list[dict], todo: str = "TODO-0001") -> None:
    (project / ".agentic" / "outbox" / f"DONE-{todo}.json").write_text(
        json.dumps({"tests_run": tests_run}), encoding="utf-8"
    )


def test_a_comment_does_not_turn_a_green_claim_red(tmp_path):
    """TODO-0385: the rerun fed `# under 16` to pytest and called it a lie."""
    project = _project(tmp_path)
    _done(
        project,
        # The script exits 5 when the comment words reach it as arguments —
        # the TODO-0385 shape, where pytest read them as paths.
        [{"cmd": "python3 -c 'import sys; sys.exit(len(sys.argv) - 1)'"
                 "  # under 16 CPU busy loops",
          "result": "1 passed"}],
    )

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert report.mismatches == []
    assert report.checked == 1


def test_a_comment_does_not_weaken_the_claim_check(tmp_path):
    """A66 kept the gate narrow: the command is still rerun and judged."""
    project = _project(tmp_path)
    _done(
        project,
        [{"cmd": "python3 -c 'import sys; sys.exit(1)'  # a note",
          "result": "1 passed"}],
    )

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert len(report.mismatches) == 1
    assert report.checked == 1


def test_an_escaped_quote_claim_is_checked_not_skipped(tmp_path):
    """REVIEW R1: a cut inside the escaped quotes skipped the whole claim."""
    project = _project(tmp_path)
    _done(
        project,
        [{"cmd": 'python3 -c "print(\\"a # b\\")"  # prints data',
          "result": "1 passed"}],
    )

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert report.mismatches == []
    assert report.checked == 1


def test_an_escaped_space_claim_is_checked_not_skipped(tmp_path):
    """The cut is not trimmed: ``a\\ `` stays a word, not a dangling ``\\``.

    An ``rstrip`` at the cut left ``echo a\\``, ``shlex`` refused the line and
    the honest claim was skipped as malformed instead of rerun (Q1).
    """
    project = _project(tmp_path)
    _done(
        project,
        [{"cmd": "python3 -c 'import sys; "
                 "sys.exit(0 if sys.argv[1:] == [\"a \"] else 2)' a\\  # c",
          "result": "1 passed"}],
    )

    report = verify_pack.check_done_claims(project, "TODO-0001")

    assert report.mismatches == []
    assert report.checked == 1
    assert "malformed" not in report.skipped
