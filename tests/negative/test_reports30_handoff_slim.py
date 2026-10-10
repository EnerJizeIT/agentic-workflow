"""REPORTS30: handoff slimming — single facts block, DONE by reference.

Owner's backlog 09.10: «Handoff шаблон раздут и содержит
дублирования» — the "Run facts" and "Stage facts" sections intersected
(stage role / worker run vs role / attempt from render_stage_facts),
and "DONE summary (from worker)" pasted the ENTIRE DONE report into
the handoff (a text wall in the dashboard chat, while the same text
already lives in .agentic/outbox/DONE-<id>.md).

Invariants (TODO-0187):
1. One non-intersecting facts block: no "## Run facts" section, no
   "- stage role:" / "- worker run:" lines; role/attempt/model/
   signals/exit facts appear exactly once.
2. DONE summary = a digest of the first meaningful lines (limit
   DONE_DIGEST_LINES) + an explicit link to .agentic/outbox/DONE-<id>.md;
   markers deep in the report do not reach the handoff.
3. The dashboard model chip contract (TODO-0185) is preserved: the
   "## Stage facts" heading and its "- model:" line survive the merge
   (extract_stage_model).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from awf.agent_stage import collect_handoff

T = "TODO-0001"
ROLE = "agent-implementer"
MODEL = "vllm/llm"


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".agentic" / "outbox").mkdir(parents=True)
    (proj / ".agentic" / "context").mkdir(parents=True)
    (proj / ".agentic" / "logs").mkdir(parents=True)
    return proj


def _stub_git(monkeypatch) -> None:
    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[:2] == ["git", "diff"]:
            if "--name-only" in cmd:
                return SimpleNamespace(stdout="")
            return SimpleNamespace(stdout="")
        return None

    monkeypatch.setattr("awf.signal_watch.subprocess.run", fake_run)


def _handoff(proj: Path, monkeypatch, **kw) -> str:
    defaults = dict(exit_code=0, duration_sec=42.4, attempt=1,
                    stage_name=ROLE, stage_id=ROLE, model=MODEL)
    defaults.update(kw)
    out = collect_handoff(ROLE, T, proj, proj / ".agentic" / "logs", **defaults)
    return out.read_text(encoding="utf-8")


def _done_section(body: str) -> list[str]:
    """Non-empty lines of the DONE summary: digest + the link line.

    Bounded by the link line (a digest line may itself start with '## ',
    so a heading scan would cut the section short)."""
    lines = body.splitlines()
    start = lines.index("## DONE summary (from worker)") + 1
    link = f"full report: `.agentic/outbox/DONE-{T}.md`"
    end = lines.index(link)
    return [ln for ln in lines[start:end + 1] if ln.strip()]


# 30 meaningful lines: 4 header lines + fillers; the markers sit at
# meaningful lines 12 and 25 — both beyond any reasonable digest limit.
LONG_DONE_LINES = (
    ["## Реализовано: тестовая задача", "",
     "Что сделано:",
     "- починил awf/agent_stage.py (выжимка-строка-1)",
     "- обновил существующие тесты"]
    + [f"filler {i}" for i in range(1, 8)]
    + ["DEEP-MARKER-line-12"]
    + [f"filler {i}" for i in range(13, 25)]
    + ["DEEP-MARKER-line-25"]
    + [f"filler {i}" for i in range(26, 31)]
)
LONG_DONE = "\n".join(LONG_DONE_LINES) + "\n"


def test_single_facts_block_no_duplicates(tmp_path, monkeypatch):
    """Invariant 1: Run/Stage merged — the duplicated lines are gone,
    every fact appears exactly once."""
    proj = _project(tmp_path)
    _stub_git(monkeypatch)
    (proj / ".agentic" / "outbox" / f"DONE-{T}.ready").touch()

    body = _handoff(proj, monkeypatch)

    assert "## Run facts" not in body
    assert "- stage role:" not in body
    assert "- worker run:" not in body
    assert body.count("## Stage facts") == 1
    assert body.count(f"- role: `{ROLE}`") == 1
    assert body.count("- attempt: 1") == 1
    assert body.count(f"- model: `{MODEL}`") == 1
    assert body.count("- signals: ") == 1
    assert body.count("- worker exit code: ") == 1
    # The merged block keeps the run metadata (not lost in the merge).
    assert "- worker exit code: 0" in body
    assert "- worker duration: 42s" in body
    assert "- signals: DONE=yes, BLOCKED=no, REVIEW=no" in body
    assert "- changes vs baseline:" in body


def test_done_summary_is_digest_plus_link(tmp_path, monkeypatch):
    """Invariant 2: the handoff carries the digest and the file link —
    not the full report."""
    proj = _project(tmp_path)
    _stub_git(monkeypatch)
    (proj / ".agentic" / "outbox" / f"DONE-{T}.md").write_text(LONG_DONE, encoding="utf-8")

    body = _handoff(proj, monkeypatch)

    assert "## DONE summary (from worker)" in body
    # First meaningful lines reach the handoff (the digest).
    assert "## Реализовано: тестовая задача" in body
    assert "выжимка-строка-1" in body
    # Deep markers do NOT reach it (no full paste).
    assert "DEEP-MARKER-line-12" not in body
    assert "DEEP-MARKER-line-25" not in body
    # Explicit link to the full report file.
    assert f".agentic/outbox/DONE-{T}.md" in body

    # The documented limit: section = digest + the link line, nothing more.
    from awf.agent_stage import DONE_DIGEST_LINES  # lazy: baseline red

    assert 1 <= DONE_DIGEST_LINES <= 11, "digest limit must stay small"
    section = _done_section(body)
    assert len(section) == DONE_DIGEST_LINES + 1, (
        f"digest must keep DONE_DIGEST_LINES lines + 1 link "
        f"(got {len(section)} for a 30-line report)"
    )


def test_model_chip_contract_preserved(tmp_path, monkeypatch):
    """Invariant 3: extract_stage_model (TODO-0185) still finds the model
   in the merged section — the heading and the line format are preserved."""
    proj = _project(tmp_path)
    _stub_git(monkeypatch)

    body = _handoff(proj, monkeypatch)

    from awf._log_reader import extract_stage_model

    assert extract_stage_model(body) == MODEL


def test_no_done_report_no_summary_section(tmp_path, monkeypatch):
    """No DONE-{id}.md → no summary section and no dangling link."""
    proj = _project(tmp_path)
    _stub_git(monkeypatch)

    body = _handoff(proj, monkeypatch)

    assert "## DONE summary" not in body
    assert "full report:" not in body


def test_machine_facts_stage_block_not_a_duplicate(tmp_path, monkeypatch):
    """A DONE.json stage block renders in Machine facts (faithful mirror
    of the file) — its key=value form is not a fact-line duplicate of the
    Stage facts section."""
    proj = _project(tmp_path)
    _stub_git(monkeypatch)
    (proj / ".agentic" / "outbox" / f"DONE-{T}.json").write_text(
        json.dumps({"stage": {"stage_id": ROLE, "attempt": 1,
                              "role": ROLE, "model": MODEL}}),
        encoding="utf-8",
    )

    body = _handoff(proj, monkeypatch)

    assert "## Machine facts (DONE.json)" in body
    assert f"- stage: stage_id=`{ROLE}`, attempt=1" in body
    # The fact lines of the merged block stay single.
    assert body.count(f"- role: `{ROLE}`") == 1
    assert body.count("- attempt: 1") == 1
    assert body.count(f"- model: `{MODEL}`") == 1
    assert "- check vs DONE.json: matched" in body
