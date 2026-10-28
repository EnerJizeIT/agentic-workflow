"""OPS IMP1 (R1): the stage time cap — resolver, loader, snapshot, inputs.

Covers T1-T3 of ``docs/spec/ops-reliability``: priority and source,
refusals before spawn, and the ``timeout`` key in both snapshots.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import pipeline_engine, stage_timeout
from awf._errors import AwfApiError
from awf.api import pipeline as api_pipeline
from awf.pipeline import (
    Stage,
    load_stages,
    pipeline_snapshot_text,
    stage_to_document,
    validate_pipeline_stages,
)


class TestResolvePriority:
    """T1: caller -> stage -> project -> 3600, with the source named."""

    def test_no_keys_is_default_3600(self) -> None:
        r = stage_timeout.resolve(None, None, {})
        assert (r.seconds, r.source) == (3600, "default")

    def test_project_key(self) -> None:
        config = {"automation": {"stage_timeout_seconds": 7200}}
        assert stage_timeout.resolve(None, None, config) == stage_timeout.Resolved(
            7200, "project"
        )

    def test_stage_beats_project(self) -> None:
        config = {"automation": {"stage_timeout_seconds": 7200}}
        assert stage_timeout.resolve(None, 600, config) == stage_timeout.Resolved(
            600, "stage"
        )

    def test_caller_beats_all(self) -> None:
        config = {"automation": {"stage_timeout_seconds": 7200}}
        assert stage_timeout.resolve(120, 600, config) == stage_timeout.Resolved(
            120, "caller"
        )

    def test_explicit_3600_is_not_the_default(self) -> None:
        r = stage_timeout.resolve(3600, None, {})
        assert (r.seconds, r.source) == (3600, "caller")


class TestRefusals:
    """T2: bad values are refused and name their input."""

    @pytest.mark.parametrize("bad", [0, -5, "1h", True])
    def test_caller_refused(self, bad) -> None:
        with pytest.raises(AwfApiError) as exc:
            stage_timeout.validate_caller(bad)
        assert "--timeout" in str(exc.value)

    def test_caller_none_is_not_given(self) -> None:
        assert stage_timeout.validate_caller(None) is None

    @pytest.mark.parametrize("bad", [0, -5, "1h", True])
    def test_project_refused(self, bad) -> None:
        config = {"automation": {"stage_timeout_seconds": bad}}
        with pytest.raises(AwfApiError) as exc:
            stage_timeout.validate_project(config)
        assert stage_timeout.PROJECT_KEY in str(exc.value)

    def test_stage_key_refused_by_loader(self) -> None:
        with pytest.raises(AwfApiError) as exc:
            validate_pipeline_stages(
                [{"name": "worker", "role": "worker", "timeout": "1h"}], "p.yaml"
            )
        text = str(exc.value)
        assert "worker" in text and "'timeout'" in text

    def test_valid_stage_key_loader(self) -> None:
        stages = validate_pipeline_stages(
            [{"name": "worker", "role": "worker", "timeout": 600}], "p.yaml"
        )
        assert stages[0]["timeout"] == 600


class TestPipelineLoader:
    """T1/T3: no key keeps the old pipeline; a key round-trips."""

    def _write(self, tmp_path: Path, body: str) -> Path:
        p = tmp_path / "pipeline.yaml"
        p.write_text("name: test\nstages:\n" + body, encoding="utf-8")
        return p

    def test_no_key_loads_with_none(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "  - name: plan\n    role: supervisor\n"
            "  - name: worker\n    role: worker\n",
        )
        stages = load_stages(path)
        assert [s.timeout for s in stages] == [None, None]

    def test_key_loads(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "  - name: plan\n    role: supervisor\n"
            "  - name: worker\n    role: worker\n    timeout: 600\n",
        )
        assert load_stages(path)[1].timeout == 600

    def test_old_snapshot_without_key_reads(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "  - name: worker\n    role: worker\n    write_check: 'off'\n",
        )
        assert load_stages(path)[0].timeout is None


class TestSnapshot:
    """T3: one serializer, both snapshots; absent stays absent."""

    def test_document_has_timeout_when_set(self) -> None:
        doc = stage_to_document(Stage(name="w", role="worker", timeout=600))
        assert doc["timeout"] == 600

    def test_document_omits_timeout_when_unset(self) -> None:
        doc = stage_to_document(Stage(name="w", role="worker"))
        assert "timeout" not in doc

    def test_per_todo_snapshot_round_trips(self, tmp_path: Path) -> None:
        stages = [
            Stage(name="plan", role="supervisor", kind="plan"),
            Stage(name="worker", role="worker", kind="execute", timeout=600),
        ]
        text = pipeline_snapshot_text(stages, "default", "TODO-0001")
        path = tmp_path / "PIPELINE-TODO-0001.yaml"
        path.write_text(text, encoding="utf-8")
        reloaded = load_stages(path)
        assert reloaded[1].timeout == 600
        assert reloaded[0].timeout is None


class TestInputs:
    """T1: CLI and MCP defaults are "not given" (``None``), not 3600."""

    def test_cli_start_without_timeout_is_none(self) -> None:
        from awf.cli import _build_parser

        parser, _sub = _build_parser()
        args = parser.parse_args(["start"])
        assert args.timeout is None

    def test_cli_explicit_timeout_kept(self) -> None:
        from awf.cli import _build_parser

        parser, _sub = _build_parser()
        assert parser.parse_args(["start", "--timeout", "120"]).timeout == 120

    def test_mcp_tools_default_to_none(self) -> None:
        import inspect

        tools = pytest.importorskip(
            "agent_workflow_ui.tools.awf",
            reason="plugin package is not importable in this environment",
        )
        for name in ("awf_start", "awf_continue", "awf_retry_stage", "awf_run_next"):
            fn = getattr(tools, name)
            assert inspect.signature(fn).parameters["timeout"].default is None, name


class TestApiRefusalsBeforeSpawn:
    """T2/INV8: a bad cap refuses before the lease, the PID file and spawn."""

    def _project(self, tmp_path: Path, body: str) -> Path:
        proj = tmp_path / "proj"
        agentic = proj / ".agentic"
        agentic.mkdir(parents=True)
        (agentic / "config.yaml").write_text(body, encoding="utf-8")
        return proj

    def test_project_key_refusal_names_key_and_writes_nothing(
        self, tmp_path: Path
    ) -> None:
        proj = self._project(
            tmp_path, "automation:\n  stage_timeout_seconds: 0\n"
        )
        result = api_pipeline.start_pipeline(proj, background=True)
        assert result.run_mode == "noop"
        assert result.exit_code == 1
        assert stage_timeout.PROJECT_KEY in result.message
        assert not (proj / ".agentic" / "logs").exists()
        assert not (proj / ".agentic" / "inbox").exists()

    def test_caller_refusal_raises(self, tmp_path: Path) -> None:
        proj = self._project(tmp_path, "project:\n  name: t\n")
        with pytest.raises(AwfApiError):
            api_pipeline.start_pipeline(proj, background=True, timeout=0)

    def test_continue_project_refusal(self, tmp_path: Path) -> None:
        proj = self._project(
            tmp_path, "automation:\n  stage_timeout_seconds: -5\n"
        )
        result = api_pipeline.continue_pipeline(proj, background=True)
        assert result.run_mode == "noop"
        assert result.exit_code == 1
        assert stage_timeout.PROJECT_KEY in result.message

    def test_continue_refusal_keeps_the_ack_unwritten(self, tmp_path: Path) -> None:
        """T2/INV8: the refusal comes before the ACK is published."""
        proj = self._project(
            tmp_path, "automation:\n  stage_timeout_seconds: 0\n"
        )
        result = api_pipeline.continue_pipeline(
            proj, background=True, ack="TODO-0001"
        )
        assert result.run_mode == "noop"
        assert stage_timeout.PROJECT_KEY in result.message
        assert not (proj / ".agentic" / "inbox" / "ACK-TODO-0001.ready").exists()

    def test_cli_zero_timeout_refuses_without_side_effects(
        self, tmp_path: Path, capsys
    ) -> None:
        """AC3: `awf start --timeout 0` is a refusal, not a silent 3600."""
        from types import SimpleNamespace

        from awf import cmd_start

        proj = self._project(tmp_path, "project:\n  name: t\n")
        args = SimpleNamespace(
            command="start",
            project_dir=str(proj),
            background=False,
            auto=False,
            pipeline=None,
            from_stage=None,
            timeout=0,
            todo="",
        )
        assert cmd_start.run(args) == 1
        assert "--timeout" in capsys.readouterr().out
        assert not (proj / ".agentic" / "logs").exists()

    def test_run_next_project_refusal(self, tmp_path: Path) -> None:
        from awf.api.run import run_next

        proj = self._project(
            tmp_path, "automation:\n  stage_timeout_seconds: '1h'\n"
        )
        result = run_next(proj)
        assert result.action == "refused"
        assert stage_timeout.PROJECT_KEY in result.message


class TestEngineResolution:
    """T1: the run log names the cap and the source before the spawn."""

    def _run(
        self, tmp_path: Path, monkeypatch, stage: Stage, config: dict,
        caller: int | None = None,
    ):
        project = tmp_path / "proj"
        outbox = project / ".agentic" / "outbox"
        outbox.mkdir(parents=True)
        logs: list[str] = []
        captured: dict[str, object] = {}

        def mock_agent(stage_, todo_id, project_dir, config_, logs_dir, **kw):
            captured["hard_timeout"] = kw.get("hard_timeout")
            (outbox / f"DONE-{todo_id}.md").write_text("# Done\n")
            (outbox / f"DONE-{todo_id}.ready").write_text("")

        monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
        monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
        monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: "abc123")
        monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_log", lambda _dir, msg: logs.append(msg))
        monkeypatch.setattr(pipeline_engine, "_write_state", lambda *a, **kw: None)
        from awf import signals as sig_module

        monkeypatch.setattr(sig_module, "clean_stage_signals", lambda *a, **kw: None)
        from awf import verify

        monkeypatch.setattr(verify, "attempt_auto_done", lambda *a, **kw: False)

        stages = [
            Stage(name="worker", role="worker", kind="execute", timeout=stage.timeout),
            Stage(name="verify", role="supervisor", kind="verify"),
        ]
        result = pipeline_engine.execute_agent_stage(
            stage=stages[0],
            current_todo="TODO-0001",
            project_dir=project,
            config=config,
            logs_dir=tmp_path,
            stages=stages,
            stage_idx=0,
            retry_counts=[0, 0],
            auto=False,
            agent_hard_timeout=caller,
            pipeline_name=None,
        )
        return logs, captured, result

    def test_stage_key_line_and_cap(self, tmp_path: Path, monkeypatch) -> None:
        logs, captured, _result = self._run(
            tmp_path,
            monkeypatch,
            Stage(name="worker", role="worker", kind="execute", timeout=600),
            {"automation": {"stage_timeout_seconds": 7200}},
        )
        assert "stage timeout: 600s (source: stage)" in logs
        assert captured["hard_timeout"] == 600

    def test_project_key_line(self, tmp_path: Path, monkeypatch) -> None:
        logs, captured, _result = self._run(
            tmp_path,
            monkeypatch,
            Stage(name="worker", role="worker", kind="execute"),
            {"automation": {"stage_timeout_seconds": 7200}},
        )
        assert "stage timeout: 7200s (source: project)" in logs
        assert captured["hard_timeout"] == 7200

    def test_caller_line_and_cap(self, tmp_path: Path, monkeypatch) -> None:
        logs, captured, _result = self._run(
            tmp_path,
            monkeypatch,
            Stage(name="worker", role="worker", kind="execute", timeout=600),
            {"automation": {"stage_timeout_seconds": 7200}},
            caller=120,
        )
        assert "stage timeout: 120s (source: caller)" in logs
        assert captured["hard_timeout"] == 120

    def test_no_keys_line_is_default_3600(self, tmp_path: Path, monkeypatch) -> None:
        logs, captured, _result = self._run(
            tmp_path,
            monkeypatch,
            Stage(name="worker", role="worker", kind="execute"),
            {},
        )
        assert "stage timeout: 3600s (source: default)" in logs
        assert captured["hard_timeout"] == 3600

    def test_bad_stage_cap_stops_before_spawn(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        logs, captured, result = self._run(
            tmp_path,
            monkeypatch,
            Stage(name="worker", role="worker", kind="execute", timeout=0),
            {},
        )
        assert captured == {}
        assert any(line.startswith("STAGE-ERROR:") for line in logs)
        assert result == ("TODO-0001", 0, 1)
