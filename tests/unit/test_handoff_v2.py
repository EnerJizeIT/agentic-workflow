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
                # M4 (TODO-0228): the handoff reads paths through the R2.1
                # model (NUL protocol), not the printed listing.
                payload = b"".join(p.encode() + b"\0" for p in changed_files)
                return SimpleNamespace(stdout=payload, returncode=0, stderr=b"")
            return SimpleNamespace(stdout=stat, returncode=0, stderr=b"")
        return None

    monkeypatch.setattr("awf.signal_watch.subprocess.run", fake_run)


def test_facts_block_with_run_metadata(tmp_path, monkeypatch):
    """REPORTS30 (TODO-0187): run metadata lives in the single "Stage
    facts" block — the old "Run facts" section duplicated the identity
    (stage role / worker run vs the render_stage_facts role / attempt)."""
    proj = _project(tmp_path)
    _stub_git(monkeypatch, [])

    out = collect_handoff(
        "agent-implementer", "TODO-0002", proj, proj / ".agentic" / "logs",
        exit_code=0, duration_sec=187.6, attempt=2,
    )
    body = out.read_text(encoding="utf-8")
    assert "## Run facts" not in body
    assert "- stage role:" not in body
    assert "- worker run:" not in body
    assert "## Stage facts" in body
    assert "- role: `agent-implementer`" in body
    assert "- attempt: 2" in body
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


def test_stage_scoped_filename(tmp_path, monkeypatch):
    """Day-3: handoff keyed by STAGE name — two QA stages stop overwriting."""
    proj = _project(tmp_path)
    _stub_git(monkeypatch, [])

    out = collect_handoff(
        "agent-qa-review", "TODO-0002", proj, proj / ".agentic" / "logs",
        stage_name="agent-qa-review-final",
    )

    assert out.name == "agent-qa-review-final-TODO-0002.md"


def test_prev_block_carries_the_newest_handoff_and_links_the_older(tmp_path):
    """IMP2b: the handoff surface is PREV/PREVLINK, not a spawn attachment."""
    import os

    from awf.stage_context import build_stage_context

    proj = tmp_path / "proj"
    for name in ("inbox", "outbox", "logs", "context", "handoff", "roles"):
        (proj / ".agentic" / name).mkdir(parents=True)
    (proj / ".agentic" / "config.yaml").write_text(
        "stage_types:\n  qa: execute\n", encoding="utf-8"
    )
    (proj / ".agentic" / "inbox" / "TODO-0002.md").write_text(
        "# TODO-0002\n", encoding="utf-8"
    )
    (proj / ".agentic" / "roles" / "agent-qa-review.md").write_text(
        "# QA\n", encoding="utf-8"
    )
    older = proj / ".agentic" / "handoff" / "agent-implementer-TODO-0002.md"
    newer = proj / ".agentic" / "handoff" / "agent-architect-review-TODO-0002.md"
    older.write_text("OLDER-BODY\n", encoding="utf-8")
    newer.write_text("NEWER-BODY\n", encoding="utf-8")
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    text = build_stage_context(
        proj, "TODO-0002", "qa", "agent-qa-review"
    ).text

    assert "NEWER-BODY" in text
    assert "agent-implementer-TODO-0002.md" in text
