"""R2.2 architect-review, iter 6 (TODO-0221): the isolate surface DROPS the
unreadable-tree fact the refusal surface keeps.

Файл КРАСНЫЙ на дереве до фикса стадии agent-architect-review.

``SourceTreeReport.error`` — не украшение: докстрока модуля объявляет инвариант
«a tree that cannot be read is NOT clean». Его держит ``format_dirty_refusal``
(ветка ``report.error`` печатает «could not be read as clean» и НЕ притворяется,
что список путей пуст). Соседняя поверхность того же модуля —
``format_isolate_report`` — тот же ``error`` игнорирует: под
``dirty_policy=isolate`` дерево, которое модель ОТКАЗАЛАСЬ читать
(unmerged/sparse/split), проходит, а артефакты рапортуют «0 pre-existing
path(s) stay OUTSIDE the unit». Это ложь: множество исключений НЕ известно, а
владелец видит уверенный ноль. Инвариант «error — это не пустой список»,
проведённый на одной поверхности рядом с другой, разъехался.

Фикс — честная ветка ``report.error`` в ``format_isolate_report`` (политика
по-прежнему proceed: решение isolate не меняется, меняется только сообщение).

Все репозитории — disposable в ``tmp_path``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from awf import api
from awf.api._errors import AwfApiError
from awf.dispatch_policy import SourceTreeReport, format_isolate_report

SRC = "TODO-0001"


def _git(repo: Path, *args: str, check: bool = True) -> None:
    subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True)


def _project(tmp_path: Path, *, policy: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    api.init_project(
        repo,
        project_name="R2Arch2",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    cfg = repo / ".agentic" / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    data["config_version"] = 1
    data["discipline"] = {"dirty_policy": policy}
    cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "awf init")
    return repo


def _make_unmerged(repo: Path) -> None:
    """Drive a real merge conflict — the tree the R2.1 model refuses to read."""
    (repo / "f.txt").write_bytes(b"base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "f.txt").write_bytes(b"side\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "side")
    _git(repo, "checkout", "-q", "-")
    (repo / "f.txt").write_bytes(b"main\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "main")
    _git(repo, "merge", "side", check=False)  # conflict → unmerged index


def _assert_strict_utf8(text: str, what: str) -> None:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        pytest.fail(f"{what} не кодируется строгим UTF-8 sink'ом")


def test_isolate_report_names_unreadable_tree_not_zero_paths():
    """Юнит-инвариант: error-отчёт isolate НЕ печатает «0 path(s)» — он
    называет ошибку и объявляет exclude-множество неизвестным."""
    report = SourceTreeReport(
        paths=(), managed=(), error="the index holds an unmerged entry"
    )
    warning, dep = format_isolate_report(SRC, report)
    assert warning, "unreadable tree must still produce an isolate warning"
    assert "0 pre-existing path(s)" not in warning, (
        "isolate must not claim a known empty exclude set for a tree it could "
        f"not read; got {warning!r}"
    )
    assert "unmerged entry" in warning, (
        "the parse error must be surfaced to the owner; got " f"{warning!r}"
    )
    assert "not know" in dep.lower() or "unknown" in dep.lower(), (
        f"the dependency note must say the exclude set is unknown; got {dep!r}"
    )


def test_isolate_dispatch_on_unmerged_tree_surfaces_error(tmp_path):
    """Интеграция: под isolate дерево с unmerged-индексом проходит, но
    артефакты не лгут «0 pre-existing path(s)» — они несут ошибку чтения."""
    repo = _project(tmp_path, policy="isolate")
    _make_unmerged(repo)

    res = api.dispatch_todo(repo, f"# {SRC}\nTask\n", todo_id=SRC)
    assert res.dirty_policy == "isolate"
    assert "0 pre-existing path(s)" not in res.isolate_warning, (
        f"isolate warning lies about a known empty set; got {res.isolate_warning!r}"
    )
    for field, value in (
        ("isolate_warning", res.isolate_warning),
        ("isolate_dependency_warning", res.isolate_dependency_warning),
    ):
        assert value, f"{field} must be non-empty for an unreadable tree"
        _assert_strict_utf8(value, f"dispatch result {field}")
    assert "UNKNOWN" in res.isolate_warning, (
        "isolate warning must declare the exclude set unknown, not a known "
        f"empty one; got {res.isolate_warning!r}"
    )
    assert "unmerged" in res.isolate_warning, (
        f"isolate warning must name the parse error; got {res.isolate_warning!r}"
    )


def test_require_clean_on_unmerged_tree_still_refuses(tmp_path):
    """Соседняя поверхность не ослаблена: require_clean на том же дереве
    по-прежнему отказывает (инвариант `error` → не clean)."""
    repo = _project(tmp_path, policy="require_clean")
    _make_unmerged(repo)

    with pytest.raises(AwfApiError) as exc:
        api.dispatch_todo(repo, f"# {SRC}\nTask\n", todo_id=SRC)
    assert "could not be read" in str(exc.value), (
        f"the refusal must name the unreadable tree; got {exc.value!r}"
    )
    assert not (repo / ".agentic" / "inbox" / f"{SRC}.md").exists(), (
        "a require_clean refusal must reserve nothing (no inbox unit file)"
    )


def test_isolate_on_readable_dirty_tree_still_lists_paths(tmp_path):
    """Регресс-охрана: честная ветка error не съела нормальный isolate —
    читаемое грязное дерево по-прежнему перечисляет исключённые пути."""
    repo = _project(tmp_path, policy="isolate")
    (repo / "wip.txt").write_bytes(b"uncommitted\n")

    res = api.dispatch_todo(repo, f"# {SRC}\nTask\n", todo_id=SRC)
    assert "wip.txt" in res.isolate_warning, (
        f"a readable dirty path must stay listed; got {res.isolate_warning!r}"
    )
    assert "could NOT be read" not in res.isolate_warning
