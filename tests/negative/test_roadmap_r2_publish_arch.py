"""R2.5 (roadmap R2) — находки architector-review, виток 10.

Три дефекта в ядре протокола публикации:
ARCH-1 — временный файл сообщения писался строгим UTF-8: байт-верный путь
         из ``plan.files`` ронял публикацию ``UnicodeEncodeError`` ДО intent;
ARCH-2 — ``commit-msg`` гонялся, но его переписанное сообщение терялось:
         ``commit-tree -m`` брал дохуковый текст (та же находка A1);
ARCH-3 — ``_advance_publication`` читал HEAD деградирующей обёрткой: сбой
         чтения выдавался за «CAS refuse» вместо типизированного ERROR
         (инвариант A4 на второй поверхности пути).

Все репозитории — disposable в ``tmp_path``. Реальный git не трогается.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

TODO = "TODO-0001"


def _publish():
    from awf import publish

    return publish


def _git(repo: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, capture_output=True, text=True
    )


def _repo(tmp_path: Path) -> Path:
    from awf import paths

    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    (repo / ".agentic" / "logs").mkdir(parents=True)
    paths.context_dir(repo).mkdir(parents=True, exist_ok=True)
    return repo


def _hook(repo: Path, name: str, body: str) -> Path:
    hook = repo / ".git" / "hooks" / name
    hook.write_text(body, encoding="utf-8")
    hook.chmod(0o755)
    return hook


def _plan(repo: Path, files: tuple[str, ...]):
    from awf import commit_plan

    return commit_plan.CommitPlan(
        todo_id=TODO, generation=0, verified_sha="", files=files
    )


def _publish_change(repo: Path, text: str = "v2\n"):
    from awf import commit_gate

    (repo / "README.md").write_text(text, encoding="utf-8")
    return commit_gate._commit_via_isolated_index(
        repo, _plan(repo, ("README.md",)), f"awf(verify): {TODO}",
        repo / ".agentic" / "logs",
    )

def test_commit_msg_accepts_a_byte_faithful_message(tmp_path):
    """ARCH-1: сообщение с байт-верным путём не роняет публикацию.

    ``build_commit_message`` перечисляет ``plan.files`` — среди них законно
    бывает имя с не-UTF-8 байтом (``surrogateescape``). A1 писал временный
    файл сообщения строгим UTF-8: ``UnicodeEncodeError`` (ValueError) летел
    ДО intent мимо всех типизированных границ. Файл пишется с
    ``surrogateescape`` — те же байты, что несла прежняя argv-форма."""
    from awf import commit_gate, commit_plan

    repo = _repo(tmp_path)
    (repo / ".agentic" / "inbox").mkdir(parents=True, exist_ok=True)
    (repo / ".agentic" / "inbox" / f"{TODO}.md").write_text(
        "# TODO — byte path\n\nbody\n", encoding="utf-8"
    )
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    raw_name = os.fsdecode(b"bad\xff.txt")
    with open(os.path.join(os.fspath(repo), raw_name), "wb") as f:
        f.write(b"content\n")

    plan = commit_plan.build_commit_plan(repo, TODO, baseline_sha=base)
    message = commit_gate.build_commit_message("verify", TODO, repo, plan)

    out = commit_gate._commit_via_isolated_index(
        repo, plan, message, repo / ".agentic" / "logs"
    )

    assert out.status == "committed", (
        "a byte-faithful path in the commit MESSAGE must not crash the "
        f"publication; got {out.status}: {out.reason}"
    )


def test_commit_msg_hook_rewrite_reaches_the_commit(tmp_path):
    """ARCH-2: переписанное hook-ом сообщение попадает в коммит.

    ``commit-msg`` — не только ворота: его вторая документированная работа —
    переписать сообщение (``Change-Id`` и подобное). Публикация гоняла хук,
    но ``commit-tree -m`` брал ДОхуковый текст — автоматика молча терялась,
    то есть ровно та находка A1, которую фикс объявлял закрытой."""
    repo = _repo(tmp_path)
    _hook(
        repo,
        "commit-msg",
        '#!/bin/sh\nprintf "\\nReviewed-by: hook\\n" >> "$1"\n',
    )

    out = _publish_change(repo)

    assert out.status == "committed", f"publish failed: {out.status}: {out.reason}"
    body = _git(repo, "log", "-1", "--format=%B").stdout
    assert "Reviewed-by: hook" in body, (
        "the commit-msg hook's rewrite must reach the commit message; got "
        f"{body!r}"
    )


def test_unreadable_head_while_advancing_keeps_the_intent(tmp_path, monkeypatch):
    """ARCH-3: сбой чтения HEAD в ``_advance_publication`` — типизированный
    ERROR, intent сохранён (тот же инвариант A4, вторая поверхность)."""
    repo = _repo(tmp_path)
    publish = _publish()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    files = ("README.md",)
    intent = publish.PublishIntent(
        todo_id=TODO,
        generation=0,
        expected_head=head,
        tree_oid="",
        commit_oid=head,
        candidate_tree="",
        files=files,
        delta_files=(),
        degraded=(),
        message="x",
        index_orig=publish._index_entries(repo, files),
        evidence_refs=(),
        transfers=(),
        residuals=(),
        created_at="",
    )
    publish._write_json(publish.intent_file(repo, TODO), intent.to_dict())
    monkeypatch.setattr(publish, "_read_head", lambda _p: (False, ""))

    out = publish._advance_publication(
        repo, intent, repo / ".agentic" / "logs"
    )

    assert out.status == "error", (
        f"an unreadable HEAD is a transient error, not a CAS refusal; got {out}"
    )
    assert publish.intent_file(repo, TODO).exists(), (
        "the resumable anchor must survive a transient HEAD read failure"
    )
    assert not publish.receipt_file(repo, TODO).exists(), (
        "no receipt may be recorded from a state the operation never reached"
    )


# ─── architector-review, iter 11: strict-sink half of the byte invariant ──
#
# F3 (iter 10) fixed the DECODE side: the shared runner no longer raises on
# hook output with a non-UTF-8 byte. But the decoded lone surrogate then
# travels into the outcome reason, which a strict sink (the log writer,
# JSON) cannot encode — the same failure class moved one step later. And
# the resume protocol's typed boundary lived in its caller, not in the
# protocol. These tests pin both.


def _intent_for(repo: Path, head: str, commit_oid: str):
    from awf import publish

    return publish.PublishIntent(
        todo_id=TODO,
        generation=0,
        expected_head=head,
        tree_oid="",
        commit_oid=commit_oid,
        candidate_tree="",
        files=(),
        delta_files=(),
        degraded=(),
        message="x",
        index_orig={},
        evidence_refs=(),
        transfers=(),
        residuals=(),
        created_at="",
    )


def test_failing_hook_with_non_utf8_stderr_keeps_the_reason_encodable(tmp_path):
    """A FAILING hook echoing a non-UTF-8 byte must still yield a reason that
    every strict sink can encode (the F3 byte class, render side)."""
    repo = _repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_bytes(b'#!/bin/sh\nprintf "bad\\377name\\n" >&2\nexit 1\n')
    hook.chmod(0o755)
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    out = _publish_change(repo, "v2\n")

    assert out.status == "error", f"a failing hook must refuse; got {out}"
    out.reason.encode("utf-8")  # strict sink: must not raise
    assert "\\xff" in out.reason, (
        "the byte-faithful part must be rendered as a visible escape, not "
        f"left as a lone surrogate: {out.reason!r}"
    )
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    assert not _publish().intent_file(repo, TODO).exists()


def test_failing_post_commit_with_non_utf8_stderr_still_publishes(tmp_path):
    """post-commit's exit code is ignored (as git does). Its byte-faithful
    stderr goes to the LOG — a strict UTF-8 writer — so the publication must
    not crash AFTER the ref moved and before the receipt landed."""
    repo = _repo(tmp_path)
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_bytes(b'#!/bin/sh\nprintf "bad\\377name\\n" >&2\\nexit 1\\n')
    hook.chmod(0o755)

    out = _publish_change(repo, "v2\n")

    assert out.status == "committed", (
        f"a failing post-commit must not fail the publication; got {out}"
    )
    assert _publish().read_receipt(repo, TODO) is not None
    (repo / ".agentic" / "logs" / "orchestrator.log").read_text(encoding="utf-8")


def test_resume_returns_a_typed_error_when_a_git_step_raises(tmp_path, monkeypatch):
    """The resume entry point owns its typed boundary: a git launch/read
    failure becomes an ERROR outcome and the intent survives — not an
    untyped exception each caller must wrap."""
    repo = _repo(tmp_path)
    publish = _publish()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    intent = _intent_for(repo, head, "a" * 40)
    publish._write_json(publish.intent_file(repo, TODO), intent.to_dict())

    def boom(*_a, **_k):
        raise OSError("git vanished")

    monkeypatch.setattr(publish, "_is_ancestor", boom)

    out = publish.resume_if_pending(repo, TODO)

    assert out is not None and out.status == "error", (
        f"a git failure while resuming must be a typed ERROR; got {out}"
    )
    assert publish.intent_file(repo, TODO).exists(), (
        "the resumable anchor must survive a transient git failure"
    )


def test_state_writer_does_not_swallow_a_recovery_required_signal(tmp_path):
    """``STATE_WRITE_FAILURES`` names the precise ``LockUnavailable``, not
    the ``AwfApiError`` base: ``OperationRecoveryRequired`` ("do not retry
    blindly") must never be degraded to "no state"."""
    import pytest

    from awf import pipeline_state
    from awf._errors import OperationRecoveryRequired

    project = tmp_path / "p"
    (project / ".agentic" / "state").mkdir(parents=True)

    def mutator(_state):
        raise OperationRecoveryRequired("op-1")

    with pytest.raises(OperationRecoveryRequired):
        pipeline_state._mutate_state_locked(
            project, mutator, logs_dir=None, failure_label="state write"
        )
