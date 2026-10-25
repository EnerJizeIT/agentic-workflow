"""SPEC IMP4 — K4 refusals (T8, AC9/AC12) and the legacy path (INV2).

Every bad link refuses BEFORE any side effect and names the locator; the
old ``source_ref`` parameter keeps working exactly as before.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api
from tests.spec_support import init_project_repo, package_content, write_package

TODO = "TODO-0001"


def _no_side_effects(repo: Path) -> None:
    inbox = repo / ".agentic" / "inbox"
    assert not (inbox / f"{TODO}.md").exists(), "TODO was created on refusal"
    assert not (inbox / f"{TODO}.ready").exists(), ".ready was created on refusal"
    assert not list((repo / ".agentic" / "context").glob(f"BASELINE-{TODO}.*"))


def _refused(repo: Path, content: str, needle: str = "") -> str:
    with pytest.raises(api.AwfApiError) as excinfo:
        api.dispatch_todo(repo, content, todo_id=TODO)
    message = str(excinfo.value)
    _no_side_effects(repo)
    if needle:
        assert needle in message, f"{needle!r} not in {message!r}"
    return message


def test_empty_section_refused(tmp_path):
    """prove_red R4: a heading whose body has no non-empty line is refused."""
    repo = init_project_repo(tmp_path)
    documents = dict()
    package = write_package(repo, commit=False)
    architecture = package / "pkg.architecture.md"
    documents["pkg.architecture.md"] = (
        architecture.read_text(encoding="utf-8") + "\n## Пустая секция\n\n"
    )
    write_package(repo, documents=documents)

    _refused(
        repo,
        package_content(TODO, ["spec/pkg/pkg.architecture.md#Пустая секция"]),
        "empty body",
    )


def test_missing_file_refused(tmp_path):
    repo = init_project_repo(tmp_path)
    write_package(repo)
    _refused(repo, package_content(TODO, ["spec/pkg/nope.md#R1"]), "no such file")


def test_missing_section_refused(tmp_path):
    repo = init_project_repo(tmp_path)
    write_package(repo)
    _refused(
        repo,
        package_content(TODO, ["spec/pkg/pkg.md#Нет такой секции"]),
        "not found",
    )


def test_path_outside_package_refused(tmp_path):
    repo = init_project_repo(tmp_path)
    write_package(repo)
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "outside.md").write_text("## R1\n\nтекст\n", encoding="utf-8")
    _refused(repo, package_content(TODO, ["docs/outside.md#R1"]), "outside the package")


def test_absolute_path_refused(tmp_path):
    repo = init_project_repo(tmp_path)
    write_package(repo)
    _refused(repo, package_content(TODO, ["/etc/hostname#x"]), "absolute")


def test_dotdot_refused(tmp_path):
    repo = init_project_repo(tmp_path)
    write_package(repo)
    _refused(repo, package_content(TODO, ["spec/pkg/../pkg/pkg.md#R1"]), "'..'")


def test_link_without_matrix_rows_refused(tmp_path):
    repo = init_project_repo(tmp_path)
    write_package(repo)
    _refused(
        repo,
        package_content(TODO, ["spec/pkg/pkg.architecture.md#Решения"]),
        "no coverage-matrix row",
    )


def test_empty_source_refs_refused(tmp_path):
    repo = init_project_repo(tmp_path)
    write_package(repo)
    content = (
        "---\n"
        "spec_package: spec/pkg\n"
        "source_refs: []\n"
        "---\n"
        f"# {TODO} — unit\n\nтело\n"
    )
    _refused(repo, content, "source_refs")


def test_spec_package_without_refs_refused(tmp_path):
    repo = init_project_repo(tmp_path)
    write_package(repo)
    content = (
        "---\n"
        "spec_package: spec/pkg\n"
        "---\n"
        f"# {TODO} — unit\n\nтело\n"
    )
    _refused(repo, content, "source_refs")


def test_package_source_ref_outside_package_refused(tmp_path):
    """K4: with a package, the legacy source_ref is a package link too."""
    repo = init_project_repo(tmp_path)
    write_package(repo)
    with pytest.raises(api.AwfApiError) as excinfo:
        api.dispatch_todo(
            repo,
            package_content(TODO),
            todo_id=TODO,
            source_ref="/etc/hostname#x",
        )
    message = str(excinfo.value)
    assert "absolute" in message
    _no_side_effects(repo)


def test_package_source_ref_inside_package_is_transferred(tmp_path):
    """A package source_ref gets the same snapshot block as source_refs."""
    repo = init_project_repo(tmp_path)
    write_package(repo)
    api.dispatch_todo(
        repo,
        package_content(TODO),
        todo_id=TODO,
        source_ref="spec/pkg/pkg.md#INV1 — Не должно измениться",
    )
    body = (repo / ".agentic" / "inbox" / f"{TODO}.md").read_text(encoding="utf-8")
    assert "## Первоисточник (INV1 — Не должно измениться)" in body
    assert "Покрытые ID: INV1" in body
    assert "Текст инварианта." in body


def test_legacy_source_ref_still_works(tmp_path):
    repo = init_project_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "source.md").write_text(
        "# Источник\n\n## R2.5 Владение\n\n- владелец один\n", encoding="utf-8"
    )
    api.dispatch_todo(
        repo,
        f"# {TODO} — unit\n\nтело\n",
        todo_id=TODO,
        source_ref="docs/source.md#R2.5 Владение",
    )
    body = (repo / ".agentic" / "inbox" / f"{TODO}.md").read_text(encoding="utf-8")
    assert "## Первоисточник (R2.5 Владение)" in body
    assert "- [ ] владелец один" in body


def test_ambiguous_heading_refused(tmp_path):
    """T8: two exact headings with the same text are ambiguous, not "first"."""
    repo = init_project_repo(tmp_path)
    package = write_package(repo)
    architecture = package / "pkg.architecture.md"
    duplicate = (
        architecture.read_text(encoding="utf-8")
        + "\n## Дубль\n\nпервое тело\n\n## Дубль\n\nвторое тело\n"
    )
    write_package(repo, documents={"pkg.architecture.md": duplicate})

    _refused(
        repo,
        package_content(TODO, ["spec/pkg/pkg.architecture.md#Дубль"]),
        "ambiguous",
    )
