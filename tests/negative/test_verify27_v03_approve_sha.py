"""V-03 (повторная проверка 27.09, 16-supervisor.md): в активном забеге
approve обязан нести verified_sha.

Дефект: ``awf_approve(evidence='probe', verified_sha='')`` в активном
забеге создавал ``APPROVE-{todo}.ready`` — пустой отпечаток не мешал
разрешающему сигналу, и commit gate коммитил дерево, которое супервизор
никогда не зафиксировал отпечатком. Решение владельца (27.09): в забеге
``verified_sha`` обязателен; вне забега поведение прежнее (опционален).

Инварианты:
1. Активный забег: approve без ``verified_sha`` — явный отказ
   (AwfApiError), ``APPROVE-{todo}.ready`` не публикуется, verdict не
   пишется. С корректным ``verified_sha`` — approve проходит (сверка с
   деревом, при расхождении — отдельный отказ).
2. Вне забега: ``verified_sha`` остаётся опциональным — approve без него
   публикует сигнал, как раньше.

До фикса тест (a) красный: сигнал создавался без отпечатка.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api, git_utils, run_state

TODO = "TODO-0001"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="V03ApproveSha")
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str = TODO, body: str = "# Task\n") -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")


def test_run_approve_without_sha_refused(tmp_git_repo):
    """(a) Активный забег: evidence есть, verified_sha пуст — отказ,
    сигнала нет, VERIFIED-файла нет, verdict не записан."""
    proj = _project(tmp_git_repo)
    _write_todo(proj)
    api.run_start(proj, queue=[TODO])

    with pytest.raises(api.AwfApiError, match="verified_sha"):
        api.approve_commit(proj, TODO, evidence="pytest -q → 42 passed; verdict: approve")

    inbox = proj / ".agentic" / "inbox"
    assert not (inbox / f"APPROVE-{TODO}.ready").exists(), (
        "run-mode approve without verified_sha published the signal — the "
        "commit gate would commit an unverified tree"
    )
    assert not (proj / ".agentic" / "context" / f"VERIFIED-{TODO}.sha").exists()
    outcomes = (run_state.read_run(proj) or {}).get("outcomes") or {}
    assert TODO not in outcomes, "a refused approve must not record a verdict"


def test_run_approve_with_sha_ok(tmp_git_repo):
    """(b) Активный забег: evidence + корректный verified_sha — approve
    проходит: сигнал, VERIFIED-файл, verdict approved."""
    proj = _project(tmp_git_repo)
    _write_todo(proj)
    api.run_start(proj, queue=[TODO])

    fp = git_utils.tree_fingerprint(proj)
    result = api.approve_commit(
        proj, TODO,
        evidence="pytest -q → 42 passed; verdict: approve",
        verified_sha=fp,
    )

    assert (proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready").is_file()
    verified = proj / ".agentic" / "context" / f"VERIFIED-{TODO}.sha"
    assert verified.is_file()
    assert verified.read_text(encoding="utf-8").strip() == fp
    assert result.verified_sha_file == str(verified)
    state = run_state.read_run(proj)
    assert state["outcomes"][TODO]["verdict"] == "approved"


def test_run_approve_with_stale_sha_refused(tmp_git_repo):
    """(b-отказ) Активный забег: sha от другого дерева — отказ (существующий
    U11-путь), сигнала нет."""
    proj = _project(tmp_git_repo)
    _write_todo(proj)
    api.run_start(proj, queue=[TODO])

    fp = git_utils.tree_fingerprint(proj)
    (proj / "sneaky.txt").write_text("moved after verify\n")

    with pytest.raises(api.AwfApiError, match="tree changed after verification"):
        api.approve_commit(proj, TODO, evidence="probes ok", verified_sha=fp)

    assert not (proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready").exists()


def test_outside_run_without_sha_as_before(tmp_git_repo):
    """(c) Вне забега: verified_sha остаётся опциональным — approve без
    него публикует сигнал, как раньше (решение владельца — только забег)."""
    proj = _project(tmp_git_repo)
    _write_todo(proj)

    result = api.approve_commit(proj, TODO)

    assert (proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready").is_file()
    assert not (proj / ".agentic" / "context" / f"VERIFIED-{TODO}.sha").exists()
    assert result.verified_sha_file == ""
