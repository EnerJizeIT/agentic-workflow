"""ORCH M6.4 (TODO-0149): NEG for the new zones (service/draft) + the
BLOCKED-signal rule in the QA role.

Twice (TODO-0140, TODO-0144) the QA role wrote its BLOCKED verdict inside
``DONE-{todo}.md`` while the signal passed as DONE — the pipeline went to
verify instead of blocking. This unit pins the signal rule into the three
QA role files and extends the NEG-3 broken-inputs matrix to the new zones
(ORCH M5.1/M5.2: the role-draft area, the service run's own state file):

- the QA role files (``.agentic/roles/``, ``awf/templates/roles/``, the
  ``templates/roles/`` mirror) all carry the rule «verdict BLOCKED ⇒ ONLY
  ``BLOCKED-{todo}.ready`` + ``BLOCKED-{todo}.md``; ``DONE-{todo}.*`` is
  never written» (test_qa_role_says_blocked_signal — RED on the baseline:
  the rule was absent, which is exactly what let 0140/0144 slip);
- a corrupt ``state/service-run.yaml`` (truncated / YAML garbage / binary)
  → ``run_service_status`` / ``brief`` / ``get_status`` degrade to "no
  service run" WITH a warning naming the file, no traceback, the file is
  kept; after repair the service run reads again
  (test_corrupt_service_run_degrades — RED on the baseline: the
  degradation was silent, indistinguishable from "never started");
- a binary draft candidate: the listing degrades (source="file") and
  ``adopt_role_draft`` is a typed refusal — no traceback, the candidate
  kept byte-for-byte, no live role created
  (test_corrupt_draft_candidate_adopt_refuses_typed — RED on the
  baseline: UnicodeDecodeError escaped adopt); a hand-placed candidate
  without the marker adopts as documented (M5.1 "file" source);
- garbage queue items (a string that is not TODO-NNNN, a non-dict item)
  in service-run.yaml → the state read degrades (the shared shape
  validation of run_state.read_run), the main run is unaffected;
- a stale declared output → the M3.2 check refuses it at unit level
  (test_stale_declared_output_refused_unit — regression of TODO-0136;
  the e2e twin is test_stage_assignment_outputs.py).

No real spawns: the state files are written directly.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

import awf.pipeline_engine as engine
from awf import api
from awf.api._errors import AwfApiError

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The three QA role files that must all carry the rule (invariant 1).
QA_ROLE_FILES = (
    REPO_ROOT / ".agentic" / "roles" / "agent-qa-review.md",
    REPO_ROOT / "awf" / "templates" / "roles" / "agent-qa-review.md",
    REPO_ROOT / "templates" / "roles" / "agent-qa-review.md",
)

# The minimal rule (TODO-0149 wording; the same one-liner in all three
# files — the ambiguous lines are not duplicated).
BLOCKED_RULE = (
    "вердикт BLOCKED ⇒ ТОЛЬКО `BLOCKED-{todo}.ready` + `BLOCKED-{todo}.md`; "
    "`DONE-{todo}.*` не пишется"
)


def _norm(text: str) -> str:
    """Normalization for the rule check: backticks off, one space, lower."""
    return " ".join(text.replace("`", " ").lower().split())


def _read(path: Path) -> str:
    # A missing file = rule absent (the prove_red baseline worktree has no
    # gitignored .agentic/ — the assertion, not a read error, goes red).
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _svc_file(project: Path) -> Path:
    return project / ".agentic" / "state" / "service-run.yaml"


def _valid_service_state() -> dict:
    return {
        "queue": [{"todo_id": "TODO-0002", "pipeline": "svc"}],
        "index": 0,
        "active": True,
        "current": "TODO-0002",
        "generation": 1,
    }


# ── 1. the BLOCKED-signal rule in the QA role files ──────────────────────


def test_qa_role_says_blocked_signal():
    """Invariant 1: all three QA role files carry the signal rule
    (normalized compare). The rule is what the engine's verify wait
    actually keys on: the SIGNAL decides, not the verdict in the report."""
    rule = _norm(BLOCKED_RULE)
    missing = [str(p) for p in QA_ROLE_FILES if rule not in _norm(_read(p))]
    assert not missing, f"the BLOCKED-signal rule is missing in: {missing}"


# ── 2. corrupt service-run.yaml → degraded, warned, recoverable ─────────


def test_corrupt_service_run_degrades(tmp_git_repo: Path):
    """Invariant 3 (service zone): a corrupt ``state/service-run.yaml``
    degrades every read surface to "no service run" WITH a warning naming
    the file — no traceback, the file is kept, the main run is
    unaffected; after repair the service run reads again."""
    project = tmp_git_repo
    api.init_project(project, project_name="T")
    api.run_start(project, queue=["TODO-0001"])  # a live MAIN run to compare
    svc_file = _svc_file(project)
    valid = yaml.safe_dump(_valid_service_state())
    svc_file.write_text(valid, encoding="utf-8")
    assert api.run_service_status(project).service_active is True

    corruptions = (
        valid.splitlines()[0] + "\n",  # truncated: parses, shape invalid
        "queue: [unclosed\n  - \x00\n: :",  # YAML garbage
        None,  # binary (handled via write_bytes below)
    )
    for body in corruptions:
        if body is None:
            svc_file.write_bytes(b"\xff\xfe\x00binary garbage")
        else:
            svc_file.write_text(body, encoding="utf-8")

        status = api.run_service_status(project)
        assert status.service_active is False
        # The degradation must be VISIBLE, not a silent "never started"
        # (getattr: the pre-fix field is absent — the assertion, not an
        # attribute error, is the red).
        warning = getattr(status, "warning", "")
        assert "service-run.yaml" in warning
        assert "unreadable" in warning
        assert warning in status.message
        # The other two read surfaces survive the corruption too.
        brief = api.brief(project)
        assert isinstance(brief.text, str)
        assert api.get_status(project) is not None
        # The main run is byte-for-byte the live one.
        assert api.run_status(project).active is True

    # Повтор после починки: the original state reads again.
    svc_file.write_text(valid, encoding="utf-8")
    status = api.run_service_status(project)
    assert status.service_active is True
    assert status.warning == ""
    assert status.todo_id == "TODO-0002"


# ── 3. corrupt draft candidates → listing + adopt degrade ───────────────


def _roles_project(base: Path) -> Path:
    proj = base / "proj"
    (proj / ".agentic").mkdir(parents=True)
    return proj


def test_corrupt_draft_candidate_listing_degrades(tmp_path: Path):
    """A binary draft and a marker-less draft both LIST (source="file",
    empty created) — the listing never tracebacks on file contents."""
    proj = _roles_project(tmp_path)
    draft_dir = proj / ".agentic" / "roles" / "draft"
    draft_dir.mkdir(parents=True)
    (draft_dir / "binary.md").write_bytes(b"\xff\xfe\x00not utf-8")
    (draft_dir / "hand-made.md").write_text("# Hand\n\nNo marker.\n", encoding="utf-8")

    result = api.list_role_drafts(proj)
    by_name = {d["name"]: d for d in result.drafts}
    assert set(by_name) == {"binary", "hand-made"}
    assert by_name["binary"]["source"] == "file"
    assert by_name["binary"]["created"] == ""
    assert by_name["hand-made"]["source"] == "file"


def test_corrupt_draft_candidate_adopt_refuses_typed(tmp_path: Path):
    """A binary candidate is a corrupt input: adopt is a TYPED refusal
    (AwfApiError, not a traceback), the candidate stays byte-for-byte and
    no live role appears. A marker-less UTF-8 candidate adopts as
    documented (the hand-placed "file" source, M5.1)."""
    proj = _roles_project(tmp_path)
    draft_dir = proj / ".agentic" / "roles" / "draft"
    draft_dir.mkdir(parents=True)
    binary = draft_dir / "binary.md"
    binary.write_bytes(b"\xff\xfe\x00not utf-8")
    (proj / ".agentic" / "roles").mkdir(exist_ok=True)

    with pytest.raises(AwfApiError, match="unreadable"):
        api.adopt_role_draft(proj, "binary")
    assert binary.read_bytes() == b"\xff\xfe\x00not utf-8"  # kept
    assert not (proj / ".agentic" / "roles" / "binary.md").exists()

    # The no-marker variant is a legitimate hand-placed candidate.
    hand = draft_dir / "hand.md"
    hand.write_text("# Hand role\n\nDoes hand work.\n", encoding="utf-8")
    result = api.adopt_role_draft(proj, "hand")
    live = proj / ".agentic" / "roles" / "hand.md"
    assert result.role_file == str(live)
    # The body is preserved (M5.3 normalization may append the BD-31
    # addendum, as in test_role_draft.py).
    assert live.read_text(encoding="utf-8").startswith("# Hand role\n\nDoes hand work.\n")
    assert not hand.exists()


# ── 4. stale declared output → the M3.2 refusal (unit level) ────────────


def test_stale_declared_output_refused_unit(tmp_path: Path):
    """Regression of TODO-0136 (ORCH M3.2): a leftover output whose mtime
    predates the stage start is REFUSED (the stage must fail, not advance
    silently); a fresh output counts; a missing output is a different,
    named refusal. The e2e twin: test_stage_assignment_outputs.py."""
    out = tmp_path / "src" / "out.txt"
    out.parent.mkdir(parents=True)
    out.write_text("leftover from a previous stage", encoding="utf-8")

    os.utime(out, (1000.0, 1000.0))
    assert "stale" in engine._declared_output_problem(tmp_path, "src/out.txt", 2000.0)

    os.utime(out, (2500.0, 2500.0))  # written at/after the stage start
    assert engine._declared_output_problem(tmp_path, "src/out.txt", 2000.0) == ""

    assert "does not exist" in engine._declared_output_problem(
        tmp_path, "src/nope.txt", 2000.0
    )


# ── 5. garbage queue in the service slot → the read degrades ────────────


def test_garbage_queue_service_slot_degrades(tmp_git_repo: Path):
    """Garbage queue items (a string that is not TODO-NNNN, a non-dict
    item, a dict with a garbage id) make the whole service state
    unreadable — the shared shape validation degrades to "no service run"
    with the warning, and the MAIN run stays untouched."""
    project = tmp_git_repo
    api.init_project(project, project_name="T")
    api.run_start(project, queue=["TODO-0001"])
    svc_file = _svc_file(project)
    svc_file.write_text(
        yaml.safe_dump(
            {
                "queue": ["not-a-todo-id", 42, {"todo_id": "garbage"}],
                "index": 0,
                "active": True,
            }
        ),
        encoding="utf-8",
    )

    status = api.run_service_status(project)
    assert status.service_active is False
    assert "service-run.yaml" in getattr(status, "warning", "")
    assert api.brief(project) is not None
    assert api.get_status(project) is not None
    assert api.run_status(project).active is True  # the main run survives
