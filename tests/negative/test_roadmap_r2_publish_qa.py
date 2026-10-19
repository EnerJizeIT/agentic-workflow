"""R2.5 (roadmap R2) — QA-проба границ (agent-qa-review).

Независимые тесты поверх ``tests/negative/test_roadmap_r2_publish.py``.
Зелёные фиксируют верное поведение, которого нет в приёмочной матрице;
красные доказывают ОТКРЫТЫЕ точки R2.5 — их чинит следующий проход, не
этот сторож.

* ``test_projection_writer_no_follow_barrier`` — ЗЕЛЁНЫЙ: R1-перенос (а)
  («no-follow-бар писателя проекции состояния — поднять до общего»)
  действительно отказал симлинкованному ``.agentic/state`` до первого
  записанного байта. Фиксируем положительное поведение.
* ``test_state_write_is_non_fatal_on_symlinked_state`` — ЗЕЛЁНЫЙ после
  REVIEW-фикса iter 2 (был красным): отказ managed no-follow-писателя
  (``ManagedPathError``, не ``OSError``) теперь ловится в
  ``pipeline_state._mutate_state_locked`` рядом с ``OSError`` — запись
  наружу запрещена, переход стадии деградирует к «no state» с логом.
* ``test_refused_resume_does_not_wedge_the_todo`` — ЗЕЛЁНЫЙ после
  REVIEW-фикса iter 2 (был красным): отказ по уехавшему HEAD суперседит
  stale ``PUBLISH-INTENT`` (durable факт + удаление), следующий
  коммит-путь перепланирует против нового HEAD.
* Ниже — три КРАСНЫХ пробы второго прохода QA (iter 2), найденные поверх
  этих фиксов: unborn-HEAD без CAS, no-code receipt затирает committed
  receipt, stale receipt маскирует живой intent. Их чинит следующий
  проход, не этот сторож.

Ленивый импорт: на базовом дереве ``awf.publish`` отсутствует.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

TODO = "TODO-0001"


def _publish():
    try:
        from awf import publish
    except ImportError as e:  # pragma: no cover — базовое дерево
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


def _logs(repo: Path) -> Path:
    d = repo / ".agentic" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _commit_unit(repo: Path, files: tuple[str, ...], crash_at: str = ""):
    from awf import commit_gate, commit_plan

    plan = commit_plan.CommitPlan(
        todo_id=TODO, generation=0, verified_sha="", files=files
    )
    return commit_gate._commit_via_isolated_index(
        repo, plan, f"awf(verify): {TODO}", _logs(repo), crash_at=crash_at
    )


# ─── R1 carry (a): the state-projection no-follow barrier ─────────────────


def test_projection_writer_no_follow_barrier(tmp_path):
    """GREEN: the R1 carry is delivered — a symlinked ``.agentic/state`` is
    refused before a single byte lands outside the project."""
    from awf.infra.managed_fs import ManagedPathError
    from awf.infra.state_store import _projection

    proj = tmp_path / "proj"
    (proj / ".agentic").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (proj / ".agentic" / "state").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ManagedPathError):
        _projection.write_state_file(proj / ".agentic" / "state" / "run.yaml", "x\n")
    assert list(outside.iterdir()) == []  # nothing escaped


def test_state_write_is_non_fatal_on_symlinked_state(tmp_path):
    """RED: the documented non-fatal contract of a state write.

    ``pipeline_state._mutate_state_locked`` promises an OSError is logged
    and swallowed ("a state write is non-fatal for the pipeline, which
    degrades to 'no state'"). After R1.5 the writer raises
    ``ManagedPathError`` (NOT an OSError) for the symlink case, so the
    refusal escapes ``except OSError`` and crashes the stage transition.
    The refusal is correct; the crash is the defect.
    """
    from awf import pipeline_state

    proj = tmp_path / "proj"
    (proj / ".agentic").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (proj / ".agentic" / "state").symlink_to(outside, target_is_directory=True)

    logs = tmp_path / "logs"
    logs.mkdir()
    # Must degrade (log + return), never raise.
    pipeline_state.write_state(proj, logs_dir=logs, stage_name="plan", todo_id=TODO)
    assert not (outside / "current.yaml").exists()


# ─── R2.5 resume: a refusal must not wedge the TODO forever ───────────────


def test_refused_resume_does_not_wedge_the_todo(tmp_path):
    """RED: a stale pending intent must not block every later publication.

    Crash between the commit object and the ref update, then another actor
    moves HEAD. The resume refuses (correct — no second commit). But the
    intent it keys on is NEVER invalidated, so the same TODO-id can never
    publish again while HEAD differs — the roadmap's "Replan against the
    new HEAD" cannot happen for this unit. The operation must be
    abandonable (intent dropped) so a fresh publication can proceed.
    """
    publish = _publish()
    from awf import commit_gate

    repo = _repo(tmp_path)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    with pytest.raises(publish.PublishCrash):
        _commit_unit(repo, ("README.md",), crash_at="commit_object")

    # Another actor commits on top before the resume.
    (repo / "other.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-qm", "someone else")

    out = commit_gate.maybe_commit(
        "verify", TODO, "commit_and_report", repo, _logs(repo), auto=False
    )
    assert out.status == "refused"
    assert not publish.intent_file(repo, TODO).exists(), (
        "the stale PUBLISH-INTENT still blocks every later publication of "
        "this TODO — the resume is neither resumable nor abandonable"
    )


# ─── QA iter 2: open points found ON TOP of the three REVIEW fixes ────────
#
# The three tests below are RED against the current tree. They are the
# evidence for the second QA pass (agent-qa-review, iter 2) — each names
# the invariant from roadmap R2.5 it shows broken. Prod code is NOT fixed
# here: the next implementer pass takes them one by one.


def _resume(repo: Path):
    from awf import commit_gate

    return commit_gate.maybe_commit(
        "verify", TODO, "commit_and_report", repo, _logs(repo), auto=False
    )


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _blob_at(repo: Path, rev: str, path: str) -> str:
    return _git(repo, "show", f"{rev}:{path}").stdout


def _empty_repo(tmp_path: Path) -> Path:
    """A repo with NO commits (unborn HEAD) — the supported ``git init``
    state (AUD05-08: the pipeline handles a zero-commit repo)."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def test_unborn_head_update_ref_is_guarded_by_cas(tmp_path, monkeypatch):
    """RED (requirement 1): the CAS is skipped when the expected HEAD is
    empty — ``update-ref`` is called WITHOUT an old-value, so it silently
    overwrites a commit another actor created between ``commit-tree`` and
    ``update-ref``. That commit becomes unreachable (orphaned) instead of
    the publication refusing.

    Expected: old-value ``0000…0`` ("must still be unborn") makes the CAS
    fail; the racer's commit stays HEAD and the publication is refused.
    """
    publish = _publish()

    repo = _empty_repo(tmp_path)
    (repo / "unit.txt").write_text("unit\n", encoding="utf-8")

    real_git = publish._git

    def racing_git(project_dir, args, env=None):
        out = real_git(project_dir, args, env=env)
        if args and args[0] == "commit-tree" and out.returncode == 0:
            # Another actor wins the race for the FIRST commit.
            (project_dir / "racer.txt").write_text("racer\n", encoding="utf-8")
            _git(project_dir, "add", "racer.txt")
            _git(project_dir, "commit", "-qm", "concurrent actor")
        return out

    monkeypatch.setattr(publish, "_git", racing_git)
    out = _commit_unit(repo, ("unit.txt",))

    subjects = _git(repo, "log", "--format=%s").stdout
    assert "concurrent actor" in subjects, (
        "the concurrent first commit was orphaned: update-ref ran without "
        "a CAS old-value because the expected HEAD was empty"
    )
    assert out.status == "refused", out.reason


def test_no_code_receipt_does_not_overwrite_a_committed_receipt(tmp_path):
    """RED (requirement 4): a committed UnitReceipt is a durable fact. A
    later no-code recording for the SAME TODO must not erase the commit
    OID — that turns a published unit into ``explicitly_verified_no_code``
    with an empty commit and loses the fact ``publication_facts`` reports.
    """
    publish = _publish()

    repo = _repo(tmp_path)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    _commit_unit(repo, ("README.md",))
    before = publish.read_receipt(repo, TODO)
    assert before is not None and before.disposition == "committed"

    outbox = repo / ".agentic" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / f"DONE-{TODO}.json").write_text("{}\n", encoding="utf-8")

    # Either a loud refusal or a no-op is acceptable — the FACT must survive.
    try:
        publish.record_no_code_receipt(repo, TODO, reason="empty plan retry")
    except publish.PublishError:
        pass

    after = publish.read_receipt(repo, TODO)
    assert after is not None
    assert after.disposition == "committed"
    assert after.commit_oid == before.commit_oid


def test_stale_no_code_receipt_does_not_mask_pending_intent(tmp_path):
    """RED (requirements 1/3): a receipt left by an EARLIER attempt is not
    the completion of the current intent. A TODO that was no-code once,
    later produced code and crashed after ``commit-tree`` must resume: the
    intent's OID is the pending signal, not "a receipt file exists".

    Current code drops the intent and reports "already reached a receipt",
    so the unit commit never reaches HEAD — and the stale no-code receipt
    would still authorize the archive. Both are false outcomes.
    """
    publish = _publish()

    repo = _repo(tmp_path)
    outbox = repo / ".agentic" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / f"DONE-{TODO}.json").write_text("{}\n", encoding="utf-8")
    stale = publish.record_no_code_receipt(repo, TODO, reason="first attempt")
    assert stale.disposition == "explicitly_verified_no_code"

    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    with pytest.raises(publish.PublishCrash):
        _commit_unit(repo, ("README.md",), crash_at="commit_object")

    intent = publish.read_intent(repo, TODO)
    assert intent is not None

    out = _resume(repo)
    assert out.status == "committed", out.reason
    assert _head(repo) == intent.commit_oid
    assert _blob_at(repo, "HEAD", "README.md") == "v2\n"
    receipt = publish.read_receipt(repo, TODO)
    assert receipt is not None
    assert receipt.disposition == "committed"
    assert receipt.commit_oid == intent.commit_oid
