"""U11 Part C (E3): awf todo-draft — task-file skeleton from AUDIT-INDEX.md.

Facts only: the generator transfers findings from the registry (id, sev,
type, title, unit, file references) and leaves the intent (criteria,
verify commands, scope narrowing) as supervisor placeholders. No
invented criteria, no network — a local index file only.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import todo_draft as td
from awf.unit_contract import parse_todo_contract

SAMPLE_INDEX = """\
# AUDIT-INDEX · test project

## Сводный реестр находок

| ID | Sev | Тип | Заголовок | Итерация | Фикс | Статус |
|----|-----|-----|-----------|----------|------|--------|
| TEST-01 | P1 | BUG | `checkpoint_pending` залипает после timeout (awf/run_state.py:55) | 02 | S | ✅ FU-13 (abc1234) |
| TEST-02 | P3 | DOC | Доки: поля run.yaml не описаны | 02 | S | ✅ FU-13 (abc1234) |
| TEST-03 | P2 | BUG | `mark_plan_step_done` не видит «Step N» (awf/plan_progress.py) | 03 | M | ✅ FU-14 (def5678) |

## Итог аудита

| Юнит | Тема | ID (суммарно) | Фикс | Волна |
|------|------|---------------|------|-------|
| FU-13 | Гигиена state (залипание ключей, порча run.yaml) | 2 | M | 3 |
| FU-14 | Setup-материализация (slug-расхождение) | 1 | S\\|L | 4 |
"""


def _index_file(tmp_path: Path) -> Path:
    p = tmp_path / "AUDIT-INDEX.md"
    p.write_text(SAMPLE_INDEX, encoding="utf-8")
    return p


class TestParse:
    def test_parses_findings_and_units(self) -> None:
        idx = td.parse_audit_index(SAMPLE_INDEX)
        assert [f["id"] for f in idx.findings] == ["TEST-01", "TEST-02", "TEST-03"]
        f1 = idx.findings[0]
        assert f1["sev"] == "P1"
        assert f1["type"] == "BUG"
        assert "checkpoint_pending" in f1["title"]
        assert f1["iteration"] == "02"
        assert f1["fix"] == "S"  # размер фикса
        assert "FU-13" in f1["status"]  # в реестре FU-NN стоит в статусе
        assert [u["unit"] for u in idx.units] == ["FU-13", "FU-14"]
        assert idx.units[0]["theme"].startswith("Гигиена state")
        assert idx.units[0]["wave"] == "3"

    def test_escaped_pipe_in_cell(self) -> None:
        idx = td.parse_audit_index(SAMPLE_INDEX)
        assert idx.units[1]["fix"] == "S|L"

    def test_empty_text(self) -> None:
        idx = td.parse_audit_index("")
        assert idx.findings == []
        assert idx.units == []


class TestExtractFiles:
    def test_extracts_paths_with_and_without_line(self) -> None:
        files = td.extract_files("порча (awf/run_state.py:55) и доки (docs/unit-contract.md)")
        assert files == ["awf/run_state.py", "docs/unit-contract.md"]

    def test_dedupes(self) -> None:
        files = td.extract_files("awf/x.py и снова awf/x.py:10")
        assert files == ["awf/x.py"]

    def test_no_paths(self) -> None:
        assert td.extract_files("никаких файлов тут") == []


class TestDraft:
    def test_finding_draft_carries_registry_facts(self) -> None:
        idx = td.parse_audit_index(SAMPLE_INDEX)
        draft = td.build_todo_draft("TEST-01", idx, "AUDIT-INDEX.md")
        assert "TEST-01" in draft
        assert "P1" in draft
        assert "checkpoint_pending" in draft
        assert "FU-13" in draft
        assert "awf/run_state.py" in draft
        assert "супервизор дополняет" in draft  # placeholders present

    def test_unit_draft_lists_member_findings(self) -> None:
        idx = td.parse_audit_index(SAMPLE_INDEX)
        draft = td.build_todo_draft("FU-13", idx, "AUDIT-INDEX.md")
        assert "Гигиена state" in draft
        assert "TEST-01" in draft
        assert "TEST-02" in draft
        assert "TEST-03" not in draft  # belongs to FU-14
        assert "awf/run_state.py" in draft

    def test_unknown_query_lists_known_ids(self) -> None:
        idx = td.parse_audit_index(SAMPLE_INDEX)
        with pytest.raises(td.TodoDraftError, match="TEST-01"):
            td.build_todo_draft("TEST-99", idx, "AUDIT-INDEX.md")

    def test_query_case_and_dash_normalization(self) -> None:
        idx = td.parse_audit_index(SAMPLE_INDEX)
        assert "TEST-01" in td.build_todo_draft("test-01", idx, "AUDIT-INDEX.md")
        assert "TEST-03" in td.build_todo_draft("TEST-03", idx, "AUDIT-INDEX.md")

    def test_front_matter_is_dispatch_valid(self) -> None:
        """The placeholder front-matter must parse as an empty contract —
        dispatch accepts a task file with no declared keys."""
        idx = td.parse_audit_index(SAMPLE_INDEX)
        for query in ("TEST-01", "FU-13"):
            draft = td.build_todo_draft(query, idx, "AUDIT-INDEX.md")
            contract, unknown = parse_todo_contract(draft)
            assert contract == {}
            assert unknown == []


class TestMakeDraft:
    def test_out_writes_file(self, tmp_path: Path) -> None:
        index = _index_file(tmp_path)
        out = tmp_path / "draft.md"
        out_path, draft = td.make_todo_draft(
            "FU-13", index_path=index, project_dir=tmp_path, out=out
        )
        assert out_path == out
        assert out.is_file()
        assert "FU-13" in out.read_text(encoding="utf-8")

    def test_existing_out_refused_without_force(self, tmp_path: Path) -> None:
        index = _index_file(tmp_path)
        out = tmp_path / "draft.md"
        out.write_text("old")
        with pytest.raises(td.TodoDraftError, match="already exists"):
            td.make_todo_draft("FU-13", index_path=index, project_dir=tmp_path, out=out)
        assert out.read_text(encoding="utf-8") == "old"

    def test_force_overwrites(self, tmp_path: Path) -> None:
        index = _index_file(tmp_path)
        out = tmp_path / "draft.md"
        out.write_text("old")
        td.make_todo_draft(
            "FU-13", index_path=index, project_dir=tmp_path, out=out, force=True
        )
        assert "FU-13" in out.read_text(encoding="utf-8")

    def test_default_search_finds_project_index(self, tmp_path: Path) -> None:
        _index_file(tmp_path)  # <project>/AUDIT-INDEX.md
        out_path, draft = td.make_todo_draft("TEST-03", project_dir=tmp_path)
        assert out_path is None  # stdout mode
        assert "TEST-03" in draft

    def test_explicit_index_wins(self, tmp_path: Path) -> None:
        index = tmp_path / "elsewhere" / "index.md"
        index.parent.mkdir()
        index.write_text(SAMPLE_INDEX, encoding="utf-8")
        _, draft = td.make_todo_draft(
            "TEST-01", index_path=index, project_dir=tmp_path / "nope"
        )
        assert "TEST-01" in draft

    def test_no_index_anywhere_is_error(self, tmp_path: Path) -> None:
        with pytest.raises(td.TodoDraftError, match="AUDIT-INDEX"):
            td.make_todo_draft("TEST-01", project_dir=tmp_path)
