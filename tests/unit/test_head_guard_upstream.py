"""A39 (TODO-0350): the HEAD guard does not mistake an upstream landing for
a foreign commit.

Incident: during TODO-0340 the supervisor merged PR #86, local ``main``
fast-forwarded onto ``origin/main``, and A31 stopped the run with
``FOREIGN COMMIT: HEAD moved during stage …``. The move was legitimate.

The shape decides:
* fast-forward onto a remote-tracking ref — upstream sync, the run goes on;
* merge whose first parent is the old HEAD and whose other parent lives in
  a remote-tracking ref — upstream sync too;
* any other move (a local commit, a merge with a foreign first parent) —
  FOREIGN, exactly as before.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import _git_init

from awf import api, head_guard, pipeline_state

UPSTREAM_REF = "refs/remotes/origin/main"


def _git(project: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=project, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _head(project: Path) -> str:
    return _git(project, "rev-parse", "HEAD")


def _commit_tree(project: Path, parents: list[str], message: str) -> str:
    """A commit object without touching the worktree or the index."""
    args = ["commit-tree", f"{parents[0]}^{{tree}}"]
    for parent in parents:
        args += ["-p", parent]
    return _git(project, *args, "-m", message)


def _land_upstream(project: Path, sha: str, ref: str = UPSTREAM_REF) -> None:
    _git(project, "update-ref", ref, sha)


def _log_text(project: Path) -> str:
    log_file = project / ".agentic" / "logs" / "orchestrator.log"
    return log_file.read_text(encoding="utf-8") if log_file.is_file() else ""


def _state(project: Path) -> dict:
    return pipeline_state.read_state(project) or {}


@pytest.fixture
def project(tmp_path) -> Path:
    _git_init(tmp_path)
    api.init_project(tmp_path, project_name="A39")
    return tmp_path


def _fast_forward(project: Path) -> tuple[str, str]:
    """Move local main forward onto origin/main, as ``git pull`` would."""
    before = _head(project)
    upstream = _commit_tree(project, [before], "upstream: PR #86")
    _land_upstream(project, upstream)
    _git(project, "reset", "--hard", upstream)
    return before, upstream


def test_fast_forward_to_origin_is_an_upstream_sync(project: Path) -> None:
    """Acceptance 1: a fast-forward from origin keeps the run alive."""
    before, upstream = _fast_forward(project)

    line = head_guard.check_stage_head(
        project, before, "TODO-0350", "verify", project / ".agentic" / "logs"
    )

    assert line is None, "a fast-forward from origin must not stop the run"
    state = _state(project)
    sync = state.get(head_guard.UPSTREAM_SYNC_KEY) or ""
    assert sync.startswith("UPSTREAM SYNC:")
    assert "fast-forward to refs/remotes/origin/main" in sync
    assert before[:12] in sync and upstream[:12] in sync
    assert not state.get(head_guard.FOREIGN_COMMIT_KEY)
    log_text = _log_text(project).lower()
    assert "upstream sync" in log_text, "the sync must reach the stage log"
    assert "foreign commit" not in log_text


def test_local_commit_on_the_same_tree_stays_foreign(project: Path) -> None:
    """Acceptance 2: the guard did not get weaker — a local commit is FOREIGN."""
    before = _head(project)
    (project / "manual.txt").write_text("manual commit\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "manual: foreign commit")

    line = head_guard.check_stage_head(
        project, before, "TODO-0350", "verify", project / ".agentic" / "logs"
    )

    assert line is not None, "a commit no upstream ref contains is foreign"
    assert line.startswith("FOREIGN COMMIT:")
    assert "manual.txt" in line
    assert _state(project)[head_guard.FOREIGN_COMMIT_KEY] == line
    assert not _state(project).get(head_guard.UPSTREAM_SYNC_KEY)


def test_merge_with_a_foreign_first_parent_stays_foreign(project: Path) -> None:
    """Acceptance 3: a merge whose first parent is NOT the old HEAD is foreign.

    The branch under the engine's feet moved: even though the second parent
    is a real upstream commit, the merge was not "old HEAD + upstream".
    """
    before = _head(project)
    upstream = _commit_tree(project, [before], "upstream: PR #86")
    _land_upstream(project, upstream)
    foreign = _commit_tree(project, [before], "local: commit behind the gate")
    merge = _commit_tree(project, [foreign, upstream], "merge")
    _git(project, "reset", "--hard", merge)

    line = head_guard.check_stage_head(
        project, before, "TODO-0350", "verify", project / ".agentic" / "logs"
    )

    assert line is not None
    assert line.startswith("FOREIGN COMMIT:")
    assert _state(project)[head_guard.FOREIGN_COMMIT_KEY] == line
    assert not _state(project).get(head_guard.UPSTREAM_SYNC_KEY)


def test_merge_of_upstream_onto_the_stage_head_is_a_sync(project: Path) -> None:
    """The other legitimate shape named in the unit: old HEAD plus upstream."""
    before = _head(project)
    upstream = _commit_tree(project, [before], "upstream: PR #86")
    _land_upstream(project, upstream)
    merge = _commit_tree(project, [before, upstream], "Merge origin/main")
    _git(project, "reset", "--hard", merge)

    line = head_guard.check_stage_head(
        project, before, "TODO-0350", "verify", project / ".agentic" / "logs"
    )

    assert line is None
    assert "merge of refs/remotes/origin/main" in _state(project)[
        head_guard.UPSTREAM_SYNC_KEY
    ]


def test_upstream_sync_then_gate_commit_in_one_stage_is_a_sync(project: Path) -> None:
    """The incident's real shape: the supervisor pulls, then approves.

    The gate commits on top of the synced HEAD inside the SAME stage, so its
    recorded parent is the upstream tip, not the stage's HEAD. The move must
    still read as a sync — otherwise the incident repeats one commit later.
    """
    before = _head(project)
    upstream = _commit_tree(project, [before], "upstream: PR #86")
    _land_upstream(project, upstream)
    _git(project, "reset", "--hard", upstream)
    _git(project, "commit", "-qm", "awf(supervisor): TODO-0350", "--allow-empty")
    gate = _head(project)
    head_guard.record_gate_commit(project, gate, project / ".agentic" / "logs")

    line = head_guard.check_stage_head(
        project, before, "TODO-0350", "supervisor", project / ".agentic" / "logs"
    )

    assert line is None, "the gate's own commit on a synced HEAD is not foreign"
    sync = _state(project).get(head_guard.UPSTREAM_SYNC_KEY) or ""
    assert "fast-forward to refs/remotes/origin/main" in sync
    assert f"gate committed {gate[:12]}" in sync
    assert not _state(project).get(head_guard.FOREIGN_COMMIT_KEY)


def test_upstream_sync_is_cleared_by_the_next_clean_stage(project: Path) -> None:
    """The informational line must not outlive the boundary that proved clean."""
    before, upstream = _fast_forward(project)
    assert head_guard.check_stage_head(
        project, before, "TODO-0350", "verify", project / ".agentic" / "logs"
    ) is None
    assert _state(project).get(head_guard.UPSTREAM_SYNC_KEY)

    head_guard.reset_stage_markers(project, project / ".agentic" / "logs")
    assert head_guard.check_stage_head(
        project, upstream, "TODO-0350", "qa-review", project / ".agentic" / "logs"
    ) is None
    assert not _state(project).get(head_guard.UPSTREAM_SYNC_KEY), (
        "a stale 'the run continues' line must not stay in awf status"
    )


def test_origin_head_alias_is_not_named_instead_of_the_branch(project: Path) -> None:
    """``origin/HEAD`` is a symbolic alias and sorts first; name the branch."""
    _git(project, "symbolic-ref", "refs/remotes/origin/HEAD", UPSTREAM_REF)
    before, _ = _fast_forward(project)

    assert head_guard.check_stage_head(
        project, before, "TODO-0350", "verify", project / ".agentic" / "logs"
    ) is None
    sync = _state(project).get(head_guard.UPSTREAM_SYNC_KEY) or ""
    assert "fast-forward to refs/remotes/origin/main" in sync


def test_local_branch_is_not_an_upstream(project: Path) -> None:
    """A ref outside ``refs/remotes/origin/`` is not a known upstream."""
    before = _head(project)
    fake = _commit_tree(project, [before], "local branch tip")
    _git(project, "update-ref", "refs/heads/upstream-lookalike", fake)
    _git(project, "reset", "--hard", fake)

    line = head_guard.check_stage_head(
        project, before, "TODO-0350", "verify", project / ".agentic" / "logs"
    )

    assert line is not None, "only a remote-tracking ref counts as upstream"
    assert line.startswith("FOREIGN COMMIT:")


def test_status_names_the_cause(project: Path, capsys) -> None:
    """Acceptance: ``awf status`` says ``upstream_sync``, not FOREIGN."""
    from awf import cli

    before, _ = _fast_forward(project)
    assert head_guard.check_stage_head(
        project, before, "TODO-0350", "verify", project / ".agentic" / "logs"
    ) is None

    status = api.get_status(project)
    assert status.upstream_sync and status.upstream_sync.startswith("UPSTREAM SYNC:")
    assert status.foreign_commit is None

    capsys.readouterr()
    rc = cli.main(["status", "--project-dir", str(project)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert status.upstream_sync in out
    assert "FOREIGN COMMIT" not in out


# --- pipeline level: the incident end to end -------------------------------

def _pipeline(project: Path, stages_yaml: str) -> None:
    pipes = project / ".agentic" / "pipelines"
    pipes.mkdir(parents=True, exist_ok=True)
    (pipes / "default.yaml").write_text(
        'name: "default"\nstages:\n' + stages_yaml, encoding="utf-8"
    )


def _todo(project: Path, todo_id: str) -> str:
    inbox = project / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\ntask\n", encoding="utf-8")
    (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")
    baseline = _head(project)
    ctx = project / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"BASELINE-{todo_id}.sha").write_text(baseline + "\n", encoding="utf-8")
    return baseline


def _args(project: Path):
    from types import SimpleNamespace

    return SimpleNamespace(
        project_dir=str(project), pipeline=None, from_stage=None, auto=True, timeout=None,
    )


def test_pipeline_survives_the_incident_shape(project: Path, monkeypatch, capsys) -> None:
    """The incident itself: the pull and the gate commit land in ONE stage.

    ``run_pipeline`` is the contract that matters — ``check_stage_head``
    returning ``None`` is only half of it; the orchestrator's ``return 1``
    and the real gate (short SHA, recorded parent) are the other half.
    """
    from awf import commit_gate, pipeline_engine
    from awf.orchestrator import run_pipeline

    (project / ".agentic" / "roles").mkdir(parents=True, exist_ok=True)
    (project / ".agentic" / "roles" / "worker.md").write_text(
        "# Worker\nExecute TODO.\n", encoding="utf-8"
    )
    _pipeline(
        project,
        '  - name: "plan"\n    role: "supervisor"\n'
        '  - name: "implement"\n    role: "worker"\n',
    )
    baseline = _todo(project, "TODO-0350")
    seen: list[str] = []

    def mock_supervisor(stage, todo_id, auto, project_dir, logs_dir, pipeline_name=None):
        seen.append(stage.name)
        return ""

    def mock_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
        seen.append(stage.name)
        _fast_forward(project_dir)  # the supervisor landed origin/main
        (project_dir / "unit.txt").write_text("unit work\n", encoding="utf-8")
        outcome = commit_gate.maybe_commit(
            stage.name, todo_id, "commit_and_report", project_dir, logs_dir,
            auto=False, baseline_sha=baseline,
        )
        assert outcome.status == "committed", outcome.reason
        outbox = project_dir / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / f"DONE-{todo_id}.md").write_text("# Done\n", encoding="utf-8")
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", mock_supervisor)
    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)

    rc = run_pipeline(_args(project))
    err = capsys.readouterr().err

    assert rc == 0, err
    assert seen == ["plan", "implement"]
    assert "FOREIGN COMMIT" not in err
    assert "UPSTREAM SYNC" in err
    # The successful run clears its state file, so the surviving evidence is
    # the stage log — the same line ``awf status`` showed while the run lived.
    logged = _log_text(project)
    assert "UPSTREAM SYNC" in logged
    assert "fast-forward to refs/remotes/origin/main" in logged
    assert "the gate committed" in logged, "the gate's own commit is named, not hidden"
    assert "FOREIGN COMMIT" not in logged


def test_foreign_after_a_sync_replaces_the_sync_line(project: Path) -> None:
    """The two causes do not coexist in ``awf status``.

    A sync is an informational fact; a later foreign commit is a stop. Left
    together, the card would read "the run continues" next to "the run
    stops".
    """
    before, upstream = _fast_forward(project)
    assert head_guard.check_stage_head(
        project, before, "TODO-0350", "verify", project / ".agentic" / "logs"
    ) is None
    assert _state(project).get(head_guard.UPSTREAM_SYNC_KEY)

    _git(project, "commit", "-q", "--allow-empty", "-m", "manual: behind the gate")
    line = head_guard.check_stage_head(
        project, upstream, "TODO-0350", "implement", project / ".agentic" / "logs"
    )

    assert line is not None and line.startswith("FOREIGN COMMIT:")
    state = _state(project)
    assert state.get(head_guard.FOREIGN_COMMIT_KEY) == line
    assert not state.get(head_guard.UPSTREAM_SYNC_KEY)
