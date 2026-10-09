"""REPORTS29 (TODO-0169): continue на verify — валидный пред-существующий
APPROVE съедается, левак остаётся леваком, подсказка доходит до супервизора.

Инцидент (репорт awf-bug-20261009-continue-na-verify-ne-vidit-uzhe-lezhaschiy-
approve.md, проект jql, забег 1.4.0, TODO-0027): пайплайн умер на verify,
когда approve супервизора уже записан. `continue --from-stage verify`
запустил новое ожидание — лежащий APPROVE AUD04-04 отбросил как stale
(mtime < старта ожидания) и молча игнорировал; цикл закрылся только
ручным пере-одобрением.

mtime-гейт существует не зря (U6b: «мёртвый approve» не должен пере-открыть
commit-гейт), поэтому ослаблять его вслепую нельзя. Фикс сверяет
пред-существующий сигнал с ТЕКУЩЕЙ попыткой — той же привязкой, что
применяет commit gate (M2.1: поколение забега + verified_sha +
files_digest):

- (a) ВАЛИДНЫЙ пред-существующий APPROVE (привязка совпадает с текущей
  попыткой) — новое ожидание его съедает: юнит коммитится и архивируется
  без пере-одобрения;
- (b) левак прошлой попытки (привязка не сходится — другое поколение)
  НЕ потребляется: ожидание тикает в таймаут, ничего не коммитится, в логе
  строка с явной инструкцией пере-одобри;
- (c) при парковке на verify и проигнорированном сигнале по текущему
  юниту ответ wait_for_event несёт stale_decision_hint.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

import awf.pipeline_engine as engine
from awf import api, git_utils, run_state
from awf.commit_gate import maybe_commit
from awf.pipeline_state import write_state
from awf.supervisor import wait_for_supervisor_signal

TODO = "TODO-0001"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="Reports29")
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str = TODO) -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text("# Task\n", encoding="utf-8")


def _head(proj: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True, text=True, check=True
    ).stdout.strip()


def _git_commit_all(proj: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=proj, check=True)


def _logs(proj: Path) -> Path:
    logs = proj / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs


def _log_text(proj: Path) -> str:
    log = _logs(proj) / "orchestrator.log"
    return log.read_text(encoding="utf-8") if log.is_file() else ""


def _verify_state(proj: Path) -> str:
    """Проект в состоянии verify: работа юнита в дереве, baseline записан
    на диск (как оставляют dispatch/run_next). Возвращает baseline sha."""
    _write_todo(proj)
    (proj / "file.txt").write_text("v1\n", encoding="utf-8")
    _git_commit_all(proj, "baseline")
    baseline_sha = _head(proj)
    (proj / "file.txt").write_text("v2 good\n", encoding="utf-8")  # работа юнита
    ctx = proj / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"BASELINE-{TODO}.sha").write_text(baseline_sha + "\n", encoding="utf-8")
    return baseline_sha


def _approve_with_old_mtime(proj: Path) -> Path:
    """Approve, записанный ДО нового ожидания: пайплайн умер после
    одобрения, перезапуск начал ждать позже. Привязка живая (забег тот же,
    дерево не двигалось), mtime — в прошлом."""
    api.approve_commit(
        proj, TODO,
        evidence="pytest -q → 42 passed; verdict: approve",
        verified_sha=git_utils.tree_fingerprint(proj),
    )
    sig = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
    old = time.time() - 3600
    os.utime(sig, (old, old))
    return sig


def _parked_verify_state(proj: Path) -> None:
    """Состояние парковки на verify (как после убитого
    continue --from-stage verify)."""
    write_state(
        proj,
        stage_name="verify",
        stage_kind="verify",
        todo_id=TODO,
        logs_dir=_logs(proj),
    )


class TestValidPreexistingApprove:
    """(a) валидный пред-существующий сигнал съедается без пере-одобрения."""

    def test_preexisting_valid_approve_consumed_and_unit_committed(
        self, tmp_git_repo, monkeypatch
    ):
        """ВАЛИДНЫЙ APPROVE, записанный до нового ожидания, закрывает цикл:
        сигнал принят (строка в логе), юнит коммитится, решение потреблено
        — никакого нового approve не записывается."""
        proj = _project(tmp_git_repo)
        baseline_sha = _verify_state(proj)
        api.run_start(proj, queue=[TODO])
        sig_path = _approve_with_old_mtime(proj)
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        sig = wait_for_supervisor_signal(
            "verify", TODO, proj, _logs(proj), timeout=10,
        )

        assert sig == f"APPROVE-{TODO}", (
            f"a valid pre-existing approval must close the wait, got {sig!r}"
        )
        log = _log_text(proj)
        assert "REPORTS29" in log, (
            "the acceptance of the pre-existing approval must be logged: "
            f"{log}"
        )
        ok = maybe_commit(
            "verify", TODO, "commit_and_next", proj, _logs(proj),
            auto=False, baseline_sha=baseline_sha,
        )
        assert ok.status == "committed", (
            f"the approved tree must commit — got {ok.status}: {ok.reason}"
        )
        assert _head(proj) != baseline_sha
        engine._consume_verify_decision(proj, TODO, sig, _logs(proj))
        assert not sig_path.exists(), "the consumed decision must not survive"


class TestStalePreexistingApprove:
    """(b) левак прошлой попытки не потребляется; (c) подсказка в wait."""

    def test_stale_generation_approve_not_consumed(self, tmp_git_repo, monkeypatch):
        """Одобрение МЁРТВОГО поколения не закрывает новое ожидание:
        таймаут, без авто-коммита, левак остаётся на диске, в логе —
        явная инструкция пере-одобри."""
        proj = _project(tmp_git_repo)
        baseline_sha = _verify_state(proj)
        api.run_start(proj, queue=[TODO])  # поколение 1
        _approve_with_old_mtime(proj)
        # Забег перезапущен после approve: поколение 2, левак — прошлой попытки.
        api.run_start(proj, queue=[TODO], force=True)
        assert run_state.generation_of(run_state.read_run(proj)) == 2
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        with pytest.raises(TimeoutError):
            wait_for_supervisor_signal(
                "verify", TODO, proj, _logs(proj), timeout=1,
            )

        assert _head(proj) == baseline_sha, (
            "a dead-generation approval must not commit"
        )
        assert (proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready").is_file(), (
            "the leftover must not be consumed"
        )
        log = _log_text(proj)
        assert "does not match the current attempt" in log, (
            f"the log must name the mismatch with a re-approve instruction: {log}"
        )
        assert "awf_approve" in log

    def test_wait_for_event_carries_stale_decision_hint(self, tmp_git_repo):
        """(c) сцена (b): пайплайн на verify, левак не соответствует попытке
        — ответ wait_for_event несёт stale_decision_hint с инструкцией
        пере-одобри."""
        proj = _project(tmp_git_repo)
        _verify_state(proj)
        api.run_start(proj, queue=[TODO])
        _approve_with_old_mtime(proj)
        api.run_start(proj, queue=[TODO], force=True)
        _parked_verify_state(proj)

        result = api.wait_for_event(proj, timeout=1)

        assert result.event_type == "verify"
        assert result.stale_decision_hint, (
            "an ignored stale signal must carry the hint to the supervisor"
        )
        assert "awf_approve" in result.stale_decision_hint

    def test_no_hint_for_valid_preexisting_approve(self, tmp_git_repo):
        """ВАЛИДНЫЙ пред-существующий approve будет съеден движком —
        подсказка «пере-одобри» в verify-ответе не появляется (ложная
        инструкция)."""
        proj = _project(tmp_git_repo)
        _verify_state(proj)
        api.run_start(proj, queue=[TODO])
        _approve_with_old_mtime(proj)
        _parked_verify_state(proj)

        result = api.wait_for_event(proj, timeout=1)

        assert result.event_type == "verify"
        assert result.stale_decision_hint == ""
