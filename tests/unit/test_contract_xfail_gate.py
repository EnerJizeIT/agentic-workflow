"""R4 (OPS IMP4): xfail/skip in contract ``verify:`` commands is not green.

The gate judges the CANONICAL (first) pytest summary of every test-like
command: more ``xfailed``/``skipped`` than the unit allows is a FAIL, an
``xpassed`` is a pass with a note. ``flake: off`` only drops the repeat
run — the summary is still judged. Every check here drives
``_contract_tests_section`` with a fake runner, so no real pytest runs.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import verify_pack as vp
from awf.unit_contract import parse_todo_contract

PYTEST = "pytest -q"


def _run_fake(monkeypatch, outputs: dict[str, list[tuple[int, str, bool]]]):
    calls: list[str] = []

    def fake(cmd, cwd, timeout, env=None):
        calls.append(cmd)
        queue = outputs[cmd]
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(vp, "_run_cmd", fake)
    return calls


def _section(tmp_path: Path, monkeypatch, contract: dict,
             outputs: dict[str, list[tuple[int, str, bool]]]):
    calls = _run_fake(monkeypatch, outputs)
    section = vp._contract_tests_section(tmp_path, contract, 10)
    return section, calls


def _out(summary: str) -> tuple[int, str, bool]:
    return 0, f"....\n{summary}\n", False


# ─── summary parsing ─────────────────────────────────────────────────────


class TestPytestCounts:
    def test_last_summary_line_wins(self) -> None:
        text = "1 xfailed in 0.1s\n..\n1 xfailed, 2 skipped in 0.2s"
        assert vp._pytest_counts(text) == {"xfailed": 1, "skipped": 2}

    def test_plural_and_missing_words(self) -> None:
        assert vp._pytest_counts("2 passed, 1 error in 0.1s") == {
            "passed": 2, "error": 1,
        }

    def test_plural_errors_share_the_key(self) -> None:
        assert vp._pytest_counts("2 errors in 0.1s") == {"error": 2}

    def test_colourised_summary_is_parsed(self) -> None:
        # PY_COLORS=1 / FORCE_COLOR / --color=yes make pytest wrap the whole
        # summary in SGR escapes; the closing "m" of the first escape is a
        # word character, so an unstripped "\b" would miss "1 xfailed".
        line = "\x1b[33m\x1b[33m\x1b[1m1 xfailed\x1b[0m\x1b[33m in 0.01s\x1b[0m"
        assert vp._pytest_counts(line) == {"xfailed": 1}

    def test_no_summary(self) -> None:
        assert vp._pytest_counts("plain output") == {}


# ─── AC11–AC13: allowance per command ────────────────────────────────────


class TestAllowance:
    def test_xfailed_without_allow_fails(self, tmp_path: Path, monkeypatch) -> None:
        section, _ = _section(
            tmp_path, monkeypatch, {"verify": [PYTEST]},
            {PYTEST: [_out("1 xfailed in 0.1s")]},
        )
        assert section.status == "fail"
        line = section.lines[0]
        assert PYTEST in line and "1 xfailed" in line

    def test_colourised_xfailed_still_fails(self, tmp_path: Path,
                                            monkeypatch) -> None:
        # AC11 under forced colour: the gate must not go green just because
        # pytest decorated the summary.
        coloured = "\x1b[33m\x1b[1m1 xfailed\x1b[0m\x1b[33m in 0.01s\x1b[0m"
        section, _ = _section(
            tmp_path, monkeypatch, {"verify": [PYTEST], "flake": "off"},
            {PYTEST: [_out(coloured)]},
        )
        assert section.status == "fail"
        assert "1 xfailed" in section.lines[0]

    def test_skip_allow_passes_with_line(self, tmp_path: Path, monkeypatch) -> None:
        section, _ = _section(
            tmp_path, monkeypatch,
            {"verify": [PYTEST], "skip_allow": 2},
            {PYTEST: [_out("2 skipped in 0.1s")]},
        )
        assert section.status == "pass"
        assert "allowed 0 xfailed, 2 skipped" in section.lines[0]

    def test_allowance_is_per_command(self, tmp_path: Path, monkeypatch) -> None:
        one, two = "pytest a.py -q", "pytest b.py -q"
        section, _ = _section(
            tmp_path, monkeypatch,
            {"verify": [one, two], "skip_allow": 1},
            {one: [_out("1 skipped in 0.1s")], two: [_out("1 skipped in 0.1s")]},
        )
        assert section.status == "pass"
        assert section.detail.startswith("2 of 2")

    def test_xfailed_over_allow_fails(self, tmp_path: Path, monkeypatch) -> None:
        section, _ = _section(
            tmp_path, monkeypatch,
            {"verify": [PYTEST], "xfail_allow": 1},
            {PYTEST: [_out("2 xfailed in 0.1s")]},
        )
        assert section.status == "fail"
        assert "2 xfailed" in section.lines[0]

    def test_rc_nonzero_fails_with_allow(self, tmp_path: Path, monkeypatch) -> None:
        section, _ = _section(
            tmp_path, monkeypatch,
            {"verify": [PYTEST], "xfail_allow": 5},
            {PYTEST: [(1, "1 failed in 0.1s", False)]},
        )
        assert section.status == "fail"
        assert "FAIL (rc=1)" in section.lines[0]

    def test_non_test_command_is_not_judged(self, tmp_path: Path, monkeypatch) -> None:
        cmd = 'echo "1 skipped"'
        section, _ = _section(
            tmp_path, monkeypatch, {"verify": [cmd]},
            {cmd: [(0, "1 skipped\n", False)]},
        )
        assert section.status == "pass"
        assert section.lines[0].endswith(": pass")


# ─── AC13: contract errors ───────────────────────────────────────────────


class TestAllowValidation:
    @pytest.mark.parametrize("block", [
        "xfail_allow: -1", 'skip_allow: "2"', "skip_allow: true",
    ])
    def test_bad_allowance_is_contract_error(self, block: str) -> None:
        text = f'---\nverify: ["pytest -q"]\n{block}\n---\n# TODO\n'
        with pytest.raises(ValueError) as err:
            parse_todo_contract(text)
        assert "allow" in str(err.value)

    def test_zero_allowance_parses(self) -> None:
        text = '---\nxfail_allow: 0\nskip_allow: 3\n---\n# TODO\n'
        contract, unknown = parse_todo_contract(text)
        assert contract == {"xfail_allow": 0, "skip_allow": 3}
        assert unknown == []


# ─── AC22/AC26: first summary, xpassed ───────────────────────────────────


class TestCanonicalSummary:
    def test_first_xfailed_second_clean_fails(self, tmp_path: Path,
                                              monkeypatch) -> None:
        section, calls = _section(
            tmp_path, monkeypatch, {"verify": [PYTEST]},
            {PYTEST: [_out("1 xfailed in 0.1s"), _out("3 passed in 0.1s")]},
        )
        assert section.status == "fail"
        assert len(calls) == 2
        assert "flake: unstable" in section.lines[0]
        assert "flake: ok" not in section.lines[0]
        # The canonical summary already failed, so the repeat note must not
        # claim that "flakiness alone does not fail the gate".
        assert "flakiness alone does not fail the gate" not in section.lines[0]
        assert "canonical (first) summary decides" in section.lines[0]

    def test_first_clean_second_xfailed_passes_unstable(self, tmp_path: Path,
                                                        monkeypatch) -> None:
        section, _ = _section(
            tmp_path, monkeypatch, {"verify": [PYTEST]},
            {PYTEST: [_out("3 passed in 0.1s"), _out("1 xfailed in 0.1s")]},
        )
        assert section.status == "pass"
        assert "flake: unstable" in section.lines[0]

    def test_xpassed_is_a_note_not_a_judgement(self, tmp_path: Path,
                                               monkeypatch) -> None:
        section, _ = _section(
            tmp_path, monkeypatch,
            {"verify": [PYTEST], "xfail_allow": 1},
            {PYTEST: [_out("1 xpassed in 0.1s")]},
        )
        assert section.status == "pass"
        assert "1 xpassed (stale xfail)" in section.lines[0]


# ─── AC25: every test-like wrapper is judged, flake: off included ───────


class TestWrappersAndFlakeOff:
    @pytest.mark.parametrize("cmd", [
        "uv run pytest", "poetry run pytest",
        "env python -m pytest", "/opt/venv/bin/pytest",
    ])
    def test_wrapper_is_judged(self, tmp_path: Path, monkeypatch,
                               cmd: str) -> None:
        section, _ = _section(
            tmp_path, monkeypatch, {"verify": [cmd]},
            {cmd: [_out("1 xfailed in 0.1s")]},
        )
        assert section.status == "fail"
        assert cmd in section.lines[0]

    def test_flake_off_still_judges_xfailed(self, tmp_path: Path,
                                            monkeypatch) -> None:
        section, calls = _section(
            tmp_path, monkeypatch,
            {"verify": [PYTEST], "flake": "off"},
            {PYTEST: [_out("1 xfailed in 0.1s")]},
        )
        assert section.status == "fail"
        assert len(calls) == 1

    def test_flake_off_clean_summary_passes(self, tmp_path: Path,
                                            monkeypatch) -> None:
        section, calls = _section(
            tmp_path, monkeypatch,
            {"verify": [PYTEST], "flake": "off"},
            {PYTEST: [_out("3 passed in 0.1s")]},
        )
        assert section.status == "pass"
        assert len(calls) == 1

# ─── C2 step 3 / AC11: the allowance boundary and combined numbers ───────


class TestBoundaryAndCombined:
    def test_xfailed_within_allow_passes_with_line(self, tmp_path: Path,
                                                   monkeypatch) -> None:
        section, _ = _section(
            tmp_path, monkeypatch,
            {"verify": [PYTEST], "xfail_allow": 1},
            {PYTEST: [_out("1 xfailed in 0.1s")]},
        )
        assert section.status == "pass"
        assert "allowed 1 xfailed, 0 skipped" in section.lines[0]

    def test_both_categories_over_allowance_are_named(self, tmp_path: Path,
                                                      monkeypatch) -> None:
        section, _ = _section(
            tmp_path, monkeypatch,
            {"verify": [PYTEST], "xfail_allow": 1},
            {PYTEST: [_out("2 xfailed, 1 skipped in 0.1s")]},
        )
        assert section.status == "fail"
        line = section.lines[0]
        assert "2 xfailed > xfail_allow 1" in line
        assert "1 skipped > skip_allow 0" in line

    def test_flake_off_judges_skipped_single_run(self, tmp_path: Path,
                                                 monkeypatch) -> None:
        section, calls = _section(
            tmp_path, monkeypatch,
            {"verify": [PYTEST], "flake": "off"},
            {PYTEST: [_out("1 skipped in 0.1s")]},
        )
        assert section.status == "fail"
        assert len(calls) == 1
        assert "1 skipped > skip_allow 0" in section.lines[0]


# ─── AC13 / INV5: a bad allowance is a broken contract, not a warning ────


class TestBrokenContractFromAllowance:
    def test_bad_allowance_fails_the_section_unsaid(self, tmp_path: Path,
                                                     monkeypatch) -> None:
        inbox = tmp_path / ".agentic" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "TODO-9999.md").write_text(
            '---\nverify: ["pytest -q"]\nxfail_allow: "2"\n---\n# TODO\n',
            encoding="utf-8",
        )

        def boom(*_args, **_kwargs):
            raise AssertionError("no command may run for a broken contract")

        monkeypatch.setattr(vp, "_run_cmd", boom)
        contract = vp._load_todo_contract(tmp_path, "TODO-9999", tmp_path)
        assert isinstance(contract, vp._BrokenContract)

        section = vp._contract_tests_section(tmp_path, contract, 10)
        assert section.status == "fail"
        assert "does not parse" in section.lines[0]
