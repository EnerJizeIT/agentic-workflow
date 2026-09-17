"""Handoff v2 (dogfood-11): fact sheet for the NEXT WORKER, no guesses.

Context: the handoff used to mix audiences — neutral facts for the worker
plus alarm prose addressed to the supervisor ("escalate via BLOCKED
signal"). That mismatch fed the theory that a "poisoned handoff" caused
repeated worker stalls. Handoff v2:
- deterministic run facts (exit code, duration, run number, signals,
  notes presence, changes vs baseline),
- prose from the worker only,
- no supervisor-directed instructions.
"""
from __future__ import annotations

from types import SimpleNamespace

from awf.agent_stage import collect_handoff


def _project(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".agentic" / "outbox").mkdir(parents=True)
    (proj / ".agentic" / "context").mkdir(parents=True)
    (proj / ".agentic" / "logs").mkdir(parents=True)
    return proj


def _stub_git(monkeypatch, changed_files: list[str], stat: str = ""):
    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[:2] == ["git", "diff"]:
            if "--name-only" in cmd:
                return SimpleNamespace(stdout="\n".join(changed_files) + "\n")
            return SimpleNamespace(stdout=stat)
        return None

    monkeypatch.setattr("awf.signal_watch.subprocess.run", fake_run)


def test_facts_block_with_run_metadata(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    _stub_git(monkeypatch, [])

    out = collect_handoff(
        "agent-implementer", "TODO-0002", proj, proj / ".agentic" / "logs",
        exit_code=0, duration_sec=187.6, attempt=2,
    )
    body = out.read_text(encoding="utf-8")
    assert "## Run facts" in body
    assert "- stage role: `agent-implementer`" in body
    assert "- worker run: 2" in body
    assert "- worker exit code: 0" in body
    assert "- worker duration: 188s" in body
    assert "- signals: DONE=no, BLOCKED=no, REVIEW=no" in body
    assert "- worker notes: PROGRESS=absent, DONE-report=absent" in body


def test_changes_fact_lists_files(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    (proj / ".agentic" / "context" / "BASELINE-TODO-0002.sha").write_text("abc123\n")
    _stub_git(
        monkeypatch,
        ["src/topic_trainer/core/schemas.py", "tests/test_schemas.py"],
        stat=" src/topic_trainer/core/schemas.py | 40 ++",
    )

    out = collect_handoff("agent-implementer", "TODO-0002", proj, proj / ".agentic" / "logs")
    body = out.read_text(encoding="utf-8")
    assert "changes vs baseline: 2 file(s)" in body
    assert "`src/topic_trainer/core/schemas.py`" in body


def test_changes_fact_none_and_no_baseline(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    _stub_git(monkeypatch, [])
    out = collect_handoff("worker", "TODO-0001", proj, proj / ".agentic" / "logs")
    assert "changes vs baseline: no baseline recorded" in out.read_text(encoding="utf-8")

    (proj / ".agentic" / "context" / "BASELINE-TODO-0002.sha").write_text("abc123\n")
    out2 = collect_handoff("worker", "TODO-0002", proj, proj / ".agentic" / "logs")
    assert "changes vs baseline: none" in out2.read_text(encoding="utf-8")


def test_notes_missing_but_changes_present(tmp_path, monkeypatch):
    """The weak-model case: code exists, notes absent — don't scream 'NO OUTPUT'."""
    proj = _project(tmp_path)
    (proj / ".agentic" / "context" / "BASELINE-TODO-0002.sha").write_text("abc123\n")
    _stub_git(monkeypatch, ["src/schemas.py"])

    out = collect_handoff("agent-implementer", "TODO-0002", proj, proj / ".agentic" / "logs")
    body = out.read_text(encoding="utf-8")
    assert "⚠️ No worker notes" in body
    assert "only evidence" in body
    assert "No worker notes and no file changes" not in body


def test_no_supervisor_directed_text(tmp_path, monkeypatch):
    """The whole point: worker-facing file never instructs about supervision."""
    proj = _project(tmp_path)
    _stub_git(monkeypatch, [])

    out = collect_handoff("worker", "TODO-0001", proj, proj / ".agentic" / "logs")
    body = out.read_text(encoding="utf-8").lower()
    assert "supervisor" not in body
    assert "escalate" not in body
    assert "salvage" not in body
