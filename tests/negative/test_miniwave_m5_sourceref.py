"""M5 (TODO-0229): source_ref in dispatch + standard BLOCKED schema + draft.

Four invariants:
  (a) ``dispatch_todo(source_ref="<file>#<section>")`` inserts the section
      as a ``## Первоисточник (<section>)`` checklist — every bullet of the
      section, nothing from the next section;
  (b) a missing file / missing section refuses BEFORE any side effect
      (no TODO, no ``.ready``, no baseline);
  (c) ``blocked_draft`` builds a ready ``## REVIEW-фикс`` text from the
      BLOCKED note (findings + fix directions + supervisor slots) and does
      NOT touch the TODO;
  (d) the tool counter (55 = 5 UI + 50 workflow) agrees in the registry
      and the live docs.

Every repository is disposable in ``tmp_path``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# Module-level (like test_docs_counters.py): during a test the autouse
# conftest fixture patches subprocess.Popen to a plain function, and the mcp
# import chain evaluates ``subprocess.Popen[bytes]`` — importing inside the
# test body raises TypeError.
pytest.importorskip("mcp.server.fastmcp")
from agent_workflow_ui.tools import registry  # noqa: E402

from awf import api

SRC = "TODO-0001"
BLOCKED_ID = "TODO-0002"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _project(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    api.init_project(
        repo,
        project_name="M5",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    (repo / "docs").mkdir()
    (repo / "docs" / "source.md").write_text(
        "# Требования\n"
        "\n"
        "## R2.5 Владение\n"
        "\n"
        "Текст раздела.\n"
        "\n"
        "- владелец один\n"
        "- второй пункт\n"
        "- третий пункт\n"
        "\n"
        "### R2.5.1 Подпункт\n"
        "\n"
        "- вложенный пункт\n"
        "\n"
        "## R2.6 Другое\n"
        "\n"
        "- чужой пункт\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _inbox(repo: Path) -> Path:
    return repo / ".agentic" / "inbox"


def _no_side_effects(repo: Path, todo_id: str) -> None:
    inbox = _inbox(repo)
    assert not (inbox / f"{todo_id}.md").exists(), "TODO was created on refusal"
    assert not (inbox / f"{todo_id}.ready").exists(), ".ready was created on refusal"
    context = repo / ".agentic" / "context"
    assert not list(context.glob(f"BASELINE-{todo_id}.*")), (
        "baseline was created on refusal"
    )


def test_a_source_ref_inserts_section_checklist(tmp_path):
    repo = _project(tmp_path)
    result = api.dispatch_todo(
        repo,
        f"# {SRC} — unit\n\nтело\n",
        todo_id=SRC,
        source_ref="docs/source.md#R2.5 Владение",
    )
    body = (_inbox(repo) / f"{SRC}.md").read_text(encoding="utf-8")
    assert result.todo_id == SRC
    assert "## Первоисточник (R2.5 Владение)" in body
    assert "- [ ] владелец один" in body
    assert "- [ ] второй пункт" in body
    assert "- [ ] третий пункт" in body
    # The nested sub-heading belongs to the section, the next same-level
    # section does not.
    assert "- [ ] вложенный пункт" in body
    assert "чужой пункт" not in body
    assert "R2.6" not in body


def test_a_source_ref_block_keeps_the_contract_valid(tmp_path):
    """The inserted block must not break the front-matter contract."""
    repo = _project(tmp_path)
    api.dispatch_todo(
        repo,
        f"---\nverify: [\"true\"]\n---\n# {SRC} — unit\n",
        todo_id=SRC,
        source_ref="docs/source.md#R2.5 Владение",
    )
    body = (_inbox(repo) / f"{SRC}.md").read_text(encoding="utf-8")
    assert body.startswith("---\nverify"), "contract block must stay first"
    assert "## Первоисточник (R2.5 Владение)" in body
    # K1: the title stays the first heading — the F5 guards read it.
    assert body.index("# TODO-0001") < body.index("## Первоисточник")


def test_k6_ambiguous_exact_heading_refuses(tmp_path):
    """K6: the match is exact; two headings with the same text are an
    explicit refusal (a silent first hit could copy the wrong section)."""
    repo = _project(tmp_path)
    (repo / "docs" / "source.md").write_text(
        "## R2.5 Ownership\n\n- один\n\n## R2.5 Ownership\n\n- два\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "duplicate")
    with pytest.raises(api.AwfApiError):
        api.dispatch_todo(
            repo,
            f"# {SRC} — unit\n",
            todo_id=SRC,
            source_ref="docs/source.md#R2.5 Ownership",
        )
    _no_side_effects(repo, SRC)


def test_k2_source_ref_keeps_prose_continuations_and_fences(tmp_path):
    """K2: only list markers become boxes — prose, item continuations and
    a fenced example survive with their context."""
    repo = _project(tmp_path)
    (repo / "docs" / "source.md").write_text(
        "## R2.5 Владение\n"
        "\n"
        "Вступление.\n"
        "\n"
        "- пункт один\n"
        "  продолжение пункта\n"
        "- пункт два\n"
        "\n"
        "```yaml\n"
        "- owner: one\n"
        "```\n"
        "\n"
        "Хвост раздела.\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "prose")
    api.dispatch_todo(
        repo, f"# {SRC} — unit\n", todo_id=SRC, source_ref="docs/source.md#R2.5 Владение"
    )
    body = (_inbox(repo) / f"{SRC}.md").read_text(encoding="utf-8")
    assert "- [ ] пункт один" in body
    assert "продолжение пункта" in body
    assert "Вступление." in body
    assert "Хвост раздела." in body
    assert "```yaml" in body and "- owner: one" in body
    assert "- [ ] owner: one" not in body, (
        "a code-block example was promoted to a requirement"
    )


def test_b_missing_file_refuses_without_side_effects(tmp_path):
    repo = _project(tmp_path)
    with pytest.raises(api.AwfApiError):
        api.dispatch_todo(
            repo, f"# {SRC} — unit\n", todo_id=SRC, source_ref="docs/nope.md#R2.5"
        )
    _no_side_effects(repo, SRC)


def test_b_missing_section_refuses_without_side_effects(tmp_path):
    repo = _project(tmp_path)
    with pytest.raises(api.AwfApiError):
        api.dispatch_todo(
            repo, f"# {SRC} — unit\n", todo_id=SRC, source_ref="docs/source.md#R9.9"
        )
    _no_side_effects(repo, SRC)


def test_b_path_outside_project_refuses(tmp_path):
    repo = _project(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("## R2.5\n- чужое\n", encoding="utf-8")
    with pytest.raises(api.AwfApiError):
        api.dispatch_todo(
            repo, f"# {SRC} — unit\n", todo_id=SRC, source_ref="../outside.md#R2.5"
        )
    _no_side_effects(repo, SRC)


def test_c_blocked_draft_builds_review_text(tmp_path):
    repo = _project(tmp_path)
    api.dispatch_todo(repo, f"# {BLOCKED_ID} — unit\nтело\n", todo_id=BLOCKED_ID)
    todo_path = _inbox(repo) / f"{BLOCKED_ID}.md"
    before = todo_path.read_bytes()
    (repo / ".agentic" / "outbox" / f"BLOCKED-{BLOCKED_ID}.md").write_text(
        "## ВЕРДИКТ\n\nblocked\n\n"
        "## НАХОДКИ\n\n"
        "- находка один: file.py:10\n"
        "- находка два: file.py:20\n\n"
        "## НАПРАВЛЕНИЯ-ФИКСА\n\n"
        "- починить file.py:10\n\n"
        "## ПЕРЕНОСЫ\n\n"
        "- нет\n",
        encoding="utf-8",
    )
    result = api.blocked_draft(repo, BLOCKED_ID)
    text = result.review_text
    assert "## REVIEW-фикс" in text
    assert "находка один" in text and "находка два" in text
    assert "починить file.py:10" in text
    assert "Скоуп:" in text and "Переносы:" in text
    assert todo_path.read_bytes() == before, "blocked_draft must not touch the TODO"


def test_c_blocked_draft_refuses_without_note(tmp_path):
    repo = _project(tmp_path)
    with pytest.raises(api.AwfApiError):
        api.blocked_draft(repo, BLOCKED_ID)


def test_c_blocked_draft_out_inside_agentic_refused(tmp_path, capsys):
    """K4: ``--out`` inside ``.agentic`` is a refusal (a draft must not
    overwrite the queue); a path outside it still works, the TODO intact."""
    from types import SimpleNamespace

    from awf import cmd_blocked_draft

    repo = _project(tmp_path)
    api.dispatch_todo(repo, f"# {BLOCKED_ID} — unit\nтело\n", todo_id=BLOCKED_ID)
    todo_path = _inbox(repo) / f"{BLOCKED_ID}.md"
    before = todo_path.read_bytes()
    (repo / ".agentic" / "outbox" / f"BLOCKED-{BLOCKED_ID}.md").write_text(
        "## НАХОДКИ\n\n- находка\n", encoding="utf-8"
    )
    args = SimpleNamespace(
        project_dir=str(repo),
        todo_id=BLOCKED_ID,
        out=str(repo / ".agentic" / "inbox" / f"{BLOCKED_ID}.md"),
    )
    assert cmd_blocked_draft.run(args) == 1
    assert todo_path.read_bytes() == before, "--out rewrote the TODO"
    assert "outside the project's .agentic" in capsys.readouterr().out

    outside = tmp_path / "draft.md"
    args.out = str(outside)
    assert cmd_blocked_draft.run(args) == 0
    assert "## REVIEW-фикс" in outside.read_text(encoding="utf-8")
    assert todo_path.read_bytes() == before, "blocked_draft must not touch the TODO"



def test_l1_section_survives_a_comment_inside_a_fence(tmp_path):
    """L1: a ``#``-line inside a fenced example is a comment, not a heading.

    The shared walker skips fence lines, so the section does not end at
    the example's comment: the rest of the requirements and the closing
    fence survive, and the next same-level section does not leak in.
    """
    repo = _project(tmp_path)
    (repo / "docs" / "source.md").write_text(
        "## R2.5 Владение\n"
        "\n"
        "- пункт до примера\n"
        "\n"
        "```sh\n"
        "# установить пакет\n"
        "pip install x\n"
        "```\n"
        "\n"
        "- пункт после примера\n"
        "\n"
        "## R2.6 Другое\n"
        "\n"
        "- чужой пункт\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fence-comment")
    api.dispatch_todo(
        repo,
        f"# {SRC} — unit\n\nтело юнита\n",
        todo_id=SRC,
        source_ref="docs/source.md#R2.5 Владение",
    )
    body = (_inbox(repo) / f"{SRC}.md").read_text(encoding="utf-8")
    assert "- [ ] пункт после примера" in body, (
        "text after a code-comment line was dropped from the section"
    )
    assert "pip install x" in body, "the fenced example lost its body"
    assert "```sh" in body and body.count("```") >= 2, (
        "the opening fence has no closing fence — the unit body is swallowed"
    )
    assert "чужой пункт" not in body, "the next same-level section leaked in"


def test_l2_out_tilde_is_expanded_once(tmp_path, monkeypatch):
    """L2: ``--out ~/draft.md`` is user-expanded once and the same path
    passes the barrier and receives the write."""
    from types import SimpleNamespace

    from awf import cmd_blocked_draft

    repo = _project(tmp_path)
    api.dispatch_todo(repo, f"# {BLOCKED_ID} — unit\nтело\n", todo_id=BLOCKED_ID)
    (repo / ".agentic" / "outbox" / f"BLOCKED-{BLOCKED_ID}.md").write_text(
        "## НАХОДКИ\n\n- находка\n", encoding="utf-8"
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    args = SimpleNamespace(
        project_dir=str(repo), todo_id=BLOCKED_ID, out="~/draft.md"
    )
    assert cmd_blocked_draft.run(args) == 0
    assert (home / "draft.md").is_file(), "~ was not expanded for the write"
    assert "## REVIEW-фикс" in (home / "draft.md").read_text(encoding="utf-8")


def test_d_tool_counter_matches_docs():
    counts = registry.tool_counts()
    assert counts == {"total": 56, "awf": 51, "ui": 5}, counts
    names = registry.tool_names()
    assert "awf_blocked_draft" in names

    repo_root = Path(__file__).resolve().parents[2]
    assert "56 tools: 51 `awf_*` workflow + 5 UI" in (
        repo_root / "USAGE.md"
    ).read_text(encoding="utf-8")
    assert "56 инструмента: 51 `awf_*` workflow + 5 UI" in (
        repo_root / "USAGE.ru.md"
    ).read_text(encoding="utf-8")
