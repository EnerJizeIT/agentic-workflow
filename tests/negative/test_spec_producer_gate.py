"""S14 (TODO-0288): the producer of a package is not consumer-gated.

A spec unit declares ``spec_package`` and the ``spec`` / ``spec-<slug>``
pipeline, and it is the run that WRITES the package. Holding it behind the
readiness gate deadlocks every BLOCKED package: the only run that could fix
the package is refused because the package is not READY yet. The gate stays
for consumer (development) units.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api
from awf.api import _lease, pipeline
from tests.spec_support import init_project_repo, package_content, write_package

TODO = "TODO-0001"


def _blocked_package(repo: Path) -> Path:
    package = write_package(repo)
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
    return package


def _producer_todo(repo: Path, pipeline_name: str = "spec-pkg") -> None:
    inbox = repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{TODO}.md").write_text(
        "---\n"
        "spec_package: spec/pkg\n"
        f"pipeline: {pipeline_name}\n"
        "---\n"
        f"# {TODO} — spec unit\n",
        encoding="utf-8",
    )


class _Sentinel(_lease.LeaseHeldError):
    pass


def _sentinel_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args, **_kwargs):
        raise _Sentinel("sentinel-after-gate")

    monkeypatch.setattr(_lease, "acquire", refuse)


def test_dispatch_producer_on_blocked_package_is_allowed(tmp_path: Path):
    repo = init_project_repo(tmp_path)
    _blocked_package(repo)
    content = (
        "---\n"
        "spec_package: spec/pkg\n"
        "pipeline: spec-pkg\n"
        "---\n"
        f"# {TODO} — spec unit\n"
    )

    result = api.dispatch_todo(repo, content, todo_id=TODO)

    assert result.todo_id == TODO
    body = (repo / ".agentic" / "inbox" / f"{TODO}.md").read_text(encoding="utf-8")
    assert "## Пакет спецификации" not in body, (
        "a producer gets no norm snapshot — it is writing the package"
    )


def test_dispatch_consumer_on_blocked_package_is_refused(tmp_path: Path):
    repo = init_project_repo(tmp_path)
    _blocked_package(repo)

    with pytest.raises(api.AwfApiError) as excinfo:
        api.dispatch_todo(repo, package_content(TODO), todo_id=TODO)

    assert "BLOCKED" in str(excinfo.value)
    assert not (repo / ".agentic" / "inbox" / f"{TODO}.md").exists()


def test_dispatch_producer_without_pipeline_names_the_cure(tmp_path: Path):
    """AUD1 (0288): the refusal must name the fix it demands.

    A hand-written producer TODO has ``spec_package`` but neither
    ``source_refs`` nor a ``pipeline:`` key. Dispatch cannot know the launch
    pipeline, so it refuses — the text has to name the way out instead of
    only restating what a producer is.
    """
    repo = init_project_repo(tmp_path)
    content = (
        "---\nspec_package: spec/pkg\n---\n"
        f"# {TODO} — spec unit\n"
    )

    with pytest.raises(api.AwfApiError) as excinfo:
        api.dispatch_todo(repo, content, todo_id=TODO)

    message = str(excinfo.value)
    assert "pipeline: spec" in message, message
    assert "--pipeline" in message, message
    assert not (repo / ".agentic" / "inbox" / f"{TODO}.md").exists()


def test_start_producer_on_blocked_package_passes_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = init_project_repo(tmp_path)
    _blocked_package(repo)
    _producer_todo(repo)
    _sentinel_lease(monkeypatch)

    result = pipeline.start_pipeline(repo, background=True, todo_id=TODO)

    assert "sentinel-after-gate" in result.message, (
        "the producer run was held by the consumer readiness gate"
    )


def test_start_producer_with_explicit_pipeline_passes_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = init_project_repo(tmp_path)
    _blocked_package(repo)
    inbox = repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{TODO}.md").write_text(
        "---\nspec_package: spec/pkg\n---\n"
        f"# {TODO} — spec unit\n",
        encoding="utf-8",
    )
    _sentinel_lease(monkeypatch)

    result = pipeline.start_pipeline(
        repo, background=True, todo_id=TODO, pipeline="spec-pkg"
    )

    assert "sentinel-after-gate" in result.message


def test_continue_producer_on_blocked_package_passes_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = init_project_repo(tmp_path)
    _blocked_package(repo)
    _producer_todo(repo)
    _sentinel_lease(monkeypatch)

    result = pipeline.continue_pipeline(repo, ack=TODO, pipeline="spec-pkg")

    assert "sentinel-after-gate" in result.message


def test_start_consumer_on_blocked_package_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = init_project_repo(tmp_path)
    _blocked_package(repo)
    # Write the consumer TODO directly (dispatch would refuse it) so the
    # launch surface itself is exercised on a BLOCKED package.
    inbox = repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{TODO}.md").write_text(
        package_content(TODO), encoding="utf-8"
    )
    _sentinel_lease(monkeypatch)

    result = pipeline.start_pipeline(repo, background=True, todo_id=TODO)

    assert result.run_mode == "noop"
    assert "spec gate" in result.message


def _write_spec_snapshot(repo: Path, pipeline_name: str = "spec-pkg") -> None:
    """Freeze the unit's pipeline snapshot, as the first run would."""
    from awf import paths
    from awf.api import setup
    from awf.pipeline import load_stages, pipeline_snapshot_text

    result, _unresolved = setup.write_spec_pipeline(repo, "pkg")
    stages = load_stages(Path(result.file))
    snapshot = paths.pipeline_snapshot_path(repo, TODO)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(
        pipeline_snapshot_text(stages, pipeline_name, TODO), encoding="utf-8"
    )


def test_continue_producer_resumes_from_the_recorded_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A resume names no pipeline: the frozen snapshot must still say spec.

    The TODO deliberately has no ``pipeline:`` key — it was started with
    ``--pipeline`` (the command ``awf spec init`` prints). After a crash or
    ``awf kill`` every launch path resumes without that argument, and the
    engine runs the snapshot; the gate must read the same definition or the
    producer deadlock comes back on the resume path.
    """
    repo = init_project_repo(tmp_path)
    _blocked_package(repo)
    inbox = repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{TODO}.md").write_text(
        "---\nspec_package: spec/pkg\n---\n"
        f"# {TODO} — spec unit\n",
        encoding="utf-8",
    )
    _write_spec_snapshot(repo)
    _sentinel_lease(monkeypatch)

    result = pipeline.continue_pipeline(repo, ack=TODO)

    assert "sentinel-after-gate" in result.message, (
        "a resume without an explicit pipeline was held by the gate"
    )


def test_continue_without_snapshot_still_refuses_a_packageless_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No snapshot and no pipeline argument = the default pipeline.

    The producer claim needs a spec pipeline SOMEWHERE (snapshot, argument
    or the TODO's own key). Without one the unit is a consumer and the
    readiness gate keeps its refusal — the fallback must not become a
    silent bypass.
    """
    repo = init_project_repo(tmp_path)
    _blocked_package(repo)
    inbox = repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{TODO}.md").write_text(
        "---\nspec_package: spec/pkg\n---\n"
        f"# {TODO} — spec unit\n",
        encoding="utf-8",
    )
    _sentinel_lease(monkeypatch)

    result = pipeline.continue_pipeline(repo, ack=TODO)

    assert result.run_mode == "noop"
    assert "spec gate" in result.message


def test_dispatch_producer_on_a_package_that_does_not_exist_yet(tmp_path: Path):
    """The first spec run: the package directory is the run's OUTPUT.

    The existing producer tests use a package that already exists (and is
    BLOCKED). The real producer start has no package at all — the gate must
    not turn "nothing to READY yet" into a refusal.
    """
    repo = init_project_repo(tmp_path)
    content = (
        "---\n"
        "spec_package: spec/pkg\n"
        "pipeline: spec-pkg\n"
        "---\n"
        f"# {TODO} — spec unit\n"
    )

    result = api.dispatch_todo(repo, content, todo_id=TODO)

    assert result.todo_id == TODO
    assert not (repo / "spec" / "pkg").exists(), "the dispatch created the package"
    body = (repo / ".agentic" / "inbox" / f"{TODO}.md").read_text(encoding="utf-8")
    assert "## Пакет спецификации" not in body


def test_start_producer_on_a_missing_package_passes_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = init_project_repo(tmp_path)
    _producer_todo(repo)
    _sentinel_lease(monkeypatch)

    result = pipeline.start_pipeline(repo, background=True, todo_id=TODO)

    assert "sentinel-after-gate" in result.message, (
        "a producer with no package yet was held by the readiness gate"
    )


def test_dispatch_consumer_on_a_ready_package_keeps_the_norm_snapshot(
    tmp_path: Path,
):
    """S14 narrowness: the consumer path is untouched (K4 snapshot).

    The producer branch sets ``package = ""`` to skip the gate; a consumer
    must still get both the gate and the ``## Пакет спецификации`` block.
    """
    repo = init_project_repo(tmp_path)
    write_package(repo)

    api.dispatch_todo(repo, package_content(TODO), todo_id=TODO)

    body = (repo / ".agentic" / "inbox" / f"{TODO}.md").read_text(encoding="utf-8")
    assert "## Пакет спецификации" in body
    assert "## Первоисточник" in body
