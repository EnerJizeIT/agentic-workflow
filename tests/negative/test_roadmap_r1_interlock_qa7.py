"""QA-находка F-A к TODO-0218 (R1.4), роль agent-implementer (iter 17).

F-A (MEDIUM, контракт CLI-флага): ``awf init --force --preview`` на
свежем проекте (без ``.agentic/``) МОЛЧА игнорировал ``--preview`` —
preview-ветка требовала существующий ``.agentic/``, иначе управление
уходило в обычный init и ПИСАЛО скелет. ``awf reset --preview`` и
``awf rollback --preview`` флаг чтут — адаптеры расходились.

Требование: ``--preview`` терминален безусловно. На существующем
проекте — план разрушения; на свежем — «нечего разрушать» (exit 0), и
ничего не записано. ``--preview`` без ``--force`` у ``init`` —
осмысленная ошибка (без force init недеструктивен), не молчаливый
прогон.
"""
from __future__ import annotations

from awf import cli


class TestInitForcePreviewTerminal:
    def test_fresh_project_force_preview_writes_nothing(self, tmp_git_repo, capsys):
        proj = tmp_git_repo
        assert not (proj / ".agentic").exists()

        rc = cli.main(["init", "--force", "--preview", "--project-dir", str(proj)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert not (proj / ".agentic").exists(), (
            "init --force --preview на свежем проекте записал скелет — "
            "флаг --preview молча проигнорирован (F-A)"
        )
        assert "nothing" in out.lower() or "нечего" in out.lower(), out

    def test_existing_project_force_preview_prints_plan_and_deletes_nothing(
        self, tmp_git_repo, capsys
    ):
        from awf import api

        proj = tmp_git_repo
        api.init_project(proj, project_name="PreviewQa7")
        victim = proj / ".agentic" / "inbox" / "KEEP-9001.md"
        victim.write_text("# keep\n", encoding="utf-8")

        rc = cli.main(["init", "--force", "--preview", "--project-dir", str(proj)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert "preview" in out.lower(), out
        assert victim.exists(), "init --force --preview не должен ничего удалять"

    def test_preview_without_force_is_refused(self, tmp_git_repo, capsys):
        proj = tmp_git_repo
        api_agentic = proj / ".agentic"
        assert not api_agentic.exists()

        rc = cli.main(["init", "--preview", "--project-dir", str(proj)])

        out = capsys.readouterr().out
        assert rc != 0, out
        assert "preview" in out.lower() and "force" in out.lower(), (
            "init --preview без --force должен явно отказать, назвав "
            f"причину; вывод: {out!r}"
        )
        assert not api_agentic.exists(), (
            "init --preview без --force записал скелет вместо явного отказа"
        )
