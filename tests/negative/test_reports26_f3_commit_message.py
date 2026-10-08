"""REPORTS26 F3 (TODO-0163): коммиты юнитов — осмысленный subject + body.

Source: awf-feature-20261007-avto-kommity-bez-suti-awf-verify-todo-nnnn-istoriya-slepaya.md.
История ``git log`` была слепой: ``awf(verify): TODO-NNNN`` — ни сути, ни
файлов.

 Инварианты (TODO-0163):
 1. subject = ``awf(<stage>): TODO-NNNN — <title>``: title — первый H1-
    заголовок ``inbox/TODO-<id>.md`` без ведущего ``#``, ``TODO(-id)`` и
    разделителей; общий subject ≤ 72 символа (title обрезается, полный
    title — в body);
 2. body = полный title + ``unit: TODO-NNNN`` + строка файлов — состав
    плана коммита (``plan.files``, что реально кладётся в коммит; REVIEW
    P2: состояние дерева не равно составу коммита), относительные пути;
    без плана/пустой набор — только title + unit;
3. ``_AWF_COMMIT_RE`` (wait_event) принимает новый subject; смысл не
   меняется — subject называет ровно один TODO, чужой текст не матчит;
4. нет TODO-файла/заголовка — деградация к ``awf(<stage>): TODO-NNNN``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf import paths
from awf.api.wait_event import _AWF_COMMIT_RE
from awf.commit_gate import maybe_commit

TODO = "TODO-0002"
TITLE_SHORT = "осмысленный subject + body с файлами"
TITLE_LONG = "коммиты юнитов: осмысленный subject + body с файлами"


@pytest.fixture
def gate_repo(tmp_git_repo: Path) -> tuple[Path, str, Path]:
    """Committed repo (gitignore + tracked src/b.py, src/c.py) + .agentic
    skeleton + baseline sha."""
    for sub in ("inbox", "context", "logs"):
        (tmp_git_repo / ".agentic" / sub).mkdir(parents=True)
    (tmp_git_repo / ".gitignore").write_text(".agentic/\n")
    (tmp_git_repo / "src").mkdir()
    (tmp_git_repo / "src" / "b.py").write_text("original\n")
    (tmp_git_repo / "src" / "c.py").write_text("tracked\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_git_repo, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_git_repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    (tmp_git_repo / ".agentic" / "context" / f"BASELINE-{TODO}.untracked").write_text("")
    return tmp_git_repo, sha, tmp_git_repo / ".agentic" / "logs"


def _todo(proj: Path, content: str) -> None:
    (paths.inbox(proj) / f"{TODO}.md").write_text(content, encoding="utf-8")


def _subject(repo: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _body(repo: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%b"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _files_line(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("files: "):
            return line
    return ""


def _unit_change(repo: Path) -> None:
    """Tracked modification + a new file — the unit's work."""
    (repo / "src" / "b.py").write_text("original\nchanged\n")
    (repo / "src" / "a.py").write_text("x = 1\n")


def _commit(repo: Path, sha: str, logs: Path) -> None:
    ok = maybe_commit(
        "verify", TODO, "commit_and_report", repo, logs,
        auto=False, baseline_sha=sha,
    )
    assert ok.status == "committed", f"the unit commit must commit — {ok.status}: {ok.reason}"


class TestUnitCommitCycle:
    """Real commit cycle on a tmp repo: subject, body, files line."""

    def test_subject_has_title_body_has_unit_and_files(self, gate_repo: tuple[Path, str, Path]):
        repo, sha, logs = gate_repo
        _todo(repo, f"# TODO — {TITLE_SHORT}\n\n**Источник:** репорт.\n")
        _unit_change(repo)
        _commit(repo, sha, logs)

        subject = _subject(repo)
        assert subject == f"awf(verify): {TODO} — {TITLE_SHORT}", subject
        assert len(subject) <= 72, subject

        body = _body(repo)
        assert f"unit: {TODO}" in body, body
        assert TITLE_SHORT in body, body
        files_line = _files_line(body)
        # plan order: modified tracked first, then new untracked
        assert files_line == "files: src/b.py, src/a.py", body

    def test_long_title_truncated_in_subject_full_in_body(self, gate_repo: tuple[Path, str, Path]):
        repo, sha, logs = gate_repo
        _todo(repo, f"# TODO — {TITLE_LONG}\n")
        _unit_change(repo)
        _commit(repo, sha, logs)

        subject = _subject(repo)
        assert subject.startswith(f"awf(verify): {TODO} — "), subject
        assert len(subject) <= 72, subject
        assert subject != f"awf(verify): {TODO} — {TITLE_LONG}", "the title must be truncated"

        body = _body(repo)
        assert TITLE_LONG in body, f"the full title must survive in the body: {body}"
        assert f"unit: {TODO}" in body, body

    def test_heading_with_todo_id_strips_the_id(self, gate_repo: tuple[Path, str, Path]):
        repo, sha, logs = gate_repo
        _todo(repo, f"# {TODO} — FU-01: матрица регрессий\n")
        _unit_change(repo)
        _commit(repo, sha, logs)

        subject = _subject(repo)
        assert subject == f"awf(verify): {TODO} — FU-01: матрица регрессий", subject

    def test_todo_without_heading_degrades_to_legacy(self, gate_repo: tuple[Path, str, Path]):
        repo, sha, logs = gate_repo
        _todo(repo, "**Источник:** репорт.\nбез заголовка\n")
        _unit_change(repo)
        _commit(repo, sha, logs)

        assert _subject(repo) == f"awf(verify): {TODO}"
        assert _body(repo) == ""

    def test_heading_only_todo_id_degrades_to_legacy(self, gate_repo: tuple[Path, str, Path]):
        """A heading that is the boilerplate only (no essence) degrades —
        the same shape the e2e fixtures use (``# TODO-0001``)."""
        repo, sha, logs = gate_repo
        _todo(repo, f"# {TODO}\nstub analytical task\n")
        _unit_change(repo)
        _commit(repo, sha, logs)

        assert _subject(repo) == f"awf(verify): {TODO}"
        assert _body(repo) == ""

    def test_missing_todo_file_degrades_to_legacy(self, gate_repo: tuple[Path, str, Path]):
        # lazy import: the symbol is new — on the baseline the module must
        # still collect, and these tests fail on assertions, not collection
        from awf.commit_gate import build_commit_message

        repo, _, _ = gate_repo
        assert build_commit_message("verify", TODO, repo) == f"awf(verify): {TODO}"

    def test_files_line_lists_only_planned_files(
        self, gate_repo: tuple[Path, str, Path]
    ):
        """REVIEW P2: the files line is the commit content, not the tree
        state — a foreign staged change (A-01) sits in the tree, outside
        the plan, and must be named neither in the body nor in the commit."""
        repo, sha, logs = gate_repo
        _todo(repo, f"# TODO — {TITLE_SHORT}\n")
        (repo / "src" / "b.py").write_text("original\nchanged\n")  # the unit's work
        (repo / "src" / "c.py").write_text("tracked\nwip\n")  # foreign WIP
        subprocess.run(["git", "add", "src/c.py"], cwd=repo, check=True)
        _commit(repo, sha, logs)

        assert _files_line(_body(repo)) == "files: src/b.py", _body(repo)

        committed = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.split()
        assert committed == ["src/b.py"], committed
        # A-01: the foreign staged change survives the unit commit
        assert "wip" in (repo / "src" / "c.py").read_text()

    def test_empty_plan_gives_title_and_unit_without_files_line(
        self, gate_repo: tuple[Path, str, Path]
    ):
        from awf.commit_gate import build_commit_message

        repo, _, _ = gate_repo
        _todo(repo, f"# TODO — {TITLE_SHORT}\n")
        message = build_commit_message("verify", TODO, repo)

        assert message.splitlines()[0] == f"awf(verify): {TODO} — {TITLE_SHORT}"
        assert f"unit: {TODO}" in message
        assert "files:" not in message


class TestCommitSubjectRegex:
    """_AWF_COMMIT_RE (wait_event): the new subject matches, foreign text
    does not — the subject still names exactly one TODO."""

    def test_new_subject_matches(self):
        m = _AWF_COMMIT_RE.match(f"awf(verify): {TODO} — {TITLE_LONG}")
        assert m is not None, "the title-suffixed subject must match"
        assert m.group(1) == TODO

    def test_old_subject_still_matches(self):
        m = _AWF_COMMIT_RE.match(f"awf(verify): {TODO}")
        assert m is not None
        assert m.group(1) == TODO

    def test_long_id_with_title_matches(self):
        m = _AWF_COMMIT_RE.match("awf(verify): TODO-10000 — title")
        assert m is not None
        assert m.group(1) == "TODO-10000"

    @pytest.mark.parametrize(
        "line",
        [
            f"awf(verify): see {TODO} in the log",
            f"fix: {TODO}",
            "awf(verify): TODO-123",
            f"awf(verify): {TODO}x",
            "awf(verify):",
            f"awf(verify): {TODO} and {TODO} together",
        ],
    )
    def test_foreign_text_does_not_match(self, line: str):
        assert _AWF_COMMIT_RE.match(line) is None, line
