"""IMP3 (TODO-0343): the verify-pack verdict as facts, not a log (T8/T9).

``read_gates_facts`` reads the SAME report module writes it: the markdown for
section order and the first failure marker, the JSON block for the machine
fields. The fixtures below build reports through ``_render_report`` — a second
hand-written parser would let the reader drift from the writer.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from awf import api
from awf import verify_pack as vp
from awf.pipeline_engine import _write_verify_pack_error_note
from awf.verify_pack import Section, read_gates_facts

TODO = "TODO-0326"
FIRST_FAILURE = "- `pytest tests/unit/test_contracts.py`: FAIL (rc=1) — 2 failed"
SECOND_FAILURE = "- `pytest tests/unit/test_other.py`: FAIL (rc=1) — later"


def _git(path: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def _git_repo(tmp_path: Path) -> Path:
    """A repo whose ``.agentic/`` is ignored — writing the report must not
    change the very tree fingerprint the pack records."""
    repo = tmp_path / "repo"
    (repo / ".agentic" / "context").mkdir(parents=True)
    (repo / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _report(
    project: Path,
    sections: list[Section],
    verdict: str,
    exit_code: int,
    measured: int,
) -> Path:
    context = project / ".agentic" / "context"
    context.mkdir(parents=True, exist_ok=True)
    path = context / f"GATES-{TODO}.md"
    path.write_text(
        vp._render_report(project, TODO, sections, verdict, exit_code, measured),
        encoding="utf-8",
    )
    return path


def _failed_sections() -> list[Section]:
    return [
        Section(
            name="contracts",
            status="fail",
            lines=[FIRST_FAILURE, SECOND_FAILURE],
            measured=True,
            detail="2 failed",
        ),
        Section(name="tests", status="pass", lines=["- `pytest`: pass"], measured=True),
    ]


class TestPackStates:
    def test_ok(self, tmp_path: Path) -> None:
        path = _report(
            tmp_path,
            [Section(name="tests", status="pass", measured=True, detail="all green")],
            "ok",
            0,
            1,
        )

        facts = read_gates_facts(path)

        assert facts is not None
        assert facts["verdict"] == "ok"
        assert facts["exit_code"] == 0
        assert facts["measured"] == 1
        assert facts["sections"] == {"tests": "pass"}
        assert facts["details"] == {"tests": "all green"}
        assert facts["first_failure"] == ""

    def test_failed_reports_first_marker_of_the_first_fail_section(
        self, tmp_path: Path
    ) -> None:
        path = _report(tmp_path, _failed_sections(), "failed", 1, 2)

        facts = read_gates_facts(path)

        assert facts is not None
        assert facts["verdict"] == "failed"
        assert facts["failed_sections"] == ["contracts"]
        # AC27: the first marker line, never the section heading or a later one.
        assert facts["first_failure"] == FIRST_FAILURE
        assert not facts["first_failure"].startswith("##")

    def test_section_heading_alone_is_not_a_reason(self, tmp_path: Path) -> None:
        path = _report(
            tmp_path,
            [
                Section(
                    name="contracts",
                    status="fail",
                    lines=["checker refused the unit"],
                    measured=True,
                )
            ],
            "failed",
            1,
            1,
        )

        facts = read_gates_facts(path)

        assert facts is not None
        assert facts["verdict"] == "failed"
        assert facts["first_failure"] == ""

    def test_nothing_measured(self, tmp_path: Path) -> None:
        path = _report(
            tmp_path,
            [Section(name="contract_tests", status="skipped")],
            "nothing-measured",
            2,
            0,
        )

        facts = read_gates_facts(path)

        assert facts is not None
        assert facts["verdict"] == "nothing-measured"
        assert facts["exit_code"] == 2
        assert facts["measured"] == 0

    def test_error_note_without_json(self, tmp_path: Path) -> None:
        # The engine's own crash note, not a hand-copied format.
        _write_verify_pack_error_note(tmp_path, TODO, RuntimeError("boom"))
        path = tmp_path / ".agentic" / "context" / f"GATES-{TODO}.md"

        facts = read_gates_facts(path)

        assert facts is not None
        assert facts["verdict"] == "error"
        assert facts["note"] == "verify-pack failed before measuring anything:"
        assert facts["sections"] == {}

    def test_missing(self, tmp_path: Path) -> None:
        assert read_gates_facts(tmp_path / "GATES-NONE.md") is None

    def test_broken_json_does_not_take_the_verdict_down(self, tmp_path: Path) -> None:
        path = tmp_path / f"GATES-{TODO}.md"
        path.write_text(
            "# GATES\n\n## Verdict: **failed** (exit 1) — 1 section(s) measured\n\n"
            "```json\n{not json\n```\n",
            encoding="utf-8",
        )

        facts = read_gates_facts(path)

        assert facts is not None
        assert facts["verdict"] == "failed"
        assert facts["sections"] == {}

    def test_broken_json_keeps_headings_and_reason(self, tmp_path: Path) -> None:
        # AC27/D6: the reason lives in the markdown; a truncated JSON block
        # must not hide the failing section or its first marker.
        path = _report(tmp_path, _failed_sections(), "failed", 1, 2)
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text[: text.index("```json")] + "```json\n{not json\n```\n",
            encoding="utf-8",
        )

        facts = read_gates_facts(path)

        assert facts is not None
        assert facts["verdict"] == "failed"
        assert facts["sections"] == {"contracts": "fail", "tests": "pass"}
        assert facts["failed_sections"] == ["contracts"]
        assert facts["first_failure"] == FIRST_FAILURE
        assert facts["generated"] != ""


class TestStaleMarker:
    def test_same_tree_is_fresh(self, tmp_path: Path) -> None:
        from awf import git_utils

        repo = _git_repo(tmp_path)
        path = _report(
            repo, [Section(name="tests", status="pass", measured=True)], "ok", 0, 1
        )
        tree = git_utils.tree_fingerprint(repo)

        facts = read_gates_facts(path, tree_sha=tree)

        assert facts is not None
        assert facts["tree_sha"] == tree
        assert facts["stale"] is False

    def test_tree_moved_after_the_pack(self, tmp_path: Path) -> None:
        from awf import git_utils

        repo = _git_repo(tmp_path)
        path = _report(
            repo, [Section(name="tests", status="pass", measured=True)], "ok", 0, 1
        )
        (repo / "new_work.py").write_text("x = 1\n", encoding="utf-8")

        facts = read_gates_facts(path, tree_sha=git_utils.tree_fingerprint(repo))

        assert facts is not None
        assert facts["stale"] is True

    def test_pack_without_a_recorded_tree_is_treated_as_old(
        self, tmp_path: Path
    ) -> None:
        repo = _git_repo(tmp_path)
        path = _report(
            repo, [Section(name="tests", status="pass", measured=True)], "ok", 0, 1
        )
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace('"tree_sha"', '"tree_sha_removed"'), encoding="utf-8"
        )

        facts = read_gates_facts(path, tree_sha="any-tree")

        assert facts is not None
        assert facts["tree_sha"] == ""
        assert facts["stale"] is True


class TestTreeShaDegradation:
    """The fingerprint is a marker, not a gate: it must never kill the pack."""

    def test_hung_git_does_not_lose_the_report(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # ``tree_fingerprint`` talks to git with a 30 s timeout. Before the
        # fix its TimeoutExpired escaped ``_render_report`` and the engine
        # replaced a real verdict with the crash note ("pack did not run").
        from awf import git_utils

        def _hang(_project: Path) -> str:
            raise subprocess.TimeoutExpired(cmd=["git", "diff"], timeout=30)

        monkeypatch.setattr(git_utils, "tree_fingerprint", _hang)

        path = _report(
            tmp_path,
            [Section(name="tests", status="pass", measured=True)],
            "ok",
            0,
            1,
        )

        facts = read_gates_facts(path)

        assert facts is not None
        assert facts["verdict"] == "ok"
        assert facts["tree_sha"] == ""
        # No recorded tree + a compared one = "older than the tree".
        assert read_gates_facts(path, tree_sha="any-tree")["stale"] is True


def test_api_exposes_the_same_readers() -> None:
    from awf import commit_gate

    assert api.read_gates_facts is vp.read_gates_facts
    assert api.read_commit_audit is commit_gate.read_commit_audit
