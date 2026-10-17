"""QA-находка F-A2 к TODO-0218 (R1.4), роль agent-qa-review (iter 18).

Класс F-A (REVIEW-фикс 13): «``--preview`` терминален безусловно».
``awf init`` и ``awf reset`` флаг чтут в любом сочетании (preview-ветка
стоит до dry-run/orphans), а ``awf rollback --preview --dry-run`` его
МОЛЧА игнорирует: preview-ветка требует ``mode in ("hard", "soft")``, а
``--dry-run`` переводит ``mode`` в ``"dry-run"`` ещё до этой проверки.
Итог: пользователь просит план разрушения, а получает обычный dry-run —
и, если baseline ещё нет, вообще ERROR вместо плана.

Требование 6 (F-R2): «CLI/MCP получают preview со scope, expected revision
и digest». Значит при ``--preview`` CLI обязан напечатать план и ничего не
исполнять — независимо от ``--dry-run`` (та же терминальность, что у init
и reset, где preview побеждает dry-run).
"""
from __future__ import annotations

from awf import api, cli


class TestRollbackPreviewTerminal:
    def test_rollback_preview_with_dry_run_prints_plan(self, tmp_git_repo, capsys):
        """F-A2: ``--preview`` обязан победить ``--dry-run``, как у init/reset."""
        proj = tmp_git_repo
        api.init_project(proj, project_name="RBPreviewQa8")

        rc = cli.main(
            [
                "rollback", "TODO-0001", "--preview", "--dry-run",
                "--project-dir", str(proj),
            ]
        )

        out = capsys.readouterr().out
        assert rc == 0, out
        assert "preview" in out.lower(), (
            "rollback --preview --dry-run проигнорировал --preview (F-A2): "
            f"план разрушения не напечатан; вывод: {out!r}"
        )
        assert "confirm digest" in out.lower(), out

    def test_rollback_preview_hard_prints_plan(self, tmp_git_repo, capsys):
        """Регресс-страховка: рабочий путь ``--preview --hard`` не сломать."""
        proj = tmp_git_repo
        api.init_project(proj, project_name="RBPreviewQa8")

        rc = cli.main(
            [
                "rollback", "TODO-0001", "--preview", "--hard",
                "--project-dir", str(proj),
            ]
        )

        out = capsys.readouterr().out
        assert rc == 0, out
        assert "preview" in out.lower(), out
