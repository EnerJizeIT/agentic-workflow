"""dogfood-11 (F7): retry_note must reach the worker prompt.

When a stage is retried after a silent exit, pipeline_engine builds a
continue-push note; run_agent_stage appends it to the prompt AFTER the
regular contract/task text (recency — weak models weight the tail).
"""
from __future__ import annotations

from awf import agent_stage
from awf.pipeline import Stage


def _capture_run(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_run(cmd, cwd, watch_paths=None, watch_new_glob=None,
                 logs_dir=None, hard_timeout=None, env=None, signal_holder=None):
        captured["cmd"] = cmd

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True,
    )
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)
    return captured


def _project(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".agentic" / "inbox").mkdir(parents=True)
    (proj / ".agentic" / "outbox").mkdir(parents=True)
    (proj / ".agentic" / "logs").mkdir(parents=True)
    roles = proj / ".agentic" / "roles"
    roles.mkdir(parents=True)
    (roles / "developer.md").write_text("# Developer role\n", encoding="utf-8")
    return proj


def test_retry_note_appended_to_prompt(tmp_path, monkeypatch):
    captured = _capture_run(monkeypatch, tmp_path)
    proj = _project(tmp_path)
    stage = Stage(name="agent-dev", role="developer", kind="execute")

    agent_stage.run_agent_stage(
        stage, "TODO-0001", proj, {}, proj / ".agentic" / "logs",
        retry_note="## RETRY 1: test push note",
    )

    prompt = captured["cmd"][-1]
    assert "RETRY 1: test push note" in prompt
    # Appended at the very end — after the regular task text.
    assert prompt.rindex("RETRY 1") > prompt.rindex("## Task")


def test_without_retry_note_prompt_unchanged(tmp_path, monkeypatch):
    captured = _capture_run(monkeypatch, tmp_path)
    proj = _project(tmp_path)
    stage = Stage(name="agent-dev", role="developer", kind="execute")

    agent_stage.run_agent_stage(stage, "TODO-0001", proj, {}, proj / ".agentic" / "logs")

    prompt = captured["cmd"][-1]
    assert "RETRY" not in prompt
