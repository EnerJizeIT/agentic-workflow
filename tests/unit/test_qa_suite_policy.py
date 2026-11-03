"""A5 (TODO-0366): QA pays for the full suite once, not on every iteration.

Two acceptance facts live here:

- the QA stage prompt carries the rule (one full run, before the verdict)
  and the explicit prohibition (no full run after every internal edit);
- an artifact that is current for the actual tree is reused — the decision
  function, not a wall of prose, says "do not spawn a second run".
"""
from __future__ import annotations

import json
from pathlib import Path

from awf import git_utils, qa_suite_policy, suite_command, suite_guard
from awf.api import roles


def _exclude_context(project: Path) -> None:
    """Keep ``.agentic/context`` out of tree-sha, as ``suite-canonical.sh`` does.

    Otherwise the artifact itself changes the tree it describes and the cache
    can never hit.
    """
    exclude = project / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(".agentic/context/\n", encoding="utf-8")


def _write_artifact(
    project: Path, sha: str, command: str, exit_code: int = 0
) -> Path:
    path = qa_suite_policy.artifact_path(project, sha, command)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"sha": sha, "command": command, "exit": exit_code, "counts": None}
        ),
        encoding="utf-8",
    )
    return path


def _current_artifact(project: Path, exit_code: int = 0) -> Path:
    _exclude_context(project)
    sha = git_utils.tree_fingerprint(project)
    canonical = suite_command.resolve(project)
    assert canonical is not None
    return _write_artifact(project, sha, canonical.command, exit_code)


def test_qa_prompt_runs_the_full_suite_once():
    """Промпт QA: один полный прогон перед вердиктом, не на каждой итерации."""
    body = roles._suite_canon_body("qa")

    assert "ОДИН канонический прогон на attempt, перед вердиктом" in body
    assert "На каждой внутренней итерации полный прогон НЕ запускается" in body
    assert "после каждой правки запрещено" in body
    assert "scripts/affected-tests.sh --run" in body
    assert "qa: full-suite runs=N, selected runs=M, total=Ns" in body


def test_qa_role_block_carries_the_once_rule():
    """Правило доезжает до роли через управляемый блок, а не только в теле."""
    block = roles._suite_canon_block("agent-qa-review")

    assert roles.role_test_policy("agent-qa-review") == "qa"
    assert "ОДИН канонический прогон" in block
    assert "запрещено" in block


def test_other_policies_do_not_get_a_full_suite_offer():
    """Чужая роль не получает «гоняй полный сьют» из общей генерации."""
    assert "ОДИН канонический прогон" not in roles._suite_canon_body("implementer")
    assert "ОДИН канонический прогон" not in roles._suite_canon_body("auditor")


def test_current_artifact_is_reused_instead_of_a_second_run(tmp_git_repo: Path):
    """Артефакт на текущее дерево — доказательство: полный прогон не нужен."""
    artifact = _current_artifact(tmp_git_repo)

    for internal in (False, True):
        decision = qa_suite_policy.plan_suite_run(
            tmp_git_repo, internal_iteration=internal
        )
        assert decision.action == qa_suite_policy.REUSE
        assert decision.artifact == artifact
        assert "не повторять" in decision.reason

    assert qa_suite_policy.current_artifact(tmp_git_repo) == artifact


def test_stale_artifact_does_not_cancel_the_verdict_run(tmp_git_repo: Path):
    """Артефакт чужого дерева — не доказательство: перед вердиктом полный."""
    _current_artifact(tmp_git_repo)
    (tmp_git_repo / "README.md").write_text("changed\n", encoding="utf-8")

    assert qa_suite_policy.current_artifact(tmp_git_repo) is None
    verdict = qa_suite_policy.plan_suite_run(tmp_git_repo)
    internal = qa_suite_policy.plan_suite_run(tmp_git_repo, internal_iteration=True)
    assert verdict.action == qa_suite_policy.FULL
    assert internal.action == qa_suite_policy.SELECT


def test_artifact_of_another_command_is_not_reused(tmp_git_repo: Path):
    """Кэш привязан к команде: узкий прогон не выдаёт себя за канонический."""
    _exclude_context(tmp_git_repo)
    sha = git_utils.tree_fingerprint(tmp_git_repo)
    _write_artifact(tmp_git_repo, sha, "python3 -m pytest tests/unit -q")

    assert qa_suite_policy.current_artifact(tmp_git_repo) is None
    assert qa_suite_policy.plan_suite_run(tmp_git_repo).action == qa_suite_policy.FULL


def test_unreadable_or_unfinished_artifact_is_not_evidence(tmp_git_repo: Path):
    """Битый JSON и артефакт без ``exit`` — «нет артефакта», а не результат."""
    _exclude_context(tmp_git_repo)
    sha = git_utils.tree_fingerprint(tmp_git_repo)
    canonical = suite_command.resolve(tmp_git_repo)
    assert canonical is not None
    path = qa_suite_policy.artifact_path(tmp_git_repo, sha, canonical.command)
    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text("{not json", encoding="utf-8")
    assert qa_suite_policy.current_artifact(tmp_git_repo) is None

    path.write_text(
        json.dumps({"sha": sha, "command": canonical.command}), encoding="utf-8"
    )
    assert qa_suite_policy.current_artifact(tmp_git_repo) is None


def test_run_journal_prints_runs_time_and_the_log_line():
    """Отчёт: сколько прогонов, время каждого, строка ``qa: full-suite``."""
    journal = qa_suite_policy.RunJournal()
    journal.record(qa_suite_policy.SELECT, 12.0, "affected tests")
    journal.record(qa_suite_policy.FULL, 300.4, "canonical")

    assert (
        journal.log_line()
        == "qa: full-suite runs=1, selected runs=1, total=312s"
    )
    lines = journal.report_lines()
    assert lines[0] == "- select: 12.0s — affected tests"
    assert lines[1] == "- full: 300.4s — canonical"
    assert lines[-1] == journal.log_line()


def test_prompt_names_the_three_actions_of_the_decision(tmp_git_repo: Path):
    """Промпт и функция говорят одними словами: иначе правило расходится.

    ``plan_suite_run`` — владелец решения; промпт ссылается на него по
    имени. Переименование действия (``reuse``/``select``/``full``) без
    правки промпта оставило бы стадию без понятного указания.
    """
    body = roles._suite_canon_body("qa")

    for action in (qa_suite_policy.REUSE, qa_suite_policy.SELECT, qa_suite_policy.FULL):
        assert f"`{action}`" in body


def test_the_gate_and_the_policy_read_one_artifact(tmp_git_repo: Path):
    """Один факт «прогон есть» на две поверхности — без расхождения.

    Гейт сырого прогона и решение стадии обязаны отвечать одинаково:
    иначе артефакт глушит предупреждение, но оплачивается второй прогон
    (или наоборот). Обе стороны спрашивают ``suite_guard.artifact_for``.
    """
    artifact = _current_artifact(tmp_git_repo)

    assert suite_guard.artifact_for(
        tmp_git_repo, git_utils.tree_fingerprint(tmp_git_repo),
        suite_command.resolve(tmp_git_repo).command,
    ) == artifact
    assert suite_guard.check(tmp_git_repo, "pytest tests/ -q\n") == ""
    assert qa_suite_policy.plan_suite_run(tmp_git_repo).action == (
        qa_suite_policy.REUSE
    )


def test_red_artifact_is_still_evidence(tmp_git_repo: Path):
    """Красный прогон — тоже результат: перед вердиктом не перезапускаем.

    Правило «полный сьют один раз» не значит «пока не позеленеет»: красный
    артефакт на этом дереве — факт для вердикта, а не повод платить снова.
    """
    artifact = _current_artifact(tmp_git_repo, exit_code=1)

    assert qa_suite_policy.current_artifact(tmp_git_repo) == artifact
    assert qa_suite_policy.plan_suite_run(tmp_git_repo).action == (
        qa_suite_policy.REUSE
    )
