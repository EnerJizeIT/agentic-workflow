"""M1 (BACKLOG, 19 + 2б): канонический прогон полного сьюта и флейк-проба.

За R2 роли гоняли полный сьют ~66 раз — доминирующая цена витка. Здесь
прогон привязан к (tree-sha, команда), а контрактные тесты в verify-pack
дублируются, чтобы тайминговый флейк (кейс T1b) был виден заранее.

(a) кэш по дереву, `--force`; (b) факты в артефакте; (c) флейк-проба;
(d) правило живёт в TRACKED-шаблонах ролей.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from awf import api, verify_pack

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "suite-canonical.sh"
ROLES = REPO_ROOT / ".agentic" / "roles"
TEMPLATES = REPO_ROOT / "awf" / "templates" / "roles"

#: Split so the ratchet's literal-token counter does not read this fixture id
#: as debt (R4: fixture noise is removed, not ratified by raising the bar).
TID = "TO" "DO-0001"
TEST_CMD = "python3 -m pytest tests/unit/test_x.py -q"
HEAVY_CMD = "bash scripts/run-all.sh"

STUB = """#!/usr/bin/env bash
printf 'run\\n' >> "$1"
echo "3 passed in 0.01s"
exit 0
"""


def _stub_command(tmp_path: Path) -> tuple[str, Path]:
    """A fake full-suite command: counts invocations, prints one pytest line."""
    stub = tmp_path / "stub.sh"
    counter = tmp_path / "counter.txt"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)
    return f"{stub} {counter}", counter


def _run_script(project: Path, cmd: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "AWF_SUITE_CMD": cmd}
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _artifacts(project: Path) -> list[Path]:
    return sorted((project / ".agentic" / "context").glob("SUITE-*.json"))


def _runs(counter: Path) -> int:
    return counter.read_text(encoding="utf-8").count("run")


def test_same_tree_sha_is_cached_and_force_reruns(tmp_git_repo: Path, tmp_path: Path):
    """(a) второй вызов на том же дереве — из артефакта; --force — прогон."""
    cmd, counter = _stub_command(tmp_path)

    first = _run_script(tmp_git_repo, cmd)
    assert first.returncode == 0, first.stderr
    assert _runs(counter) == 1
    artifacts = _artifacts(tmp_git_repo)
    assert len(artifacts) == 1

    second = _run_script(tmp_git_repo, cmd)
    assert second.returncode == 0, second.stderr
    assert "cached" in second.stdout
    assert _runs(counter) == 1, "повторный вызов на том же tree-sha не должен гонять тесты"

    forced = _run_script(tmp_git_repo, cmd, "--force")
    assert forced.returncode == 0, forced.stderr
    assert _runs(counter) == 2


def test_changed_command_invalidates_the_cache(tmp_git_repo: Path, tmp_path: Path):
    """(a) кэш привязан к дереву И к команде: другая команда — новый прогон.

    Иначе артефакт узкого прогона (`AWF_SUITE_CMD="... tests/unit"`) был бы
    отдан роли как канонический полный сьют на том же дереве.
    """
    first_cmd, first_counter = _stub_command(tmp_path)
    first = _run_script(tmp_git_repo, first_cmd)
    assert first.returncode == 0, first.stderr
    assert _runs(first_counter) == 1

    other_dir = tmp_path / "other"
    other_dir.mkdir()
    second_cmd, second_counter = _stub_command(other_dir)
    second = _run_script(tmp_git_repo, second_cmd)

    assert second.returncode == 0, second.stderr
    assert "cached" not in second.stdout
    assert _runs(second_counter) == 1, "другая команда обязана перезапуститься"
    artifacts = _artifacts(tmp_git_repo)
    assert len(artifacts) == 2, "разные команды — разные файлы артефакта"
    payloads = [json.loads(p.read_text(encoding="utf-8")) for p in artifacts]
    assert {p["command"] for p in payloads} == {first_cmd, second_cmd}
    assert len({p.name for p in artifacts}) == 2, "cmd8 отличает файлы на одном дереве"


def test_artifact_has_facts_and_foreign_sha_reruns(tmp_git_repo: Path, tmp_path: Path):
    """(b) артефакт: sha/exit/счётчики; изменённое дерево — новый прогон."""
    cmd, counter = _stub_command(tmp_path)

    first = _run_script(tmp_git_repo, cmd)
    assert first.returncode == 0, first.stderr
    old = json.loads(_artifacts(tmp_git_repo)[0].read_text(encoding="utf-8"))
    assert old["sha"] and len(old["sha"]) == 64
    assert old["exit"] == 0
    assert old["counts"]["passed"] == 3
    assert old["started"] and old["finished"]
    assert "3 passed" in old["tail"]

    (tmp_git_repo / "README.md").write_text("changed\n", encoding="utf-8")
    second = _run_script(tmp_git_repo, cmd)
    assert second.returncode == 0, second.stderr
    assert _runs(counter) == 2, "другое дерево обязано дать новый прогон"

    artifacts = _artifacts(tmp_git_repo)
    assert len(artifacts) == 2
    shas = {json.loads(p.read_text(encoding="utf-8"))["sha"] for p in artifacts}
    assert old["sha"] in shas
    assert len(shas) == 2, "изменённое дерево обязано дать другой sha"


def _project_with_contract(
    tmp_path: Path, verify: str = TEST_CMD, front_extra: str = ""
) -> Path:
    project = tmp_path / "project"
    inbox = project / ".agentic" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / f"{TID}.md").write_text(
        f"---\nverify:\n  - {verify}\n{front_extra}---\n\n# {TID}\n",
        encoding="utf-8",
    )
    return project


def _fake_runner(monkeypatch, outcomes):
    """Patch the command runner; ruff stays green, contract commands take outcomes."""
    calls: list[str] = []

    def fake(cmd, cwd, timeout):
        calls.append(cmd)
        if cmd.startswith("ruff"):
            return 0, "", False
        return next(outcomes)

    monkeypatch.setattr(verify_pack, "_run_cmd", fake)
    return calls


def _report(project: Path, tmp_path: Path) -> tuple[verify_pack.VerifyPackResult, str]:
    report_path = tmp_path / "GATES.md"
    result = verify_pack.verify_pack(
        project, TID, report_path=report_path, write_report=True
    )
    return result, report_path.read_text(encoding="utf-8")


def test_flake_probe_marks_deterministic_command_ok(tmp_path, monkeypatch):
    """(c) одинаковая тест-команда дважды — `flake: ok (2/2)`, ровно два прогона."""
    project = _project_with_contract(tmp_path)
    calls = _fake_runner(monkeypatch, iter([(0, "1 passed", False)] * 2))

    result, report = _report(project, tmp_path)

    assert calls.count(TEST_CMD) == 2
    assert result.sections["contract_tests"] == "pass"
    assert result.verdict == "ok"
    assert "flake: ok (2/2)" in report


def test_flake_probe_warns_on_unstable_command_without_failing_gate(tmp_path, monkeypatch):
    """(c) первый pass, второй fail — warning `flake: unstable`, гейт зелёный."""
    project = _project_with_contract(tmp_path)
    _fake_runner(
        monkeypatch,
        iter([(0, "1 passed", False), (1, "1 failed", False)]),
    )

    result, report = _report(project, tmp_path)

    assert result.sections["contract_tests"] == "pass"
    assert result.verdict == "ok"
    assert "flake: unstable" in report
    assert "WARNING" in report
    assert "flake: ok (2/2)" not in report


def test_flake_probe_keeps_stable_failure_red(tmp_path, monkeypatch):
    """Нестабильность — предупреждение, но стабильное падение валит гейт."""
    project = _project_with_contract(tmp_path)
    _fake_runner(monkeypatch, iter([(1, "1 failed", False)] * 2))

    result, report = _report(project, tmp_path)

    assert result.sections["contract_tests"] == "fail"
    assert result.verdict == "failed"
    assert "flake: ok (2/2)" in report


def test_first_failure_is_not_hidden_by_a_passing_second_run(tmp_path, monkeypatch):
    """(c) первый fail, второй pass — гейт красный, строка называет оба исхода."""
    project = _project_with_contract(tmp_path)
    calls = _fake_runner(
        monkeypatch,
        iter([(1, "1 failed", False), (0, "1 passed", False)]),
    )

    result, report = _report(project, tmp_path)

    assert calls.count(TEST_CMD) == 2
    assert result.sections["contract_tests"] == "fail"
    assert result.verdict == "failed"
    assert "second run passed" in report
    assert "the red is not hidden" in report


def test_heavy_command_runs_once(tmp_path, monkeypatch):
    """(c) тяжёлые ворота не удваиваются: ровно один прогон, без `flake:`."""
    project = _project_with_contract(tmp_path, verify=HEAVY_CMD)
    calls = _fake_runner(monkeypatch, iter([(0, "gates ok", False)]))

    result, report = _report(project, tmp_path)

    assert calls.count(HEAVY_CMD) == 1
    assert result.sections["contract_tests"] == "pass"
    assert result.verdict == "ok"
    assert "flake:" not in report


def test_coverage_wrapper_is_not_doubled(tmp_path, monkeypatch):
    """(c) `coverage run -m pytest` — тяжёлая обёртка: один прогон (R1).

    Подстрочный поиск токена `pytest` удвоил бы coverage; структурная
    проверка сегмента видит вызываемую программу `coverage`.
    """
    cmd = "coverage run -m pytest tests/ -q"
    project = _project_with_contract(tmp_path, verify=cmd)
    calls = _fake_runner(monkeypatch, iter([(0, "coverage ok", False)]))

    result, report = _report(project, tmp_path)

    assert calls.count(cmd) == 1
    assert result.sections["contract_tests"] == "pass"
    assert "flake:" not in report


@pytest.mark.parametrize(
    "cmd",
    [
        "python3 -m pytest tests/ -q; echo done",
        "python3 -m pytest tests/ -q&&echo ok",
        "cd tests;pytest . -q",
    ],
)
def test_shell_operators_without_spaces_still_double(cmd, tmp_path, monkeypatch):
    """(c) S3: оператор без пробелов не выключает флейк-пробу."""
    project = _project_with_contract(tmp_path, verify=cmd)
    calls = _fake_runner(monkeypatch, iter([(0, "1 passed", False)] * 2))

    result, _report_text = _report(project, tmp_path)

    assert calls.count(cmd) == 2
    assert result.sections["contract_tests"] == "pass"
    assert result.verdict == "ok"


@pytest.mark.parametrize(
    "cmd",
    [
        "uv run pytest tests/ -q",
        "poetry run pytest tests/ -q",
        "env python3 -m pytest tests/ -q",
        "python3 -m pytest tests/ -q",
    ],
)
def test_runner_wrappers_are_doubled(cmd, tmp_path, monkeypatch):
    """(c) S3: обёртки-раннеры считаются тест-подобными → ×2."""
    project = _project_with_contract(tmp_path, verify=cmd)
    calls = _fake_runner(monkeypatch, iter([(0, "1 passed", False)] * 2))

    result, _report_text = _report(project, tmp_path)

    assert calls.count(cmd) == 2
    assert result.sections["contract_tests"] == "pass"
    assert result.verdict == "ok"


def test_timeout_is_not_retried(tmp_path, monkeypatch):
    """(c) после timeout повтора нет: один прогон и fail."""
    project = _project_with_contract(tmp_path)
    calls = _fake_runner(monkeypatch, iter([(None, "", True)]))
    result, report = _report(project, tmp_path)

    assert calls.count(TEST_CMD) == 1
    assert result.sections["contract_tests"] == "fail"
    assert result.verdict == "failed"
    assert "timeout after" in report


def test_contract_can_turn_the_flake_probe_off(tmp_path, monkeypatch):
    """(c) `flake: off` — тест-команда выполняется один раз."""
    project = _project_with_contract(tmp_path, front_extra="flake: off\n")
    calls = _fake_runner(monkeypatch, iter([(0, "1 passed", False)]))
    result, report = _report(project, tmp_path)

    assert calls.count(TEST_CMD) == 1
    assert result.sections["contract_tests"] == "pass"
    assert result.verdict == "ok"
    assert "flake:" not in report


def test_init_installs_the_canonical_suite_script(tmp_git_repo: Path):
    """(d) S4: `awf init` кладёт скрипт в чужой проект — правило не упирается в 404."""
    result = api.init_project(tmp_git_repo, project_name="M1Suite")
    dest = tmp_git_repo / "scripts" / "suite-canonical.sh"
    assert dest.is_file(), "init обязан положить scripts/suite-canonical.sh"
    assert os.access(dest, os.X_OK), "скрипт должен быть исполняемым"
    assert "tree-sha" in dest.read_text(encoding="utf-8")
    assert "scripts/suite-canonical.sh" in result.created_files


def test_init_falls_back_to_the_package_templates(tmp_git_repo: Path, tmp_path, monkeypatch):
    """S4: в wheel нет корневой templates/ — init берёт awf/templates."""
    from awf.api import lifecycle

    pkg = tmp_path / "site-packages"
    fake = pkg / "awf" / "api" / "lifecycle.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("", encoding="utf-8")
    shipped = REPO_ROOT / "awf" / "templates" / "scripts" / "suite-canonical.sh"
    template = pkg / "awf" / "templates" / "scripts" / "suite-canonical.sh"
    template.parent.mkdir(parents=True)
    template.write_bytes(shipped.read_bytes())
    monkeypatch.setattr(lifecycle, "__file__", str(fake))
    result = api.init_project(tmp_git_repo, project_name="WheelInstall")
    dest = tmp_git_repo / "scripts" / "suite-canonical.sh"
    assert dest.read_bytes() == shipped.read_bytes()
    assert "scripts/suite-canonical.sh" in result.created_files


def test_unwritable_exclude_warns_instead_of_silence(tmp_git_repo: Path, tmp_path: Path):
    """(в) S4: не удалось записать .git/info/exclude — предупреждение в stderr."""
    exclude = tmp_git_repo / ".git" / "info" / "exclude"
    if exclude.exists():
        exclude.unlink()
    exclude.mkdir()
    cmd, _counter = _stub_command(tmp_path)

    result = _run_script(tmp_git_repo, cmd)

    assert result.returncode == 0
    assert "WARNING" in result.stderr
    assert "exclude" in result.stderr


CANONICAL_ROLES = [
    "agent-implementer.md",
    "agent-architect-review.md",
    "agent-qa-review.md",
    "agent-project-auditor.md",
]


def _assert_canonical_rule(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "<!-- M1-canonical-suite:start -->" in text
    assert "<!-- M1-canonical-suite:end -->" in text
    assert "scripts/suite-canonical.sh" in text
    assert "tree-sha" in text


@pytest.mark.parametrize("role", ["agent-implementer.md", "agent-qa-review.md"])
def test_tracked_role_templates_carry_the_canonical_suite_rule(role: str):
    """(d) правило доказано на TRACKED-шаблонах — свежий клон его видит."""
    _assert_canonical_rule(TEMPLATES / role)


@pytest.mark.parametrize("role", CANONICAL_ROLES)
def test_materialized_roles_carry_the_canonical_suite_rule(role: str):
    """(d) materialized `.agentic/roles/` — вторая строка проверки."""
    path = ROLES / role
    if not path.is_file():
        pytest.skip(f"{path} not materialized — .agentic/ is runtime, not tracked")
    _assert_canonical_rule(path)
