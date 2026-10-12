"""REPORTS33 (TODO-0204): dirty-warning обещает исключение — сделай это правдой.

Репорт awf-bug-20261012-dirty-warning-obeschaet-isklyuchenie-fayla-iz-yunit-kommita.md
(opencode-session-manager): диспатч при «M .gitignore» обещал «will NOT enter
the commit — these changes pre-date the unit», но коммит включил pre-dirty
содержимое целиком.

Кейсы (соответствуют блоку «Приёмка» TODO-0204):

- a) dirty-диспатч (M file) → warning говорит, что pre-dispatch-правки
  останутся вне коммита; юнит ПРАВИТ файл → в коммите только дельта юнита
  (git show: pre-dispatch-строка отсутствует, юнит-строка есть;
  pre-dispatch-правка осталась в worktree незакоммиченной);
- b) юнит не касается pre-dirty файла → файла нет в коммите, дерево всё
  ещё грязное;
- c) staged-на-baseline → вне коммита (0195 не сломан); чистые на baseline
  файлы — как раньше (полная правка юнита в коммите);
- d) заголовок «# TODO — · Text» → subject без артефакта «— ·»
  (косметика репорта, формат REPORTS26 F3 не сломан).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from awf import api, commit_gate, commit_plan

TODO = "TODO-0001"


@pytest.fixture
def project(tmp_git_repo: Path) -> Path:
    """Git-репо с .agentic/, пустыми test-командами и ЗАКОММИЧЕННЫМ
    .agentic/ (definitions) — дерево на диспатче чистое, runtime-директории
    уже gitignored самим init."""
    api.init_project(
        tmp_git_repo,
        project_name="R33Delta",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "awf init"], cwd=tmp_git_repo, check=True)
    return tmp_git_repo


def _git(project: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=project, check=True, capture_output=True, text=True)


def _git_out(project: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=project, check=True, capture_output=True, text=True
    ).stdout


def _dispatch(project: Path):
    return api.dispatch_todo(project, f"# {TODO}\nTask\n", todo_id=TODO)


def _logs_dir(project: Path) -> Path:
    return project / ".agentic" / "logs"


def _committed_files(project: Path) -> list[str]:
    return _git_out(project, "show", "--name-only", "--format=", "HEAD").split()


def test_a_dirty_dispatch_unit_edit_commits_only_unit_delta(project: Path):
    """(a) pre-dispatch правка + правка юнита (юнит дописывает строку ПОСЛЕ
    pre-dispatch-строки — соседний ханк) → коммит несёт только дельту
    юнита; pre-dispatch-правка остаётся в worktree незакоммиченной."""
    (project / "core.py").write_text("core v1\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    # pre-dispatch WIP (до диспатча)
    (project / "core.py").write_text("core v1\npre-dispatch wip\n", encoding="utf-8")

    dispatch = _dispatch(project)
    assert "core.py" in dispatch.dirty_files
    assert "stay OUTSIDE the unit commit" in dispatch.dirty_warning, (
        "warning обязан обещать по факту: pre-dispatch-правки останутся "
        f"вне коммита (получено: {dispatch.dirty_warning})"
    )
    assert (project / ".agentic" / "context" / f"BASELINE-{TODO}.worktree").is_file(), (
        "грязное дерево на диспатче обязано оставить снапшот worktree "
        "(BASELINE-{id}.worktree)"
    )

    # работа юнита: строка после pre-dispatch-строки (соседний ханк)
    (project / "core.py").write_text(
        "core v1\npre-dispatch wip\nunit work\n", encoding="utf-8"
    )

    plan = commit_plan.build_commit_plan(project, TODO, dispatch.baseline_sha)
    assert "core.py" in plan.files, (
        "файл, который юнит правил после диспатча, обязан войти в план "
        f"(получено: {plan.files})"
    )

    outcome = commit_gate.maybe_commit(
        "worker", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )
    assert outcome.status == "committed", (
        f"коммит юнита обязан состояться (получено {outcome.status}: {outcome.reason})"
    )
    assert "core.py" in _committed_files(project)

    show = _git_out(project, "show", "HEAD", "--", "core.py")
    assert "unit work" in show, "дельта юнита обязана быть в коммите"
    assert "pre-dispatch wip" not in show, (
        "pre-dispatch-правка обязана остаться ВНЕ коммита "
        f"(git show: {show!r})"
    )
    # pre-dispatch-правка осталась в worktree — незакоммиченная
    assert (project / "core.py").read_text(encoding="utf-8") == (
        "core v1\npre-dispatch wip\nunit work\n"
    )
    # незакоммиченной остаётся ровно pre-dispatch-правка: в ДОБАВЛЕННЫХ
    # строках worktree-diff она есть, строки юнита — нет (как контекст —
    # может быть, коммит их несёт)
    worktree_diff = _git_out(project, "diff", "--", "core.py")
    added = [
        ln for ln in worktree_diff.splitlines()
        if ln.startswith("+") and not ln.startswith("+++")
    ]
    assert any("pre-dispatch wip" in ln for ln in added), (
        "pre-dispatch-правка обязана светиться как незакоммиченная "
        f"(worktree diff: {worktree_diff!r})"
    )
    assert not any("unit work" in ln for ln in added)


def test_b_dirty_file_unit_untouched_stays_out_of_commit(project: Path):
    """(b) юнит не касается pre-dirty файла → пустая дельта → файла нет в
    коммите, дерево всё ещё грязное."""
    (project / "core.py").write_text("core v1\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    (project / "core.py").write_text("core v1\npre-dispatch wip\n", encoding="utf-8")

    dispatch = _dispatch(project)
    (project / "work.py").write_text("work\n", encoding="utf-8")  # работа юнита

    plan = commit_plan.build_commit_plan(project, TODO, dispatch.baseline_sha)
    assert "work.py" in plan.files
    assert "core.py" not in plan.files, (
        "файл, которого юнит не касался, обязан выпасть из плана "
        f"(получено: {plan.files})"
    )
    reasons = dict(plan.excluded)
    assert "core.py" in reasons, (
        f"выпавший из плана файл обязан быть в excluded с причиной: {plan.excluded}"
    )

    outcome = commit_gate.maybe_commit(
        "worker", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )
    assert outcome.status == "committed", outcome
    committed = _committed_files(project)
    assert "work.py" in committed
    assert "core.py" not in committed, (
        "неквалифицированная pre-dispatch-правка не может попасть в коммит юнита"
    )
    # дерево всё ещё грязное — pre-dispatch-правка на месте
    assert "pre-dispatch wip" in (project / "core.py").read_text(encoding="utf-8")
    status = _git_out(project, "status", "--short")
    assert " M core.py" in status


def test_c_staged_at_baseline_out_and_clean_files_as_before(project: Path):
    """(c) staged-на-baseline → вне коммита (0195 не сломан); чистый на
    baseline файл — как раньше: полная правка юнита в коммите."""
    (project / "core.py").write_text("core v1\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    (project / "clean.py").write_text("clean v1\n", encoding="utf-8")
    _git(project, "add", "clean.py")
    _git(project, "commit", "-qm", "clean")
    (project / "staged.txt").write_text("s\n", encoding="utf-8")
    _git(project, "add", "staged.txt")  # foreign WIP в индексе НА момент диспатча

    dispatch = _dispatch(project)
    # юнит правит только чистый файл
    (project / "clean.py").write_text("clean v1\nunit work\n", encoding="utf-8")

    plan = commit_plan.build_commit_plan(project, TODO, dispatch.baseline_sha)
    assert "clean.py" in plan.files
    assert "staged.txt" not in plan.files, (
        "staged-на-baseline обязан оставаться вне коммита юнита (0195)"
    )
    assert ("staged.txt", "staged-at-baseline") in plan.excluded

    outcome = commit_gate.maybe_commit(
        "worker", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )
    assert outcome.status == "committed", outcome
    committed = _committed_files(project)
    assert "clean.py" in committed
    assert "staged.txt" not in committed

    show = _git_out(project, "show", "HEAD", "--", "clean.py")
    assert "unit work" in show, (
        "чистый на baseline файл: полная правка юнита в коммите, как раньше"
    )
    # foreign staged-запись не тронута — всё ещё в индексе
    assert "staged.txt" in _git_out(project, "diff", "--cached", "--name-only")


def test_d_heading_bullet_stripped_from_subject(project: Path):
    """(d) заголовок «# TODO-0001 — · Text» → subject без артефакта «— ·»;
    формат REPORTS26 F3 (awf(stage): TODO — title) сохранён."""
    (project / ".agentic" / "inbox" / f"{TODO}.md").write_text(
        f"# {TODO} — · Инструментация: counters\nTask\n", encoding="utf-8"
    )
    message = commit_gate.build_commit_message("verify", TODO, project)
    subject = message.splitlines()[0]
    assert "— ·" not in message, (
        f"артефакт «— ·» обязан исчезнуть из subject (получено: {subject!r})"
    )
    assert "·" not in subject, (
        f"ведущий маркер-точка обязан срезаться (получено: {subject!r})"
    )
    assert subject.startswith("awf(verify): TODO-0001 — Инструментация"), (
        f"формат REPORTS26 F3 и суть заголовка обязаны сохраниться: {subject!r}"
    )


# ─── QA findings (REVIEW pass, 2026-10-12) — red on purpose ─────────────


def test_qa_f1_unit_delta_misplaced_with_duplicated_context(project: Path):
    """QA F1 (red): 7+ pre-dispatch-строк ПЕРЕД правкой юнита + дублирующийся
    контекст-паттерн в файле → дельта юнита молча ставится в МЕСТО ПЕРВОГО
    дубликата (коммит несёт повреждённое содержимое, warning не срабатывает).

    Геометрия: HEAD несёт тройку X,Y,Z в начале и в конце файла; pre-dispatch
    вставляет 10 строк перед последней тройкой; юнит дописывает 2 строки в
    КОНЕЦ файла. Корректный коммит: unit-строки в конце, pre-строки вне."""
    lines = ["X", "Y", "Z", "d1", "d2"] + [f"filler {i}" for i in range(40)] + ["X", "Y", "Z"]
    (project / "core.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    # pre-dispatch WIP: 10 строк перед последней тройкой
    pre = [f"pre {i}" for i in range(10)]
    (project / "core.py").write_text(
        "\n".join(lines[:45] + pre + lines[45:]) + "\n", encoding="utf-8"
    )
    dispatch = _dispatch(project)
    assert "core.py" in dispatch.dirty_files
    assert (project / ".agentic" / "context" / f"BASELINE-{TODO}.worktree").is_file()
    # работа юнита: 2 строки в конец файла
    (project / "core.py").write_text(
        "\n".join(lines[:45] + pre + lines[45:] + ["unit1", "unit2"]) + "\n",
        encoding="utf-8",
    )

    plan = commit_plan.build_commit_plan(project, TODO, dispatch.baseline_sha)
    assert "core.py" in plan.files
    assert "core.py" in plan.delta_files, (
        f"файл обязан идти по delta-механизму (получено: {plan.delta_files})"
    )
    outcome = commit_gate.maybe_commit(
        "worker", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )
    assert outcome.status == "committed", (
        f"{outcome.status}: {outcome.reason}"
    )
    committed = _git_out(project, "show", "HEAD:core.py").splitlines()
    assert not any(ln.startswith("pre ") for ln in committed), (
        "pre-dispatch-строки обязаны остаться вне коммита"
    )
    assert committed[-2:] == ["unit1", "unit2"], (
        "дельта юнита обязана лечь в то место, где её внёс юнит (конец файла); "
        f"получено: unit1 на позиции {committed.index('unit1')}, конец "
        f"коммита: {committed[-4:]!r}"
    )


def test_qa_f2_executable_mode_lost_in_delta_commit(project: Path):
    """QA F2 (red): delta-путь коммитит исполняемый файл (100755 в HEAD)
    с mode 100644 — исполняемый бит молча теряется в коммите юнита."""
    (project / "run.sh").write_text("#!/bin/sh\necho v1\n", encoding="utf-8")
    os.chmod(project / "run.sh", 0o755)
    _git(project, "add", "run.sh")
    _git(project, "commit", "-qm", "run")
    # pre-dispatch WIP
    (project / "run.sh").write_text("#!/bin/sh\necho v1\necho pre\n", encoding="utf-8")
    dispatch = _dispatch(project)
    assert "run.sh" in dispatch.dirty_files
    # работа юнита
    (project / "run.sh").write_text(
        "#!/bin/sh\necho v1\necho pre\necho unit\n", encoding="utf-8"
    )
    plan = commit_plan.build_commit_plan(project, TODO, dispatch.baseline_sha)
    assert "run.sh" in plan.files
    assert "run.sh" in plan.delta_files
    outcome = commit_gate.maybe_commit(
        "worker", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )
    assert outcome.status == "committed", f"{outcome.status}: {outcome.reason}"
    entry = _git_out(project, "ls-tree", "HEAD", "--", "run.sh").split()
    assert entry[0] == "100755", (
        f"исполняемый бит обязан сохраниться в коммите юнита "
        f"(mode {entry[0]}, ожидается 100755)"
    )


def test_qa_f3_unit_delta_misplaced_duplicate_within_anchor_window(project: Path):
    """QA F3 (red, REVIEW iter 3): окно поиска стартует с anchor-4, а не с
    точного якоря — копия 4-строчного контекст-паттерна на 4 строки ДО
    якоря перехватывает дельту юнита. F1-фикс ограничил окно, но приоритет
    точному якорю не дал: _find_seq сканирует от начала окна, первый
    дубликат выигрывает, хотя верное совпадение лежит ровно на якоря.

    Геометрия (проверена scratch-репо): HEAD = [x,a,b,c,x,a,b,c] (паттерн
    дважды); pre-dispatch дописывает 'p' в начало (якорь = 5-1 = 4,
    _head_position считает верно); юнит меняет ПОСЛЕДНИЙ 'c' на 'C'.
    Корректный коммит: 'C' на конце, первая тройка нетронута."""
    head = ["x", "a", "b", "c", "x", "a", "b", "c"]
    (project / "core.py").write_text("\n".join(head) + "\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    (project / "core.py").write_text(
        "\n".join(["p"] + head) + "\n", encoding="utf-8"
    )
    dispatch = _dispatch(project)
    assert "core.py" in dispatch.dirty_files
    assert (project / ".agentic" / "context" / f"BASELINE-{TODO}.worktree").is_file()
    (project / "core.py").write_text(
        "\n".join(["p"] + head[:-1] + ["C"]) + "\n", encoding="utf-8"
    )
    plan = commit_plan.build_commit_plan(project, TODO, dispatch.baseline_sha)
    assert "core.py" in plan.files
    assert "core.py" in plan.delta_files, (
        f"файл обязан идти по delta-механизму (получено: {plan.delta_files})"
    )
    outcome = commit_gate.maybe_commit(
        "worker", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )
    assert outcome.status == "committed", f"{outcome.status}: {outcome.reason}"
    committed = _git_out(project, "show", "HEAD:core.py").splitlines()
    assert "p" not in committed, "pre-dispatch-строка обязана остаться вне коммита"
    assert committed[:4] == ["x", "a", "b", "c"], (
        "первая копия паттерна обязана остаться нетронутой — дельта юнита "
        f"обязана лечь на ТОЧНЫЙ якорь (конец файла), а не на первый "
        f"дубликат в окне (получено: {committed!r})"
    )
    assert committed[-1] == "C", (
        f"юнит-правка обязана быть в конце файла (получено: {committed!r})"
    )


def test_qa_f4_emptied_file_unit_write_merges_head_lines_silently(project: Path):
    """QA F4 (red, REVIEW iter 3): файл, ОПУСТОШЕННЫЙ до диспатча (снапшот
    пустой), + юнит пишет в него содержимое. Ханк юнита без контекста
    (git: @@ -0,0 +1,K @@ → base_pos = -1); пустой-seq-ветка считает
    idx = min(-1 + shift, len(target)) = -1 — merge вшивает юнит-строки
    ПЕРЕД последней строкой HEAD (строки HEAD утекают в коммит),
    деградация не срабатывает: ни warning, ни legacy. По docstring
    _commit_delta_file «не помещается» = деградация на legacy (весь
    worktree-контент), громко, никогда молча."""
    (project / "core.py").write_text("h1\nh2\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    (project / "core.py").write_text("", encoding="utf-8")  # pre-dispatch: пусто
    dispatch = _dispatch(project)
    assert "core.py" in dispatch.dirty_files
    assert (project / ".agentic" / "context" / f"BASELINE-{TODO}.worktree").is_file()
    (project / "core.py").write_text("w1\nw2\nw3\n", encoding="utf-8")
    plan = commit_plan.build_commit_plan(project, TODO, dispatch.baseline_sha)
    assert "core.py" in plan.files
    assert "core.py" in plan.delta_files, (
        f"файл обязан идти по delta-механизму (получено: {plan.delta_files})"
    )
    outcome = commit_gate.maybe_commit(
        "worker", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )
    assert outcome.status == "committed", f"{outcome.status}: {outcome.reason}"
    committed = _git_out(project, "show", "HEAD:core.py").splitlines()
    assert committed == ["w1", "w2", "w3"], (
        "пустой pre-dispatch-снапшот: деградация на legacy (весь worktree-"
        f"контент юнита) — строки HEAD не имеют права попасть в коммит "
        f"(получено: {committed!r})"
    )


def test_qa_f5_unit_append_misplaced_when_context_edited_pre_dispatch(project: Path):
    """QA F5 (red, REVIEW iter 4, 12.10): pre-dispatch меняет ровно ту
    строку, которую ханк юнита использует как контекст, а юнит дописывает
    строку в КОНЕЦ файла. Порядок fuzz-цикла _apply_unit_delta доходит
    до пустой-seq-ветки (комбинация (0,2)) РАНЬШЕ, чем до варианта (1,0),
    который бы совпал на точном якоря; пустая ветка считает
    idx = min(base_pos + shift, len(target)) — начало ханка (prepend-
    семантика), игнорируя и то, что добавление ИДЁТ ПОСЛЕ контекста, и
    pre-dispatch-сдвиг. Юнит-строка ложится в НАЧАЛО файла в коммите,
    молча (деградация не срабатывает, ни warning, ни legacy).

    Геометрия (проверена scratch-репо): HEAD = [a, b]; pre-dispatch:
    a → a2 (снапшот на диспатче = [a2, b]); юнит дописывает 'u' в конец
    (worktree = [a2, b, u]). Корректный коммит: [a, b, u] (дельта юнита —
    после контекста, pre-dispatch-правка a2 вне коммита). Получено:
    [u, a, b] — юнит-строка в начале, строки HEAD на местах."""
    (project / "core.py").write_text("a\nb\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    # pre-dispatch WIP: a -> a2 (именно строка, которую ханк юнита возьмёт
    # в контекст)
    (project / "core.py").write_text("a2\nb\n", encoding="utf-8")
    dispatch = _dispatch(project)
    assert "core.py" in dispatch.dirty_files
    assert (project / ".agentic" / "context" / f"BASELINE-{TODO}.worktree").is_file()
    # работа юнита: строка в конец файла
    (project / "core.py").write_text("a2\nb\nu\n", encoding="utf-8")
    plan = commit_plan.build_commit_plan(project, TODO, dispatch.baseline_sha)
    assert "core.py" in plan.files
    assert "core.py" in plan.delta_files, (
        f"файл обязан идти по delta-механизму (получено: {plan.delta_files})"
    )
    outcome = commit_gate.maybe_commit(
        "worker", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )
    assert outcome.status == "committed", f"{outcome.status}: {outcome.reason}"
    committed = _git_out(project, "show", "HEAD:core.py").splitlines()
    assert "a2" not in committed, (
        "pre-dispatch-правка обязана остаться вне коммита "
        f"(получено: {committed!r})"
    )
    assert committed == ["a", "b", "u"], (
        "дельта юнита (строка 'u' в конец файла) обязана лечь ПОСЛЕ "
        f"контекста, а не в начале файла (получено: {committed!r})"
    )


def test_qa_f6_stale_worktree_snapshot_on_redispatch(project: Path, monkeypatch):
    """QA F6 (REVIEW iter 5, 12.10): re-диспатч того же id (re-issue после
    rejection) при сбое `git stash create`. До фикса `.worktree` был
    единственный условно-пишущийся baseline-файл: снапшот ПЕРВОЙ попытки
    переживал re-диспатч, коммит-гейт читал устаревший снапшот, и правка
    WIP пользователя МЕЖДУ попытками (W1 → W2) молча входила в коммит
    юнита, пока warning (snapshot_available=True по старому файлу) обещал,
    что pre-dispatch-правки «stay OUTSIDE the unit commit».

    Фикс: `.worktree` пишется безусловно — свежий снапшот при грязном
    дереве, файл удаляется при пустом/сбойном `stash create` (никаких
    пережитков). Геометрия (проверена scratch-репо): HEAD = [a,b,c,d,e];
    попытка 1: pre-dispatch a→a2 (снапшот S1 = [a2,b,c,d,e]); re-issue:
    пользователь дописывает w2 (worktree = [a2,b,c,d,e,w2]),
    `git stash create` падает (симулировано — git failure, глотается
    check=False); юнит дописывает u. После фикса: снапшот первой попытки
    не переживает re-диспатч, warning честный («WILL ENTER the unit commit
    as-is»), коммит = legacy-содержимое worktree [a2,b,c,d,e,w2,u] — ровно
    то, что warning обещал (деградация честная, обещания нет)."""
    (project / "core.py").write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    # попытка 1: pre-dispatch WIP a -> a2
    (project / "core.py").write_text("a2\nb\nc\nd\ne\n", encoding="utf-8")
    _dispatch(project)
    snap_file = project / ".agentic" / "context" / f"BASELINE-{TODO}.worktree"
    assert snap_file.is_file()
    stale_sha = snap_file.read_text(encoding="utf-8").strip()
    # re-issue того же id (md в inbox удалён — поддерживаемое precondition)
    (project / ".agentic" / "inbox" / f"{TODO}.md").unlink()
    # пользователь поправил свой WIP между попытками: W1 -> W2 (w2 в конец)
    (project / "core.py").write_text("a2\nb\nc\nd\ne\nw2\n", encoding="utf-8")
    from awf import git_utils as awf_git_utils
    real_git_stdout = awf_git_utils.git_stdout

    def failing_stash(cwd, *args, check=True):
        if args == ("stash", "create"):
            return ""  # git failure, глотается семантикой check=False
        return real_git_stdout(cwd, *args, check=check)

    monkeypatch.setattr(awf_git_utils, "git_stdout", failing_stash)
    dispatch2 = api.dispatch_todo(project, f"# {TODO}\nTask v2\n", todo_id=TODO)
    # устаревший снапшот не пережил re-диспатч (никаких пережитков)
    assert not snap_file.is_file(), (
        "re-диспатч без свежего снапшота обязан не оставлять снапшот "
        f"первой попытки на месте — он отравляет delta-механизм "
        f"(на месте: {stale_sha!r})"
    )
    # warning честный: без свежего снапшота исключение не обещается
    assert "stay OUTSIDE the unit commit" not in dispatch2.dirty_warning, (
        f"warning без свежего снапшота не обязан обещать исключение: "
        f"{dispatch2.dirty_warning!r}"
    )
    assert "WILL ENTER the unit commit as-is" in dispatch2.dirty_warning, (
        f"деградация обязана быть обещана явно: {dispatch2.dirty_warning!r}"
    )
    # работа юнита: строка в конец
    (project / "core.py").write_text("a2\nb\nc\nd\ne\nw2\nu\n", encoding="utf-8")
    plan = commit_plan.build_commit_plan(project, TODO, dispatch2.baseline_sha)
    assert "core.py" not in plan.delta_files, (
        f"без снапшота delta-механизм обязан быть выключен "
        f"(получено: {plan.delta_files})"
    )
    outcome = commit_gate.maybe_commit(
        "worker", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch2.baseline_sha,
    )
    assert outcome.status == "committed", f"{outcome.status}: {outcome.reason}"
    committed = _git_out(project, "show", "HEAD:core.py").splitlines()
    assert committed == ["a2", "b", "c", "d", "e", "w2", "u"], (
        "честная деградация = legacy-коммит всего worktree-содержимого — "
        "ровно то, что warning обещал (получено: "
        f"{committed!r})"
    )


def test_qa_f7_crlf_file_degrades_loudly_not_lf_normalized(project: Path):
    """QA F7 (red, REVIEW iter 6, 12.10, аудит супервизора): CRLF-файл в
    delta-пути молча нормализуется в LF. splitlines() ест \\r (и в разбивке
    HEAD-контента, и в парсере патча), обратный join собирает файл с LF —
    коммит получает ВЕСЬ файл с LF, worktree остаётся CRLF (фантомная
    «грязь» после коммита, скрытая смена контента). По docstring
    _commit_delta_file «не помещается» = громкая деградация на legacy
    (весь worktree-контент, CRLF цел), никогда молча.

    Геометрия: HEAD = [a, b] (CRLF); pre-dispatch: a -> a2 (CRLF); юнит
    дописывает 'u' в конец (CRLF). До фикса коммит: [a, b, u] с LF
    (нормализация); после: legacy-контент [a2, b, u] с CRLF + WARNING
    с причиной «line endings» в логе. Чистая функция: \\r в head_text
    ИЛИ patch_text → None (merge CRLF-контента запрещён); LF-контент
    мержится как раньше (контроль)."""
    (project / "core.py").write_bytes(b"a\r\nb\r\n")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    # pre-dispatch WIP: a -> a2 (CRLF сохранён)
    (project / "core.py").write_bytes(b"a2\r\nb\r\n")
    dispatch = _dispatch(project)
    assert "core.py" in dispatch.dirty_files
    assert (project / ".agentic" / "context" / f"BASELINE-{TODO}.worktree").is_file()
    # работа юнита: строка в конец файла (CRLF сохранён)
    (project / "core.py").write_bytes(b"a2\r\nb\r\nu\r\n")
    plan = commit_plan.build_commit_plan(project, TODO, dispatch.baseline_sha)
    assert "core.py" in plan.files
    assert "core.py" in plan.delta_files, (
        f"файл обязан идти по delta-механизму (получено: {plan.delta_files})"
    )
    outcome = commit_gate.maybe_commit(
        "worker", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )
    assert outcome.status == "committed", f"{outcome.status}: {outcome.reason}"
    committed = subprocess.run(
        ["git", "cat-file", "blob", "HEAD:core.py"],
        cwd=project, check=True, capture_output=True,
    ).stdout
    assert committed == b"a2\r\nb\r\nu\r\n", (
        "CRLF-файл в delta-пути: тихая нормализация в LF недопустима — "
        "коммит несёт либо legacy-контент worktree с CRLF, либо файл "
        f"банится из пути (получено: {committed!r})"
    )
    log_text = (
        project / ".agentic" / "logs" / "orchestrator.log"
    ).read_text(encoding="utf-8")
    assert "line endings" in log_text, (
        "деградация обязана быть громкой: WARNING с причиной «line "
        "endings» обязан быть в логе (строки про REPORTS33: "
        f"{[ln for ln in log_text.splitlines() if 'REPORTS33' in ln]!r})"
    )
    # прямой прогон чистой функции: \r в head_text или patch_text → None
    assert commit_gate._apply_unit_delta(
        "a\r\nb\r\nc\r\n", "@@ -1,2 +1,2 @@\n a\r\n-b\r\n+B\r\n", []
    ) is None, "CRLF в HEAD-контенте обязан деградировать, а не нормализоваться"
    assert commit_gate._apply_unit_delta(
        "a\nb\nc\n", "@@ -1,2 +1,2 @@\n a\r\n-b\r\n+B\r\n", []
    ) is None, "CRLF в патче обязан деградировать, а не нормализоваться"
    # контроль: LF-контент мержится как раньше (бан не перестраховочный)
    assert commit_gate._apply_unit_delta(
        "a\nb\nc\n", "@@ -1,1 +1,1 @@\n-a\n+A\n", []
    ) == "A\nb\nc\n", "LF-контент обязан мержиться по-прежнему"
