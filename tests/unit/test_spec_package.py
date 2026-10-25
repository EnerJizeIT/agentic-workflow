"""SPEC IMP1 — K1 headers, K2 readiness gate, fingerprint and matrix.

Proves T2 (header as data + refusals), T3 (readiness predicate), T4
(fingerprint, staleness, repeat) and T9 (four-column matrix and locators).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import cli
from awf import spec_package as sp
from awf.spec_package import SpecError

FINGERPRINT_VECTOR = "820b644ee4afc011044b1a82207471464d13cfd240a0d610d3580eec1a643766"


def _gate(tmp_path: Path, package: Path) -> sp.SpecGate:
    return sp.spec_gate(tmp_path, package)


def _rewrite(path: Path, old: str, new: str, count: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path.name}"
    path.write_text(text.replace(old, new, count), encoding="utf-8")


# --- T2: header as data -------------------------------------------------


def test_parse_header_reads_fields_with_line_numbers() -> None:
    text = (
        "# Документ\n\n"
        "Статус: REVIEW\n"
        "Ревизия: req-1\n"
        "Входы: a.md@req-1, b.md@b-2; TODO-0001\n"
        "База кода: commit `abcdef1`; в дереве — untracked\n"
        "\n## Тело\n"
    )
    header = sp.parse_spec_header(text, "requirements", "doc.md")
    assert header.status == "REVIEW"
    assert header.revision == "req-1"
    assert header.inputs == {"a.md": "req-1", "b.md": "b-2"}
    assert header.code_base == "abcdef1"
    assert header.lines["Статус"] == 3
    assert header.lines["База кода"] == 6


def test_parse_review_header_reads_checked_revisions() -> None:
    text = (
        "# Ревью\n\n"
        "Статус: PASS\n"
        "Ревизия: rev-1\n"
        "Входы: a.md@req-1\n"
        "База кода: commit `abcdef1`\n"
        "Проверено: a.md=req-1, b.md=b-2\n"
    )
    header = sp.parse_spec_header(text, "review", "rev.md")
    assert header.status == "PASS"
    assert header.checked == {"a.md": "req-1", "b.md": "b-2"}


def test_parse_readiness_header_reads_package_sha() -> None:
    sha = "a" * 64
    text = (
        "# Готовность\n\n"
        "Статус: READY\n"
        "Ревизия: ready-1\n"
        "Входы: a.md@req-1\n"
        "База кода: commit `abcdef1`\n"
        "Проверено: a.md=req-1\n"
        f"Пакет-sha: `{sha}` — пять документов\n"
    )
    header = sp.parse_spec_header(text, "readiness", "ready.md")
    assert header.package_sha == sha


def test_header_without_naznachenie_parses() -> None:
    text = (
        "# Документ\n\n"
        "Статус: REVIEW\n"
        "Ревизия: req-1\n"
        "Входы: a.md@req-1\n"
        "База кода: commit `abcdef1`\n"
    )
    assert sp.parse_spec_header(text, "requirements", "doc.md").revision == "req-1"


@pytest.mark.parametrize(
    ("text", "role", "needle"),
    [
        (
            "# D\n\nРевизия: req-1\nВходы: a@r\nБаза кода: commit `abcdef1`\n",
            "requirements",
            "Статус",
        ),
        (
            "# D\n\nСтатус: REVIEW\nРевизия:\nВходы: a@r\nБаза кода: commit `abcdef1`\n",
            "requirements",
            "Ревизия",
        ),
        (
            "# D\n\nСтатус: REVEIW\nРевизия: r\nВходы: a@r\nБаза кода: commit `abcdef1`\n",
            "requirements",
            "REVEIW",
        ),
        (
            "# D\n\nСтатус: REVIEW\nРевизия: r\nВходы: a@r\nБаза кода: нет sha\n",
            "requirements",
            "База кода",
        ),
        (
            "# D\n\nСтатус: PASS\nРевизия: r\nВходы: a@r\nБаза кода: commit `abcdef1`\n",
            "review",
            "Проверено",
        ),
        (
            "# D\n\nСтатус: READY\nРевизия: r\nВходы: a@r\n"
            "База кода: commit `abcdef1`\nПроверено: a=r\n",
            "readiness",
            "Пакет-sha",
        ),
    ],
)
def test_header_refusals_name_file_and_key(text: str, role: str, needle: str) -> None:
    with pytest.raises(SpecError) as excinfo:
        sp.parse_spec_header(text, role, "doc.md")
    message = str(excinfo.value)
    assert "doc.md" in message
    assert needle in message


def test_typo_status_is_not_draft() -> None:
    text = (
        "# D\n\nСтатус: DRAFF\nРевизия: r\nВходы: a@r\n"
        "База кода: commit `abcdef1`\n"
    )
    with pytest.raises(SpecError):
        sp.parse_spec_header(text, "requirements", "doc.md")


# --- T4: fingerprint ----------------------------------------------------


def test_fingerprint_vector() -> None:
    assert (
        sp.fingerprint_documents({"a.md": b"x\n", "b/c.md": b"y\n"})
        == FINGERPRINT_VECTOR
    )


def test_fingerprint_is_not_normalized() -> None:
    plain = sp.fingerprint_documents({"a.md": b"x\n"})
    assert sp.fingerprint_documents({"a.md": b"x\r\n"}) != plain
    assert sp.fingerprint_documents({"a.md": b"\xef\xbb\xbfx\n"}) != plain


def test_package_fingerprint_excludes_readiness(make_spec_package) -> None:
    package = make_spec_package()
    before = sp.package_fingerprint(package)
    readiness = package / "pkg.readiness.md"
    readiness.write_text(
        readiness.read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )
    assert sp.package_fingerprint(package) == before
    requirements = package / "pkg.md"
    requirements.write_text(
        requirements.read_text(encoding="utf-8") + "\nЕщё строка.\n",
        encoding="utf-8",
    )
    assert sp.package_fingerprint(package) != before


# --- T3: readiness predicate -------------------------------------------


def test_gate_ready_package(make_spec_package, tmp_path: Path) -> None:
    package = make_spec_package()
    gate = _gate(tmp_path, package)
    assert gate.ok, gate.reason + " / " + " / ".join(gate.details)
    assert gate.package_sha == gate.actual_sha
    assert gate.details == ()
    assert gate.package_dir == "pkg"


def test_gate_refuses_unparsed_header(make_spec_package, tmp_path: Path) -> None:
    package = make_spec_package()
    _rewrite(package / "pkg.md", "Статус: REVIEW", "Статус: REVEIW")
    first = _gate(tmp_path, package)
    second = _gate(tmp_path, package)
    assert not first.ok
    assert "REVEIW" in first.reason
    assert "pkg.md" in first.reason
    assert first.reason == second.reason
    assert first.details == second.details


def test_gate_author_draft_is_not_ready(make_spec_package, tmp_path: Path) -> None:
    package = make_spec_package()
    _rewrite(package / "pkg.context.md", "Статус: REVIEW", "Статус: DRAFT")
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "DRAFT" in gate.reason
    assert "pkg.context.md" in gate.reason


def test_gate_extra_readme_is_the_finding_not_the_requirements(
    make_spec_package, tmp_path: Path
) -> None:
    package = make_spec_package()
    (package / "README.md").write_text("# extra\n", encoding="utf-8")
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "README.md" in gate.reason
    assert "pkg.md" not in gate.reason


def test_gate_ambiguous_requirements_names_both(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "a.md").write_text("a\n", encoding="utf-8")
    (package / "b.md").write_text("b\n", encoding="utf-8")
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "неоднозначна" in gate.reason
    assert "a.md" in gate.reason
    assert "b.md" in gate.reason


def _append_open_questions(path: Path, question: str, owner: str) -> None:
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n\n## Открытые вопросы\n\n"
        + "| ID | Вопрос | Владелец | Влияние |\n"
        + "|---|---|---|---|\n"
        + f"| Q1 | {question} | {owner} | READY |\n",
        encoding="utf-8",
    )


def test_gate_author_blocked_names_question_and_owner(
    make_spec_package, tmp_path: Path
) -> None:
    package = make_spec_package()
    requirements = package / "pkg.md"
    _rewrite(requirements, "Статус: REVIEW", "Статус: BLOCKED")
    _append_open_questions(requirements, "Что делать с бюджетом?", "аналитик")
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "Что делать с бюджетом?" in gate.reason
    assert "аналитик" in gate.reason


def test_gate_blocked_without_open_questions_names_the_section(
    make_spec_package, tmp_path: Path
) -> None:
    package = make_spec_package()
    _rewrite(package / "pkg.md", "Статус: REVIEW", "Статус: BLOCKED")
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "раздела «Открытые вопросы» нет" in gate.reason


def test_gate_readiness_blocked_names_question(make_spec_package, tmp_path: Path) -> None:
    package = make_spec_package()
    readiness = package / "pkg.readiness.md"
    _rewrite(readiness, "Статус: READY", "Статус: BLOCKED")
    _append_open_questions(readiness, "Кто владелец риска?", "ревьюер")
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "Кто владелец риска?" in gate.reason
    assert "ревьюер" in gate.reason


def test_gate_review_fail_refused(make_spec_package, tmp_path: Path) -> None:
    package = make_spec_package()
    _rewrite(
        package / "pkg.requirements-review.md", "Статус: PASS", "Статус: FAIL"
    )
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "FAIL" in gate.reason


def test_gate_stale_review_revision_names_both(
    make_spec_package, tmp_path: Path
) -> None:
    package = make_spec_package()
    _rewrite(package / "pkg.md", "Ревизия: req-1", "Ревизия: req-2")
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "req-1" in gate.reason
    assert "req-2" in gate.reason
    assert "устаревшая ревизия" in gate.reason


def test_gate_readiness_missing_revision(make_spec_package, tmp_path: Path) -> None:
    package = make_spec_package()
    _rewrite(
        package / "pkg.readiness.md",
        ", pkg.test-spec.md=qa-1",
        "",
    )
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "pkg.test-spec.md" in gate.reason
    assert "готовность не покрывает" in gate.reason


def test_gate_base_divergence(make_spec_package, tmp_path: Path) -> None:
    package = make_spec_package()
    _rewrite(
        package / "pkg.architecture.md",
        "commit `abcdef1234`",
        "commit `fffffff999`",
    )
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "расхождение базы" in gate.reason
    assert "abcdef1" in gate.reason


def test_gate_stale_bytes_names_two_shas_and_repeats(
    make_spec_package, tmp_path: Path
) -> None:
    package = make_spec_package()
    declared = sp.package_fingerprint(package)
    requirements = package / "pkg.md"
    requirements.write_text(
        requirements.read_text(encoding="utf-8") + "\nНовая строка.\n",
        encoding="utf-8",
    )
    actual = sp.package_fingerprint(package)
    first = _gate(tmp_path, package)
    second = _gate(tmp_path, package)
    assert not first.ok
    assert declared in first.reason
    assert actual in first.reason
    assert first.reason == second.reason


# --- T9: matrix --------------------------------------------------------


def _matrix_text(rows: str, header: str | None = None) -> str:
    head = header or "| ID | Источник | Сценарий/контракт | Проверка |"
    columns = head.strip("|").count("|") + 1
    return (
        "## Матрица покрытия\n\n"
        f"{head}\n"
        + "|" + "---|" * columns + "\n"
        + rows
    )


def test_parse_matrix_positive(make_spec_package) -> None:
    text = (make_spec_package() / "pkg.readiness.md").read_text(encoding="utf-8")
    rows = sp.parse_coverage_matrix(text, "pkg.readiness.md")
    assert [row.id for row in rows] == ["R1", "AC1", "INV1"]
    assert rows[0].source == "pkg.md#R1"
    assert rows[2].check == "метод: pytest tests/test_x.py"


@pytest.mark.parametrize(
    ("body", "header", "needle"),
    [
        (
            "| R1 | pkg.md#R1 | C1/K1 | T1 |\n",
            "| ID | Источник | Проверка |",
            "4 колонки",
        ),
        (
            "| R1 | pkg.md#R1 | C1/K1 | T1 |\n",
            "| ID | Источник | Сценарий | Проверка |",
            "заголовок матрицы",
        ),
        (
            "| R1 | pkg.md#R1 | C1/K1 | T1 |\n| R1 | pkg.md#R1 | C1/K1 | T1 |\n",
            None,
            "повторяется",
        ),
        (
            "| R1 |  | C1/K1 | T1 |\n",
            None,
            "Источник",
        ),
        (
            "| R1 | pkg.md#R1 | C1/K1 |  |\n",
            None,
            "Проверка",
        ),
        (
            "| R1 | pkg.md#R1 | C1/K1 | метод: см. T1 |\n",
            None,
            "обе формы",
        ),
        (
            "| R1 | pkg.md#R1 | X1/K1 | T1 |\n",
            None,
            "токен сценария",
        ),
        (
            "| R1 | pkg.md#R1 | C1/K1 | IMP1 |\n",
            None,
            "без T#",
        ),
    ],
)
def test_matrix_refusals(body: str, header: str | None, needle: str) -> None:
    with pytest.raises(SpecError) as excinfo:
        sp.parse_coverage_matrix(_matrix_text(body, header), "pkg.readiness.md")
    assert needle in str(excinfo.value)


def test_gate_matrix_missing_row(make_spec_package, tmp_path: Path) -> None:
    package = make_spec_package()
    readiness = package / "pkg.readiness.md"
    text = readiness.read_text(encoding="utf-8")
    readiness.write_text(
        "\n".join(line for line in text.splitlines() if not line.startswith("| AC1 |"))
        + "\n",
        encoding="utf-8",
    )
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "AC1" in gate.reason
    assert "нет строки" in gate.reason


def test_gate_matrix_missing_section(make_spec_package, tmp_path: Path) -> None:
    package = make_spec_package()
    _rewrite(package / "pkg.readiness.md", "pkg.md#R1", "pkg.md#R9")
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "R1" in gate.reason
    assert "секция не найдена" in gate.reason


def test_gate_matrix_missing_token(make_spec_package, tmp_path: Path) -> None:
    package = make_spec_package()
    _rewrite(package / "pkg.readiness.md", "C1/K1/D1", "C1/K9/D1")
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "R1" in gate.reason
    assert "K9" in gate.reason


def test_declared_ids_are_the_requirements_ones() -> None:
    text = "### R1 — a\nAC1 — b\n\nINV1 — c\nтекст AC2 не в начале\n"
    assert sp.declared_requirement_ids(text) == {"R1", "AC1", "INV1"}


# --- CLI parity --------------------------------------------------------


def test_cli_spec_check_ready_and_not_ready(
    make_spec_package, tmp_path: Path, capsys
) -> None:
    package = make_spec_package()
    assert cli.main(["spec", "check", str(package), "--project-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "READY" in out
    assert "Пакет-sha:" in out

    assert cli.main(["spec-check", str(package), "--project-dir", str(tmp_path)]) == 0

    _rewrite(
        package / "pkg.readiness.md",
        "| ID | Источник | Сценарий/контракт | Проверка |",
        "| ID | Источник | Проверка |",
    )
    capsys.readouterr()
    assert cli.main(["spec", "check", str(package), "--project-dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "NOT READY" in out


def test_cli_spec_check_bad_path(tmp_path: Path, capsys) -> None:
    assert cli.main(["spec", "check", "missing", "--project-dir", str(tmp_path)]) == 2
    assert "не найден" in capsys.readouterr().out


# --- QA (TODO-0253): uncovered normative branches ---------------------


def _refresh_package_sha(package: Path) -> None:
    """Rewrite the readiness ``Пакет-sha`` after editing a covered document."""
    import re

    sha = sp.package_fingerprint(package)
    readiness = package / "pkg.readiness.md"
    text = readiness.read_text(encoding="utf-8")
    readiness.write_text(
        re.sub(r"Пакет-sha: \S+", f"Пакет-sha: {sha}", text, count=1),
        encoding="utf-8",
    )


def test_gate_assets_are_declared_attachments(make_spec_package, tmp_path: Path) -> None:
    """AC21: files under ``assets/`` are declared and never extras."""
    package = make_spec_package()
    (package / "assets").mkdir()
    (package / "assets" / "pic.png").write_bytes(b"\x89PNG")
    (package / "assets" / "sub").mkdir()
    (package / "assets" / "sub" / "deep.md").write_text("x", encoding="utf-8")
    gate = _gate(tmp_path, package)
    assert gate.ok, gate.reason + " / " + " / ".join(gate.details)


def test_gate_nested_file_is_an_extra(make_spec_package, tmp_path: Path) -> None:
    """AC21: a subdirectory document is not one of the six."""
    package = make_spec_package()
    (package / "drafts").mkdir()
    (package / "drafts" / "note.md").write_text("x", encoding="utf-8")
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "лишний файл" in gate.reason
    assert "drafts/note.md" in gate.reason


def test_gate_blocked_with_empty_open_questions_names_the_section(
    make_spec_package, tmp_path: Path
) -> None:
    """AC8: a BLOCKED doc without a question row is refused as such."""
    package = make_spec_package()
    requirements = package / "pkg.md"
    _rewrite(requirements, "Статус: REVIEW", "Статус: BLOCKED")
    requirements.write_text(
        requirements.read_text(encoding="utf-8")
        + "\n\n## Открытые вопросы\n\n"
        + "| ID | Вопрос | Владелец | Влияние |\n"
        + "|---|---|---|---|\n",
        encoding="utf-8",
    )
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "раздел «Открытые вопросы» пуст" in gate.reason


def test_gate_stale_readiness_revision_names_both(
    make_spec_package, tmp_path: Path
) -> None:
    """AC8: the third staleness text (old READY) names document and revisions."""
    package = make_spec_package()
    _rewrite(package / "pkg.context.md", "Ревизия: ctx-1", "Ревизия: ctx-2")
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "устаревшая ревизия" in gate.reason
    assert "pkg.context.md=ctx-1" in gate.reason
    assert "ctx-2" in gate.reason


def test_gate_matrix_accepts_plan_and_branch_tokens(
    make_spec_package, tmp_path: Path
) -> None:
    """K1: ``план`` and ``ветка:<текст>`` are legal scenario tokens."""
    package = make_spec_package()
    _rewrite(
        package / "pkg.readiness.md",
        "| R1 | pkg.md#R1 | C1/K1/D1 | T1/IMP1 |",
        "| R1 | pkg.md#R1 | план/ветка:совместимость | T1/IMP1 |",
    )
    gate = _gate(tmp_path, package)
    assert gate.ok, gate.reason + " / " + " / ".join(gate.details)


@pytest.mark.parametrize("source", ["/etc/passwd#R1", "../pkg.md#R1"])
def test_gate_matrix_source_outside_package_refused(
    make_spec_package, tmp_path: Path, source: str
) -> None:
    """K1: the source path is relative to the package directory."""
    package = make_spec_package()
    _rewrite(
        package / "pkg.readiness.md",
        "| R1 | pkg.md#R1 |",
        f"| R1 | {source} |",
    )
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "источник вне пакета" in gate.reason


def test_declared_ids_read_criteria_prefix() -> None:
    """The analyst template declares criteria as ``Критерии: AC1 — ...``."""
    text = "### R1 — a\nКритерии: AC1 — b\nAC2 — c\n\nINV1 — d\nтекст AC3\n"
    assert sp.declared_requirement_ids(text) == {"R1", "AC1", "AC2", "INV1"}


def test_gate_row_for_criteria_prefixed_ac(make_spec_package, tmp_path: Path) -> None:
    """An AC declared on a ``Критерии:`` line still matches its matrix row."""
    package = make_spec_package()
    _rewrite(package / "pkg.md", "AC1 — Первый критерий.", "Критерии: AC1 — первый критерий.")
    _refresh_package_sha(package)
    gate = _gate(tmp_path, package)
    assert gate.ok, gate.reason + " / " + " / ".join(gate.details)


def test_gate_missing_row_for_criteria_prefixed_ac(
    make_spec_package, tmp_path: Path
) -> None:
    """AC13: an inline-declared AC without a row is not ready."""
    package = make_spec_package()
    _rewrite(package / "pkg.md", "AC1 — Первый критерий.", "Критерии: AC1 — первый критерий.")
    _refresh_package_sha(package)
    readiness = package / "pkg.readiness.md"
    readiness.write_text(
        "\n".join(
            line
            for line in readiness.read_text(encoding="utf-8").splitlines()
            if not line.startswith("| AC1 |")
        )
        + "\n",
        encoding="utf-8",
    )
    gate = _gate(tmp_path, package)
    assert not gate.ok
    assert "AC1" in gate.reason
    assert "нет строки" in gate.reason
