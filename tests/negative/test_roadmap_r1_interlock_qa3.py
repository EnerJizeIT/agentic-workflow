"""QA-находки к TODO-0218 (R1.4), iter-12 (роль agent-qa-review).

Каждый тест КРАСНЫЙ на текущей реализации и доказывает поведенческий
разрыв в границах юнита (требование 6 и требование 4/класс F-B).
Прод-код не трогается — только тесты.

Находка 1 (требование 6): «адаптер фиксирует источник решения». Модуль
``awf/api/_destructive.py`` объявляет ``execute_destruction(decided_by=...)``
и в своём docstring говорит: «Адаптер, требующий решения человека, идёт
через execute_destruction — он фиксирует источник решения». Но CLI-адаптеры
(``cmd_reset``/``cmd_rollback``/``cmd_init``, как раз изменённые в этом юните)
зовут ``api.reset_runtime`` / ``api.rollback`` / ``api.init_project``
НАПРЯМУЮ с ``confirm_digest`` и НЕ проходят через ``execute_destruction``.
Итог: строка ``[destructive] … decided_by=…`` не пишется ни на одном
прод-пути, а ``execute_destruction`` не имеет ни одного прод-вызова
(используется только тестами).

Находка 2 (требование 4, класс уже принятого блокера F-B): force-init
сносит ``.agentic/state/.lock`` — файл RMW-блокировки Store/run_state
(``awf/_lock.py``). ``_clean_state_dir`` бережёт ``.interlock`` (фикс F-B),
но не ``.lock``: тот удаляется вместе со всем содержимым ``state``. Пока
конкурентный writer держит flock на старом inode, force-init создаёт проект
заново — его блокировка осиротела (новый writer откроет НОВЫЙ inode и
пройдёт мимо). reset этот файл не трогает (он вообще не чистит содержимое
``state``), поэтому расхождение — только у force-init, ровно класс F-B.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from awf import api

TID = "TODO-0001"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="QaFindings3")
    return tmp_git_repo


def _read_all_logs(proj: Path) -> str:
    logs = proj / ".agentic" / "logs"
    if not logs.is_dir():
        return ""
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in logs.rglob("*")
        if p.is_file()
    )


class TestCliDestructiveRecordsDecisionSource:
    """Требование 6: адаптер (CLI) фиксирует источник решения при разрушении."""

    def test_cli_reset_with_digest_journals_decision_source(self, tmp_git_repo):
        from awf import cmd_reset

        proj = _project(tmp_git_repo)
        victim = proj / ".agentic" / "inbox" / "ORPHAN-9001.md"
        victim.write_text("# orphan\n", encoding="utf-8")

        plan = api.preview_destruction(proj, "reset")
        args = SimpleNamespace(
            tasks_only=False,
            full=False,
            orphans=False,
            force=True,
            preview=False,
            confirm_digest=plan.digest,
            project_dir=str(proj),
        )
        rc = cmd_reset.run(args)
        assert rc == 0

        logs = _read_all_logs(proj)
        assert "[destructive]" in logs, (
            "требование 6: CLI-адаптер обязан зафиксировать источник решения "
            "(строка [destructive] … decided_by=…); сейчас confirm_digest "
            "уходит в api.reset_runtime напрямую, минуя execute_destruction."
        )
        assert "decided_by" in logs, logs

    def test_cli_rollback_with_digest_journals_decision_source(self, tmp_git_repo):
        from awf import cmd_rollback

        proj = _project(tmp_git_repo)
        inbox = proj / ".agentic" / "inbox"
        (inbox / f"{TID}.md").write_text("# T\n", encoding="utf-8")
        api.create_baseline(proj, TID)

        plan = api.preview_destruction(proj, "rollback", mode="hard", todo_id=TID)
        args = SimpleNamespace(
            todo_id=TID,
            hard=True,
            soft=False,
            dry_run=False,
            preview=False,
            confirm_digest=plan.digest,
            project_dir=str(proj),
        )
        rc = cmd_rollback.run(args)
        assert rc == 0

        logs = _read_all_logs(proj)
        assert "[destructive]" in logs, (
            "требование 6: rollback-адаптер обязан зафиксировать источник "
            "решения; сейчас confirm_digest уходит в api.rollback напрямую."
        )


class TestForceInitPreservesStateRmwLock:
    """Требование 4 / класс F-B: force-init не должен осиротить `.lock`."""

    def test_force_init_keeps_state_lock_inode(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        lock = proj / ".agentic" / "state" / ".lock"
        lock.write_text("", encoding="utf-8")
        before = lock.stat().st_ino

        api.init_project(proj, force=True)

        assert lock.exists(), (
            "force-init удалил .agentic/state/.lock — файл RMW-блокировки "
            "Store/run_state. Конкурентный writer на старом inode осиротел "
            "(класс F-B; reset этот файл бережёт, force-init — нет)."
        )
        assert lock.stat().st_ino == before, (
            "force-init заменил inode блокировки — новый writer откроет НОВЫЙ "
            "файл и пройдёт мимо осиротевшей блокировки конкурента."
        )
