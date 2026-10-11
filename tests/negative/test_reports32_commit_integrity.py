"""REPORTS32 п.1 (TODO-0195): коммит юнита не теряет его файлы.

Рань C–G из репорта awf-bug-20261011-recovery-i-commit-gate-...md:
0067/0068 диспатчнуты при незакоммиченном дереве; в коммит 0067 ушли
только 2 тест-файла (main-правки остались в дереве), 0068 заархивирован
вообще без коммита; ловилось уже на запушенном main и добиралось
catch-up-коммитом.

Кейсы (соответствуют блоку «Приёмка» TODO-0195):

- a) файл, staged НА baseline (чужой WIP), юнит его не трогает →
  не в коммите (поведение R-03/A-01 сохранено) + путь виден в
  ``plan.excluded`` с причиной;
- b) файл, застейдженный ПОСЛЕ baseline (воркер нарушил hard rule) →
  входит в коммит юнита + предупреждение «worker staged N file(s) …
  included in the unit commit»;
- c) диспатч на грязном дереве → ``dirty_files``/``dirty_warning`` со
  списком путей (warning, не отказ);
- d) непустой план + все файлы плана совпадают с HEAD (catch-up
  закоммитил работу юнита вне юнита) → громкий отказ (refused, не тихий
  SKIPPED-архив) + факт в COMMIT-AUDIT для RUN-REPORT;
- d2) двойная коммит-форма пайплайна (execute закоммитил, verify
  перезапускает гейт на том же дереве) → честный SKIPPED, как раньше
  (анти-регрессия к пункту d);
- d2b) коммит в СОБСТВЕННОМ формате юнита
  (``awf(<stage>): TODO-NNNN — <title>``) — это коммит юнита, честный
  SKIPPED (анти-регрессия: форматная проверка не перетягивает);
- d3) REVIEW-F2: catch-up-коммит, который лишь УПОМИНАЕТ id юнита в
  произвольном subject («chore: pick up TODO-0001 leftovers»), НЕ является
  коммитом юнита → громкий отказ, как в d;
- e) RUN-REPORT несёт строки «вне коммита»: excluded-пути с причиной и
  отказанный тихий skip;
- f1) REVIEW-F1: legacy-baseline (только .sha + .untracked, без .status) —
  excluded-метки точные: staged-против-baseline = «staged», pre-existing
  untracked = «pre-existing untracked» (не «staged» всё подряд).
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from awf import api, commit_gate, commit_plan

TODO = "TODO-0001"


@pytest.fixture
def project(tmp_git_repo: Path) -> Path:
    """Git-репо с .agentic/, пустыми test-командами и ЗАКОММИЧЕННЫМ
    .agentic/ (definitions) — дерево на диспатче чистое, runtime-директории
    уже gitignored самим init."""
    api.init_project(
        tmp_git_repo,
        project_name="R32Commit",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "awf init"], cwd=tmp_git_repo, check=True)
    return tmp_git_repo


def _git(project: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=project, check=True, capture_output=True, text=True)


def _git_out(project: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=project, check=True, capture_output=True, text=True
    ).stdout


def _dispatch(project: Path):
    return api.dispatch_todo(project, f"# {TODO}\nTask\n", todo_id=TODO)


def _logs_dir(project: Path) -> Path:
    return project / ".agentic" / "logs"


def _committed_files(project: Path) -> list[str]:
    return _git_out(project, "show", "--name-only", "--format=", "HEAD").split()


def test_a_staged_at_baseline_stays_out_of_unit_commit(project: Path):
    """(a) чужой WIP, застейдженный НА момент baseline, юнит не трогает →
    не в коммите (поведение сохранено) + путь в plan.excluded с причиной."""
    (project / "core.py").write_text("core v1\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    # foreign WIP: уже в индексе на момент диспатча
    (project / "foreign.py").write_text("foreign wip\n", encoding="utf-8")
    _git(project, "add", "foreign.py")

    dispatch = _dispatch(project)
    (project / "work.py").write_text("work\n", encoding="utf-8")  # работа юнита

    plan = commit_plan.build_commit_plan(project, TODO, dispatch.baseline_sha)
    assert "work.py" in plan.files
    assert "foreign.py" not in plan.files, (
        "foreign staged-на-baseline должен оставаться вне коммита юнита (R-03/A-01)"
    )
    assert ("foreign.py", "staged-at-baseline") in plan.excluded

    outcome = commit_gate.maybe_commit(
        "worker", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )
    assert outcome.status == "committed", outcome
    committed = _committed_files(project)
    assert "work.py" in committed
    assert "foreign.py" not in committed


def test_b_worker_staged_after_baseline_joins_commit_with_warning(
    project: Path, capsys: pytest.CaptureFixture[str]
):
    """(b) файл, застейдженный ПОСЛЕ baseline (воркер нарушил hard rule) →
    входит в коммит юнита + предупреждение в stderr и лог."""
    (project / "core.py").write_text("core v1\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")

    dispatch = _dispatch(project)
    (project / "staged.py").write_text("staged by worker\n", encoding="utf-8")
    _git(project, "add", "staged.py")  # нарушение hard rule после baseline

    plan = commit_plan.build_commit_plan(project, TODO, dispatch.baseline_sha)
    assert "staged.py" in plan.files, (
        "staged-после-baseline — работа юнита, а не чужой WIP: "
        "должна войти в коммит (detained staged-капкан, рана C)"
    )
    assert list(plan.worker_staged) == ["staged.py"]

    logs_dir = _logs_dir(project)
    outcome = commit_gate.maybe_commit(
        "worker", TODO, "commit_and_next", project, logs_dir,
        auto=False, baseline_sha=dispatch.baseline_sha,
    )
    assert outcome.status == "committed", outcome
    assert "staged.py" in _committed_files(project)

    err = capsys.readouterr().err
    assert "worker staged 1 file(s)" in err
    assert "included in the unit commit" in err
    assert "worker staged 1 file(s)" in (logs_dir / "orchestrator.log").read_text(
        encoding="utf-8"
    )


def test_c_dirty_tree_dispatch_warns_with_paths(project: Path):
    """(c) диспатч на грязном дереве (tracked-dirty + staged + untracked) →
    dirty_files/dirty_warning со списком путей; диспатч не отказывает."""
    (project / "core.py").write_text("core v1\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    (project / "core.py").write_text("core v2 UNCOMMITTED\n", encoding="utf-8")
    (project / "staged.txt").write_text("s\n", encoding="utf-8")
    _git(project, "add", "staged.txt")
    (project / "loose.txt").write_text("l\n", encoding="utf-8")

    result = api.dispatch_todo(project, f"# {TODO}\nTask\n", todo_id=TODO)

    for path in ("core.py", "staged.txt", "loose.txt"):
        assert path in result.dirty_files, f"{path} должен быть в dirty_files"
    assert result.dirty_warning, "грязное дерево на диспатче обязано предупреждать"
    for path in ("core.py", "staged.txt", "loose.txt"):
        assert path in result.dirty_warning
    assert "will NOT enter the TODO-0001 commit" in result.dirty_warning
    # warning, не отказ: диспатч состоялся
    assert result.todo_id == TODO
    assert (project / ".agentic" / "inbox" / f"{TODO}.md").is_file()
    assert (project / ".agentic" / "context" / f"BASELINE-{TODO}.sha").is_file()


def test_d_nonempty_plan_matching_head_refuses_silent_skip(project: Path):
    """(d) непустой план + все файлы плана совпадают с HEAD (catch-up
    закоммитил работу юнита вне юнита, рана G) → громкий отказ, не тихий
    SKIPPED-архив; факт — в COMMIT-AUDIT для RUN-REPORT."""
    (project / "core.py").write_text("core v1\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    dispatch = _dispatch(project)
    (project / "work.py").write_text("work\n", encoding="utf-8")
    # catch-up вне юнита уносит работу (0068-кейс)
    _git(project, "add", "work.py")
    _git(project, "commit", "-qm", "catch-up: pick up work.py")

    outcome = commit_gate.maybe_commit(
        "verify", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )
    assert outcome.status == "refused", (
        "непустой план, совпадающий с HEAD, не может архивироваться тихо "
        f"(получено {outcome.status}: {outcome.reason})"
    )
    assert "plan-skip-inconsistent" in outcome.reason
    assert outcome.status not in commit_plan.PROCEED_STATUSES

    audit_file = project / ".agentic" / "context" / f"COMMIT-AUDIT-{TODO}.json"
    assert audit_file.is_file(), "факт отказа должен попасть в COMMIT-AUDIT"
    audit = json.loads(audit_file.read_text(encoding="utf-8"))
    assert audit["skip_inconsistent"] is True
    assert "plan-skip-inconsistent" in audit["skip_reason"]


def test_d2_unit_own_commit_in_head_is_honest_skip(project: Path):
    """(d2) анти-регрессия к пункту d: execute-стадия закоммитил работу
    юнита, verify-гейт на том же дереве — честный SKIPPED (двойная
    коммит-форма пайплайна продолжает работать)."""
    (project / "core.py").write_text("core v1\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    dispatch = _dispatch(project)
    (project / "work.py").write_text("work\n", encoding="utf-8")

    first = commit_gate.maybe_commit(
        "worker", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )
    assert first.status == "committed", first

    # verify-гейт: план против СТАРОГО baseline непуст (work.py отличается
    # от него), дерево чистое — раньше это был тихий SKIPPED
    plan = commit_plan.build_commit_plan(project, TODO, dispatch.baseline_sha)
    assert "work.py" in plan.files

    second = commit_gate.maybe_commit(
        "verify", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )
    assert second.status == "skipped", (
        "собственный коммит юнита уже в HEAD — повторный гейт должен "
        f"честно пропустить (получено {second.status}: {second.reason})"
    )


def test_d2b_awf_format_subject_is_the_units_own_commit(project: Path):
    """(d2b, REVIEW-F2 анти-регрессия) коммит в собственном ФОРМАТЕ юнита
    (``awf(<stage>): TODO-NNNN — <title>``, REPORTS26 F3) — это коммит
    юнита: повторный гейт честно пропускает (форматная проверка не должна
    перетянуть честный skip)."""
    (project / "core.py").write_text("core v1\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    dispatch = _dispatch(project)
    (project / "work.py").write_text("work\n", encoding="utf-8")
    _git(project, "add", "work.py")
    _git(project, "commit", "-qm", f"awf(verify): {TODO} — done early")

    plan = commit_plan.build_commit_plan(project, TODO, dispatch.baseline_sha)
    assert "work.py" in plan.files

    outcome = commit_gate.maybe_commit(
        "verify", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )
    assert outcome.status == "skipped", (
        "коммит в формате ``awf(<stage>): TODO-NNNN — <title>`` — "
        f"собственный коммит юнита, честный skip (получено {outcome.status}: "
        f"{outcome.reason})"
    )


def test_d3_named_catchup_is_not_the_units_own_commit(project: Path):
    """(d3, REVIEW-F2) catch-up, который лишь УПОМИНАЕТ id юнита в
    произвольном subject («chore: pick up TODO-0001 leftovers» — repro QA),
    НЕ является коммитом юнита: субстринг-проверка прощала его (тихий
    SKIPPED-архив), форматная — отказывает."""
    (project / "core.py").write_text("core v1\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    dispatch = _dispatch(project)
    (project / "work.py").write_text("work\n", encoding="utf-8")
    # named catch-up (QA repro /tmp/opencode/qa-0195/named-catchup):
    # subject УПОМИНАЕТ id юнита, но не в его формате
    _git(project, "add", "work.py")
    _git(project, "commit", "-qm", f"chore: pick up {TODO} leftovers")

    outcome = commit_gate.maybe_commit(
        "verify", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )
    assert outcome.status == "refused", (
        "catch-up, упоминающий id юнита, — не коммит юнита: тихий архив "
        f"должен быть отказан (получено {outcome.status}: {outcome.reason})"
    )
    assert "plan-skip-inconsistent" in outcome.reason
    assert outcome.status not in commit_plan.PROCEED_STATUSES

    audit_file = project / ".agentic" / "context" / f"COMMIT-AUDIT-{TODO}.json"
    assert json.loads(audit_file.read_text(encoding="utf-8"))["skip_inconsistent"] is True


def test_f1_legacy_baseline_labels_exclusions(project: Path):
    """(f1, REVIEW-F1) legacy-baseline (только .sha + .untracked, без
    .status): метки excluded точные — staged-против-baseline = «staged»,
    pre-existing untracked = «pre-existing untracked» (не «staged» для
    всего подряд)."""
    (project / "core.py").write_text("core v1\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    (project / "junk.py").write_text("pre-existing junk\n", encoding="utf-8")
    dispatch = _dispatch(project)

    # legacy-окно: снапшота .status нет (авто-базelines его не писали)
    status_file = project / ".agentic" / "context" / f"BASELINE-{TODO}.status"
    assert status_file.is_file()
    status_file.unlink()
    (project / "work.py").write_text("work\n", encoding="utf-8")
    (project / "foreign.py").write_text("foreign wip\n", encoding="utf-8")
    _git(project, "add", "foreign.py")  # staged (legacy не различает момент)

    plan = commit_plan.build_commit_plan(project, TODO, dispatch.baseline_sha)
    assert "work.py" in plan.files
    assert "foreign.py" not in plan.files
    assert "junk.py" not in plan.files
    reasons = dict(plan.excluded)
    assert reasons.get("foreign.py") == "staged", plan.excluded
    assert reasons.get("junk.py") == "pre-existing untracked", (
        "legacy-baseline: pre-existing untracked обязан помечаться "
        f"'pre-existing untracked', а не 'staged' (получено {plan.excluded})"
    )


def test_e_run_report_carries_outside_commit_lines(project: Path):
    """(e) RUN-REPORT несёт строки «вне коммита»: excluded-пути с причиной
    и отказанный тихий skip."""
    from awf.api import run as run_api

    (project / "core.py").write_text("core v1\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    (project / "foreign.py").write_text("foreign wip\n", encoding="utf-8")
    _git(project, "add", "foreign.py")
    dispatch = _dispatch(project)
    (project / "work.py").write_text("work\n", encoding="utf-8")
    # catch-up уносит только работу юнита; foreign.py остаётся staged
    _git(project, "add", "work.py")
    _git(project, "commit", "-qm", "catch-up: work.py")

    outcome = commit_gate.maybe_commit(
        "verify", TODO, "commit_and_next", project, _logs_dir(project),
        auto=False, baseline_sha=dispatch.baseline_sha,
    )
    assert outcome.status == "refused", outcome

    state = {
        "queue": [{"todo_id": TODO, "pipeline": ""}],
        "completed": [TODO],
        "rejects": {},
        "outcomes": {},
        "started_at": datetime.now(timezone.utc).isoformat(),
        "budget_minutes": 0,
        "current": "",
    }
    report = run_api._write_report(project, state, "test stop")
    assert report is not None
    text = report.read_text(encoding="utf-8")
    assert "## Outside the commit" in text
    assert "outside the commit: foreign.py (staged-at-baseline)" in text
    assert "refused the silent skip" in text
