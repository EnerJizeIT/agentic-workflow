"""ORCH M2.3: evidence plan of the launched TODO in the run memory.

At the launch of a queue element (``run_next``) the launched TODO's
contract (verify/gates/prove_red) + the file hash of ``TODO-*.md`` are
snapped into the run memory (per-item evidence plan) — the verification
plan is not lost when the TODO is edited or retired afterwards. The
field lives in the existing ``state/run.yaml`` (derived, no second
store), written by the SAME atomic path as decisions (M1) — the advance
commit of the launch. ``awf_brief`` / ``awf_load_supervisor_context``
show the current/last element's plan (verify commands + prove_red ids,
brief) before the supervisor decides. Degradation: a TODO without a
contract → empty plan + note; a broken ``evidence_plans`` field →
dropped with a warning, no traceback (M1 style).

The pipeline spawn is mocked (``awf.api.pipeline.start_pipeline``) — hermetic.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

import awf.api.pipeline as api_pipeline
from awf import api, run_state
from awf.run_plan_read import read_run_record

T = "TODO-0001"
T2 = "TODO-0002"

VERIFY_CMD = "python3 -m pytest tests/negative/test_run_evidence_plan.py -q"
PROVE_ID = "tests/negative/test_run_evidence_plan.py::test_launch_snapshots_todo_contract"

CONTRACT_TODO = (
    "---\n"
    f'verify: ["{VERIFY_CMD}"]\n'
    'gates: ["contracts", "ratchet", "instructions"]\n'
    f'prove_red: ["{PROVE_ID}"]\n'
    "---\n"
    "\n"
    "# Task\n"
)
PLAIN_TODO = "# Task\n"
# the block is opened but never closed — parse_todo_contract raises
BROKEN_TODO = "---\nverify: [unclosed\n# Task\n"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="EvidencePlan")
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str, body: str = PLAIN_TODO) -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")


def _todo_path(proj: Path, todo_id: str) -> Path:
    return proj / ".agentic" / "inbox" / f"{todo_id}.md"


def _file_sha(proj: Path, todo_id: str) -> str:
    return hashlib.sha256(_todo_path(proj, todo_id).read_bytes()).hexdigest()


def _fake_start(monkeypatch, proj: Path, *, fail: bool = False) -> None:
    """Mock the pipeline spawn — hermetic. ``fail`` = a refused launch
    (foreground with a non-zero exit)."""

    def fake_start(project_dir, **kw):
        if fail:
            return api_pipeline.StartResult(
                run_mode="foreground", run_id=None, log_file=None,
                exit_code=1, message="Pipeline completed with exit code 1",
            )
        return api_pipeline.StartResult(
            run_mode="background", run_id=1,
            log_file=str(proj / ".agentic" / "logs" / "awf-start.out"),
            exit_code=0, message="ok",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)


def _raw_write(proj: Path, state: dict) -> None:
    """Write run.yaml exactly as given — an on-disk corruption the
    sanitized reader must survive (the write path cannot produce it)."""
    f = run_state.run_file(proj)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump(state, allow_unicode=True), encoding="utf-8")


def _launch(proj: Path, monkeypatch, queue: list[str]) -> None:
    api.run_start(proj, queue=queue)
    _fake_start(monkeypatch, proj)
    assert api.run_next(proj).action == "started"


def test_launch_snapshots_todo_contract(tmp_git_repo: Path, monkeypatch) -> None:
    """(1) at the launch the launched TODO's contract + file hash land in
    the run record. Red before the fix: today there is no plan in the
    record at all."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T, CONTRACT_TODO)
    sha_before = _file_sha(proj, T)
    _launch(proj, monkeypatch, [T])

    state = run_state.read_run(proj)
    plans = state.get("evidence_plans") or []
    assert plans, "the launch must leave the evidence plan in the run record"
    entry = plans[-1]
    assert entry["todo_id"] == T
    assert entry["verify"] == [VERIFY_CMD]
    assert entry["gates"] == ["contracts", "ratchet", "instructions"]
    assert entry["prove_red"] == [PROVE_ID]
    assert entry["todo_sha"] == sha_before
    assert entry["ts"]


def test_plan_is_a_snapshot_not_a_live_reference(tmp_git_repo: Path, monkeypatch) -> None:
    """(1) the plan survives a LATER edit and a retire of the TODO — it is
    the launch-time snapshot, not a pointer to the (mutable) file."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T, CONTRACT_TODO)
    _write_todo(proj, T2, PLAIN_TODO)
    _launch(proj, monkeypatch, [T, T2])
    plans = run_state.read_run(proj).get("evidence_plans") or []
    assert plans, "the launch must have recorded a plan"
    entry = plans[-1]

    # the TODO is edited after the launch (a replan-style rewrite)
    _write_todo(proj, T, "# Rewritten task\n")
    # and retired (the file leaves the inbox)
    api.retire_todo(proj, T, "replanned")
    assert not _todo_path(proj, T).exists()

    state = run_state.read_run(proj)
    plans = state.get("evidence_plans") or []
    assert len(plans) == 1, "retire does not add or drop the recorded plan"
    assert plans[0]["verify"] == [VERIFY_CMD]
    assert plans[0]["prove_red"] == [PROVE_ID]
    assert plans[0]["todo_sha"] == entry["todo_sha"]  # the launch-time hash


def test_launch_without_contract_records_empty_plan_with_note(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(4) a TODO without a contract block: the plan is recorded anyway —
    empty + a note — so the surfaces can say why there is nothing to show."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T, PLAIN_TODO)
    _launch(proj, monkeypatch, [T])

    plans = run_state.read_run(proj).get("evidence_plans") or []
    assert plans, "the launch must record the (empty) plan anyway"
    entry = plans[-1]
    assert entry["todo_id"] == T
    assert entry["verify"] == []
    assert entry["gates"] == []
    assert entry["prove_red"] == []
    assert entry["todo_sha"] == _file_sha(proj, T)
    assert entry["note"], "the empty plan must carry a note (why it is empty)"


def test_launch_with_broken_contract_degrades_to_note(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(4) a broken contract block (opened, never closed): the launch
    still happens, the plan is recorded with a note — no crash."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T, BROKEN_TODO)
    _launch(proj, monkeypatch, [T])

    state = run_state.read_run(proj)
    assert state["active"] is True
    plans = state.get("evidence_plans") or []
    assert plans, "a broken contract still records the plan (with a note)"
    entry = plans[-1]
    assert entry["verify"] == []
    assert entry["prove_red"] == []
    assert entry["todo_sha"] == _file_sha(proj, T)
    assert entry["note"]


def test_refused_launch_records_no_plan(tmp_git_repo: Path, monkeypatch) -> None:
    """(1) the plan lands in the SAME atomic write as the position commit:
    a refused launch (no advance commit) records nothing."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T, CONTRACT_TODO)
    api.run_start(proj, queue=[T])
    _fake_start(monkeypatch, proj, fail=True)

    result = api.run_next(proj, background=False)

    assert result.action == "refused"
    state = run_state.read_run(proj)
    assert state.get("evidence_plans", []) == []
    assert state["index"] == 0


def test_new_run_resets_plans(tmp_git_repo: Path, monkeypatch) -> None:
    """(1) a fresh run resets the plan (like decisions — M1): a force
    replace must not inherit the previous run's evidence plans."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T, CONTRACT_TODO)
    _launch(proj, monkeypatch, [T])
    assert run_state.read_run(proj).get("evidence_plans"), (
        "the launch must have recorded a plan before the reset"
    )

    api.run_start(proj, queue=[T], force=True)
    assert run_state.read_run(proj).get("evidence_plans", []) == []


class TestSurfaces:
    """(2) brief / load_supervisor_context show the current/last element's
    plan (verify commands + prove_red ids, brief) before the decision."""

    def test_brief_and_context_show_the_plan(self, tmp_git_repo: Path, monkeypatch):
        proj = _project(tmp_git_repo)
        _write_todo(proj, T, CONTRACT_TODO)
        _launch(proj, monkeypatch, [T])

        b = api.brief(proj)
        assert "run evidence plan" in b.text
        assert T in b.text.split("run evidence plan", 1)[1]
        assert VERIFY_CMD in b.text
        assert PROVE_ID in b.text

        c = api.load_supervisor_context(proj)
        assert c.run_evidence_plan is not None
        assert c.run_evidence_plan["todo_id"] == T
        assert c.run_evidence_plan["verify"] == [VERIFY_CMD]
        assert c.run_evidence_plan["prove_red"] == [PROVE_ID]
        assert c.run_evidence_plan["todo_sha"] == _file_sha(proj, T)

    def test_no_run_no_plan_lines(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        b = api.brief(proj)
        c = api.load_supervisor_context(proj)
        assert "run evidence plan" not in b.text
        assert c.run_evidence_plan is None

    def test_current_item_wins_over_last_entry(self, tmp_git_repo: Path) -> None:
        """The reader shows the CURRENT element's snapshot when it is
        recorded; only without one does it fall back to the last entry."""
        proj = _project(tmp_git_repo)
        _raw_write(
            proj,
            {
                "active": True,
                "queue": [{"todo_id": T, "pipeline": ""}, {"todo_id": T2, "pipeline": ""}],
                "index": 1,
                "current": T,
                "evidence_plans": [
                    {"ts": "t1", "todo_id": T, "todo_sha": "a" * 64,
                     "verify": ["cmd1"], "gates": [], "prove_red": [], "note": ""},
                    {"ts": "t2", "todo_id": T2, "todo_sha": "b" * 64,
                     "verify": ["cmd2"], "gates": [], "prove_red": [], "note": ""},
                ],
            },
        )
        record = read_run_record(proj)
        assert record.evidence_plan is not None
        assert record.evidence_plan["todo_id"] == T
        assert record.evidence_plan["todo_sha"] == "a" * 64

    def test_closed_run_keeps_last_elements_plan(self, tmp_git_repo: Path) -> None:
        """A closed run still shows its last element's plan (the record is
        an audit surface after the run, not only a live one)."""
        proj = _project(tmp_git_repo)
        _raw_write(
            proj,
            {
                "active": False,
                "queue": [{"todo_id": T, "pipeline": ""}],
                "index": 1,
                "current": T,
                "stop_reason": "queue exhausted",
                "evidence_plans": [
                    {"ts": "t1", "todo_id": T, "todo_sha": "a" * 64,
                     "verify": ["cmd1"], "gates": ["contracts"],
                     "prove_red": [], "note": ""},
                ],
            },
        )
        b = api.brief(proj)
        assert "run evidence plan" in b.text
        assert "cmd1" in b.text


class TestDegradation:
    """(4)/(5) legacy and broken state: no migration, warnings, no
    traceback (the M1 style)."""

    def test_legacy_run_yaml_reads_empty_plans(self, tmp_git_repo: Path) -> None:
        """A pre-M2.3 run.yaml (no evidence_plans key) reads exactly as
        before — absent is not corruption, no warning."""
        proj = _project(tmp_git_repo)
        _raw_write(
            proj,
            {
                "active": True,
                "queue": [{"todo_id": T, "pipeline": ""}],
                "index": 0,
                "started_at": run_state.now_iso(),
            },
        )
        state = run_state.read_run(proj)
        assert state is not None
        assert state["evidence_plans"] == []
        assert read_run_record(proj).evidence_plan is None
        b = api.brief(proj)
        assert b.run_warning == ""

    def test_broken_plans_value_degrades_to_empty(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        _raw_write(
            proj,
            {
                "active": True,
                "queue": [{"todo_id": T, "pipeline": ""}],
                "evidence_plans": "corrupt",
            },
        )
        state = run_state.read_run(proj)
        assert state is not None
        assert state["evidence_plans"] == []
        # the rest of the run stays usable, both surfaces work
        assert api.run_status(proj).active is True
        assert api.brief(proj).run_goal == ""

    def test_garbage_entries_degrade_entrywise(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        _raw_write(
            proj,
            {
                "active": True,
                "queue": [{"todo_id": T, "pipeline": ""}],
                "evidence_plans": [
                    "garbage",
                    {"no_todo_id": True},
                    {"ts": "t1", "todo_id": T, "todo_sha": "a" * 64,
                     "verify": ["cmd1"], "gates": "not-a-list",
                     "prove_red": [None, "id1"], "note": 7},
                ],
            },
        )
        state = run_state.read_run(proj)
        assert state["evidence_plans"] == [
            {"ts": "t1", "todo_id": T, "todo_sha": "a" * 64,
             "verify": ["cmd1"], "gates": [], "prove_red": ["id1"], "note": "7"},
        ]

    def test_broken_plans_do_not_traceback_on_surfaces(
        self, tmp_git_repo: Path
    ) -> None:
        proj = _project(tmp_git_repo)
        _raw_write(
            proj,
            {
                "active": True,
                "queue": [{"todo_id": T, "pipeline": ""}],
                "evidence_plans": {"not": "a list"},
            },
        )
        b = api.brief(proj)
        c = api.load_supervisor_context(proj)
        assert "run evidence plan" not in b.text
        assert c.run_evidence_plan is None


class TestRejectNextStep:
    """(3) the reject path of next_action: each path leads to exactly ONE
    next action — closing the reject decision (re-plan + retire, or
    awf_run_finish). The old wording said "fix the assignment, then
    awf_run_next" — but the run gate REFUSES that run_next until the
    rejected TODO is retired (test_run_plan_recovery pins the gate), and
    the retire step was missing from the text."""

    def test_after_reject_next_action_names_the_closing(
        self, tmp_git_repo: Path, monkeypatch
    ) -> None:
        proj = _project(tmp_git_repo)
        _write_todo(proj, T, CONTRACT_TODO)
        _write_todo(proj, T2, PLAIN_TODO)
        _launch(proj, monkeypatch, [T, T2])
        api.reject_commit(proj, T, "missing regression test")

        b = api.brief(proj)
        c = api.load_supervisor_context(proj)
        for na in (b.next_action, c.next_action):
            low = na.lower()
            assert "close the reject decision" in low
            assert "awf_todo_retire" in low
            assert "awf_run_finish" in low
            assert "awf_start" not in low
            # the loop after the closing is named, not guessed
            assert "awf_run_next" in low
