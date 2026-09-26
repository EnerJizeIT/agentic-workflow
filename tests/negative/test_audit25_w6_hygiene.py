"""Волна 6.3 (аудит 2026-09-25, слой 10 + находки волн): гигиена.

Кандидаты мёртвого кода удаляются (репозиторий до 1.4 — публичный
контракт не зафиксирован), мёртвая ветка checkpoint-ack убирается,
P4-угол парсера закрывается guard'ом, дубль суффикс-помощников
консолидируется в один механизм. Поведение живого кода не меняется.

Красные на baseline:
- test_dead_wrappers_removed — D-02/D-03 ещё на месте;
- test_plan_checkpoint_no_dead_ack_branch — already_decided ещё живёт;
- test_cmd_init_comment_matches_behavior — комментарий ещё утверждает
  старое R1-поведение;
- test_rm_rename_source_not_parsed_as_path — RM-пара не потребляется
  целиком, сиротский источник '?a b' даёт фантом 'b' в план.

test_legacy_plan_keeps_worktree_deletions — регрессия на дереве попытки 1
(F1: guard .exists() шире угла, выбрасывает запись D из legacy-плана);
на baseline зелёный, обязан стать зелёным после фикса.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import awf
from awf import api, git_utils, metrics

try:
    from awf import commit_plan
except ImportError:  # baseline (pre-R-03): the module does not exist yet
    commit_plan = None

TODO = "TODO-0001"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="W6Hygiene")
    proj = tmp_git_repo
    (proj / ".agentic" / "inbox" / f"{TODO}.md").write_text("# Task\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=proj, check=True)
    return proj


def test_dead_wrappers_removed():
    """D-02/D-03: обёртки без вызовов удалены.

    working_tree_clean (git_utils) — обёртка над status_porcelain,
    load_reference_costs (metrics) — обёртка над load_models_catalog +
    model_costs_from_catalog; живые шаги (status_porcelain, каталог с
    fallback) остаются на месте. commit_all НЕ проверяется здесь: у него
    есть живые потребители в тестах (TestCommitAll, H1, AUD01-02) —
    решение об удалении за владельцем, см. DONE-0104.
    """
    assert not hasattr(git_utils, "working_tree_clean"), (
        "D-02: working_tree_clean — обёртка без вызовов, должна быть удалена"
    )
    assert not hasattr(metrics, "load_reference_costs"), (
        "D-03: load_reference_costs — обёртка без вызовов, должна быть удалена"
    )
    # живые шаги, на которые обёртки дублировали работу — не задеты
    assert hasattr(git_utils, "status_porcelain")
    assert hasattr(metrics, "load_models_catalog")
    assert hasattr(metrics, "model_costs_from_catalog")


def test_plan_checkpoint_no_dead_ack_branch():
    """A-03 F2: мёртвая ветка already_decided убрана.

    Токен формы одноразовый: первое решение ставит token_state["token"]
    = None под decision_lock, повторный POST не проходит токен-проверку
    (403 — закреплено A-03.4). Ветка elif decision_holder недостижима,
    ack-страница «Решение уже принято» никогда не рендерится.
    """
    src = (Path(awf.__file__).parent / "plan_checkpoint.py").read_text(
        encoding="utf-8"
    )
    assert "already_decided" not in src, (
        "A-03 F2: ветка already_decided недостижима (токен одноразовый) "
        "и должна быть удалена"
    )


def test_cmd_init_comment_matches_behavior():
    """Комментарий в cmd_init.py не утверждает старое R1-поведение.

    R1: «init без force чистит runtime» — устарело: с A-11 re-init без
    force недеструктивен (только недостающие пустые каталоги), force —
    единственный разрушительный путь.
    """
    src = (Path(awf.__file__).parent / "cmd_init.py").read_text(encoding="utf-8")
    assert "now cleans runtime" not in src, (
        "комментарий всё ещё утверждает, что init без force чистит runtime — "
        "с A-11 это не так (re-init недеструктивен)"
    )


def test_rm_rename_source_not_parsed_as_path(tmp_git_repo: Path):
    """REVIEW-0087 P4: staged rename + правка в worktree (статус RM) с
    именем источника, похожим на статус-префикс.

    Porcelain -z выдаёт пару "RM <target>" + голый источник. Фикс (F1,
    попытка 2): пара потребляется целиком для любого состояния цели
    (Y = " "/M/D) — сиротский токен источника '?a b' до path-ветки не
    доходит, хвост "b" не становится фантомом. Реально существующий
    файл "b" (остаточный угол F1: на старом .exists() guard'е фантом
    проходил бы проверку и ехал в план) план не загрязняет; реальные
    записи (M/??) проходят.
    """
    assert commit_plan is not None, "R-03 module missing (pre-R-03 baseline)"
    proj = _project(tmp_git_repo)

    # rename-источник должен быть в HEAD, иначе git не выдаст R-запись;
    # "b" — реально существующий (tracked, чистый) файл: единственный
    # путь, которым "b" мог бы попасть в план, — фантомный хвост источника
    (proj / "?a b").write_text("rename source\n", encoding="utf-8")
    (proj / "b").write_text("real file named b\n", encoding="utf-8")
    subprocess.run(["git", "add", "?a b", "b"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "add rename source + b"], cwd=proj, check=True)

    # user WIP: staged rename + правка цели в worktree → статус RM
    subprocess.run(["git", "mv", "?a b", "renamed.txt"], cwd=proj, check=True)
    (proj / "renamed.txt").write_text("rename source\nedited\n", encoding="utf-8")

    # юнитная работа, которая в план должна попасть
    (proj / "file.txt").write_text("unit work\n", encoding="utf-8")
    (proj / "new.txt").write_text("untracked\n", encoding="utf-8")

    status = subprocess.run(
        ["git", "status", "--porcelain", "-z"],
        cwd=proj, capture_output=True, text=True, check=True,
    ).stdout.split("\0")
    assert "RM renamed.txt" in status, (
        f"тест строит именно RM-сценарий (staged rename + worktree правка), "
        f"получено: {status}"
    )

    plan = commit_plan.build_commit_plan(proj, TODO, baseline_sha="")

    assert "b" not in plan.files, (
        "P4: сиротский токен источника RM-пары не должен парситься как путь — "
        f"в плане фантом: {plan.files}"
    )
    assert "renamed.txt" not in plan.files and "?a b" not in plan.files, (
        "staged rename — WIP index'а пользователя, в план не попадает"
    )
    assert plan.files == ("file.txt", "new.txt"), (
        f"guard не должен выкидывать реальные записи (M/??), got {plan.files}"
    )


def test_legacy_plan_keeps_worktree_deletions(tmp_git_repo: Path):
    """QA-находка (REVIEW-0104): guard P4 слишком широк — выбрасывает D.

    Legacy-множество (docstring ``_all_changed_files``) — «everything
    ``git add -A`` used to stage», включая tracked-файлы, удалённые в
    worktree (запись `` D``: файл в index, отсутствует в worktree,
    deletion не staged). Guard ``.exists()`` отбрасывает такие записи:
    на no-baseline пути удаление молча не попадает в коммит, а каждая
    следующая legacy-план отбрасывает её снова — запись висит грязной
    навсегда. На baseline запись в плане есть; после guard — нет.
    """
    assert commit_plan is not None, "R-03 module missing (pre-R-03 baseline)"
    proj = _project(tmp_git_repo)
    (proj / "victim.txt").write_text("tracked, then deleted\n", encoding="utf-8")
    subprocess.run(["git", "add", "victim.txt"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "add victim"], cwd=proj, check=True)
    (proj / "victim.txt").unlink()
    (proj / "file.txt").write_text("unit work\n", encoding="utf-8")

    status = subprocess.run(
        ["git", "status", "--porcelain", "-z"],
        cwd=proj, capture_output=True, text=True, check=True,
    ).stdout.split("\0")
    assert " D victim.txt" in status, (
        f"тест строит D-сценарий (tracked, удалён в worktree, deletion не staged), "
        f"получено: {status}"
    )

    plan = commit_plan.build_commit_plan(proj, TODO, baseline_sha="")
    assert "victim.txt" in plan.files, (
        "P4 guard: .exists() выкидывает реальную запись D из legacy-плана — "
        f"удаление молча не попадает в коммит: {plan.files}"
    )


def test_suffix_helpers_keep_their_conventions(tmp_path: Path):
    """Дубль консолидирован: поведение обоих конвенций не изменилось.

    _suffixed_name (todos.archive): базовое имя занято победителем —
    результат ВСЕГДА с суффиксом, поиск с первого суффикса.
    _unique_name (api.hygiene): свободное базовое имя возвращается
    как есть, занятое — первый свободный суффикс.
    """
    from awf.api.hygiene import _unique_name
    from awf.todos import _suffixed_name

    d = tmp_path / "d"
    d.mkdir()

    # _suffixed_name: base free OR occupied — всегда суффикс
    assert _suffixed_name(d, "PROGRESS-TODO-0001.md") == "PROGRESS-TODO-0001-1.md"
    (d / "PROGRESS-TODO-0001.md").touch()
    (d / "PROGRESS-TODO-0001-1.md").touch()
    assert _suffixed_name(d, "PROGRESS-TODO-0001.md") == "PROGRESS-TODO-0001-2.md"
    (d / "plain").touch()
    assert _suffixed_name(d, "plain") == "plain-1"

    # _unique_name: свободное base — как есть
    assert _unique_name(d, "free.md") == "free.md"
    (d / "PROGRESS-TODO-0001.md").unlink()
    (d / "PROGRESS-TODO-0001-1.md").unlink()
    assert _unique_name(d, "PROGRESS-TODO-0001.md") == "PROGRESS-TODO-0001.md"
    (d / "PROGRESS-TODO-0001.md").touch()
    assert _unique_name(d, "PROGRESS-TODO-0001.md") == "PROGRESS-TODO-0001-1.md"
