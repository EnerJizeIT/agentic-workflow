"""TODO-0262: the unit's review-pass budget + the run economics.

A unit's ``## REVIEW-фикс`` sections are the only source of the pass count
(the supervisor appends them; a silent retry never does). The engine refuses
to enter verify — and ``awf continue`` refuses to enter the implementer
stage — once the count reaches ``limits.max_review_passes``: the unit goes
to the owner with a BUDGET note instead of another polish round.

Cases:
(a) 2 review sections with max=3 — the unit passes the gate;
(b) 3 sections with max=3 — BUDGET note, verify entry stops, continue
    does not start;
(c) max=0 — no limit;
(d) the RUN-REPORT carries the unit economics line;
(e) status/brief show ``passes: N/max`` and ``budget: exhausted``;
(f) a silent retry (a repeated stage, no review headings) does not grow
    the pass count.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf import api, budget
from awf.budget import BudgetConfigError

TODO = "TODO-0001"


def _project(tmp_path: Path, max_passes: int | str | None = 3) -> Path:
    proj = tmp_path / "repo"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=proj, check=True)
    (proj / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=proj, check=True)
    api.init_project(proj, project_name="Budget", test_cmd="/bin/true")
    if max_passes is not None:
        _set_limit(proj, max_passes)
    return proj


def _set_limit(proj: Path, value: int | str) -> None:
    config = proj / ".agentic" / "config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + f"\nlimits:\n  max_review_passes: {value}\n",
        encoding="utf-8",
    )


def _review_sections(count: int) -> str:
    return "".join(
        f"\n## REVIEW-фикс (супервизор, виток {i})\n\n- пункт {i}\n"
        for i in range(1, count + 1)
    )


def _activate(proj: Path, body: str = "# Task\n", todo_id: str = TODO) -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")
    (inbox / f"{todo_id}.ready").touch()


def _log(proj: Path, lines: list[str]) -> Path:
    logs = proj / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / "orchestrator.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


class TestReviewPassCount:
    def test_two_sections_pass_the_gate(self, tmp_path: Path):
        proj = _project(tmp_path)
        _activate(proj, "# Task\n" + _review_sections(2))
        cfg = {"limits": {"max_review_passes": 3}}

        assert budget.review_passes(proj, TODO) == 2
        assert budget.is_exhausted(proj, TODO, cfg) is False
        assert budget.check_and_note(proj, TODO, cfg) == ""
        assert not (proj / ".agentic" / "outbox" / f"BUDGET-{TODO}.md").exists()

    def test_silent_retry_does_not_grow_the_count(self, tmp_path: Path):
        proj = _project(tmp_path)
        _activate(proj, "# Task\n")
        _log(
            proj,
            [
                "[2026-10-26T10:00:00Z] Pipeline snapshot captured: "
                f"PIPELINE-{TODO}.yaml (source default.yaml)",
                "[2026-10-26T10:00:00Z] Stage 0: plan (supervisor :: plan)",
                "[2026-10-26T10:01:00Z] Stage 1: agent-implementer "
                "(agent-implementer :: execute)",
                "[2026-10-26T10:02:00Z] salvage path: stage 1 retried",
                "[2026-10-26T10:03:00Z] Stage 1: agent-implementer "
                "(agent-implementer :: execute)",
                "[2026-10-26T10:04:00Z] Pipeline complete",
            ],
        )
        cfg = {"limits": {"max_review_passes": 3}}

        assert budget.review_passes(proj, TODO) == 0
        assert budget.is_exhausted(proj, TODO, cfg) is False

    def test_legacy_heading_counts(self, tmp_path: Path):
        proj = _project(tmp_path)
        _activate(
            proj,
            "# Task\n\n## REVIEW-фикс 2 (старая запись)\n\n- пункт A\n"
            "\n## REVIEW-фикс (супервизор, виток 3)\n\n- пункт B\n",
        )

        assert budget.review_passes(proj, TODO) == 2

    def test_zero_disables_the_limit(self, tmp_path: Path):
        proj = _project(tmp_path, max_passes=0)
        _activate(proj, "# Task\n" + _review_sections(5))
        cfg = {"limits": {"max_review_passes": 0}}

        assert budget.review_passes(proj, TODO) == 5
        assert budget.is_exhausted(proj, TODO, cfg) is False
        assert budget.check_and_note(proj, TODO, cfg) == ""


class TestConfigRange:
    @pytest.mark.parametrize("value", [-1, 21, 2.5, "many", True])
    def test_out_of_range_is_a_config_refusal(self, value):
        with pytest.raises(BudgetConfigError):
            budget.max_review_passes({"limits": {"max_review_passes": value}})

    def test_missing_key_uses_the_default(self):
        assert budget.max_review_passes({}) == 3


class TestVerifyEntryStop:
    def test_third_section_stops_and_writes_the_budget_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import awf.pipeline_engine as engine
        from awf.pipeline import Stage

        proj = _project(tmp_path)
        _activate(proj, "# Task\n" + _review_sections(3))
        outbox = proj / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / f"BLOCKED-{TODO}.md").write_text(
            "## ВЕРДИКТ\n\nblocked\n\n## НАХОДКИ\n\n- открытый пункт A\n",
            encoding="utf-8",
        )
        ran: list[bool] = []
        monkeypatch.setattr(
            engine, "_run_supervisor_stage", lambda *a, **kw: ran.append(True)
        )
        monkeypatch.setattr(engine, "_maybe_run_verify_pack", lambda *a, **kw: None)

        _, _, rc = engine.execute_supervisor_stage(
            Stage(name="verify", role="supervisor", kind="verify"),
            TODO,
            auto=True,
            project_dir=proj,
            config={"limits": {"max_review_passes": 3}},
            logs_dir=proj / ".agentic" / "logs",
            pipeline_name=None,
        )

        assert rc == 1, "the verify stage must stop the pipeline"
        assert not ran, "the supervisor must not be asked to verify"
        note = (outbox / f"BUDGET-{TODO}.md").read_text(encoding="utf-8")
        assert "passes: 3/3" in note
        assert "открытый пункт A" in note
        assert "виток 3" in note


class TestContinueRefuses:
    def test_continue_from_implementer_does_not_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import awf.api.pipeline as api_pipeline

        proj = _project(tmp_path)
        _activate(proj, "# Task\n" + _review_sections(3))

        def _must_not_start(*_args, **_kwargs):
            pytest.fail("awf continue started a budget-exhausted unit")

        monkeypatch.setattr(api_pipeline, "start_pipeline", _must_not_start)

        result = api_pipeline.continue_pipeline(
            proj, from_stage="implementer", background=False
        )

        assert result.run_mode == "noop"
        assert "budget" in result.message.lower()
        note = proj / ".agentic" / "outbox" / f"BUDGET-{TODO}.md"
        assert note.is_file()


class TestRunNextStops:
    def test_run_next_returns_stopped_budget_exhausted(self, tmp_path: Path):
        proj = _project(tmp_path)
        _activate(proj, "# Task\n" + _review_sections(3))
        api.run_start(proj, queue=[TODO])

        result = api.run_next(proj)

        assert result.action == "stopped"
        assert "budget" in result.message.lower()
        assert result.report_file
        note = proj / ".agentic" / "outbox" / f"BUDGET-{TODO}.md"
        assert "passes: 3/3" in note.read_text(encoding="utf-8")


class TestRunReportEconomics:
    def test_report_carries_the_unit_line(self, tmp_path: Path):
        from awf.api import run as run_mod

        proj = _project(tmp_path)
        _activate(proj, "# Task\n" + _review_sections(1))
        _log(
            proj,
            [
                "[2026-10-26T10:00:00Z] Pipeline snapshot captured: "
                f"PIPELINE-{TODO}.yaml (source default.yaml)",
                "[2026-10-26T10:00:00Z] Stage 0: plan (supervisor :: plan)",
                "[2026-10-26T10:01:00Z] Stage 1: agent-implementer "
                "(agent-implementer :: execute)",
                "[2026-10-26T10:03:00Z] Pipeline complete",
            ],
        )
        state = {
            "active": True,
            "queue": [TODO],
            "index": 1,
            "current": TODO,
            "completed": [],
            "rejects": {},
            "started_at": "2026-10-26T10:00:00Z",
            "budget_minutes": 0,
        }

        report = run_mod._write_report(proj, state, "test stop")
        assert report is not None
        text = report.read_text(encoding="utf-8")
        assert "passes: 1" in text
        assert "stages: plan 1/impl 2/" in text
        assert "gates:" in text


class TestStatusSurface:
    def test_status_and_brief_show_passes_and_exhaustion(self, tmp_path: Path):
        proj = _project(tmp_path)
        _activate(proj, "# Task\n" + _review_sections(2))

        entry = api.get_status(proj).active_todos[0]
        assert entry["review_passes"] == 2
        assert entry["review_passes_max"] == 3
        assert entry["budget_exhausted"] is False
        assert "passes: 2/3" in api.brief(proj).text

        _activate(proj, "# Task\n" + _review_sections(3))
        entry = api.get_status(proj).active_todos[0]
        assert entry["budget_exhausted"] is True
        assert "budget: exhausted" in api.brief(proj).text

    def test_zero_limit_still_shows_the_pass_count(self, tmp_path: Path):
        """A3: max_review_passes=0 disables the cap, it does not hide the line."""
        proj = _project(tmp_path, max_passes=0)
        _activate(proj, "# Task\n" + _review_sections(2))

        entry = api.get_status(proj).active_todos[0]

        assert entry["review_passes"] == 2
        assert entry["review_passes_max"] == 0
        assert entry["budget_exhausted"] is False
        assert "passes: 2 (без лимита)" in api.brief(proj).text


class TestContinueStateStage:
    def test_bare_continue_refuses_the_state_recorded_implementer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import awf.api.pipeline as api_pipeline
        from awf import pipeline_state

        proj = _project(tmp_path)
        _activate(proj, "# Task\n" + _review_sections(3))
        pipeline_state.write_state(proj, todo_id=TODO, stage_name="agent-implementer")

        def _must_not_start(*_args, **_kwargs):
            pytest.fail("a bare awf continue started a budget-exhausted unit")

        monkeypatch.setattr(api_pipeline, "start_pipeline", _must_not_start)

        result = api_pipeline.continue_pipeline(proj, background=False)

        assert result.run_mode == "noop"
        assert "budget" in result.message.lower()
        note = proj / ".agentic" / "outbox" / f"BUDGET-{TODO}.md"
        assert note.is_file()


class TestStartRefuses:
    def test_start_on_an_exhausted_unit_creates_no_lease_or_stage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A1: the launch gate runs BEFORE the lease and the first stage.

        Without it ``awf start`` paid a full plan/implementer/arch/qa/audit
        round and only then stopped at verify.
        """
        import awf.api.pipeline as api_pipeline
        from awf import pipeline_state

        proj = _project(tmp_path)
        _activate(proj, "# Task\n" + _review_sections(3))

        def _must_not_run(*_args, **_kwargs):
            pytest.fail("awf start launched an exhausted unit")

        monkeypatch.setattr(api_pipeline, "start_in_background", _must_not_run)
        monkeypatch.setattr(
            "awf.orchestrator.run_pipeline", _must_not_run
        )

        result = api_pipeline.start_pipeline(proj, background=False)

        assert result.run_mode == "noop"
        assert "budget" in result.message.lower()
        note = proj / ".agentic" / "outbox" / f"BUDGET-{TODO}.md"
        assert note.is_file()
        assert not (proj / ".agentic" / "logs" / "awf-launch.lease").exists(), (
            "the refused start still took the launch lease"
        )
        assert pipeline_state.read_state(proj) is None, (
            "the refused start wrote a stage state"
        )


def _run_verify(proj: Path, config: dict, monkeypatch: pytest.MonkeyPatch):
    import awf.pipeline_engine as engine
    from awf.pipeline import Stage

    ran: list[bool] = []
    monkeypatch.setattr(
        engine, "_run_supervisor_stage", lambda *a, **kw: ran.append(True)
    )
    monkeypatch.setattr(engine, "_maybe_run_verify_pack", lambda *a, **kw: None)
    _, _, rc = engine.execute_supervisor_stage(
        Stage(name="verify", role="supervisor", kind="verify"),
        TODO,
        auto=True,
        project_dir=proj,
        config=config,
        logs_dir=proj / ".agentic" / "logs",
        pipeline_name=None,
    )
    return ran, rc


class TestVerifyEntryLimits:
    def test_zero_limit_lets_the_supervisor_verify(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        proj = _project(tmp_path, max_passes=0)
        _activate(proj, "# Task\n" + _review_sections(5))

        ran, _ = _run_verify(proj, {"limits": {"max_review_passes": 0}}, monkeypatch)

        assert ran == [True], "max=0 must not stop the verify entry"
        assert not (proj / ".agentic" / "outbox" / f"BUDGET-{TODO}.md").exists()

    def test_invalid_limit_refuses_before_the_supervisor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        proj = _project(tmp_path)
        _activate(proj, "# Task\n" + _review_sections(1))

        ran, rc = _run_verify(proj, {"limits": {"max_review_passes": 99}}, monkeypatch)

        assert rc == 1, "an unreadable limit must stop the pipeline"
        assert not ran, "the supervisor must not run under a broken limit"
        assert not (proj / ".agentic" / "outbox" / f"BUDGET-{TODO}.md").exists()



class TestBudgetNoteStageTimes:
    def test_note_reads_stage_minutes_from_the_log(self, tmp_path: Path):
        proj = _project(tmp_path)
        _activate(proj, "# Task\n" + _review_sections(3))
        _log(
            proj,
            [
                "[2026-10-26T10:00:00Z] Pipeline snapshot captured: "
                f"PIPELINE-{TODO}.yaml (source default.yaml)",
                "[2026-10-26T10:00:00Z] Stage 0: plan (supervisor :: plan)",
                "[2026-10-26T10:02:00Z] Stage 1: agent-implementer "
                "(agent-implementer :: execute)",
                "[2026-10-26T10:04:00Z] Pipeline complete",
            ],
        )

        note = budget.budget_note_text(proj, TODO, passes=3, cap=3)

        assert "- impl: 2 мин" in note
        assert "- qa: n/a" in note


class TestEconomicsNoData:
    def test_missing_gates_report_reads_na(self, tmp_path: Path):
        proj = _project(tmp_path)
        _activate(proj, "# Task\n")

        line = budget.economics_line(proj, TODO)

        assert "wall: n/a" in line
        assert "stages: n/a" in line
        assert "gates: n/a" in line

    def test_gate_failures_are_reported(self, tmp_path: Path):
        proj = _project(tmp_path)
        _activate(proj, "# Task\n")
        context = proj / ".agentic" / "context"
        context.mkdir(parents=True, exist_ok=True)
        (context / f"GATES-{TODO}.md").write_text(
            "# GATES\n\n```json\n"
            '{"sections": {"qa_findings": "fail", "contract_tests": "pass"}}\n'
            "```\n",
            encoding="utf-8",
        )

        line = budget.economics_line(proj, TODO)

        assert "qa_fail 1 раз, contract_fail 0" in line


# ─── REVIEW-фикс CTX1–CTX4 (аудит 0262) ─────────────────────────────────


def _budget_stop(proj: Path) -> None:
    """Record a real budget stop for the unit (the durable BUDGET note)."""
    _activate(proj, "# Task\n" + _review_sections(3))
    reason = budget.check_and_note(
        proj, TODO, {"limits": {"max_review_passes": 3}}
    )
    assert reason


class TestBudgetActionableSurfaces:
    """CTX1: an exhausted unit is an owner decision, never ``awf_continue``."""

    def _lingering_stage(self, proj: Path) -> None:
        from awf import pipeline_state

        pipeline_state.write_state(
            proj,
            stage_idx=1,
            stage_name="verify",
            stage_kind="verify",
            todo_id=TODO,
            # Outside the OS pid range — the engine that stopped is dead.
            pipeline_pid=2**31,
        )

    def test_status_advice_is_not_continue(self, tmp_path: Path):
        from awf.brief import next_action_from_status

        proj = _project(tmp_path)
        _budget_stop(proj)
        self._lingering_stage(proj)

        status = api.get_status(proj)

        assert status.budget_exhausted_todo == TODO
        advice = next_action_from_status(status)
        assert "awf_continue" not in advice
        assert "max_review_passes" in advice

    def test_wait_event_returns_the_budget_event(self, tmp_path: Path):
        proj = _project(tmp_path)
        _budget_stop(proj)
        self._lingering_stage(proj)

        result = api.wait_for_event(proj, timeout=1, poll_interval=0.1)

        assert result.event_type == "budget"
        assert "awf_continue" not in result.next_action
        assert "max_review_passes" in result.message

    def test_raised_limit_retires_the_stop(self, tmp_path: Path):
        proj = _project(tmp_path)
        _budget_stop(proj)
        self._lingering_stage(proj)
        # The owner raised the cap above the unit's 3 rounds: the recorded
        # stop no longer holds — the surfaces must not keep saying exhausted.
        _set_limit(proj, 5)

        status = api.get_status(proj)

        assert status.budget_exhausted_todo is None


class TestSuperviseStops:
    """CTX2: the watchdog must not spin on a unit that cannot resume."""

    def _dead_run(self, proj: Path) -> None:
        from awf import pipeline_state, run_state

        run_state.write_run(
            proj,
            active=True,
            queue=[{"todo_id": TODO, "pipeline": ""}],
            index=0,
            current=TODO,
            generation=1,
        )
        pipeline_state.write_state(
            proj,
            stage_name="agent-implementer",
            stage_kind="execute",
            todo_id=TODO,
            pipeline_pid=2**31,
        )

    def test_watchdog_stops_on_the_budget_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from awf.api import pipeline, supervise

        proj = _project(tmp_path)
        _budget_stop(proj)
        self._dead_run(proj)

        calls: list[dict] = []
        monkeypatch.setattr(
            pipeline,
            "continue_pipeline",
            lambda *a, **k: calls.append(k) or {"run_mode": "background"},
        )

        result = supervise.supervise_run(
            proj, iterations=5, pause_seconds=0, sleep=lambda _s: None
        )

        assert result["action"] == "budget"
        assert not calls, "the watchdog resumed a budget-exhausted unit"

    def test_watchdog_stops_after_consecutive_noops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from awf.api import pipeline, supervise

        proj = _project(tmp_path)
        _activate(proj, "# Task\n")
        self._dead_run(proj)
        monkeypatch.setattr(
            pipeline, "continue_pipeline", lambda *a, **k: {"run_mode": "noop"}
        )

        result = supervise.supervise_run(
            proj, iterations=20, pause_seconds=0, sleep=lambda _s: None
        )

        assert result["action"] == "stopped"
        assert result["restarts"] == 0
        assert "no-op" in result["stop_reason"]


class TestBudgetNoteArchive:
    """CTX3: the note belongs to the archived unit, not to the outbox."""

    def test_archive_moves_the_budget_note(self, tmp_path: Path):
        from awf import todos

        proj = _project(tmp_path)
        _budget_stop(proj)
        outbox_note = proj / ".agentic" / "outbox" / f"BUDGET-{TODO}.md"
        assert outbox_note.is_file()

        dest = todos.archive_todo(proj, TODO)

        assert dest is not None
        assert (dest / f"BUDGET-{TODO}.md").is_file()
        assert not outbox_note.exists()


class TestRunReportFocus:
    """CTX4: the unit a run stopped on gets its economics line."""

    def test_budget_stop_report_names_the_unit(self, tmp_path: Path):
        proj = _project(tmp_path)
        _activate(proj, "# Task\n" + _review_sections(3))
        api.run_start(proj, queue=[TODO])

        result = api.run_next(proj)

        assert result.action == "stopped"
        assert result.report_file
        text = Path(result.report_file).read_text(encoding="utf-8")
        assert f"- {TODO}: passes: 3" in text


class TestInvalidLimitRefusals:
    """A limit outside 0..20 is a loud refusal on every launch surface.

    The TODO's acceptance names the range refusal explicitly; the module-level
    ``max_review_passes`` test proves the validator, these prove the three
    entry points never launch a stage under a broken limit.
    """

    def test_start_refuses_before_the_lease(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import awf.api.pipeline as api_pipeline
        from awf import pipeline_state

        proj = _project(tmp_path, max_passes=99)
        _activate(proj, "# Task\n")

        def _must_not_run(*_args, **_kwargs):
            pytest.fail("awf start launched under a broken limit")

        monkeypatch.setattr(api_pipeline, "start_in_background", _must_not_run)
        monkeypatch.setattr("awf.orchestrator.run_pipeline", _must_not_run)

        result = api_pipeline.start_pipeline(proj, background=False)

        assert result.run_mode == "noop"
        assert result.exit_code == 1
        assert "limits.max_review_passes" in result.message
        assert not (proj / ".agentic" / "logs" / "awf-launch.lease").exists()
        assert pipeline_state.read_state(proj) is None

    def test_continue_at_the_implementer_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import awf.api.pipeline as api_pipeline
        from awf import pipeline_state

        proj = _project(tmp_path, max_passes=99)
        _activate(proj, "# Task\n")
        pipeline_state.write_state(proj, stage_name="agent-implementer")

        def _must_not_run(*_args, **_kwargs):
            pytest.fail("awf continue resumed the implementer under a broken limit")

        monkeypatch.setattr(api_pipeline, "start_in_background", _must_not_run)
        monkeypatch.setattr("awf.orchestrator.run_pipeline", _must_not_run)

        result = api_pipeline.continue_pipeline(proj, background=False)

        assert result.run_mode == "noop"
        assert result.exit_code == 1
        assert "limits.max_review_passes" in result.message

    def test_run_next_stops_the_run(self, tmp_path: Path):
        proj = _project(tmp_path, max_passes=99)
        _activate(proj, "# Task\n")
        api.run_start(proj, queue=[TODO])

        result = api.run_next(proj)

        assert result.action == "stopped"
        assert "limits.max_review_passes" in result.message
        assert result.report_file


def test_two_headings_of_one_round_count_once(tmp_path: Path) -> None:
    proj = tmp_path
    body = (
        "# Task\n\n## REVIEW-фикс (супервизор, виток 1)\n\n- fix\n\n"
        "## REVIEW-фикс (аудит 0262)\n\n- finding\n"
    )
    _activate(proj, body)
    assert budget.review_passes(proj, TODO) == 1


def test_spec_review_heading_is_not_a_pass(tmp_path: Path) -> None:
    proj = tmp_path
    body = (
        "# Task\n\n## REVIEW-фикс (супервизор, виток 1)\n\n- fix\n\n"
        "## REVIEW-фикс (spec, ревизия требований)\n\n- finding\n"
    )
    _activate(proj, body)
    assert budget.review_passes(proj, TODO) == 1
