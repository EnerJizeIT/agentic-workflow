"""SPEC IMP4 — K4 norm transfer into the TODO (T7, AC11/D3/D8).

The TODO gets a snapshot: package path, ``Пакет-sha``, six revisions, all
matrix IDs and the residual risks; then one verbatim section body per
declared link, in order, with the IDs that link covers.
"""
from __future__ import annotations

from pathlib import Path

from awf import api
from awf import spec_package as sp
from tests.spec_support import init_project_repo, package_content, write_package

TODO = "TODO-0001"


def _dispatch(repo: Path, refs: list[str]) -> str:
    api.dispatch_todo(repo, package_content(TODO, refs), todo_id=TODO)
    return (repo / ".agentic" / "inbox" / f"{TODO}.md").read_text(encoding="utf-8")


def test_package_block_carries_path_sha_revisions_ids_and_risks(tmp_path):
    repo = init_project_repo(tmp_path)
    package = write_package(repo)
    body = _dispatch(
        repo,
        [
            "spec/pkg/pkg.md#R1 — Первое правило",
            "spec/pkg/pkg.md#INV1 — Не должно измениться",
        ],
    )

    assert "## Пакет спецификации" in body
    assert "- Путь: `spec/pkg`" in body
    assert f"- Пакет-sha: `{sp.package_fingerprint(package)}`" in body
    for revision in ("ctx-1", "req-1", "rev-1", "arch-1", "qa-1", "ready-1"):
        assert revision in body, f"revision {revision} missing from the block"
    assert "- ID: R1, AC1, INV1" in body
    assert "Остаточные риски: не заявлены" in body

    first = body.index("## Первоисточник (R1 — Первое правило)")
    second = body.index("## Первоисточник (INV1 — Не должно измениться)")
    assert body.index("## Пакет спецификации") < first < second
    r1_block = body[first:second]
    assert "Текст правила." in r1_block
    assert "Текст инварианта." not in r1_block, (
        "the body must stop at the next heading of the same level"
    )


def test_source_body_is_a_snapshot_not_a_checklist(tmp_path):
    repo = init_project_repo(tmp_path)
    write_package(repo)
    body = _dispatch(repo, ["spec/pkg/pkg.md#R1 — Первое правило"])
    norm = body.split("## Чек-лист завершения", 1)[0]
    assert "AC1 — Первый критерий." in norm
    assert "- [ ]" not in norm, "the package norm is a verbatim snapshot"


def test_covered_ids_are_listed_per_link(tmp_path):
    repo = init_project_repo(tmp_path)
    write_package(repo)
    body = _dispatch(repo, ["spec/pkg/pkg.md#R1 — Первое правило"])
    assert "Покрытые ID: R1, AC1" in body


def test_residual_risks_are_transferred(tmp_path):
    repo = init_project_repo(tmp_path)
    package = write_package(repo)
    architecture = package / "pkg.architecture.md"
    architecture.write_text(
        architecture.read_text(encoding="utf-8")
        + "\n## Остаточные риски\n\n- сроки зависят от внешней команды\n",
        encoding="utf-8",
    )
    write_package(
        repo,
        documents={
            "pkg.architecture.md": architecture.read_text(encoding="utf-8")
        },
    )
    body = _dispatch(repo, ["spec/pkg/pkg.md#R1 — Первое правило"])
    assert "сроки зависят от внешней команды" in body
