"""TODO-0178: бюджет забега — паузы владельца в downtime, не в продуктивные.

Инцидент (бэклог супервизора, 08.10): забег считал «продуктивными» паузы,
пока владелец думал/отсутствовал (сожгло ~40 мин бюджета за один заход).
Инфраструктура уже была: ``run_brief``/``run_status`` считают
``productive = elapsed − downtime``, но downtime покрывал только
ожидания движка (checkpoint-wait, salvage, net-backoff) — не паузы
владельца.

Инварианты (TODO-0178):
- (a) активный забег, разрыв между отметками 40 мин (>15) →
  downtime +40, productive уменьшился ровно на это;
- (b) разрывы < порога → downtime не растёт;
- (c) вне забега — no-op (файл не создаётся, неактивный забег не
  меняется);
- (d) порог=0 (и отрицательный) → выключено.
Плюс защита от двойного счёта: downtime движка внутри разрыва
(checkpoint/salvage/net-backoff) вычитается из кредита простоя —
тот же интервал не считается дважды.

Hermetic: состояние забега синтезируется (write_run), время — через
параметр ``now`` у ``run_state.supervisor_beat``; реальных ожиданий нет.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from awf import api, run_state

T = "TODO-0001"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="Reports29Budget")
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str = T) -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text("# Task\n", encoding="utf-8")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _set_idle_minutes(proj: Path, value: object) -> None:
    cfg = proj / ".agentic" / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    data.setdefault("automation", {})["owner_idle_minutes"] = value
    cfg.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _active_run(proj: Path, budget_minutes: int = 240) -> None:
    _write_todo(proj)
    api.run_start(proj, queue=[T], budget_minutes=budget_minutes)


def _state(proj: Path) -> dict:
    state = run_state.read_run(proj)
    assert state is not None, "the run state must be readable"
    return state


class TestRunStartInit:
    """Свежий забег начинает учёт простоя с чистого листа."""

    def test_fresh_run_has_beat_fields(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        _active_run(proj)

        state = _state(proj)
        assert run_state._parse_started_at(
            state["last_supervisor_beat"]
        ), "last_supervisor_beat must be a valid ISO timestamp"
        assert state["downtime_seconds_at_last_beat"] == 0


class TestIdleCredit:
    """(a): разрыв > порога — весь разрыв (за вычетом engine-downtime) в
    downtime; productive = elapsed − downtime, как и раньше."""

    def test_gap_above_threshold_credits_whole_gap(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        _active_run(proj)
        now = datetime.now(timezone.utc)
        # владелец был 20 минут, потом 40 минут отсутствовал;
        # забег стартовал 60 минут назад
        run_state.write_run(
            proj,
            started_at=_iso(now - timedelta(minutes=60)),
            last_supervisor_beat=_iso(now - timedelta(minutes=40)),
        )
        before = api.run_status(proj)
        assert before.downtime_minutes == 0
        assert before.productive_minutes == 60

        run_state.supervisor_beat(proj, now=now)

        after = api.run_status(proj)
        assert after.downtime_minutes == 40, (
            f"the 40-minute gap must be credited: {after.downtime_minutes}"
        )
        assert after.productive_minutes == before.productive_minutes - 40, (
            "productive must shrink by exactly the credited gap"
        )
        state = _state(proj)
        assert 40 * 60 <= state["downtime_seconds"] < 40 * 60 + 10
        # снимок обновлён: повторный beat с тем же now не кредитует повторно
        run_state.supervisor_beat(proj, now=now)
        assert _state(proj)["downtime_seconds"] == state["downtime_seconds"]

    def test_gap_below_threshold_does_not_grow(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        _active_run(proj)
        now = datetime.now(timezone.utc)
        run_state.write_run(
            proj,
            started_at=_iso(now - timedelta(minutes=60)),
            last_supervisor_beat=_iso(now - timedelta(minutes=10)),
        )

        run_state.supervisor_beat(proj, now=now)

        after = api.run_status(proj)
        assert after.downtime_minutes == 0, "a 10-min gap (< 15) is noise"
        assert after.productive_minutes == 60
        # отметка при этом обновлена (референс для следующего разрыва)
        assert run_state._parse_started_at(
            _state(proj)["last_supervisor_beat"]
        ) is not None

    def test_engine_downtime_in_gap_is_not_double_counted(self, tmp_git_repo):
        """30 мин checkpoint-wait внутри 40-мин разрыва: engine уже
        учёл 30 — кредит простоя только оставшиеся 10. Итого 40, не 70."""
        proj = _project(tmp_git_repo)
        _active_run(proj)
        now = datetime.now(timezone.utc)
        run_state.write_run(
            proj,
            started_at=_iso(now - timedelta(minutes=60)),
            last_supervisor_beat=_iso(now - timedelta(minutes=40)),
        )
        run_state.add_downtime(proj, 30 * 60, reason="checkpoint-wait")

        run_state.supervisor_beat(proj, now=now)

        after = api.run_status(proj)
        assert after.downtime_minutes == 40, (
            f"30 engine + 10 idle = 40, never 70: {after.downtime_minutes}"
        )

    def test_engine_downtime_covering_the_gap_credits_nothing_more(
        self, tmp_git_repo
    ):
        """Ожидание движка длиннее разрыва — кредит не уходит в минус."""
        proj = _project(tmp_git_repo)
        _active_run(proj)
        now = datetime.now(timezone.utc)
        run_state.write_run(
            proj,
            started_at=_iso(now - timedelta(minutes=60)),
            last_supervisor_beat=_iso(now - timedelta(minutes=40)),
        )
        run_state.add_downtime(proj, 50 * 60, reason="salvage")

        run_state.supervisor_beat(proj, now=now)

        after = api.run_status(proj)
        assert after.downtime_minutes == 50, "no negative idle credit"


class TestNoOp:
    """(c)+(d): вне забега heartbeat ничего не трогает; порог <= 0 —
    учёт выключен (отметка при этом освежается — без «снарядов» при
    повторном включении)."""

    def test_no_run_creates_no_file(self, tmp_git_repo):
        proj = _project(tmp_git_repo)

        run_state.supervisor_beat(proj)

        assert not (proj / ".agentic" / "state" / "run.yaml").exists()

    def test_inactive_run_untouched(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        _active_run(proj)
        run_state.write_run(proj, active=False)
        before = _state(proj)

        run_state.supervisor_beat(proj, now=datetime.now(timezone.utc))

        assert _state(proj) == before, "an inactive run must stay unchanged"

    def test_threshold_zero_disables_credit(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        _active_run(proj)
        _set_idle_minutes(proj, 0)
        now = datetime.now(timezone.utc)
        run_state.write_run(
            proj,
            last_supervisor_beat=_iso(now - timedelta(minutes=40)),
        )

        run_state.supervisor_beat(proj, now=now)

        state = _state(proj)
        assert state.get("downtime_seconds", 0) == 0, "disabled: no credit"
        # референс всё равно освежён — повторное включение не даст
        # 40-минутный «старый» разрыв
        assert run_state._parse_started_at(state["last_supervisor_beat"]) is not None

    def test_negative_threshold_disables_credit(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        _active_run(proj)
        _set_idle_minutes(proj, -5)
        now = datetime.now(timezone.utc)
        run_state.write_run(
            proj,
            last_supervisor_beat=_iso(now - timedelta(minutes=40)),
        )

        run_state.supervisor_beat(proj, now=now)

        assert _state(proj).get("downtime_seconds", 0) == 0


class TestDegradation:
    """Повреждённые/отсутствующие поля — деградация без кредита и без
    исключения (heartbeat сопровождает вызов инструмента и не вправе его
    ломать)."""

    def test_corrupt_last_beat_reinitializes_without_credit(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        _active_run(proj)
        now = datetime.now(timezone.utc)
        run_state.write_run(proj, last_supervisor_beat="not-a-timestamp")

        run_state.supervisor_beat(proj, now=now)

        state = _state(proj)
        assert run_state._parse_started_at(
            state["last_supervisor_beat"]
        ), "the corrupt value must be replaced by a valid one"
        assert state.get("downtime_seconds", 0) == 0

    def test_api_wrapper_never_raises(self, tmp_git_repo):
        result = api.supervisor_beat(Path("/nonexistent/awf/no-such-project"))
        assert result["status"] == "ok"
        # проект без .agentic — тоже no-op без исключений
        result = api.supervisor_beat(tmp_git_repo)
        assert result["status"] == "ok"
