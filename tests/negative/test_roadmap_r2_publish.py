"""R2.5 (roadmap R2): публикация и архив — возобновляемая операция.

На базовом дереве файл КРАСНЫЙ: модуля ``awf.publish`` нет (ленивый
импорт, падает утверждением «модуля нет», а не сбор).

Приёмка §8 (crash-матрица): crash после baseline / commit object /
update-ref / index sync / archive — повтор НЕ создаёт второй commit и
НЕ теряет артефакты; crash после ref-update до receipt распознаётся по
конкретному OID + intent, не по substring commit message.
Приёмка §4: пользовательский staged file, изменённый во время commit,
сохранён; исходный index при отказе прежний; disposition —
``committed_with_reconciliation_required``, не повторный commit.
Приёмка §10: два проекта с одним TODO-ID не делят baseline/receipt.
No-code unit: валидный исход с evidence, без искусственного пустого commit.

Все репозитории — disposable в ``tmp_path``. Реальный git не трогается.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from awf import commit_gate, commit_plan

TODO = "TODO-0001"


def _publish():
    """Ленивый импорт: на базовом дереве модуля нет. Провал — утверждение,
    а не import-ошибка (настоящий красный, не broken-runner)."""
    try:
        from awf import publish
    except ImportError as e:
        raise AssertionError(f"R2.5 publish module is missing: {e}") from e
    return publish


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, capture_output=True, text=True
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD").stdout.strip())


def _blob_at(repo: Path, rev: str, path: str) -> str:
    return _git(repo, "show", f"{rev}:{path}").stdout


def _plan(files: tuple[str, ...], todo: str = TODO) -> commit_plan.CommitPlan:
    return commit_plan.CommitPlan(
        todo_id=todo, generation=0, verified_sha="", files=files
    )


def _logs(repo: Path) -> Path:
    d = repo / ".agentic" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _commit_unit(repo: Path, files: tuple[str, ...], crash_at: str = ""):
    """Stage + publish a unit commit through the gate's isolated index.

    Returns the outcome, or raises ``publish.PublishCrash`` at ``crash_at``.
    """
    publish = _publish()
    return commit_gate._commit_via_isolated_index(
        repo, _plan(files), f"awf(verify): {TODO}", _logs(repo), crash_at=crash_at
    )


def _resume(repo: Path):
    return commit_gate.maybe_commit(
        "verify", TODO, "commit_and_report", repo, _logs(repo), auto=False
    )


# ─── crash matrix (§8) ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "stage",
    ["baseline", "commit_object", "update_ref", "index_sync"],
)
def test_crash_then_resume_creates_exactly_one_commit(tmp_path, stage):
    publish = _publish()
    repo = _repo(tmp_path)
    initial = _count(repo)
    head0 = _head(repo)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")

    with pytest.raises(publish.PublishCrash):
        _commit_unit(repo, ("README.md",), crash_at=stage)

    # Resume must land exactly ONE unit commit — never a second.
    out = _resume(repo)
    assert out.status == "committed", out.reason
    assert _count(repo) == initial + 1
    # The unit's work is in HEAD, not lost.
    assert _blob_at(repo, "HEAD", "README.md") == "v2\n"
    # The receipt is the durable fact; the intent is gone.
    receipt = publish.read_receipt(repo, TODO)
    assert receipt is not None and receipt.disposition == "committed"
    assert not publish.intent_file(repo, TODO).exists()

    if stage in ("update_ref", "index_sync"):
        # The ref had already moved before the crash — the crashed HEAD is
        # the final HEAD (resume added no commit).
        assert _head(repo) != head0
    else:
        assert _head(repo) != head0


def test_crash_after_update_ref_recognized_by_oid_not_message(tmp_path):
    """A crash between the ref update and the receipt is recognized by the
    intent's OID — even when the commit message mentions nothing of the id."""
    publish = _publish()
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")

    with pytest.raises(publish.PublishCrash):
        commit_gate._commit_via_isolated_index(
            repo, _plan(("README.md",)), "unrelated subject line", _logs(repo),
            crash_at="update_ref",
        )

    intent = publish.read_intent(repo, TODO)
    assert intent is not None
    # The ref is at exactly the intent's OID, and the message carries no id.
    assert _head(repo) == intent.commit_oid
    assert TODO not in _git(repo, "log", "-1", "--format=%s").stdout

    out = _resume(repo)
    assert out.status == "committed"
    assert _count(repo) == 2  # init + the one unit commit; no second


def test_resume_refuses_when_head_moved_elsewhere(tmp_path):
    """A pending intent whose expected HEAD no longer matches (someone else
    moved HEAD) is refused — no second commit, no silent success."""
    publish = _publish()
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")

    with pytest.raises(publish.PublishCrash):
        _commit_unit(repo, ("README.md",), crash_at="commit_object")

    # Another actor commits on top before the resume.
    (repo / "other.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-qm", "someone else")
    moved = _head(repo)
    count = _count(repo)

    out = _resume(repo)
    assert out.status == "refused"
    assert _head(repo) == moved
    assert _count(repo) == count  # no second commit


# ─── user index during the operation (§4) ─────────────────────────────────


def test_user_staged_change_during_op_is_preserved(tmp_path):
    publish = _publish()
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    (repo / "b.txt").write_text("new\n", encoding="utf-8")

    # Publish up to (and including) the commit object; the ref has NOT moved.
    with pytest.raises(publish.PublishCrash):
        _commit_unit(repo, ("README.md", "b.txt"), crash_at="commit_object")

    # The user edits and stages a PLAN file while the op is in flight.
    (repo / "README.md").write_text("v3-user\n", encoding="utf-8")
    _git(repo, "add", "README.md")

    out = _resume(repo)
    assert out.status == "committed"
    receipt = publish.read_receipt(repo, TODO)
    assert receipt.disposition == "committed_with_reconciliation_required"

    # The unit's version is in HEAD (v2), the user's staged v3 survives.
    assert _blob_at(repo, "HEAD", "README.md") == "v2\n"
    assert _git(repo, "show", ":README.md").stdout == "v3-user\n"
    staged = _git(repo, "diff", "--cached", "--name-only", "HEAD").stdout.split()
    assert "README.md" in staged  # user's entry NOT reset by the sync
    # The untouched plan file WAS synced (no stale staged-vs-HEAD entry).
    assert "b.txt" not in staged
    assert _count(repo) == 2  # not re-committed


def test_mode_only_user_change_during_op_is_preserved(tmp_path):
    """§4 with a chmod: the owned-index sync compares (blob, MODE).

    A blob-only comparison treats a mode-only change as "unchanged" and
    resets the user's entry away — the same rule that preserves a staged
    edit must preserve a staged chmod (R2.5 REVIEW, iter 6, E2)."""
    publish = _publish()
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    (repo / "b.txt").write_text("new\n", encoding="utf-8")

    with pytest.raises(publish.PublishCrash):
        _commit_unit(repo, ("README.md", "b.txt"), crash_at="commit_object")

    # The user stages a MODE-ONLY change of a plan file: the original blob,
    # now executable. The unit's own version of README is NOT in the real
    # index, so only the mode distinguishes the user's entry from ours.
    orig_blob = _git(repo, "rev-parse", "HEAD:README.md").stdout.strip()
    _git(repo, "update-index", "--cacheinfo", f"100755,{orig_blob},README.md")

    out = _resume(repo)
    assert out.status == "committed"
    receipt = publish.read_receipt(repo, TODO)
    assert receipt.disposition == "committed_with_reconciliation_required"

    entry = _git(repo, "ls-files", "-s", "README.md").stdout
    assert entry.startswith("100755 "), entry


# ─── two projects, one TODO id (§10) ──────────────────────────────────────


def test_two_projects_one_todo_id_do_not_share_receipt(tmp_path):
    publish = _publish()
    a = _repo(tmp_path / "a")
    b = _repo(tmp_path / "b")
    (a / "README.md").write_text("a2\n", encoding="utf-8")
    (b / "README.md").write_text("b2\n", encoding="utf-8")

    _commit_unit(a, ("README.md",))
    _commit_unit(b, ("README.md",))

    ra = publish.read_receipt(a, TODO)
    rb = publish.read_receipt(b, TODO)
    assert ra is not None and rb is not None
    assert ra.commit_oid != rb.commit_oid
    assert publish.receipt_file(a, TODO) != publish.receipt_file(b, TODO)
    assert _blob_at(a, "HEAD", "README.md") == "a2\n"
    assert _blob_at(b, "HEAD", "README.md") == "b2\n"


# ─── no-code unit ─────────────────────────────────────────────────────────


def test_no_code_receipt_requires_evidence(tmp_path):
    publish = _publish()
    repo = _repo(tmp_path)
    with pytest.raises(publish.PublishError):
        publish.record_no_code_receipt(repo, TODO)
    # No artificial empty commit was created.
    assert _count(repo) == 1


def test_no_code_receipt_is_valid_without_commit(tmp_path):
    publish = _publish()
    repo = _repo(tmp_path)
    outbox = repo / ".agentic" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / f"DONE-{TODO}.json").write_text("{}\n", encoding="utf-8")

    receipt = publish.record_no_code_receipt(repo, TODO, reason="audit only")
    assert receipt.disposition == "explicitly_verified_no_code"
    assert receipt.commit_oid == ""
    assert receipt.evidence_refs
    assert _count(repo) == 1  # no empty commit invented


# ─── archive (§ archive crash + gate) ─────────────────────────────────────


def test_archive_requires_allowed_disposition(tmp_path):
    publish = _publish()
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    # No receipt yet → archive refused (DONE presence would not help).
    (repo / ".agentic" / "outbox").mkdir(parents=True, exist_ok=True)
    (repo / ".agentic" / "outbox" / f"DONE-{TODO}.md").write_text("x\n", encoding="utf-8")
    with pytest.raises(publish.PublishError):
        publish.archive_after_receipt(repo, TODO)


def test_crash_after_archive_resumes_without_losing_artifacts(tmp_path):
    publish = _publish()
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    inbox = repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{TODO}.md").write_text("# TODO\n", encoding="utf-8")

    _commit_unit(repo, ("README.md",))  # committed → receipt
    count = _count(repo)

    with pytest.raises(publish.PublishCrash):
        publish.archive_after_receipt(repo, TODO, crash_at="archive")

    # The archive move landed; resume is idempotent and loses nothing.
    archived = publish.archive_after_receipt(repo, TODO)
    assert archived is not None
    assert (repo / ".agentic" / "done" / TODO / "TODO.md").is_file()
    assert not (inbox / f"{TODO}.md").exists()
    assert _count(repo) == count  # archive never commits


# ─── R1 carry (a): the destructive sibling of the projection writer ───────

def test_projection_remove_no_follow_barrier(tmp_path):
    """R2.5 (architect-review): ``remove_state_file`` carries the SAME
    no-follow barrier as the write. A symlinked ``.agentic/state`` used to
    let ``unlink`` follow the link and delete a file OUTSIDE the project —
    the asymmetry the R1 carry left behind. The preflight refuses before
    the unlink, so nothing outside the project is touched."""
    from awf.infra import state_store
    from awf.infra.managed_fs import ManagedPathError

    proj = tmp_path / "proj"
    (proj / ".agentic").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "run.yaml").write_text("victim\n", encoding="utf-8")
    (proj / ".agentic" / "state").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ManagedPathError):
        state_store.remove_state_file(proj / ".agentic" / "state" / "run.yaml")
    assert (outside / "run.yaml").is_file()  # nothing deleted outside


# ─── publication facts in status / RUN-REPORT (requirement 4) ─────────────


def test_publication_facts_reach_status_and_run_report(tmp_path):
    """REVIEW iter 2 (fix 3): commit / receipt / archive are surfaced as
    SEPARATE facts — in run status (``run_status.publication``) and in the
    RUN-REPORT «Publication» section (requirement 4). The run's current
    unit is set directly (run_next would spawn a pipeline — out of scope
    for a hermetic test)."""
    publish = _publish()
    from awf import api, run_state
    from awf.api import run as run_api

    repo = _repo(tmp_path)
    api.init_project(repo, project_name="R25Facts")
    inbox = repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{TODO}.md").write_text("# Task\n", encoding="utf-8")
    api.run_start(repo, queue=[TODO], budget_minutes=60)
    run_state.write_run(repo, current=TODO)

    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    _commit_unit(repo, ("README.md",))
    receipt = publish.read_receipt(repo, TODO)
    assert receipt is not None and receipt.disposition == "committed"

    st = api.run_status(repo)
    assert st.publication["commit"] == receipt.commit_oid[:12]
    assert st.publication["receipt"] == "committed"

    publish.archive_after_receipt(repo, TODO, allow_no_receipt=True)
    st2 = api.run_status(repo)
    assert st2.publication["archived"]  # a separate fact, not inferred

    state = {
        "current": TODO,
        "queue": [{"todo_id": TODO}],
        "completed": [TODO],
        "rejects": {},
    }
    report = run_api._write_report(repo, state, "test reason")
    text = report.read_text(encoding="utf-8")
    assert "## Publication" in text
    assert f"- {TODO}: commit {receipt.commit_oid[:12]}" in text
    assert f"- {TODO}: receipt committed" in text
    assert f"- {TODO}: archived " in text


# ─── REVIEW iter 4 (auditor): M1 / L1 / L2 ────────────────────────────────


def test_resumed_publish_leaves_commit_audit_and_run_report(tmp_path):
    """M1 [RED before fix]: the resume path returns BEFORE ``_finalize``, so
    a publication resumed out of a crash left NO ``COMMIT-AUDIT-{todo}.json``
    — the TODO-0195 invariant «any post-plan outcome leaves the per-unit
    audit fact» was violated and the RUN-REPORT dropped the unit's commit.

    Crash after ``update-ref`` (index-sync stage), then resume through the
    gate: the audit fact must exist (``resumed`` mark + the commit OID) and
    the RUN-REPORT must render the unit's commit."""
    publish = _publish()
    from awf.api import run as run_api

    repo = _repo(tmp_path)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")

    with pytest.raises(publish.PublishCrash):
        _commit_unit(repo, ("README.md",), crash_at="index_sync")

    out = _resume(repo)
    assert out.status == "committed", out.reason
    assert out.sha

    audit_file = repo / ".agentic" / "context" / f"COMMIT-AUDIT-{TODO}.json"
    assert audit_file.is_file(), (
        "a resumed publish is a post-plan outcome — it must leave the "
        "per-unit COMMIT-AUDIT fact (TODO-0195)"
    )
    audit = json.loads(audit_file.read_text(encoding="utf-8"))
    assert audit["resumed"] is True
    assert audit["status"] == "committed"
    assert audit["commit"] == out.sha

    state = {
        "current": TODO,
        "queue": [{"todo_id": TODO}],
        "completed": [TODO],
        "rejects": {},
    }
    report = run_api._write_report(repo, state, "test reason")
    text = report.read_text(encoding="utf-8")
    assert "## Outside the commit" in text
    assert f"- {TODO} — publication resumed: commit {out.sha}" in text


def test_state_write_degrades_on_lock_refusal(tmp_path, monkeypatch):
    """L1 [RED before fix]: the lock's refusal ("no flock", an exotic
    platform) escaped ``_mutate_state_locked``'s ``except (OSError,
    ManagedPathError)`` and crashed the stage transition — the documented
    "non-fatal → no state" contract did not hold for that branch. The one
    shared catch-list must swallow it too.

    R2.5 architector-review: the lock raises the PRECISE ``LockUnavailable``
    (an ``AwfApiError`` subclass) — the family must name the condition, not
    the base that also covers ``OperationRecoveryRequired``."""
    import contextlib

    from awf import pipeline_state
    from awf._errors import LockUnavailable

    @contextlib.contextmanager
    def refusing_lock(_project_dir):
        raise LockUnavailable("advisory state locking needs fcntl (POSIX)")
        yield  # pragma: no cover — contextmanager needs a yield to be a CM

    monkeypatch.setattr("awf._lock.locked", refusing_lock)

    proj = tmp_path / "proj"
    (proj / ".agentic").mkdir(parents=True)
    logs = tmp_path / "logs"
    logs.mkdir()

    # Must degrade (log + return), never raise.
    pipeline_state.write_state(proj, logs_dir=logs, stage_name="plan", todo_id=TODO)
    assert not (proj / ".agentic" / "state" / "current.yaml").exists()


def test_clear_state_logs_managed_refusal_and_propagates(tmp_path, monkeypatch):
    """L1 [RED before fix]: ``clear_state`` caught only ``OSError``, so a
    ``ManagedPathError`` from the store's removed-projection barrier
    propagated WITHOUT a log line. It still propagates (reset needs the
    failure) — but the shared catch-list must log it first.

    R2.5 REVIEW (iter 6, E1): the propagated type is the ``OSError`` family —
    the documented caller contract every ``except OSError`` degradation path
    relies on (clean exit, kill path, close gate). The barrier's cause is
    preserved via ``__cause__``.
    """
    from awf import pipeline_state
    from awf.infra import state_store
    from awf.infra.managed_fs import ManagedPathError

    proj = tmp_path / "proj"
    state_dir = proj / ".agentic" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "current.yaml").write_text("a: 1\n", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()

    def refusing_remove(_path):
        raise ManagedPathError("state component is a symlink")

    monkeypatch.setattr(state_store, "remove_state_file", refusing_remove)

    with pytest.raises(OSError) as excinfo:
        pipeline_state.clear_state(proj, logs_dir=logs)

    assert isinstance(excinfo.value.__cause__, ManagedPathError)

    log_text = (logs / "orchestrator.log").read_text(encoding="utf-8")
    assert "state clear failed" in log_text
    assert "symlink" in log_text


def test_publication_io_error_is_not_labelled_a_git_failure(tmp_path, monkeypatch):
    """L2 [RED before fix]: the whole ``publish_from_env`` call was wrapped in
    ``except (SubprocessError, OSError)`` with the text «git commit failed».
    The git steps report their own return codes, so an ``OSError`` there is
    publication I/O (the intent/receipt write), not a git failure — the old
    message sent the operator hunting the wrong cause."""
    publish = _publish()

    repo = _repo(tmp_path)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")

    def io_boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(publish, "publish_from_env", io_boom)

    out = _commit_unit(repo, ("README.md",))
    assert out.status == "error"
    assert "publication I/O failed" in out.reason
    assert "git commit failed" not in out.reason


def test_git_launch_failure_still_says_git_command_failed(tmp_path, monkeypatch):
    """L2 [guard]: a genuine git LAUNCH failure (``SubprocessError``) keeps
    its honest diagnosis — the split must not blur the two."""
    publish = _publish()

    repo = _repo(tmp_path)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")

    def subprocess_boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1.0)

    monkeypatch.setattr(publish, "publish_from_env", subprocess_boom)

    out = _commit_unit(repo, ("README.md",))
    assert out.status == "error"
    assert "git command failed" in out.reason


# ─── REVIEW iter 5 (auditor): D1 / D2 / D3 ────────────────────────────────


def test_managed_refusal_on_publish_path_is_a_typed_outcome(tmp_path):
    """D3 [RED before fix]: a symlinked ``.agentic/context`` makes the managed
    no-follow writer refuse the intent write with ``ManagedPathError`` (a bare
    ``Exception``, NOT an ``OSError``). The publication path caught only
    ``SubprocessError``/``OSError``, so the refusal escaped the gate and
    crashed the stage transition instead of surfacing as a typed outcome.
    ``publish._write_json`` now translates it to ``PublishError``; the gate
    returns a typed ``error`` and the barrier holds (nothing lands outside)."""
    _publish()
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    (repo / ".agentic").mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside_context"
    outside.mkdir()
    (repo / ".agentic" / "context").symlink_to(outside, target_is_directory=True)

    out = _commit_unit(repo, ("README.md",))
    assert out.status == "error", out.reason
    assert "managed-path barrier" in out.reason

    # The barrier held: no publication fact landed outside the project.
    assert not list(outside.iterdir())

    # A retry is still a typed refusal, never a crash.
    again = _commit_unit(repo, ("README.md",))
    assert again.status == "error", again.reason


# ─── architector-review (iter 6): one writer, one typed refusal ───────────


def test_publication_fact_io_refusal_is_a_typed_publish_error(tmp_path, monkeypatch):
    """The ONE publication-fact writer (``_write_json``) types its WHOLE
    failure family — the managed no-follow barrier (``ManagedPathError``) and a
    generic I/O refusal (disk full / EACCES) — as ``PublishError``. Before this
    a bare ``OSError`` escaped the resume/archive paths (which catch only
    ``PublishError``), crashing the exact step the resumable protocol exists to
    protect."""
    publish = _publish()
    repo = _repo(tmp_path)

    from awf.infra import managed_fs

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(managed_fs, "managed_write_text", boom)

    with pytest.raises(publish.PublishError) as ei:
        publish._write_json(publish.intent_file(repo, TODO), {"todo_id": TODO})
    assert "could not be written" in str(ei.value)
    assert "disk full" in str(ei.value)


def test_resume_io_refusal_is_a_typed_outcome(tmp_path, monkeypatch):
    """A generic I/O failure while recording the receipt on the RESUME path is
    a typed ``error`` outcome, not a crash: the ref already carries the unit's
    commit, so the stage must not die on a transient write failure — a later
    publish resumes."""
    publish = _publish()
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")

    with pytest.raises(publish.PublishCrash):
        _commit_unit(repo, ("README.md",), crash_at="index_sync")

    from awf.infra import managed_fs

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(managed_fs, "managed_write_text", boom)

    out = _resume(repo)
    assert out.status == "error", out.reason
    assert "could not advance" in out.reason



def test_corrupt_pending_intent_fact_does_not_crash_the_resume(tmp_path):
    """R2.5 (architect-review): the publication-fact readers are TOTAL.

    A fact file holding valid JSON of the WRONG shape (a hand-edited or
    foreign-schema object) used to raise ``KeyError`` out of ``read_intent``
    — an untyped crash of the commit gate, while the sibling reader
    (``_read_commit_audit``) already degrades to ``None``. The corrupt fact
    is not a pending operation: the gate proceeds and the commit lands once.
    """
    publish = _publish()
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")

    with pytest.raises(publish.PublishCrash):
        _commit_unit(repo, ("README.md",), crash_at="commit_object")
    intent = next(publish.paths.context_dir(repo).glob("PUBLISH-INTENT-*.json"))
    intent.write_text('{"broken": "shape"}\n', encoding="utf-8")

    out = _resume(repo)

    assert out.status == "committed", out.reason
    assert _count(repo) == 2


def test_resume_git_failure_is_a_typed_outcome(tmp_path, monkeypatch):
    """R2.5 (architect-review): the RESUME entry point crosses the SAME typed
    boundary as the fresh publish.

    A git launch/read failure while finishing a pending operation used to
    escape ``maybe_commit`` as a raw ``OSError`` (the fresh path already
    typed that pair) — the stage crashed instead of reporting a resumable
    error. The durable intent keeps the operation resumable.
    """
    publish = _publish()
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")

    with pytest.raises(publish.PublishCrash):
        _commit_unit(repo, ("README.md",), crash_at="commit_object")

    def _vanished(*_args, **_kwargs):
        raise OSError("git vanished")

    monkeypatch.setattr(publish, "_git", _vanished)

    out = _resume(repo)

    assert out.status == "error", out.reason
    assert "later publish resumes" in out.reason
    assert any(publish.paths.context_dir(repo).glob("PUBLISH-INTENT-*.json"))
