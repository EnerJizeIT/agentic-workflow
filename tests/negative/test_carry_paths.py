"""CARRY IMP4 (TODO-0284): paths and text — R5/R6/R7.

Three surfaces that used to trust the wrong thing:

  R5 — the contract/title parser walked the leading lines by hand, so a
       TWO-LINE ``<!-- ... -->`` hid the contract, and a fenced ``#`` line
       became the commit subject.
  R6 — ``todo-draft --out`` wrote with a bare ``write_text`` anywhere the
       caller pointed; ``blocked_draft`` knew only the canonical note;
       ``--content-file`` was resolved from the shell's CWD; the three
       dispatch flags lived only in the MCP form.
  R7 — the metrics cache classified "inside/outside the project" by
       comparing an absolute leaf with a RELATIVE ``project_dir``.

Every project is disposable in ``tmp_path``; no network, no live run.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf import api, cmd_dispatch, cmd_review_append, cmd_todo_update
from awf import metrics as M
from awf import todo_draft as td
from awf._markdown import skip_comments_and_blanks
from awf.commit_gate import _todo_title
from awf.infra import managed_fs
from awf.unit_contract import _find_contract_block, parse_todo_contract

#: The real front matter of TODO-0266 (T8 asks for a COPY, not a path into
#: ``.agentic/done``): the block sits at the very top, no comment above it.
TODO_0266_FRONT_MATTER = """\
---
pipeline: spec
verify: ["/home/pklochkov/.local/share/awf-venv/bin/python -m awf spec check docs/spec/carry-cd"]
---
# TODO — SPEC: carry batch C+D
"""


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
        project_name="CARRY-PATHS",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _index(repo: Path) -> Path:
    path = repo / "AUDIT-INDEX.md"
    path.write_text(
        "# AUDIT-INDEX · test\n\n"
        "| ID | Sev | Тип | Заголовок | Итерация | Фикс | Статус |\n"
        "|----|-----|-----|-----------|----------|------|--------|\n"
        "| TEST-01 | P1 | BUG | `x` падает (awf/x.py:10) | 01 | S | FU-01 |\n",
        encoding="utf-8",
    )
    return path


def _tree(root: Path) -> dict[Path, bytes]:
    return {
        p.relative_to(root): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.parts
    }


# ─── R5: trivia and fences ─────────────────────────────────────────────


class TestContractTrivia:
    def test_copy_of_0266_front_matter_found(self) -> None:
        assert _find_contract_block(TODO_0266_FRONT_MATTER) == (0, 3, None)
        contract, _unknown = parse_todo_contract(TODO_0266_FRONT_MATTER)
        assert contract is not None
        assert contract["pipeline"] == "spec"

    def test_two_line_comment_before_contract_is_skipped(self) -> None:
        content = (
            "<!-- a\n"
            "b -->\n"
            "---\n"
            'verify: ["pytest -q"]\n'
            "---\n"
            "# Task\n"
        )
        assert skip_comments_and_blanks(content.splitlines()) == 2
        assert _find_contract_block(content) == (2, 4, None)
        contract, _unknown = parse_todo_contract(content)
        assert contract == {"verify": ["pytest -q"]}

    def test_unterminated_comment_is_no_block_no_error(self) -> None:
        content = "<!-- a\nb\n\nno comment close, no contract\n"
        assert skip_comments_and_blanks(content.splitlines()) == len(
            content.splitlines()
        )
        assert _find_contract_block(content) == (None, None, None)
        assert parse_todo_contract(content) == (None, [])

    def test_unterminated_block_names_the_content_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        repo = _project(tmp_path)
        bad = repo / "bad-todo.md"
        bad.write_text(
            '---\nverify: ["pytest -q"]\n# no closing dash\n', encoding="utf-8"
        )
        args = SimpleNamespace(
            project_dir=str(repo),
            content="",
            content_file="bad-todo.md",
            resume="",
            no_baseline_tests=True,
            source_ref=None,
            role=None,
            todo_id=None,
            pipeline=None,
            force=False,
            allow_mismatch=False,
            carry_over_from=None,
            include_untracked=None,
        )
        assert cmd_dispatch.run(args) == 1
        out = capsys.readouterr().out
        assert "bad-todo.md" in out
        assert "closing" in out
        assert not (repo / ".agentic" / "inbox" / "TODO-0001.md").exists()

    def test_comment_between_title_and_contract_is_skipped(self) -> None:
        # R5 says the contract scan skips blank lines and completed
        # ``<!-- ... -->`` comments of any length. The after-title branch
        # calls skip_comments_and_blanks only at the top, so the comment
        # below the title makes the whole block disappear: no contract and
        # no error — the unit would run without its verify commands.
        content = (
            "# TODO-0001 — task\n"
            "<!-- audit note -->\n"
            "---\n"
            'verify: ["p -q"]\n'
            "---\n"
            "body\n"
        )
        assert _find_contract_block(content) == (2, 4, None)
        contract, _unknown = parse_todo_contract(content)
        assert contract == {"verify": ["p -q"]}

    def test_contract_after_heading_still_found(self) -> None:
        content = "# TODO — task\n\n---\nwave: w1\n---\n\nbody\n"
        contract, _unknown = parse_todo_contract(content)
        assert contract == {"wave": "w1"}

    def test_horizontal_rule_is_not_a_block(self) -> None:
        content = "# TODO — task\n\nIntro.\n\n---\n\nMore.\n"
        contract, _unknown = parse_todo_contract(content)
        assert contract is None


class TestCommitTitle:
    def _write(self, repo: Path, text: str) -> Path:
        path = repo / ".agentic" / "inbox" / "TODO-0001.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_fenced_heading_is_not_the_title(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        self._write(
            repo,
            "```\n# TODO-9999 fake\n```\n\n# TODO-0001 — real title\n",
        )
        assert _todo_title(repo, "TODO-0001") == "real title"

    def test_front_matter_comment_is_not_the_title(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        self._write(
            repo,
            "---\n# gates: [\"contracts\"]\npipeline: spec\n---\n"
            "# TODO-0001 — real title\n",
        )
        assert _todo_title(repo, "TODO-0001") == "real title"

    def test_no_heading_is_legacy_subject(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        self._write(repo, "just prose, no heading\n")
        assert _todo_title(repo, "TODO-0001") == ""

    def test_empty_heading_is_legacy_subject(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        self._write(repo, "# \n\nbody\n")
        assert _todo_title(repo, "TODO-0001") == ""

    def test_comment_after_contract_is_not_the_title(self, tmp_path: Path) -> None:
        # AUD-0284-5: trivia is not only "before the contract". A comment
        # between the closing ``---`` and the title is still a comment —
        # the fake H1 inside it must not become the commit subject.
        repo = _project(tmp_path)
        self._write(
            repo,
            "---\npipeline: spec\n---\n"
            "<!--\n# TODO-9999 fake\n-->\n"
            "# TODO-0001 — real title\n",
        )
        assert _todo_title(repo, "TODO-0001") == "real title"


class TestTitleAfterContract:
    def test_comment_after_contract_does_not_hide_the_title(self) -> None:
        # AUD-0284-5: ``_title_line_index`` (renumber, DoD insert) and the
        # commit subject share ``skip_leading_trivia``. The index must pass
        # the comment that follows the front matter.
        from awf._markdown import skip_leading_trivia

        content = (
            "---\npipeline: spec\n---\n"
            "<!--\n# TODO-9999 fake\n-->\n"
            "# TODO-0001 — real title\n"
        )
        lines = content.splitlines()
        assert lines[skip_leading_trivia(lines)] == "# TODO-0001 — real title"

    def test_renumber_does_not_write_into_the_comment(
        self, tmp_path: Path
    ) -> None:
        # AUD-0284-5 repro: the renumber and the DoD block resolved the
        # fake heading inside the comment and wrote the issued id there.
        repo = _project(tmp_path)
        result = api.dispatch_todo(
            repo,
            "---\npipeline: spec\n---\n"
            "<!--\n# TODO-9999 fake\n-->\n"
            "# TODO-9999 real title\n\nbody\n",
        )
        assert result.renumbered is True
        md = (repo / ".agentic" / "inbox" / f"{result.todo_id}.md").read_text()
        assert "# TODO-9999 fake" in md
        assert f"# {result.todo_id} real title" in md


# ─── R6: draft barriers, legacy note, CWD and CLI parity ───────────────


class TestTodoDraftBarrier:
    def test_out_outside_agentic_refused_without_files(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        index = _index(repo)
        before = _tree(repo / ".agentic")
        out = repo / "notes" / "draft.md"
        with pytest.raises(td.TodoDraftError, match="refusing --out"):
            td.make_todo_draft("TEST-01", index_path=index, project_dir=repo, out=out)
        assert not out.exists()
        assert not out.parent.exists()
        assert _tree(repo / ".agentic") == before

    def test_out_relative_escape_refused(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        index = _index(repo)
        out = repo / ".agentic" / "drafts" / ".." / ".." / "escape.md"
        with pytest.raises(td.TodoDraftError, match="refusing --out"):
            td.make_todo_draft("TEST-01", index_path=index, project_dir=repo, out=out)
        assert not (repo / "escape.md").exists()

    def test_existing_outside_file_refuses_not_force(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        index = _index(repo)
        outside = tmp_path / "notes"
        outside.mkdir()
        out = outside / "draft.md"
        out.write_text("keep", encoding="utf-8")
        with pytest.raises(td.TodoDraftError, match="refusing --out"):
            td.make_todo_draft(
                "TEST-01", index_path=index, project_dir=repo, out=out, force=True
            )
        assert out.read_text(encoding="utf-8") == "keep"

    def test_symlinked_component_refused(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        index = _index(repo)
        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / ".agentic" / "drafts").symlink_to(outside)
        out = repo / ".agentic" / "drafts" / "draft.md"
        with pytest.raises(td.TodoDraftError, match="refusing --out"):
            td.make_todo_draft("TEST-01", index_path=index, project_dir=repo, out=out)
        assert not (outside / "draft.md").exists()

    def test_symlinked_agentic_dir_refused(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        index = _index(repo)
        outside = tmp_path / "agentic-outside"
        (repo / ".agentic").rename(outside)
        (repo / ".agentic").symlink_to(outside, target_is_directory=True)
        out = repo / ".agentic" / "drafts" / "draft.md"
        with pytest.raises(td.TodoDraftError, match="symlink"):
            td.make_todo_draft("TEST-01", index_path=index, project_dir=repo, out=out)
        assert not (outside / "drafts").exists()

    def test_regular_file_inside_agentic_written(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        index = _index(repo)
        out = repo / ".agentic" / "drafts" / "draft.md"
        out_path, draft = td.make_todo_draft(
            "TEST-01", index_path=index, project_dir=repo, out=out
        )
        assert out_path == out
        assert "TEST-01" in out.read_text(encoding="utf-8")

    def test_nested_agentic_keeps_the_callers_path(self, tmp_path: Path) -> None:
        # Design review: the raw-basis projection anchored a surface by its
        # nearest ``.agentic`` component, so ``.agentic/sub/.agentic/x`` was
        # silently written to ``.agentic/x``. A path INSIDE our ``.agentic``
        # keeps its own chain: refusal is acceptable, substitution is not
        # (PA-0284-8 "refuse, never redirect").
        repo = _project(tmp_path)
        index = _index(repo)
        out = repo / ".agentic" / "sub" / ".agentic" / "draft.md"
        out_path, _draft = td.make_todo_draft(
            "TEST-01", index_path=index, project_dir=repo, out=out
        )
        assert out_path == out
        assert out.is_file()
        assert not (repo / ".agentic" / "draft.md").exists()

    def test_symlinked_leaf_refused_before_force_advice(self, tmp_path: Path) -> None:
        # AUD-0284-3: the no-follow walk runs before "already exists". A
        # symlinked leaf used to answer "pass --force" (without force) and
        # "component is a symlink" (with force) — the advice led nowhere.
        repo = _project(tmp_path)
        index = _index(repo)
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "keep.md"
        target.write_text("keep", encoding="utf-8")
        drafts = repo / ".agentic" / "drafts"
        drafts.mkdir()
        (drafts / "draft.md").symlink_to(target)
        for force in (False, True):
            with pytest.raises(td.TodoDraftError) as exc:
                td.make_todo_draft(
                    "TEST-01",
                    index_path=index,
                    project_dir=repo,
                    out=drafts / "draft.md",
                    force=force,
                )
            assert "symlink" in str(exc.value)
            assert "--force" not in str(exc.value)
        assert target.read_text(encoding="utf-8") == "keep"

    def test_dotdot_does_not_launder_a_symlink_component(self, tmp_path: Path) -> None:
        # QA-0284-3 (R6/AC16): the walk used the ``abspath`` spelling, so
        # ``lnk/..`` erased the symlinked component from the chain BEFORE
        # the no-follow walk and the draft landed at the lexical path the
        # caller never named (``lnk`` pointed outside). The walk now takes
        # the caller's spelling; the refusal names the symlink.
        repo = _project(tmp_path)
        index = _index(repo)
        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / ".agentic" / "lnk").symlink_to(outside, target_is_directory=True)
        out = repo / ".agentic" / "lnk" / ".." / "escaped.md"
        before = sorted(p.name for p in (repo / ".agentic").iterdir())
        for force in (False, True):
            with pytest.raises(td.TodoDraftError, match="symlink"):
                td.make_todo_draft(
                    "TEST-01",
                    index_path=index,
                    project_dir=repo,
                    out=out,
                    force=force,
                )
        assert not (repo / ".agentic" / "escaped.md").exists()
        assert not (outside / "escaped.md").exists()
        assert sorted(p.name for p in (repo / ".agentic").iterdir()) == before

    def test_clean_dotdot_still_lands_at_the_normalized_target(
        self, tmp_path: Path
    ) -> None:
        # The walk sees the spelling, the write uses the normalized path: a
        # ``..`` over ordinary directories is not a new refusal.
        repo = _project(tmp_path)
        index = _index(repo)
        out = repo / ".agentic" / "drafts" / ".." / "draft.md"
        out_path, _draft = td.make_todo_draft(
            "TEST-01", index_path=index, project_dir=repo, out=out
        )
        assert out_path == repo / ".agentic" / "draft.md"
        assert out_path.is_file()

    def test_out_without_agentic_names_the_missing_init(self, tmp_path: Path) -> None:
        # AUD-0284-4: a project without .agentic used to answer with a raw
        # errno from mkdir(parents=True). The refusal names the real
        # precondition — the project is not initialized.
        bare = tmp_path / "bare"
        bare.mkdir()
        index = _index(bare)
        out = bare / ".agentic" / "drafts" / "draft.md"
        with pytest.raises(td.TodoDraftError, match="not awf-initialized"):
            td.make_todo_draft(
                "TEST-01", index_path=index, project_dir=bare, out=out
            )
        assert not (bare / ".agentic").exists()

    def test_out_under_a_foreign_agentic_is_refused(self, tmp_path: Path) -> None:
        # QA-0284-1 (R6/AC16): the target lies under THE PROJECT's .agentic.
        # The raw projection anchors on the nearest ``.agentic`` component,
        # so containment must run on the RESOLVED parent: a nested
        # ``sub/.agentic/draft.md`` resolves outside the project's
        # ``.agentic`` and refuses before ``<out>`` and its parents exist.
        repo = _project(tmp_path)
        index = _index(repo)
        (repo / "sub").mkdir()
        before = _tree(repo / ".agentic")
        with pytest.raises(td.TodoDraftError, match="refusing --out"):
            td.make_todo_draft(
                "TEST-01",
                index_path=index,
                project_dir=repo,
                out=repo / "sub" / ".agentic" / "draft.md",
            )
        assert not (repo / "sub" / ".agentic").exists()
        assert not (repo / ".agentic" / "draft.md").exists()
        assert _tree(repo / ".agentic") == before

    def test_out_into_another_projects_agentic_is_refused(self, tmp_path: Path) -> None:
        # QA-0284-2 (R6/AC16): an absolute path into a FOREIGN project's
        # ``.agentic`` is "outside this project's .agentic" — the resolved
        # parent is not under OUR ``.agentic``, so it refuses instead of
        # being redirected into the current project.
        repo = _project(tmp_path)
        index = _index(repo)
        foreign = tmp_path / "foreign"
        (foreign / ".agentic").mkdir(parents=True)
        wanted = foreign / ".agentic" / "draft.md"
        with pytest.raises(td.TodoDraftError, match="refusing --out"):
            td.make_todo_draft(
                "TEST-01", index_path=index, project_dir=repo, out=wanted
            )
        assert not wanted.exists()
        assert not (repo / ".agentic" / "draft.md").exists()

    def test_out_through_symlinked_project_parent_allowed(self, tmp_path: Path) -> None:
        # Q4b (tests/negative/test_miniwave_m3c_writers.py): a symlinked
        # PARENT of the project is not an escape for a managed writer.
        # ``paths.agentic_dir`` resolves the root while the leaf keeps the
        # caller's spelling, so this normal path is falsely refused.
        repo = _project(tmp_path)
        index = _index(repo)
        link = tmp_path / "link"
        link.symlink_to(repo, target_is_directory=True)
        out = link / ".agentic" / "drafts" / "draft.md"
        out_path, _draft = td.make_todo_draft(
            "TEST-01", index_path=index, project_dir=link, out=out
        )
        assert out_path == out
        assert (repo / ".agentic" / "drafts" / "draft.md").is_file()

    def test_mixed_spelling_of_project_and_out_allowed(self, tmp_path: Path) -> None:
        # AUD-0284-8: the project reached through a symlinked parent and
        # ``--out`` written with the REAL spelling name the same location.
        # The lexical containment used to read this as an escape.
        repo = _project(tmp_path)
        index = _index(repo)
        link = tmp_path / "link"
        link.symlink_to(repo, target_is_directory=True)
        real_out = repo / ".agentic" / "drafts" / "draft.md"
        _out_path, _draft = td.make_todo_draft(
            "TEST-01", index_path=index, project_dir=link, out=real_out
        )
        assert real_out.is_file()

    def test_mixed_spelling_still_refuses_a_symlinked_component(
        self, tmp_path: Path
    ) -> None:
        # The same-spelling case is refused (test_symlinked_leaf_refused_
        # before_force_advice); spelling the project through a symlinked
        # parent must not turn that refusal into a silent follow of the
        # link target. Today it does: realpath(out) resolves the leaf and
        # the projected path never contains the symlink, so --force
        # overwrites the OTHER file.
        repo = _project(tmp_path)
        index = _index(repo)
        other = repo / ".agentic" / "other"
        other.mkdir()
        keep = other / "keep.md"
        keep.write_text("keep", encoding="utf-8")
        drafts = repo / ".agentic" / "drafts"
        drafts.mkdir()
        (drafts / "draft.md").symlink_to(keep)
        link = tmp_path / "link"
        link.symlink_to(repo, target_is_directory=True)
        with pytest.raises(td.TodoDraftError, match="symlink"):
            td.make_todo_draft(
                "TEST-01",
                index_path=index,
                project_dir=link,
                out=repo / ".agentic" / "drafts" / "draft.md",
                force=True,
            )
        assert keep.read_text(encoding="utf-8") == "keep"

    def test_relative_out_resolves_from_the_current_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # AUD-0284-6: a relative ``--out`` is a normal CLI path — it
        # resolves from the shell's CWD (the spec's lexical ``normpath``),
        # not from the project root. The help names the barrier, not a
        # second resolution rule.
        repo = _project(tmp_path)
        index = _index(repo)
        monkeypatch.chdir(repo)
        out_path, _draft = td.make_todo_draft(
            "TEST-01", index_path=index, project_dir=repo, out=Path(".agentic/drafts/d.md")
        )
        assert out_path == repo / ".agentic" / "drafts" / "d.md"
        assert out_path.is_file()


class TestBlockedDraftLegacy:
    def _note(self, repo: Path, name: str, marker: str) -> None:
        (repo / ".agentic" / "outbox" / name).write_text(
            f"## НАХОДКИ\n\n- {marker}\n\n## НАПРАВЛЕНИЯ-ФИКСА\n\n- fix\n",
            encoding="utf-8",
        )

    def test_legacy_short_note_is_read(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        self._note(repo, "BLOCKED-0042.md", "legacy-marker")
        result = api.blocked_draft(repo, "TODO-0042")
        assert "legacy-marker" in result.review_text

    def test_canonical_wins_over_legacy(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        self._note(repo, "BLOCKED-0042.md", "legacy-marker")
        self._note(repo, "BLOCKED-TODO-0042.md", "canonical-marker")
        result = api.blocked_draft(repo, "TODO-0042")
        assert "canonical-marker" in result.review_text
        assert "legacy-marker" not in result.review_text

    def test_missing_note_names_both_paths(self, tmp_path: Path) -> None:
        repo = _project(tmp_path)
        with pytest.raises(api.AwfApiError) as exc:
            api.blocked_draft(repo, "TODO-0042")
        message = str(exc.value)
        assert "BLOCKED-TODO-0042.md" in message
        assert "BLOCKED-0042.md" in message

    def test_schema_error_names_the_file_that_was_read(
        self, tmp_path: Path
    ) -> None:
        repo = _project(tmp_path)
        (repo / ".agentic" / "outbox" / "BLOCKED-0042.md").write_text(
            "no sections here\n", encoding="utf-8"
        )
        with pytest.raises(api.AwfApiError) as exc:
            api.blocked_draft(repo, "TODO-0042")
        message = str(exc.value)
        assert "BLOCKED-0042.md" in message
        assert "BLOCKED-TODO-0042.md" not in message


class TestDispatchCwd:
    def test_relative_content_file_from_project_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _project(tmp_path)
        (repo / "task.md").write_text(
            "# TODO — from a file\n\nbody\n", encoding="utf-8"
        )
        other = tmp_path / "elsewhere"
        other.mkdir()
        monkeypatch.chdir(other)
        args = SimpleNamespace(
            project_dir=str(repo),
            content="",
            content_file="task.md",
            resume="",
            no_baseline_tests=True,
            source_ref=None,
            role=None,
            todo_id="TODO-0001",
            pipeline=None,
            force=False,
            allow_mismatch=False,
            carry_over_from=None,
            include_untracked=None,
        )
        assert cmd_dispatch.run(args) == 0
        assert (repo / ".agentic" / "inbox" / "TODO-0001.md").is_file()

    def test_content_file_not_blamed_for_missing_agentic(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # AUD-0284-2: the file-name prefix belongs to the contract-parse
        # refusal. A project precondition must not read as "the file did it".
        bare = tmp_path / "bare"
        bare.mkdir()
        (bare / "task-input.md").write_text(
            "# TODO — from a file\n\nbody\n", encoding="utf-8"
        )
        args = SimpleNamespace(
            project_dir=str(bare),
            content="",
            content_file="task-input.md",
            resume="",
            no_baseline_tests=True,
            source_ref=None,
            role=None,
            todo_id="TODO-0001",
            pipeline=None,
            force=False,
            allow_mismatch=False,
            carry_over_from=None,
            include_untracked=None,
        )
        assert cmd_dispatch.run(args) == 1
        out = capsys.readouterr().out
        assert "No .agentic" in out
        assert "task-input.md" not in out

    def test_todo_update_content_file_from_project_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # PA-0284-2: the same base as dispatch — a relative --content-file
        # belongs to the project, not to the shell's CWD.
        repo = _project(tmp_path)
        (repo / "task.md").write_text("# TODO — reworded\n", encoding="utf-8")
        (repo / ".agentic" / "inbox" / "TODO-0001.md").write_text(
            "# TODO — old\n", encoding="utf-8"
        )
        other = tmp_path / "elsewhere"
        other.mkdir()
        monkeypatch.chdir(other)
        args = SimpleNamespace(
            project_dir=str(repo),
            content="",
            content_file="task.md",
            todo_id="TODO-0001",
            reason="",
            operation_id=None,
        )
        assert cmd_todo_update.run(args) == 0
        inbox = (repo / ".agentic" / "inbox" / "TODO-0001.md").read_text(
            encoding="utf-8"
        )
        assert "reworded" in inbox

    def test_review_append_text_file_from_project_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # PA-0284-2: review-append's --text-file follows the same base.
        repo = _project(tmp_path)
        (repo / "review.md").write_text("## REVIEW-фикс\n\nfix it\n", encoding="utf-8")
        (repo / ".agentic" / "inbox" / "TODO-0001.md").write_text(
            "# TODO — started\n", encoding="utf-8"
        )
        (repo / ".agentic" / "outbox" / "PROGRESS-TODO-0001.ready").write_text(
            "working\n", encoding="utf-8"
        )
        other = tmp_path / "elsewhere"
        other.mkdir()
        monkeypatch.chdir(other)
        args = SimpleNamespace(
            project_dir=str(repo),
            text="",
            text_file="review.md",
            todo_id="TODO-0001",
            reason="",
            operation_id=None,
        )
        assert cmd_review_append.run(args) == 0
        inbox = (repo / ".agentic" / "inbox" / "TODO-0001.md").read_text(
            encoding="utf-8"
        )
        assert "fix it" in inbox

    def test_cli_flags_reach_the_api(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _project(tmp_path)
        seen: dict[str, object] = {}

        def fake_dispatch(project_dir: Path, content: str, **kwargs: object):
            seen.update(kwargs)
            return SimpleNamespace(
                todo_id="TODO-0001",
                baseline_sha="deadbeef",
                files_written=[],
                pre_check_warnings=[],
                size_warning="",
                resumed=False,
                baseline_test_status="",
            )

        monkeypatch.setattr(api, "dispatch_todo", fake_dispatch)
        args = SimpleNamespace(
            project_dir=str(repo),
            content="# TODO — unit\n\nbody\n",
            content_file="",
            resume="",
            no_baseline_tests=True,
            source_ref=None,
            role=None,
            todo_id=None,
            pipeline=None,
            force=False,
            allow_mismatch=True,
            carry_over_from="TODO-0002",
            include_untracked=["a.txt", "b.txt"],
        )
        assert cmd_dispatch.run(args) == 0
        assert seen["allow_mismatch"] is True
        assert seen["carry_over_from"] == "TODO-0002"
        assert seen["include_untracked"] == ["a.txt", "b.txt"]


# ─── R7: metrics path classification ──────────────────────────────────


class TestMetricsPath:
    def test_relative_project_dir_internal_leaf_is_managed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = tmp_path / "proj"
        (proj / ".agentic" / "state").mkdir(parents=True)
        leaf = proj / ".agentic" / "state" / "models.json"
        monkeypatch.chdir(tmp_path)
        warnings: list[str] = []
        M._write_cache_managed(
            leaf, '{"ok": 1}', project_dir=Path("proj"), warnings=warnings
        )
        assert leaf.read_text(encoding="utf-8") == '{"ok": 1}'
        assert warnings == []

    def test_relative_leaf_with_relative_project_dir_is_internal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proj = tmp_path / "proj"
        (proj / ".agentic" / "state").mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        leaf = M._models_cache_file(Path("proj"), None)
        assert not leaf.is_absolute()
        warnings: list[str] = []
        M._write_cache_managed(
            leaf, '{"ok": 1}', project_dir=Path("proj"), warnings=warnings
        )
        assert (proj / ".agentic" / "state" / "metrics_models_cache.json").is_file()
        assert warnings == []

    def test_internal_dotdot_path_keeps_the_managed_barrier(
        self, tmp_path: Path
    ) -> None:
        # The module docstring's own claim: ``cache/../state/x.json`` is a
        # lexical path INSIDE the project (``abspath``, not ``resolve``),
        # so it goes through the managed barrier with no warning.
        proj = tmp_path / "proj"
        (proj / ".agentic" / "state").mkdir(parents=True)
        (proj / "cache").mkdir()
        leaf = proj / "cache" / ".." / "state" / "models.json"
        warnings: list[str] = []
        M._write_cache_managed(
            leaf, '{"ok": 1}', project_dir=proj, warnings=warnings
        )
        assert (proj / "state" / "models.json").is_file()
        assert warnings == []

    def test_symlinked_component_refused_external_untouched(
        self, tmp_path: Path
    ) -> None:
        proj = tmp_path / "proj"
        (proj / ".agentic").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (proj / "cache").symlink_to(outside)
        leaf = proj / "cache" / "models.json"
        warnings: list[str] = []
        with pytest.raises(managed_fs.ManagedPathError):
            M._write_cache_managed(leaf, "{}", project_dir=proj, warnings=warnings)
        assert not (outside / "models.json").exists()

    def test_external_leaf_warns_with_the_path(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        leaf = tmp_path / "external" / "models.json"
        warnings: list[str] = []
        M._write_cache_managed(leaf, "{}", project_dir=proj, warnings=warnings)
        assert leaf.is_file()
        assert len(warnings) == 1
        assert str(leaf) in warnings[0]

    def test_default_report_path_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        (home / "Desktop").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        path = M._resolve_out_path(None, tmp_path / "proj", {})
        assert path.parent == home / "Desktop"
        assert path.name.startswith("awf-metrics-")
        assert path.name.endswith(".md")
