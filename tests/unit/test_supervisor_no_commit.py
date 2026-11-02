"""A31 (TODO-0339): the supervisor role never commits; the engine notices a
foreign commit.

Two halves of one incident (TODO-0323):
* the role template told the supervisor to ``git add -A && git commit`` in
  auto mode — a commit outside the engine's commit gate;
* the engine never looked at HEAD, so the bypass ran on silently.

Covers:
1. the role text forbids git commit/push in auto mode and tells the
   supervisor what to do when the gate refused;
2. a stage that commits on its own stops the run with a loud error naming
   the SHA and the files;
3. a commit recorded by the gate is NOT foreign — the run continues.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import _git_init

from awf import api, head_guard, pipeline_engine, pipeline_state
from awf.orchestrator import run_pipeline


def _normalized(text: str) -> str:
    return " ".join(text.lower().split())


ROLE_FILES = [
    Path("awf/templates/roles/supervisor.md"),
    Path("awf/templates/roles/supervisor/_core.md"),
    Path("awf/templates/roles/supervisor/phase-verify.md"),
]


@pytest.mark.parametrize("rel", ROLE_FILES)
def test_supervisor_role_forbids_git_commit(rel: Path) -> None:
    """The template states the prohibition, not just its absence."""
    text = _normalized(rel.read_text(encoding="utf-8"))

    assert "git commit" in text, f"{rel}: the role must NAME git commit"
    assert "never" in text, f"{rel}: the prohibition must be explicit"
    # No instruction that tells the role to run the commit itself.
    assert "git commit -m" not in text, f"{rel}: no commit recipe in the role"
    assert "git push origin head" not in text, f"{rel}: no push recipe"


def test_supervisor_role_gate_refusal_rule() -> None:
    """A refused gate is a REVIEW with the reason — never a manual commit."""
    text = _normalized(Path("awf/templates/roles/supervisor.md").read_text(encoding="utf-8"))

    assert "gate refused" in text or "refused gate" in text
    assert "review" in text
    assert "do not" in text


def test_runtime_snippets_forbid_commit() -> None:
    """The snippets the supervisor actually receives say the same thing.

    The templates are not the whole prompt: ``awf/supervisor.py`` appends
    these stage snippets at the END of it, and they used to say "the commit
    is yours" — the drift behind the TODO-0323 incident.
    """
    from awf.supervisor import _SNIPPET_ALWAYS, _SNIPPET_SALVAGE, _SNIPPET_VERIFY

    for snippet in (_SNIPPET_ALWAYS, _SNIPPET_VERIFY, _SNIPPET_SALVAGE):
        text = _normalized(snippet)
        assert "git commit" in text, "the snippet must NAME the forbidden command"
        assert "never" in text, "the prohibition must be explicit"
        assert "commit is yours" not in text
        assert "push is always" not in text


def _make_pipeline(project: Path, stages_yaml: str) -> None:
    pipes = project / ".agentic" / "pipelines"
    pipes.mkdir(parents=True, exist_ok=True)
    (pipes / "default.yaml").write_text(
        'name: "default"\nstages:\n' + stages_yaml, encoding="utf-8"
    )


def _create_todo(project: Path, todo_id: str) -> None:
    inbox = project / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\ntask\n", encoding="utf-8")
    (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True,
    ).stdout.strip()
    ctx = project / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"BASELINE-{todo_id}.sha").write_text(sha + "\n", encoding="utf-8")


def _make_args(project: Path) -> SimpleNamespace:
    return SimpleNamespace(
        project_dir=str(project),
        pipeline=None,
        from_stage=None,
        auto=True,
        timeout=None,
    )


@pytest.fixture
def project(tmp_git_repo):
    repo = tmp_git_repo
    api.init_project(repo, project_name="A31")
    (repo / ".agentic" / "roles" / "worker.md").write_text(
        "# Worker\nExecute TODO.\n", encoding="utf-8"
    )
    return repo


def _commit_foreign(project: Path, name: str = "foreign.txt") -> str:
    (project / name).write_text("manual commit\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "manual: foreign commit"], cwd=project, check=True
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_foreign_commit_stops_the_run(project, monkeypatch, capsys):
    """A fake stage commits: the engine reports SHA + files and stops before
    the next stage."""
    _make_pipeline(
        project,
        '  - name: "plan"\n    role: "supervisor"\n'
        '  - name: "implement"\n    role: "worker"\n'
        '  - name: "verify"\n    role: "supervisor"\n',
    )
    _create_todo(project, "TODO-0001")

    seen: list[str] = []

    def mock_supervisor(stage, todo_id, auto, project_dir, logs_dir, pipeline_name=None):
        seen.append(stage.name)
        return ""

    def mock_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
        _commit_foreign(project)
        outbox = project_dir / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / f"DONE-{todo_id}.md").write_text("# Done\n", encoding="utf-8")
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", mock_supervisor)
    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)

    rc = run_pipeline(_make_args(project))

    assert rc == 1, "a foreign commit must stop the run"
    assert seen == ["plan"], "the verify stage must NOT run after the drift"

    line = pipeline_state.read_state(project)["foreign_commit"]
    assert line.startswith("FOREIGN COMMIT:")
    assert "foreign.txt" in line
    assert "not made by the commit gate" in line
    assert "ERROR: FOREIGN COMMIT:" in capsys.readouterr().err
    # Acceptance: the fact also reaches the stage log, not only stderr.
    stage_log = project / ".agentic" / "logs" / "orchestrator.log"
    assert line in stage_log.read_text(encoding="utf-8")


def test_gate_commit_is_not_foreign(project, monkeypatch, capsys):
    """A commit the REAL gate made before the stage ended is the normal path.

    QA A31: the first version of this test called ``record_gate_commit`` by
    hand with a full SHA from ``git rev-parse`` — it exercised the function,
    not the production sequence, and stayed green while every real commit
    was flagged FOREIGN (the gate's outcome SHA is the short form). Only the
    real gate inside the stage proves the invariant end to end.
    """
    from awf import commit_gate

    _make_pipeline(
        project,
        '  - name: "plan"\n    role: "supervisor"\n'
        '  - name: "implement"\n    role: "worker"\n',
    )
    _create_todo(project, "TODO-0001")
    baseline = (
        project / ".agentic" / "context" / "BASELINE-TODO-0001.sha"
    ).read_text(encoding="utf-8").strip()

    def mock_supervisor(stage, todo_id, auto, project_dir, logs_dir, pipeline_name=None):
        return ""

    def mock_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
        (project_dir / "gate.txt").write_text("unit work\n", encoding="utf-8")
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

    rc = run_pipeline(_make_args(project))
    err = capsys.readouterr().err

    assert rc == 0, err
    assert "FOREIGN COMMIT" not in err


def test_head_guard_accepts_gate_commit_and_flags_manual_one(tmp_path):
    """The pure check: a commit the gate did not record is foreign; a commit
    the gate recorded on the stage's HEAD passes."""
    _git_init(tmp_path)
    before = head_guard.head_sha(tmp_path)

    sha = _commit_foreign(tmp_path)
    line = head_guard.check_stage_head(
        tmp_path, before, "TODO-0001", "implement", tmp_path
    )
    assert line is not None, "an unrecorded commit is foreign"
    assert sha[:12] in line and "foreign.txt" in line

    head_guard.reset_stage_markers(tmp_path)
    before2 = head_guard.head_sha(tmp_path)
    sha2 = _commit_foreign(tmp_path, "second.txt")
    head_guard.record_gate_commit(tmp_path, sha2)
    assert head_guard.check_stage_head(
        tmp_path, before2, "TODO-0001", "implement", tmp_path
    ) is None, "the gate's own commit is not foreign"


def test_foreign_line_survives_resume_until_a_clean_check(tmp_path):
    """The run-stopping evidence is not erased by the next stage start.

    ``awf continue`` begins a new stage, and the previous design cleared the
    FOREIGN COMMIT line there — the reason vanished before the resumed stage
    even ran. It must survive until a boundary comes back clean.
    """
    _git_init(tmp_path)
    before = head_guard.head_sha(tmp_path)
    _commit_foreign(tmp_path)

    line = head_guard.check_stage_head(
        tmp_path, before, "TODO-0001", "implement", tmp_path
    )
    assert line is not None

    head_guard.reset_stage_markers(tmp_path)
    assert (pipeline_state.read_state(tmp_path) or {}).get("foreign_commit") == line

    after = head_guard.head_sha(tmp_path)
    assert head_guard.check_stage_head(
        tmp_path, after, "TODO-0001", "verify", tmp_path
    ) is None, "a clean boundary"
    assert not (pipeline_state.read_state(tmp_path) or {}).get("foreign_commit")


def test_real_gate_commit_is_not_foreign(project) -> None:
    """The PRODUCTION sequence, not a fake recorder.

    ``maybe_commit`` records its own outcome SHA through
    ``head_guard.record_gate_commit`` in ``_finalize``. The drift check then
    compares that stored value with ``HEAD``. The gate's outcome SHA is the
    SHORT form (``publish._short``: what the user sees in "Auto-committed …
    (abc1234)"), while ``head_sha`` returns the full 40-char OID — a
    comparison without one form turns every legitimate commit into a
    FOREIGN COMMIT and stops the run.
    """
    from awf import commit_gate

    _create_todo(project, "TODO-0001")
    baseline = (
        project / ".agentic" / "context" / "BASELINE-TODO-0001.sha"
    ).read_text(encoding="utf-8").strip()
    (project / "work.txt").write_text("unit work\n", encoding="utf-8")
    logs = project / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    before = head_guard.head_sha(project)

    outcome = commit_gate.maybe_commit(
        "verify", "TODO-0001", "commit_and_report", project, logs,
        auto=False, baseline_sha=baseline,
    )

    assert outcome.status == "committed", outcome.reason
    line = head_guard.check_stage_head(
        project, before, "TODO-0001", "verify", logs
    )
    assert line is None, (
        "the gate's own commit must never read as foreign — the recorded "
        f"SHA and HEAD must be compared in one form: {line}"
    )


def test_status_surfaces_the_foreign_commit_line(project) -> None:
    """Acceptance: the fact reaches ``awf status`` (human line + JSON field),
    not only the run log — a silent stop was the incident's other half."""
    before = head_guard.head_sha(project)
    _commit_foreign(project, "manual.txt")

    line = head_guard.check_stage_head(
        project, before, "TODO-0001", "implement", project / ".agentic" / "logs"
    )
    assert line is not None

    status = api.get_status(project)
    assert status.foreign_commit == line


def test_foreign_commit_drives_the_next_action(project) -> None:
    """The advice owner must not send the owner to ``awf_continue``.

    After the stop the engine-death branch would answer "resume with
    awf_continue" — the exact step that lets the next clean stage boundary
    erase the FOREIGN evidence. The fact outranks it and names the recovery.
    """
    from awf.brief import _NEXT_ACTION_MAX_WORDS, _actionable_fact, next_action_from_status

    before = head_guard.head_sha(project)
    _commit_foreign(project, "manual.txt")
    head_guard.check_stage_head(
        project, before, "TODO-0001", "implement", project / ".agentic" / "logs"
    )

    status = api.get_status(project)
    advice = next_action_from_status(status)

    assert "FOREIGN COMMIT" in advice
    assert "git log" in advice
    assert "resume with" not in advice, (
        f"the resume advice buries the foreign commit: {advice}"
    )
    # The card clips ``next:`` at this budget. Evidence (SHAs + every file)
    # in front of the action would cut the recovery sentence off.
    assert len(advice.split()) <= _NEXT_ACTION_MAX_WORDS, (
        f"the card would clip the recovery out of the advice: {advice}"
    )
    assert _actionable_fact(status), (
        "the fact must gate the setup chain, not fall through to it"
    )


def test_wait_event_names_the_foreign_commit(project) -> None:
    """The supervisor's wake-up surface must not answer "resume" either.

    ``awf_wait_for_event`` has its own engine-death answer next to the
    status advice; without a branch there it keeps pointing at
    ``awf_continue`` and the cause never reaches the session that acts.
    """
    from awf.api import wait_event

    before = head_guard.head_sha(project)
    _commit_foreign(project, "manual.txt")
    head_guard.check_stage_head(
        project, before, "TODO-0001", "implement", project / ".agentic" / "logs"
    )

    state = dict(pipeline_state.read_state(project) or {})
    state.update(
        {"stage_name": "implement", "todo_id": "TODO-0001", "pipeline_pid": 999_999}
    )
    event = wait_event._engine_dead_event(state, project)

    assert event is not None
    assert "FOREIGN COMMIT" in event.message
    assert "awf_continue(todo_id" not in (event.next_action or "")


def test_cli_status_prints_the_foreign_commit_line(project, capsys) -> None:
    """Acceptance: the line reaches the ``awf status`` entry point itself —
    the surface the owner reads first, not only the dataclass field."""
    from awf import cli

    before = head_guard.head_sha(project)
    _commit_foreign(project, "manual.txt")
    line = head_guard.check_stage_head(
        project, before, "TODO-0001", "implement", project / ".agentic" / "logs"
    )
    assert line is not None
    capsys.readouterr()

    rc = cli.main(["status", "--project-dir", str(project)])

    out = capsys.readouterr().out
    assert rc == 0, out
    assert line in out
