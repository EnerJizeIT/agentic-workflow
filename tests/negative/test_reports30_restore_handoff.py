"""REPORTS30 (TODO-0191): handoff в restore-цикле — хвост архива не путает новую попытку.

До: ``restore_todo`` переносил архивный handoff-хвост (``done/<id>/handoff/*``)
в живой ``.agentic/handoff/`` голым ``shutil.move``. Повторный прогон
восстановленного юнита стартует со стадии 0, а до завершения владелицы
файлы *прошлой* попытки лежали в живом каталоге под каноническими именами
``{stage}-{todo_id}.md``:

- возобновление с поздней стадии (``--from-stage`` / ``stage_name`` из
  state) передавало handoff прошлой попытки следующему воркеру как свежий
  (``resolve_prev_handoffs``);
- salvage-промпт указывал на файл прошлой попытки для стадии, которая
  умерла, не написав свой;
- голый move тихо перезаписывал живую копию того же имени (у правила
  A-12 «архив никогда не перезаписывает» не было двойника на стороне
  restore), а при повторной архивации улика первой попытки терялась
  вместо версионирования рядом с новым отчётом (кандидат P3 из QA
  TODO-0079, «restore-цикл теряет старый handoff»).

Закреплённые инварианты:
- test_restore_keeps_handoff_tail_in_archive — restore возвращает юнит в
  работу, но handoff-хвост остаётся в ``done/<id>/handoff/`` (аудит), а в
  живой каталог не попадает.
- test_restore_never_overwrites_live_handoff — restore не перезаписывает
  ни живого файла, ни архивного.
- test_rerun_does_not_forward_archive_tail — сразу после restore (до
  любой стадии новой попытки) ``resolve_prev_handoffs`` для следующей
  стадии не возвращает существующего файла: следующий воркер видит ровно
  свои хендоффы — пока их нет.
- test_rearchive_after_restore_versions_both_attempts — повторный прогон →
  повторная архивация: новый handoff занимает каноническое имя, хвост
  первой попытки цел под уникальным суффиксом — обе улики сохранены.
"""
from __future__ import annotations

from pathlib import Path

from awf import api, paths
from awf.agent_stage import resolve_prev_handoffs
from awf.pipeline import Stage
from awf.todos import archive_todo

TODO_ID = "TODO-0191"
STAGE = "implementer"
STALE = b"# handoff of the FIRST attempt - audit evidence\n"
FRESH = b"# handoff of the second attempt\n"
LIVE = b"# live handoff written by a concurrent cycle - do not lose\n"


def _seed_archived_unit(proj: Path, handoff: bytes | None = STALE) -> None:
    """done/<id>/ with TODO.md and (optionally) one archived handoff."""
    done_unit = paths.done_dir(proj) / TODO_ID
    (done_unit / "handoff").mkdir(parents=True, exist_ok=True)
    (done_unit / "TODO.md").write_bytes(b"# task body\n")
    if handoff is not None:
        (done_unit / "handoff" / f"{STAGE}-{TODO_ID}.md").write_bytes(handoff)


def _stages_up_to_qa() -> list[Stage]:
    """plan (supervisor) -> implementer -> qa: the qa stage reads the
    implementer's handoff, exactly like the engine does."""
    return [
        Stage(name="plan", role="supervisor"),
        Stage(name=STAGE, role="agent-implementer"),
        Stage(name="qa", role="agent-qa-review"),
    ]


def test_restore_keeps_handoff_tail_in_archive(tmp_git_repo):
    """Новая попытка не получает хвост прошлой под видом своего."""
    api.init_project(tmp_git_repo, project_name="RHKept")
    _seed_archived_unit(tmp_git_repo)
    live_handoff = paths.handoff_dir(tmp_git_repo) / f"{STAGE}-{TODO_ID}.md"
    archive_handoff = (
        paths.done_dir(tmp_git_repo) / TODO_ID / "handoff" / f"{STAGE}-{TODO_ID}.md"
    )

    result = api.restore_todo(tmp_git_repo, TODO_ID)

    # the unit is active again
    assert (paths.inbox(tmp_git_repo) / f"{TODO_ID}.md").is_file()
    assert (paths.inbox(tmp_git_repo) / f"{TODO_ID}.ready").is_file()
    # the new attempt must not see the previous attempt's handoff as fresh
    assert not live_handoff.exists(), (
        "restore must not put the archived tail into the live handoff dir"
    )
    # the archive tail is not lost — it stays the audit evidence
    assert archive_handoff.read_bytes() == STALE
    # the answer says where the history stays
    assert f"kept in done/{TODO_ID}/handoff" in result.message, result.message


def test_restore_never_overwrites_live_handoff(tmp_git_repo):
    """Живой файл с тем же именем переживает restore побайтово."""
    api.init_project(tmp_git_repo, project_name="RHNoclobber")
    _seed_archived_unit(tmp_git_repo)
    live_dir = paths.handoff_dir(tmp_git_repo)
    live_dir.mkdir(parents=True, exist_ok=True)
    live_handoff = live_dir / f"{STAGE}-{TODO_ID}.md"
    live_handoff.write_bytes(LIVE)
    archive_handoff = (
        paths.done_dir(tmp_git_repo) / TODO_ID / "handoff" / f"{STAGE}-{TODO_ID}.md"
    )

    api.restore_todo(tmp_git_repo, TODO_ID)

    assert live_handoff.read_bytes() == LIVE, (
        "restore must never overwrite a live handoff file"
    )
    assert archive_handoff.read_bytes() == STALE


def test_rerun_does_not_forward_archive_tail(tmp_git_repo):
    """Следующая стадия до прогона своих стадий не видит чужих хендоффов."""
    api.init_project(tmp_git_repo, project_name="RHForward")
    _seed_archived_unit(tmp_git_repo)

    api.restore_todo(tmp_git_repo, TODO_ID)

    # what the qa stage would be forwarded, engine-style (is_file filter
    # from run_agent_stage) — before any stage of the new attempt ran
    forwarded = [
        p
        for p in resolve_prev_handoffs(_stages_up_to_qa(), 2, tmp_git_repo, TODO_ID)
        if p.is_file()
    ]
    assert forwarded == [], (
        f"the previous attempt's tail must not be forwarded as fresh: {forwarded}"
    )


def test_rearchive_after_restore_versions_both_attempts(tmp_git_repo):
    """Цикл restore → повторный прогон → архивация: обе улики живы."""
    api.init_project(tmp_git_repo, project_name="RHVersion")
    _seed_archived_unit(tmp_git_repo)

    api.restore_todo(tmp_git_repo, TODO_ID)
    # the re-run's implementer stage writes its own handoff
    live_dir = paths.handoff_dir(tmp_git_repo)
    live_dir.mkdir(parents=True, exist_ok=True)
    (live_dir / f"{STAGE}-{TODO_ID}.md").write_bytes(FRESH)

    result = archive_todo(tmp_git_repo, TODO_ID)

    assert result is not None
    done_handoff = paths.done_dir(tmp_git_repo) / TODO_ID / "handoff"
    # the second attempt takes the canonical name
    assert (done_handoff / f"{STAGE}-{TODO_ID}.md").read_bytes() == FRESH
    # the first attempt's tail survives under a unique suffix — not lost
    suffixed = sorted(
        p for p in done_handoff.iterdir() if p.name != f"{STAGE}-{TODO_ID}.md"
    )
    assert len(suffixed) == 1, f"expected exactly one suffixed copy, got {suffixed}"
    assert suffixed[0].read_bytes() == STALE
    # the live dir is clean — the re-run produced exactly its own
    assert not (live_dir / f"{STAGE}-{TODO_ID}.md").exists()
