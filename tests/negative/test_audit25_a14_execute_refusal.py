"""A-14 (аудит 2026-09-25, слой 3): отказ commit-гейта на execute-стадии
останавливает цикл, а не переходит к следующей стадии.

Находка аудита: при политике `commit_and_next`/`commit_and_report`
`_handle_next` на агентской стадии игнорировал отказ commit gate и
переходил дальше. Фикс пришёл в составе R-03 (`pipeline_engine.py`:
`_commit_outcome_ok` + остановка), но проверка, которую аудит задал
явно, не была написана: «подмена `_maybe_commit` на refused-исход на
execute стадии с `commit_and_next` даёт exit code 1 и не запускает
следующую стадию».

Этот файл — та самая проверка: полный прогон через `run_pipeline`
(оркестратор + движок), стадии-заглушки вместо реального воркера, гейт
возвращает типизированный исход:

- test_refused_commit_on_execute_stage_stops_run — refused: exit != 0,
  следующая стадия (verify) не запускалась, TODO активен (не
  архивирован);
- test_skipped_commit_on_execute_stage_continues — `skipped` входит в
  `PROCEED_STATUSES`: переход состоялся, цикл дошёл до конца
  (verify + архив).

Покраснение до фикса доказано свапом: откат обработки R-03 в
`_handle_next` (исход гейта игнорируется — поведение pre-R-03) красит
`test_refused_commit_on_execute_stage_stops_run` (verify запускается);
восстановление файла побайтно (md5) возвращает зелёный. Детали — в
DONE-TODO-0110.md.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf import api, commit_plan, pipeline_engine
from awf.orchestrator import run_pipeline

TODO = "TODO-0001"


@pytest.fixture
def project(tmp_git_repo: Path) -> Path:
    """Git-репо с .agentic/ и пайплайном plan → worker → verify.

    Ключевое отличие от e2e-фикстуры: `on_approved: commit_and_next`
    стоит на EXECUTE-стадии (worker) — именно там живёт A-14.
    """
    api.init_project(tmp_git_repo, project_name="A14Refusal")

    pipes = tmp_git_repo / ".agentic" / "pipelines"
    pipes.mkdir(parents=True, exist_ok=True)
    (pipes / "default.yaml").write_text(
        'name: "default"\n'
        "stages:\n"
        '  - name: "plan"\n    role: "supervisor"\n'
        '  - name: "worker"\n    role: "worker"\n'
        '    on_blocked: "escalate"\n    max_retries: 3\n'
        '    on_approved: "commit_and_next"\n'
        '  - name: "verify"\n    role: "supervisor"\n'
        '    on_approved: "commit_and_next"\n    on_rejected: "replan"\n',
        encoding="utf-8",
    )

    roles = tmp_git_repo / ".agentic" / "roles"
    roles.mkdir(parents=True, exist_ok=True)
    (roles / "worker.md").write_text("# Worker\nExecute TODO.\n", encoding="utf-8")

    return tmp_git_repo


def _create_todo(project: Path) -> None:
    """TODO в inbox + baseline-снимок (как в e2e-фикстурах)."""
    inbox = project / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{TODO}.md").write_text(f"# {TODO}\nTest task\n", encoding="utf-8")
    (inbox / f"{TODO}.ready").write_text("")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True, check=True
    ).stdout.strip()
    ctx = project / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"BASELINE-{TODO}.sha").write_text(sha + "\n", encoding="utf-8")


def _make_args(project: Path) -> SimpleNamespace:
    return SimpleNamespace(
        project_dir=str(project), pipeline=None, from_stage=None,
        auto=True, timeout=None, todo_id=TODO,
    )


def _wire(project: Path, monkeypatch, gate_status: str, gate_reason: str) -> dict:
    """Стадии-заглушки + гейт с фиксированным типизированным исходом.

    Возвращает журнал вызовов: supervisor-стадии по именам/видам,
    agent-стадии по именам, вызовы гейта по имени стадии.
    """
    calls: dict = {"supervisor": [], "agent": [], "gate": []}

    def mock_supervisor(stage, todo_id, auto, project_dir, logs_dir, pipeline_name=None):
        calls["supervisor"].append((stage.name, stage.kind))
        if stage.kind == "plan":
            return ""
        inbox_path = project_dir / ".agentic" / "inbox"
        inbox_path.mkdir(parents=True, exist_ok=True)
        (inbox_path / f"ACK-{todo_id}.ready").write_text("", encoding="utf-8")
        (inbox_path / f"APPROVE-{todo_id}.ready").write_text("", encoding="utf-8")
        return f"ACK-{todo_id}"

    def mock_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
        calls["agent"].append(stage.name)
        outbox = project_dir / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / f"DONE-{todo_id}.md").write_text(f"# Done\n{todo_id} complete\n", encoding="utf-8")
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")
        (project_dir / "src.txt").write_text("worker output\n", encoding="utf-8")

    def mock_gate(s_name, *a, **kw):
        calls["gate"].append(s_name)
        return commit_plan.CommitOutcome(gate_status, gate_reason)

    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", mock_supervisor)
    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
    monkeypatch.setattr(pipeline_engine, "_maybe_commit", mock_gate)

    return calls


def test_refused_commit_on_execute_stage_stops_run(project: Path, monkeypatch):
    """A-14: отказ гейта на execute-стадии останавливает цикл.

    Worker-стадия с политикой `commit_and_next` пишет DONE, гейт
    возвращает refused. Инвариант: exit != 0, следующая стадия (verify)
    не запускалась (счётчик), TODO остаётся активным — не архивируется.
    """
    _create_todo(project)
    calls = _wire(project, monkeypatch, commit_plan.OUTCOME_REFUSED, "test refusal (A-14)")

    rc = run_pipeline(_make_args(project))

    assert rc != 0, (
        f"A-14: refused commit на execute-стадии обязан остановить цикл с "
        f"ненулевым кодом — получено {rc}; журнал: {calls}"
    )
    assert calls["agent"] == ["worker"], (
        f"execute-стадия отработала ровно один раз — получено {calls['agent']}"
    )
    verify_runs = [name for name, kind in calls["supervisor"] if kind == "verify"]
    assert not verify_runs, (
        f"A-14: следующая стадия не должна запускаться после refused — "
        f"verify отработала {len(verify_runs)} раз(а); журнал: {calls}"
    )
    assert calls["gate"] == ["worker"], (
        f"гейт вызывался только на execute-стадии — получено {calls['gate']}"
    )
    assert (project / ".agentic" / "inbox" / f"{TODO}.md").exists(), (
        "A-14: TODO должен остаться активным (не архивированным) после refused"
    )
    assert not (project / ".agentic" / "done" / TODO).exists(), (
        "A-14: TODO не должен попадать в done/ после refused"
    )


def test_skipped_commit_on_execute_stage_continues(project: Path, monkeypatch):
    """A-14 (инвариант 2): `skipped` входит в `PROCEED_STATUSES` —
    переход состоялся, цикл дошёл до конца (verify + архив)."""
    assert commit_plan.OUTCOME_SKIPPED in commit_plan.PROCEED_STATUSES, (
        "premise: код считает skipped проходимым статусом (PROCEED_STATUSES)"
    )
    _create_todo(project)
    calls = _wire(project, monkeypatch, commit_plan.OUTCOME_SKIPPED, "nothing to commit")

    rc = run_pipeline(_make_args(project))

    assert rc == 0, f"skipped не должен останавливать цикл — получено {rc}; журнал: {calls}"
    sup_kinds = [kind for _, kind in calls["supervisor"]]
    assert "verify" in sup_kinds, (
        f"A-14: после skipped переход должен состояться — verify не "
        f"запустилась; журнал: {calls}"
    )
    assert calls["agent"] == ["worker"], (
        f"execute-стадия отработала ровно один раз — получено {calls['agent']}"
    )
    assert calls["gate"] == ["worker", "verify"], (
        f"гейт вызывался на execute и на verify — получено {calls['gate']}"
    )
    assert (project / ".agentic" / "done" / TODO / "TODO.md").is_file(), (
        "цикл дошёл до конца — TODO заархивирован verify-стадией"
    )
