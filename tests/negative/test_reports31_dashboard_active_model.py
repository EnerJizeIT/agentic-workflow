"""REPORTS31: the active stage card shows its model IMMEDIATELY.

Owner's request 10.10: «в дашборде модель показывается только после
передачи handoff — надо чтобы сразу». 0185 sources the model from the
stage's log record ("Stage facts") — that record appears only at the
handoff, at the stage's END. Until then the active card carried an
empty model (the chip rendered an honest «—» for the whole stage).

Invariants (TODO-0194):
1. The active entry's model comes from the project config immediately —
   the same source the engine uses to launch the worker
   (config.yaml ``models.<role>.model``, awf/supervisor.get_role_model):
   (a) ``models.agent-implementer.model: vllm/llm`` → the active entry
       carries model="vllm/llm" while the log is still empty;
   (b) no key for the role → "" (the chip's honest «—»);
   (c) a placeholder value (``<из сессии>`` — "the session's model",
       no fixed id) → "" — the card must not show a fake model id.
2. The stage's ROLE is the pipeline stage's role (stage name → role
   mapping from pipeline.yaml); a stage missing from the displayed
   pipeline falls back to the stage name as slug — custom roles and
   the supervisor resolve by slug, no key → "".
3. 0185 untouched: the COMPLETED entries keep the model from the stage
   log record (it may match or refine the config's); the active entry
   no longer waits for the handoff.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from awf import api
from awf.api.dashboard import generate_state_dict
from awf.pipeline_state import write_state


def _utc(*args: int) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def dash_project(tmp_git_repo: Path) -> Path:
    """Project with .agentic/ + the 4-stage default pipeline."""
    api.init_project(tmp_git_repo, project_name="ActiveModelDash")
    pipes_dir = tmp_git_repo / ".agentic" / "pipelines"
    pipes_dir.mkdir(parents=True, exist_ok=True)
    (pipes_dir / "default.yaml").write_text(
        "stages:\n"
        "  - name: plan\n    role: supervisor\n"
        "  - name: agent-implementer\n    role: agent-implementer\n"
        "  - name: agent-qa-review\n    role: agent-qa-review\n"
        "  - name: verify\n    role: supervisor\n",
        encoding="utf-8",
    )
    return tmp_git_repo


def _set_role_model(proj: Path, role: str, value: str | None) -> None:
    """Set (or drop, value=None) ``models.<role>.model`` in config.yaml."""
    cfg_path = proj / ".agentic" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    models = cfg.setdefault("models", {})
    if value is None:
        models.pop(role, None)
    else:
        entry = models.get(role)
        if not isinstance(entry, dict):
            entry = {}
            models[role] = entry
        entry["model"] = value
    cfg_path.write_text(
        yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )


def _write_log(proj: Path, lines: list[str]) -> None:
    logs = proj / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "orchestrator.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_handoff(
    proj: Path, stage: str, todo: str, mtime: float, body: str,
) -> None:
    d = proj / ".agentic" / "handoff"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{stage}-{todo}.md"
    f.write_text(body, encoding="utf-8")
    os.utime(f, (mtime, mtime))


def _stage_facts(model: str = "", attempt: int = 1) -> str:
    """The handoff skeleton as the engine writes it (ORCH M4.3)."""
    lines = [
        "# Handoff from `agent-implementer` (TODO TODO-0001)",
        "",
        "## Run facts",
        "",
        "- generated: 2026-10-10T17:14:00Z",
        "- stage role: `agent-implementer`",
        "- worker run: 1",
        "",
        "## Stage facts",
        "",
        "- stage_id: `agent-implementer`",
        f"- attempt: {attempt}",
        "- role: `agent-implementer`",
    ]
    if model:
        lines.append(f"- model: `{model}`")
    lines += [
        "- check vs DONE.json: matched",
        "",
        "## DONE summary (from worker)",
        "",
        "done",
    ]
    return "\n".join(lines) + "\n"


def _impl_active(proj: Path, todo: str, stage_name: str = "agent-implementer") -> None:
    """State with the implementer stage running (the log may be empty)."""
    write_state(
        proj, stage_idx=1, stage_name=stage_name,
        stage_kind="execute", todo_id=todo,
    )


class TestConfigSource:
    """Invariant 1: the active entry's model — from the config, not the log."""

    def test_active_entry_gets_model_from_config(self, dash_project):
        """(a) config has the model → the active card shows it at once,
        while the stage's log record (the handoff) does not exist yet."""
        _set_role_model(dash_project, "agent-implementer", "vllm/llm")
        _impl_active(dash_project, "TODO-0001")

        d = generate_state_dict(dash_project)
        active = next(c for c in d["handoffs"] if c["active"])

        assert active["role"] == "agent-implementer"
        assert active["model"] == "vllm/llm"

    def test_active_entry_no_key_is_empty(self, dash_project):
        """(b) the skeleton config has no model for the role → honest «—»
        (""), not a guess."""
        _impl_active(dash_project, "TODO-0001")

        d = generate_state_dict(dash_project)
        active = next(c for c in d["handoffs"] if c["active"])
        assert active["model"] == ""

    def test_active_entry_placeholder_value_is_empty(self, dash_project):
        """(c) a placeholder value (no fixed model id — "the session's
        model") must not reach the chip as a model id."""
        _set_role_model(dash_project, "agent-implementer", "<из сессии>")
        _impl_active(dash_project, "TODO-0001")

        d = generate_state_dict(dash_project)
        active = next(c for c in d["handoffs"] if c["active"])
        assert active["model"] == ""

    def test_active_entry_ignores_log_model(self, dash_project):
        """The config is the active entry's source even when a log line
        for the stage exists (the log's model belongs to the handoff,
        0185 — the active card must not wait for it)."""
        _set_role_model(dash_project, "agent-implementer", "vllm/llm")
        _write_log(dash_project, [
            "[2026-10-10T17:00:00Z] Pipeline started with 4 stages: plan a b c",
            "[2026-10-10T17:05:00Z] Stage 1: agent-implementer (agent-implementer :: execute)",
        ])
        _impl_active(dash_project, "TODO-0001")

        d = generate_state_dict(dash_project)
        active = next(c for c in d["handoffs"] if c["active"])
        assert active["model"] == "vllm/llm"


class TestRoleMapping:
    """Invariant 2: the role is the pipeline stage's role, not the name."""

    def test_active_entry_maps_stage_name_to_pipeline_role(self, dash_project):
        """A stage whose NAME differs from its role resolves the model
        by the ROLE (the engine launches the worker by role, so the
        config key is the role's slug)."""
        pipes = dash_project / ".agentic" / "pipelines" / "default.yaml"
        pipes.write_text(
            "stages:\n"
            "  - name: plan\n    role: supervisor\n"
            "  - name: impl\n    role: agent-implementer\n"
            "  - name: qa\n    role: agent-qa-review\n"
            "  - name: verify\n    role: supervisor\n",
            encoding="utf-8",
        )
        _set_role_model(dash_project, "agent-implementer", "vllm/llm")
        _impl_active(dash_project, "TODO-0001", stage_name="impl")

        d = generate_state_dict(dash_project)
        active = next(c for c in d["handoffs"] if c["active"])

        assert active["role"] == "impl"
        assert active["model"] == "vllm/llm"

    def test_custom_role_slug_without_key_is_empty(self, dash_project):
        """A custom role not in the config → honest «—» (no key)."""
        pipes = dash_project / ".agentic" / "pipelines" / "default.yaml"
        pipes.write_text(
            "stages:\n"
            "  - name: plan\n    role: supervisor\n"
            "  - name: agent-my-auditor\n    role: agent-my-auditor\n"
            "  - name: verify\n    role: supervisor\n",
            encoding="utf-8",
        )
        write_state(
            dash_project, stage_idx=1, stage_name="agent-my-auditor",
            stage_kind="execute", todo_id="TODO-0001",
        )

        d = generate_state_dict(dash_project)
        active = next(c for c in d["handoffs"] if c["active"])
        assert active["model"] == ""

    def test_supervisor_stage_without_model_key_is_empty(self, dash_project):
        """The supervisor (current session — no fixed model id in the
        skeleton config) stays an honest «—»."""
        write_state(
            dash_project, stage_idx=0, stage_name="plan",
            stage_kind="plan", todo_id="TODO-0001",
        )

        d = generate_state_dict(dash_project)
        active = next(c for c in d["handoffs"] if c["active"])
        assert active["model"] == ""


class TestCompletedUntouched:
    """Invariant 3: 0185 regression — the completed entries keep the
    model from the stage log record; the config only feeds the active
    entry."""

    def test_completed_entry_keeps_log_model(self, dash_project):
        _write_log(dash_project, [
            "[2026-10-10T17:00:00Z] Pipeline started with 4 stages: plan a b c",
            "[2026-10-10T17:00:00Z] Stage 0: plan (supervisor :: plan)",
            "[2026-10-10T17:05:00Z] Stage 1: agent-implementer (agent-implementer :: execute)",
            "[2026-10-10T17:15:00Z] Stage 2: agent-qa-review (agent-qa-review :: execute)",
        ])
        _write_handoff(
            dash_project, "agent-implementer", "TODO-0001",
            _utc(2026, 10, 10, 17, 14), _stage_facts(model="vllm/log-model"),
        )
        _set_role_model(dash_project, "agent-implementer", "vllm/config-model")
        _set_role_model(dash_project, "agent-qa-review", "vllm/qa-model")
        write_state(
            dash_project, stage_idx=2, stage_name="agent-qa-review",
            stage_kind="execute", todo_id="TODO-0001",
        )

        d = generate_state_dict(dash_project)
        impl = next(c for c in d["handoffs"] if c["role"] == "agent-implementer")
        qa = next(c for c in d["handoffs"] if c["active"])

        # The finished record keeps its own log model (0185) — the
        # config's value must not overwrite it.
        assert impl["model"] == "vllm/log-model"
        # The running stage shows the config's model immediately.
        assert qa["model"] == "vllm/qa-model"


class TestNonStringModel:
    """F1 QA (REVIEW 10.10): YAML 1.1 reads ``model: 123`` as int,
    ``on``/``off`` as bool, ``1.5`` as float. The engine tolerates such
    config (the value is just interpolated into the worker prompt), but
    the dashboard used to feed it straight into ``re.fullmatch`` —
    ``TypeError`` in ``generate_state_dict`` → ``/api/state`` 500 on
    every poll (the live dashboard goes blind). The chip must get the
    value's string form instead; a placeholder ``<...>`` / absent key
    stays an honest «—»."""

    @pytest.mark.parametrize(
        ("value", "chip"),
        [(123, "123"), (True, "True"), (1.5, "1.5")],
    )
    def test_non_string_model_shows_string_form(self, dash_project, value, chip):
        _set_role_model(dash_project, "agent-implementer", value)
        _impl_active(dash_project, "TODO-0001")

        d = generate_state_dict(dash_project)
        active = next(c for c in d["handoffs"] if c["active"])
        assert active["model"] == chip
