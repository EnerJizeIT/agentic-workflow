"""SPEC IMP8 — the readiness path end to end (T15, AC25, C1/C3).

One package crosses the whole path: READY gives a dev TODO with the norm
IDs, an unaccepted review and an empty section refuse without TODO,
baseline, lease or PID, the gate holds at creation AND at start/continue,
and verify-pack reports the package budget as a reference without calling
the readiness predicate (D6). No language model runs here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api
from awf import spec_package as sp
from awf.api import _lease, pipeline
from tests.spec_support import init_project_repo, package_content, write_package

TODO = "TODO-0001"


def _no_side_effects(repo: Path) -> None:
    inbox = repo / ".agentic" / "inbox"
    assert not (inbox / f"{TODO}.md").exists(), "TODO was created on refusal"
    assert not (inbox / f"{TODO}.ready").exists(), ".ready was created on refusal"
    assert not list(
        (repo / ".agentic" / "context").glob(f"BASELINE-{TODO}.*")
    ), "baseline was created on refusal"
    assert not _lease.lease_path(repo).exists(), "the lease was taken"


def _refused(repo: Path, content: str, needle: str = "") -> str:
    with pytest.raises(api.AwfApiError) as excinfo:
        api.dispatch_todo(repo, content, todo_id=TODO)
    message = str(excinfo.value)
    _no_side_effects(repo)
    if needle:
        assert needle in message, f"{needle!r} not in {message!r}"
    return message


def _empty_invariant_package(repo: Path) -> None:
    sample = write_package(repo, commit=False)
    text = (sample / "pkg.md").read_text(encoding="utf-8")
    assert "Текст инварианта." in text
    write_package(
        repo,
        documents={"pkg.md": text.replace("Текст инварианта.\n", "")},
    )


def _not_launched(repo: Path) -> None:
    assert not _lease.lease_path(repo).exists(), "the lease was taken"
    logs = repo / ".agentic" / "logs"
    assert not (logs / "awf-start.pid").exists(), "the PID file was written"
    assert not (logs / "awf-start.out").exists(), "a pipeline log was opened"


def _ready_todo(repo: Path) -> None:
    write_package(repo)
    api.dispatch_todo(repo, package_content(TODO), todo_id=TODO)


def test_ready_package_gives_a_dev_todo_with_norm_ids(tmp_path: Path) -> None:
    repo = init_project_repo(tmp_path)
    _ready_todo(repo)
    body = (repo / ".agentic" / "inbox" / f"{TODO}.md").read_text(encoding="utf-8")
    assert "## Пакет спецификации" in body
    assert "- ID: R1, AC1, INV1" in body
    assert "## Первоисточник (R1 — Первое правило)" in body
    assert "Текст правила." in body


def test_review_not_accepted_refused_without_side_effects(tmp_path: Path) -> None:
    repo = init_project_repo(tmp_path)
    sample = write_package(repo, commit=False)
    review = (sample / "pkg.requirements-review.md").read_text(encoding="utf-8")
    write_package(
        repo,
        documents={
            "pkg.requirements-review.md": review.replace(
                "Статус: PASS", "Статус: FAIL"
            )
        },
    )
    _refused(repo, package_content(TODO), "FAIL")


def test_empty_section_refused_without_side_effects(tmp_path: Path) -> None:
    repo = init_project_repo(tmp_path)
    _empty_invariant_package(repo)
    _refused(
        repo,
        package_content(
            TODO, ["spec/pkg/pkg.md#INV1 — Не должно измениться"]
        ),
        "empty body",
    )


def test_gate_holds_at_start_and_continue_and_never_reaches_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_project_repo(tmp_path)
    _ready_todo(repo)
    requirements = repo / "spec" / "pkg" / "pkg.md"
    original = requirements.read_text(encoding="utf-8")
    requirements.write_text(original + "\nправка без ревизии\n", encoding="utf-8")

    for launch in (pipeline.start_pipeline, pipeline.continue_pipeline):
        result = launch(repo)
        assert result.run_mode == "noop"
        assert "spec gate" in result.message
        _not_launched(repo)

    requirements.write_text(original, encoding="utf-8")

    class Sentinel(_lease.LeaseHeldError):
        pass

    def refuse(*_args, **_kwargs):
        raise Sentinel("sentinel-after-gate")

    monkeypatch.setattr(_lease, "acquire", refuse)
    started = pipeline.start_pipeline(repo, background=True)
    assert "sentinel-after-gate" in started.message


def test_verify_pack_reports_budget_without_rechecking_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = init_project_repo(tmp_path)
    _ready_todo(repo)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("verify-pack must not re-check readiness (D6)")

    monkeypatch.setattr(sp, "spec_gate", forbidden)
    result = api.verify_pack(repo, TODO, write_report=False)

    assert result.sections.get("spec_budget") == "pass"
    assert "words" in result.details.get("spec_budget", "")


def test_late_over_budget_edit_stays_a_reference_and_blocks_new_starts(
    tmp_path: Path,
) -> None:
    """D6: a late edit must not kill finished work, but it is still shown.

    The package is edited after a READY dispatch until one document crosses
    the K6 limit. Verify-pack keeps the section ``pass`` and only marks the
    row MAJOR; a NEW start sees the same bytes and the gate refuses.
    """
    repo = init_project_repo(tmp_path)
    _ready_todo(repo)
    requirements = repo / "spec" / "pkg" / "pkg.md"
    text = requirements.read_text(encoding="utf-8")
    requirements.write_text(
        text + "\n" + " ".join(["слово"] * 2201) + "\n", encoding="utf-8"
    )

    result = api.verify_pack(repo, TODO)
    assert result.sections.get("spec_budget") == "pass"
    report = (repo / ".agentic" / "context" / f"GATES-{TODO}.md").read_text(
        encoding="utf-8"
    )
    expected_words = len(text.split()) + 2201
    assert str(expected_words) in report
    assert "MAJOR" in report and "порог 2200" in report

    started = pipeline.start_pipeline(repo)
    assert started.run_mode == "noop"
    assert "spec gate" in started.message
    _not_launched(repo)


def test_budget_section_is_skipped_without_a_package(tmp_path: Path) -> None:
    """Legacy path: a TODO without ``spec_package`` gets the old behaviour."""
    repo = init_project_repo(tmp_path)
    api.dispatch_todo(repo, f"# {TODO} — unit\n\nтело\n", todo_id=TODO)

    result = api.verify_pack(repo, TODO, write_report=False)

    assert result.sections.get("spec_budget") == "skipped"


def test_budget_section_names_a_broken_contract(tmp_path: Path) -> None:
    """A block that exists but does not parse is named, not shown as absent."""
    repo = init_project_repo(tmp_path)
    inbox = repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{TODO}.md").write_text(
        "---\nspec_package: [\n---\n# TODO-0001 — unit\n", encoding="utf-8"
    )

    result = api.verify_pack(repo, TODO, write_report=False)

    assert result.sections.get("spec_budget") == "skipped"
    assert "broken contract" in result.details.get("spec_budget", "")
