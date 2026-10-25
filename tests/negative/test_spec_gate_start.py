"""SPEC IMP3 — K3 gate at start and continue (T6, AC29).

A TODO created from a READY package must be refused when the package is no
longer READY (edited bytes, blocked readiness). The refusal comes BEFORE
the lease, the PID file and any stage: nothing is launched.
"""
from __future__ import annotations

from pathlib import Path

from awf import api
from awf.api import _lease, pipeline
from awf.pipeline_state import read_state
from tests.spec_support import (
    init_project_repo,
    package_content,
    write_package,
)

TODO = "TODO-0001"


def _ready_todo(tmp_path: Path) -> tuple[Path, Path]:
    repo = init_project_repo(tmp_path)
    package = write_package(repo)
    api.dispatch_todo(repo, package_content(TODO), todo_id=TODO)
    return repo, package


def _break_package(package: Path, name: str = "pkg.md") -> None:
    requirements = package / name
    requirements.write_text(
        requirements.read_text(encoding="utf-8") + "\nправка без ревизии\n",
        encoding="utf-8",
    )


def _assert_not_launched(repo: Path) -> None:
    assert not _lease.lease_path(repo).exists(), "the lease was taken"
    logs = repo / ".agentic" / "logs"
    assert not (logs / "awf-start.pid").exists(), "the PID file was written"
    assert not (logs / "awf-start.out").exists(), "a pipeline log was opened"
    state = read_state(repo) or {}
    assert not state.get("stage_name"), "a stage was recorded"


def test_start_refused_before_lease_and_pid(tmp_path):
    repo, package = _ready_todo(tmp_path)
    _break_package(package)

    result = pipeline.start_pipeline(repo, background=True)

    assert result.run_mode == "noop"
    assert "spec gate" in result.message
    assert result.run_id is None
    _assert_not_launched(repo)


def test_continue_refused_before_lease_and_pid(tmp_path):
    repo, package = _ready_todo(tmp_path)
    _break_package(package)

    result = pipeline.continue_pipeline(repo, background=True)

    assert result.run_mode == "noop"
    assert "spec gate" in result.message
    _assert_not_launched(repo)


def test_blocked_readiness_refused_on_continue(tmp_path):
    repo, package = _ready_todo(tmp_path)
    readiness = package / "pkg.readiness.md"
    readiness.write_text(
        readiness.read_text(encoding="utf-8").replace(
            "Статус: READY", "Статус: BLOCKED"
        )
        + "\n## Открытые вопросы\n\n"
        "| ID | Вопрос | Владелец | Влияние |\n"
        "|---|---|---|---|\n"
        "| Q1 | Нужен ли X? | аналитик | блокирует R1 |\n",
        encoding="utf-8",
    )

    result = pipeline.continue_pipeline(repo)

    assert result.run_mode == "noop"
    assert "Нужен ли X?" in result.message
    assert "аналитик" in result.message
    _assert_not_launched(repo)


def test_ready_package_passes_the_gate(tmp_path, monkeypatch):
    """The gate passes BEFORE the lease — a lease refusal proves the order."""
    repo, _package = _ready_todo(tmp_path)

    class Sentinel(_lease.LeaseHeldError):
        pass

    def refuse(*_args, **_kwargs):
        raise Sentinel("sentinel-after-gate")

    monkeypatch.setattr(_lease, "acquire", refuse)
    result = pipeline.start_pipeline(repo, background=True)
    assert "sentinel-after-gate" in result.message


def _two_units(tmp_path: Path) -> Path:
    """TODO-0001 on a READY package, TODO-0002 on a package broken afterwards.

    TODO-0002 is the newest active unit, so a gate resolving "newest active"
    instead of the caller's unit would refuse TODO-0001 by mistake.
    """
    repo = init_project_repo(tmp_path)
    write_package(repo, scope="pkg")
    write_package(repo, scope="pkg2")
    api.dispatch_todo(
        repo,
        package_content("TODO-0001", ["spec/pkg/pkg.md#R1 — Первое правило"]),
        todo_id="TODO-0001",
    )
    api.dispatch_todo(
        repo,
        package_content(
            "TODO-0002",
            ["spec/pkg2/pkg2.md#R1 — Первое правило"],
            scope="spec/pkg2",
        ),
        todo_id="TODO-0002",
    )
    _break_package(repo / "spec" / "pkg2", "pkg2.md")
    return repo


def _sentinel_lease(monkeypatch) -> None:
    class Sentinel(_lease.LeaseHeldError):
        pass

    def refuse(*_args, **_kwargs):
        raise Sentinel("sentinel-after-gate")

    monkeypatch.setattr(_lease, "acquire", refuse)


def test_start_gate_checks_the_pinned_unit_not_the_newest(tmp_path, monkeypatch):
    """K3/D2: the gate refuses the PINNED unit, not "newest active".

    A broken package on the newest TODO must not block the pinned READY
    unit — and the pinned broken unit must still be refused.
    """
    repo = _two_units(tmp_path)
    _sentinel_lease(monkeypatch)

    result = pipeline.start_pipeline(repo, background=True, todo_id="TODO-0001")
    assert "sentinel-after-gate" in result.message, (
        "the gate checked the newest active TODO instead of the pinned one"
    )

    refused = pipeline.start_pipeline(repo, background=True, todo_id="TODO-0002")
    assert refused.run_mode == "noop"
    assert "spec gate" in refused.message


def test_continue_gate_checks_the_acked_unit_not_the_newest(tmp_path, monkeypatch):
    """K3/D2: continue resolves the unit through the SAME priority list."""
    repo = _two_units(tmp_path)
    _sentinel_lease(monkeypatch)

    result = pipeline.continue_pipeline(repo, ack="TODO-0001", background=True)
    assert "sentinel-after-gate" in result.message, (
        "the gate checked the newest active TODO instead of the acked unit"
    )

    refused = pipeline.continue_pipeline(repo, ack="TODO-0002", background=True)
    assert refused.run_mode == "noop"
    assert "spec gate" in refused.message


def test_legacy_contract_parse_error_is_not_a_spec_gate_refusal(tmp_path, monkeypatch):
    """AUD3: a unit WITHOUT ``spec_package`` keeps the pre-unit behavior.

    ``gates: [bogus-gate]`` is a broken LEGACY contract: the launch used to
    be refused as a "spec gate" failure even though no package was asked
    for. The gate must not even parse it.
    """
    repo = init_project_repo(tmp_path)
    inbox = repo / ".agentic" / "inbox"
    (inbox / "TODO-0001.md").write_text(
        "---\ngates: [bogus-gate]\n---\n# TODO-0001 — legacy unit\n",
        encoding="utf-8",
    )
    _sentinel_lease(monkeypatch)

    result = pipeline.start_pipeline(repo, background=True, todo_id="TODO-0001")

    assert "sentinel-after-gate" in result.message, (
        "a legacy contract must reach the lease, not the readiness gate"
    )
    assert "spec gate" not in result.message
    assert "bogus-gate" not in result.message


def test_spec_package_unit_with_a_broken_contract_is_still_refused(tmp_path, monkeypatch):
    """AUD3 guard: the key's presence keeps the readiness refusal."""
    repo = init_project_repo(tmp_path)
    inbox = repo / ".agentic" / "inbox"
    (inbox / "TODO-0001.md").write_text(
        "---\n"
        "spec_package: spec/pkg\n"
        "source_refs:\n"
        "  - spec/pkg/pkg.md#R1 — Первое правило\n"
        "gates: [bogus-gate]\n"
        "---\n"
        "# TODO-0001 — unit\n",
        encoding="utf-8",
    )
    _sentinel_lease(monkeypatch)

    result = pipeline.start_pipeline(repo, background=True, todo_id="TODO-0001")

    assert result.run_mode == "noop"
    assert "spec gate" in result.message
    assert "bogus-gate" in result.message
