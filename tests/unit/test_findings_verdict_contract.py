"""A67: the findings contract — schema, delivery, ownership, fix-round text.

The verdict itself is pinned in ``test_findings_verdict.py``. Here the
contract around it: the closed severity table, the DONE/PROBES ownership
(A27), the schema line the reviewer actually receives, the one-line-per-
finding rendering, and the text the fix round gets (A2).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from awf import done_owner, pipeline_engine
from awf.stage_context_blocks import _OUTPUT_TEXT, _RULES_TEXT
from awf.unit_contract import (
    collect_findings,
    finding_line,
    finding_severity,
    findings_lines,
    findings_worst,
    parse_done_json,
    render_done_json,
)
from tests.spec_support import init_project_repo

TODO = "TODO-0001"


def _outbox(repo: Path) -> Path:
    outbox = repo / ".agentic" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    return outbox


# ─── the closed severity table ─────────────────────────────────────────


def test_known_severities_map_to_themselves():
    assert finding_severity("blocker") == "blocker"
    assert finding_severity(" MAJOR ") == "major"
    assert finding_severity("minor") == "minor"


def test_unknown_severity_is_a_blocker():
    """A typo is not a fourth value: the table is closed, unknown stops."""
    assert finding_severity("критично") == "blocker"
    assert finding_severity(None) == "blocker"
    assert finding_severity(3) == "blocker"
    assert findings_worst([{"severity": "критично"}]) == "blocker"


def test_worst_finding_wins():
    findings = [
        {"severity": "minor"},
        {"severity": "major"},
        {"severity": "minor"},
    ]
    assert findings_worst(findings) == "major"


# ─── the schema ────────────────────────────────────────────────────────


def test_no_findings_field_is_no_findings():
    data = parse_done_json(json.dumps({"tests_run": []}))
    assert data == {"tests_run": []}
    assert findings_worst(data.get("findings")) == ""


def test_findings_schema_accepts_objects_and_keeps_values():
    data = parse_done_json(
        json.dumps({
            "findings": [
                {"id": "R1", "severity": "major", "file": "awf/x.py",
                 "claim": "one line"},
            ]
        })
    )
    assert data is not None
    assert data["findings"][0]["id"] == "R1"


def test_a_non_object_finding_invalidates_the_done_json():
    assert parse_done_json(json.dumps({"findings": ["major"]})) is None
    assert parse_done_json(json.dumps({"findings": "major"})) is None


def test_null_findings_mean_no_findings(tmp_path):
    """Explicit null is the schema's "absent", not a malformed block."""
    repo = init_project_repo(tmp_path)
    outbox = _outbox(repo)
    (outbox / f"DONE-{TODO}.json").write_text(
        json.dumps({"findings": None}), encoding="utf-8"
    )
    assert parse_done_json(json.dumps({"findings": None})) == {"findings": None}
    assert collect_findings(outbox, TODO) == []


def test_malformed_findings_block_counts_as_a_blocker(tmp_path):
    """Closed table: a findings block the reader cannot parse stops the stage."""
    repo = init_project_repo(tmp_path)
    outbox = _outbox(repo)
    (outbox / f"DONE-{TODO}.json").write_text(
        json.dumps({"findings": "major"}), encoding="utf-8"
    )
    assert findings_worst(collect_findings(outbox, TODO)) == "blocker"


# ─── union of both claims files (A67, виток 3) ─────────────────────────


def test_findings_from_done_and_probes_are_unioned(tmp_path):
    """Neither file may overwrite the other: the reviewer uses both."""
    repo = init_project_repo(tmp_path)
    outbox = _outbox(repo)
    (outbox / f"PROBES-{TODO}.json").write_text(
        json.dumps({"findings": [{"id": "P1", "severity": "minor"}]}),
        encoding="utf-8",
    )
    (outbox / f"DONE-{TODO}.json").write_text(
        json.dumps({"findings": [{"id": "D1", "severity": "major"}]}),
        encoding="utf-8",
    )
    collected = collect_findings(outbox, TODO)
    assert {item["id"] for item in collected} == {"P1", "D1"}
    assert findings_worst(collected) == "major"


def test_duplicate_id_keeps_the_higher_severity(tmp_path):
    repo = init_project_repo(tmp_path)
    outbox = _outbox(repo)
    (outbox / f"PROBES-{TODO}.json").write_text(
        json.dumps({"findings": [
            {"id": "R1", "severity": "minor", "claim": "проба"},
        ]}),
        encoding="utf-8",
    )
    (outbox / f"DONE-{TODO}.json").write_text(
        json.dumps({"findings": [
            {"id": "R1", "severity": "major", "claim": "отчёт"},
        ]}),
        encoding="utf-8",
    )
    collected = collect_findings(outbox, TODO)
    assert len(collected) == 1
    assert collected[0]["severity"] == "major"


def test_empty_probes_does_not_zero_the_done_findings(tmp_path):
    """A PROBES file without `findings` is not a veto on the DONE report."""
    repo = init_project_repo(tmp_path)
    outbox = _outbox(repo)
    (outbox / f"PROBES-{TODO}.json").write_text(
        json.dumps({"tests_run": [{"cmd": "pytest", "result": "3 passed"}]}),
        encoding="utf-8",
    )
    (outbox / f"DONE-{TODO}.json").write_text(
        json.dumps({"findings": [{"severity": "major"}]}), encoding="utf-8"
    )
    assert findings_worst(collect_findings(outbox, TODO)) == "major"


def test_findings_survive_the_restore_when_probes_exist(tmp_path):
    """The QA probe: findings in DONE, facts in PROBES, then A27 restores DONE."""
    repo = init_project_repo(tmp_path)
    outbox = _outbox(repo)
    done = outbox / f"DONE-{TODO}.json"
    done.write_text(json.dumps({
        "stage": {"stage_id": "agent-implementer", "attempt": 1},
        "tests_run": [{"cmd": "pytest", "result": "1 passed"}],
    }), encoding="utf-8")

    started = time.time()
    snapshot = done_owner.snapshot(outbox, TODO)
    done.write_text(json.dumps({
        "stage": {"stage_id": "agent-qa-review", "attempt": 1},
        "findings": [{"id": "R1", "severity": "major", "file": "awf/x.py",
                      "claim": "дыра в гейте"}],
    }), encoding="utf-8")
    (outbox / f"PROBES-{TODO}.json").write_text(
        json.dumps({"tests_run": [{"cmd": "pytest", "result": "3 passed"}]}),
        encoding="utf-8",
    )
    done_owner.restore(
        outbox, TODO, stage_id="agent-qa-review", snapshot_text=snapshot
    )

    collected = collect_findings(outbox, TODO, since=int(started))
    assert pipeline_engine.review_findings_rejected(
        collected, threshold="major"
    ) is True, f"находка ревьюера потерялась: {collected}"


def test_a_malformed_reader_block_is_not_lost_on_the_merge(tmp_path):
    """The closed table holds on the restore path too: malformed = blocker.

    A reader that writes both files may have a broken ``findings`` block in
    its DONE. The merge into PROBES must keep it as the verdict reader's
    blocker instead of silently dropping it with the restored author file.
    """
    repo = init_project_repo(tmp_path)
    outbox = _outbox(repo)
    done = outbox / f"DONE-{TODO}.json"
    done.write_text(json.dumps({
        "stage": {"stage_id": "agent-implementer", "attempt": 1},
        "tests_run": [{"cmd": "pytest", "result": "1 passed"}],
    }), encoding="utf-8")

    started = time.time()
    snapshot = done_owner.snapshot(outbox, TODO)
    done.write_text(json.dumps({
        "stage": {"stage_id": "agent-qa-review", "attempt": 1},
        "findings": "major",
    }), encoding="utf-8")
    (outbox / f"PROBES-{TODO}.json").write_text(
        json.dumps({"tests_run": [{"cmd": "pytest", "result": "3 passed"}]}),
        encoding="utf-8",
    )
    done_owner.restore(
        outbox, TODO, stage_id="agent-qa-review", snapshot_text=snapshot
    )

    assert findings_worst(collect_findings(outbox, TODO, since=int(started))) == (
        "blocker"
    )


def test_the_author_finding_cannot_judge_the_reviewer(tmp_path):
    """The QA scenario: reviewer minor + restored author major = must pass."""
    repo = init_project_repo(tmp_path)
    outbox = _outbox(repo)
    done = outbox / f"DONE-{TODO}.json"
    done.write_text(json.dumps({
        "stage": {"stage_id": "agent-architect-review", "attempt": 1},
        "findings": [{"severity": "major", "claim": "архитектор: дрейф"}],
    }), encoding="utf-8")

    started = time.time()
    snapshot = done_owner.snapshot(outbox, TODO)
    done.write_text(json.dumps({
        "stage": {"stage_id": "agent-qa-review", "attempt": 1},
    }), encoding="utf-8")
    (outbox / f"PROBES-{TODO}.json").write_text(
        json.dumps({"findings": [{"severity": "minor"}]}), encoding="utf-8"
    )
    done_owner.restore(
        outbox, TODO, stage_id="agent-qa-review", snapshot_text=snapshot
    )

    collected = collect_findings(outbox, TODO, since=int(started))
    assert pipeline_engine.review_findings_rejected(
        collected, threshold="major"
    ) is False, f"вердикт поднят находкой автора: {collected}"


def test_collect_findings_drops_stale_claims(tmp_path):
    repo = init_project_repo(tmp_path)
    outbox = _outbox(repo)
    path = outbox / f"DONE-{TODO}.json"
    path.write_text(
        json.dumps({"findings": [{"severity": "blocker"}]}), encoding="utf-8"
    )
    # A stage that starts later must not inherit the previous attempt's word.
    assert collect_findings(outbox, TODO, since=path.stat().st_mtime + 60) == []


# ─── the schema reaches the reviewer (A1) ──────────────────────────────


def test_output_contract_names_the_findings_field():
    assert "findings" in _OUTPUT_TEXT
    assert "blocker|major|minor" in _OUTPUT_TEXT
    assert "нет поля — находок нет" in _OUTPUT_TEXT
    assert "severity вне трёх значений = blocker" in _OUTPUT_TEXT
    # The rules that must survive the edit (RULES keeps its 350 budget).
    assert "прошлая сессия не источник фактов" in _RULES_TEXT
    assert len(_RULES_TEXT) <= 350
    assert len(_OUTPUT_TEXT) <= 800


def test_reviewer_role_templates_name_the_findings_field():
    root = Path(__file__).resolve().parents[2] / "awf" / "templates" / "roles"
    for name in (
        "agent-architect-review.md",
        "agent-qa-review.md",
        "agent-project-auditor.md",
        "agent-requirements-reviewer.md",
    ):
        text = (root / name).read_text(encoding="utf-8")
        assert "findings" in text, name
        assert "blocker|major|minor" in text, name


# ─── one line per finding: handoff + fix round (A2) ────────────────────


def test_render_done_json_emits_each_finding_with_its_address():
    lines = render_done_json({
        "findings": [
            {"id": "R1", "severity": "major", "file": "awf/x.py",
             "claim": "второй парсер"},
            {"id": "R2", "severity": "minor", "file": "awf/y.py",
             "claim": "мёртвый код"},
        ]
    })
    text = "\n".join(lines)
    assert "R1 [major] `awf/x.py` — второй парсер" in text
    assert "R2 [minor] `awf/y.py` — мёртвый код" in text


def test_finding_line_has_the_same_spelling_everywhere():
    item = {"id": "R1", "severity": "major", "file": "awf/x.py",
            "claim": "  вторая   копия "}
    assert finding_line(item) == "R1 [major] `awf/x.py` — вторая копия"
    assert findings_lines([item]) == [f"- {finding_line(item)}"]


def test_the_fix_round_carries_the_structured_findings(tmp_path):
    """A2: the implementer gets id/severity/file/claim, not a count."""
    repo = init_project_repo(tmp_path)
    outbox = _outbox(repo)
    (outbox / f"PROBES-{TODO}.json").write_text(
        json.dumps({"findings": [
            {"id": "R1", "severity": "major", "file": "awf/x.py",
             "claim": "вторая копия правила"},
        ]}),
        encoding="utf-8",
    )
    handoff = repo / ".agentic" / "handoff" / "agent-qa-review-TODO-0001.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# review handoff\n", encoding="utf-8")

    text = pipeline_engine._fix_round_text(
        repo, TODO, "agent-qa-review"
    )

    assert "R1 [major] `awf/x.py` — вторая копия правила" in text
    assert text.index("R1 [major]") < text.index("# review handoff")


def test_stage_without_findings_has_an_empty_fix_round_block(tmp_path):
    repo = init_project_repo(tmp_path)
    _outbox(repo)
    assert pipeline_engine._findings_text(repo, TODO) == ""


def test_the_authors_restored_done_is_not_the_fix_round_work_list(tmp_path):
    """A5 on the second surface: freshness guards the verdict AND the fix round.

    A27 puts the author's DONE back after a reader stage; its mtime is fresh
    but it predates the review stage's start, so its findings are not this
    round's instruction.
    """
    repo = init_project_repo(tmp_path)
    outbox = _outbox(repo)
    done = outbox / f"DONE-{TODO}.json"
    done.write_text(json.dumps({"findings": [
        {"id": "A1", "severity": "major", "file": "awf/x.py",
         "claim": "находка автора"},
    ]}), encoding="utf-8")
    handoff = repo / ".agentic" / "handoff" / "agent-qa-review-TODO-0001.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text("# review handoff\n", encoding="utf-8")

    text = pipeline_engine._fix_round_text(
        repo, TODO, "agent-qa-review", since=done.stat().st_mtime + 60
    )

    assert "находка автора" not in text
    assert "# review handoff" in text
