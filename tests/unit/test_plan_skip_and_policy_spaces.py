"""TODO-0352 (A30/A36): the plan does not drift from the diff, and a policy
target with a space after the colon works.

A30 — ``plan-skip-inconsistent`` on a dispatched unit: the plan's file set
is read against the unit's baseline while the gate's diff is read against
HEAD. When they disagree, the empty-plan path used to write a no-code
receipt and archive the unit WITHOUT its work ("no changes to commit"),
and the non-empty path refused with a message the supervisor could not act
on. Both now name the stale plan and ask for a rebuild.

A36 — ``on_rejected: "fix_round: agent-implementer"`` passed validation (the
target was non-empty) and failed at run time: the resolver returned the
target WITH the leading space and ``_handle_fix_round`` said "not found".
The target is now parsed and normalized in ONE place.
"""
from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest

from awf import api, commit_gate, commit_plan
from awf.api._errors import AwfApiError
from awf.pipeline import (
    Stage,
    load_stages,
    normalize_policy,
    policy_target,
    validate_pipeline_stages,
)
from awf.transitions import resolve_transition

TODO = "TODO-0001"


# ─── fixtures / helpers ─────────────────────────────────────────────────


@pytest.fixture
def project(tmp_git_repo: Path) -> Path:
    """An initialized awf project with a committed ``.agentic/``."""
    api.init_project(
        tmp_git_repo,
        project_name="PlanSkipPolicy",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    _git(tmp_git_repo, "add", "-A")
    _git(tmp_git_repo, "commit", "-qm", "awf init")
    return tmp_git_repo


def _git(project: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=project, check=True, capture_output=True, text=True
    )


def _logs_dir(project: Path) -> Path:
    logs = project / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs


def _dispatch(project: Path):
    return api.dispatch_todo(project, f"# {TODO}\nTask\n", todo_id=TODO)


def _receipt(project: Path) -> Path:
    return project / ".agentic" / "context" / f"RECEIPT-{TODO}.json"


def _stages(**review_policy: str) -> list[dict[str, object]]:
    review: dict[str, object] = {
        "id": "agent-qa-review",
        "role": "agent-qa-review",
    }
    review.update(review_policy)
    return [
        {"id": "plan", "role": "supervisor"},
        {"id": "agent-implementer", "role": "agent-implementer"},
        review,
        {"id": "verify", "role": "supervisor"},
    ]


# ─── A36: policy targets with spaces ────────────────────────────────────


def test_policy_target_is_parsed_once_and_stripped():
    assert policy_target("fix_round: agent-implementer") == "agent-implementer"
    assert policy_target("rollback_to:\tagent-implementer ") == "agent-implementer"
    assert policy_target("escalate") == ""
    assert normalize_policy("fix_round: agent-implementer") == (
        "fix_round:agent-implementer"
    )
    assert normalize_policy("rollback_to: develop") == "rollback_to:develop"
    assert normalize_policy("escalate") == "escalate"


def test_fix_round_with_space_loads_normalized_and_resolves():
    validated = validate_pipeline_stages(
        _stages(on_rejected="fix_round: agent-implementer"), "test"
    )
    assert validated[2]["on_rejected"] == "fix_round:agent-implementer"

    stage = Stage(name="agent-qa-review", role="agent-qa-review",
                  on_rejected="fix_round: agent-implementer")
    assert resolve_transition(stage, "rejected") == (
        "fix_round",
        "agent-implementer",
    )


def test_rollback_to_with_space_loads_normalized_and_resolves():
    validated = validate_pipeline_stages(
        _stages(on_blocked="rollback_to: agent-implementer"), "test"
    )
    assert validated[2]["on_blocked"] == "rollback_to:agent-implementer"

    stage = Stage(name="agent-qa-review", role="agent-qa-review",
                  on_blocked="rollback_to: agent-implementer")
    assert resolve_transition(stage, "blocked") == (
        "rollback",
        "agent-implementer",
    )


def test_fix_round_with_space_works_through_the_yaml_loader(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".agentic" / "pipelines").mkdir(parents=True)
    path = proj / ".agentic" / "pipelines" / "spaced.yaml"
    path.write_text(
        "name: spaced\n"
        "stages:\n"
        "  - id: plan\n"
        "    role: supervisor\n"
        "  - id: implement\n"
        "    role: agent-implementer\n"
        "  - id: review\n"
        "    role: agent-qa-review\n"
        "    on_rejected: 'fix_round: implement'\n"
        "  - id: verify\n"
        "    role: supervisor\n",
        encoding="utf-8",
    )

    stages = load_stages(path)
    by_name = {stage.name: stage for stage in stages}
    assert by_name["review"].on_rejected == "fix_round:implement"
    assert resolve_transition(by_name["review"], "rejected") == (
        "fix_round",
        "implement",
    )


def test_a_typo_target_is_refused_at_load_with_the_name():
    with pytest.raises(AwfApiError) as caught:
        validate_pipeline_stages(
            _stages(on_rejected="fix_round: agent-implementr"), "test"
        )
    text = str(caught.value)
    assert "agent-implementr" in text
    assert "not found in the pipeline" in text


def test_a_space_only_target_is_still_a_configuration_error():
    with pytest.raises(AwfApiError, match="fix_round needs a stage name"):
        validate_pipeline_stages(_stages(on_rejected="fix_round:  "), "test")


# ─── A30: the plan does not drift from the diff ─────────────────────────


def test_empty_plan_over_a_dirty_tree_is_a_stale_plan(project: Path):
    """The unit's work in the tree, the plan empty → refuse, never skip."""
    _dispatch(project)
    (project / "work.py").write_text("work\n", encoding="utf-8")

    plan = commit_plan.build_commit_plan(
        project, TODO, (project / ".agentic" / "context" / f"BASELINE-{TODO}.sha")
        .read_text(encoding="utf-8").strip(),
    )
    emptied = dataclasses.replace(plan, files=())

    refusal = commit_plan.plan_stale_refusal(emptied, project)

    assert refusal.startswith(commit_plan.PLAN_STALE_PREFIX), refusal
    assert "work.py" in refusal
    assert "plan is stale" in refusal
    assert "rebuild it" in refusal


def test_gate_refuses_the_stale_plan_instead_of_skipping(project: Path, monkeypatch):
    """The empty plan + a dirty tree must not write a no-code receipt."""
    dispatch = _dispatch(project)
    (project / "work.py").write_text("work\n", encoding="utf-8")
    # Evidence exists, so the no-code receipt WOULD be recorded by the
    # "no changes to commit" path — this test proves that path is closed.
    (project / ".agentic" / "outbox").mkdir(parents=True, exist_ok=True)
    (project / ".agentic" / "outbox" / f"DONE-{TODO}.md").write_text(
        "done\n", encoding="utf-8"
    )

    real_build = commit_plan.build_commit_plan

    def empty_plan(*args, **kwargs):
        return dataclasses.replace(real_build(*args, **kwargs), files=())

    monkeypatch.setattr(commit_gate.commit_plan, "build_commit_plan", empty_plan)

    outcome = commit_gate.maybe_commit(
        "verify", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )

    assert outcome.status == "refused", outcome
    assert outcome.reason.startswith(commit_plan.PLAN_STALE_PREFIX)
    assert not _receipt(project).exists(), (
        "a stale plan must not be laundered into a no-code receipt"
    )


def test_a_genuinely_empty_unit_still_skips_cleanly(project: Path):
    """No work at all → the honest "no changes to commit" skip stays."""
    dispatch = _dispatch(project)

    outcome = commit_gate.maybe_commit(
        "verify", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )

    assert outcome.status == "skipped", outcome
    assert outcome.reason == commit_plan.NO_CHANGES_REASON


def test_pre_existing_dirt_does_not_make_the_plan_stale(project: Path):
    """Dirty at dispatch → an empty plan is honest, not stale."""
    (project / "core.py").write_text("core v1\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    (project / "core.py").write_text("core v2 WIP\n", encoding="utf-8")
    dispatch = _dispatch(project)

    empty = commit_plan.CommitPlan(
        todo_id=TODO,
        generation=0,
        verified_sha="",
        files=(),
        baseline_sha=dispatch.baseline_sha,
    )

    assert commit_plan.plan_stale_refusal(empty, project) == ""


def test_a_definition_edit_is_the_units_work_not_a_runtime_artifact(project: Path):
    """``.agentic/`` DEFINITIONS are committed files (paths.RUNTIME_DIRS says
    so). Only the runtime dirs are engine artifacts — a pipeline edit must
    not be filtered out of the divergence check and skipped as "no code"."""
    dispatch = _dispatch(project)
    pipeline = project / ".agentic" / "pipelines" / "extra.yaml"
    pipeline.write_text("name: extra\nstages: []\n", encoding="utf-8")

    empty = commit_plan.CommitPlan(
        todo_id=TODO,
        generation=0,
        verified_sha="",
        files=(),
        baseline_sha=dispatch.baseline_sha,
    )

    refusal = commit_plan.plan_stale_refusal(empty, project)
    assert refusal.startswith(commit_plan.PLAN_STALE_PREFIX), refusal
    assert ".agentic/pipelines/extra.yaml" in refusal


def test_a_runtime_artifact_alone_is_not_a_stale_plan(project: Path):
    """The engine's own artifacts are not the unit's work (write fence)."""
    dispatch = _dispatch(project)
    (project / ".agentic" / "context" / "PACK-note.txt").write_text(
        "engine artifact\n", encoding="utf-8"
    )

    outcome = commit_gate.maybe_commit(
        "verify", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )

    assert outcome.status == "skipped", outcome
    assert outcome.reason == commit_plan.NO_CHANGES_REASON


def test_pre_existing_untracked_does_not_make_the_plan_stale(project: Path):
    """Dispatch over a stray untracked file, no unit work → honest skip.

    The plan itself leaves the file outside with the reason "pre-existing
    untracked"; the divergence check must read the same baseline fact
    instead of treating every untracked file as the unit's work."""
    (project / "own.txt").write_text("user wip\n", encoding="utf-8")
    dispatch = _dispatch(project)

    outcome = commit_gate.maybe_commit(
        "verify", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )

    assert outcome.status == "skipped", outcome
    assert outcome.reason == commit_plan.NO_CHANGES_REASON


def test_pre_existing_dirt_does_not_hide_new_work(project: Path):
    """A dirty tree at dispatch must not switch the divergence check off.

    The tracked dirt explains an empty plan for THAT file, not for a file
    the unit created after the baseline — the A30 silent skip would come
    back for every unit dispatched over unfinished work."""
    (project / "core.py").write_text("core v1\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    (project / "core.py").write_text("core v2 WIP\n", encoding="utf-8")
    dispatch = _dispatch(project)
    (project / "work.py").write_text("unit work\n", encoding="utf-8")

    empty = commit_plan.CommitPlan(
        todo_id=TODO,
        generation=0,
        verified_sha="",
        files=(),
        baseline_sha=dispatch.baseline_sha,
    )

    refusal = commit_plan.plan_stale_refusal(empty, project)
    assert refusal.startswith(commit_plan.PLAN_STALE_PREFIX), refusal
    assert "work.py" in refusal
    assert "core.py" not in refusal, (
        "the baseline's own dirt is not the unit's work"
    )


def test_the_write_path_persists_the_normalized_policy(project: Path):
    """The space is a spelling: what lands on disk is the canonical form."""
    api.write_pipeline(
        project,
        "spaced",
        _stages(on_rejected="fix_round: agent-implementer"),
        force=True,
    )

    text = (project / ".agentic" / "pipelines" / "spaced.yaml").read_text(
        encoding="utf-8"
    )
    assert "fix_round:agent-implementer" in text
    assert "fix_round: agent-implementer" not in text


def test_the_validator_normalizes_the_returned_document_only():
    """The write path gets the normalized value; the caller's mapping is not
    rewritten under it (the setup preview re-renders the same dicts)."""
    stages = _stages(on_rejected="fix_round: agent-implementer")

    validated = validate_pipeline_stages(stages, "test")

    assert validated[2]["on_rejected"] == "fix_round:agent-implementer"
    assert stages[2]["on_rejected"] == "fix_round: agent-implementer"


def test_nonempty_plan_matching_head_says_the_plan_is_stale(project: Path):
    """The TODO-0323 shape: the unit's work is already in HEAD."""
    dispatch = _dispatch(project)
    (project / "work.py").write_text("work\n", encoding="utf-8")
    _git(project, "add", "work.py")
    _git(project, "commit", "-qm", "fix: work done outside the unit")

    outcome = commit_gate.maybe_commit(
        "verify", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )

    assert outcome.status == "refused", outcome
    assert "plan-skip-inconsistent" in outcome.reason
    assert "the plan is stale, rebuild it" in outcome.reason
