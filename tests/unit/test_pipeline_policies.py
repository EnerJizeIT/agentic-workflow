"""A33: one policy validator — the write path and the loader agree.

Two entry points used to own the policy rules: the YAML loader
(``validate_pipeline_document``) knew ``fix_round:<stage>`` while the write
path (``validate_pipeline_stages``, behind ``awf_write_pipeline``) refused it
with "is not a valid policy. Expected one of: escalate, replan". A pipeline
written by hand worked; the same pipeline written through the tool did not.

Now both paths go through ``validate_pipeline_stages``: the same allowed
words, the same prefixes, the same refusal text. These tests lock that down
from the outside — through the public API and the public loader.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api
from awf.api._errors import AwfApiError
from awf.pipeline import (
    load_stages,
    validate_pipeline_document,
    validate_pipeline_stages,
)


def _project(tmp_path: Path) -> Path:
    """Minimal awf project: .agentic/ + config (no pipeline needed)."""
    proj = tmp_path / "proj"
    (proj / ".agentic").mkdir(parents=True)
    (proj / ".agentic" / "config.yaml").write_text(
        "project:\n  name: Policies\n", encoding="utf-8"
    )
    return proj


def _stages(**review_policy: str) -> list[dict[str, object]]:
    """plan → implementer → reviewer → verify; the reviewer is the knobs."""
    review: dict[str, object] = {
        "id": "agent-qa-review",
        "role": "agent-qa-review",
    }
    review.update(review_policy)
    return [
        {"id": "plan", "role": "supervisor"},
        {"id": "agent-implementer", "role": "agent-implementer"},
        review,
        {"id": "verify", "role": "supervisor"},
    ]


# ─── the write path accepts what the loader accepts ──────────────────────


def test_write_pipeline_accepts_fix_round_and_the_loader_reads_it_back(tmp_path):
    """The reported pain: fix_round through the tool, then a plain read."""
    proj = _project(tmp_path)

    result = api.write_pipeline(
        proj, "fixes", _stages(on_rejected="fix_round:agent-implementer")
    )

    path = Path(result.file)
    assert path.is_file()
    loaded = load_stages(path)
    by_name = {stage.name: stage for stage in loaded}
    assert by_name["agent-qa-review"].on_rejected == "fix_round:agent-implementer"
    # The loader's own document check agrees with the file it just read.
    import yaml

    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    validate_pipeline_document(document, path.name)


def test_write_path_holds_the_fix_round_target_rules(tmp_path):
    """A target that is not an execute stage is refused on write, not at run."""
    proj = _project(tmp_path)

    with pytest.raises(AwfApiError, match="execute"):
        api.write_pipeline(
            proj, "bad-target", _stages(on_rejected="fix_round:plan")
        )
    assert not (proj / ".agentic" / "pipelines" / "bad-target.yaml").exists()


# ─── the rules themselves did not move ──────────────────────────────────


def test_fix_round_on_blocked_is_refused_with_a_clear_text():
    stages = _stages(on_blocked="fix_round:agent-implementer")

    with pytest.raises(AwfApiError) as caught:
        validate_pipeline_stages(stages, "test")

    text = str(caught.value)
    assert "on_blocked" in text
    assert "fix_round:<stage> is only supported for on_rejected" in text


@pytest.mark.parametrize(
    "policy",
    ["escalate", "replan", "rollback_to:agent-implementer"],
)
def test_plain_policies_still_work_on_rejected(policy):
    validated = validate_pipeline_stages(
        _stages(on_rejected=policy), "test"
    )
    assert validated[2]["on_rejected"] == policy


def test_rollback_to_still_works_on_blocked_and_still_not_on_approved():
    ok = validate_pipeline_stages(
        _stages(on_blocked="rollback_to:agent-implementer"), "test"
    )
    assert ok[2]["on_blocked"] == "rollback_to:agent-implementer"

    with pytest.raises(AwfApiError, match="rollback_to:<stage> is only supported"):
        validate_pipeline_stages(
            _stages(on_approved="rollback_to:agent-implementer"), "test"
        )

    # A33: a bare ``rollback_to:`` is a missing operand, not a stale target
    # — refused by name here, instead of only warning at load time.
    with pytest.raises(AwfApiError, match="rollback_to needs a stage name"):
        validate_pipeline_stages(_stages(on_blocked="rollback_to:"), "test")


def test_plain_words_still_work_on_every_key():
    validated = validate_pipeline_stages(
        _stages(
            on_blocked="stop",
            on_approved="commit_and_next",
            on_rejected="replan",
            on_failed="escalate",
        ),
        "test",
    )
    assert validated[2]["on_approved"] == "commit_and_next"
    assert validated[2]["on_blocked"] == "stop"


# ─── one source of the refusal text ─────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("on_rejected", "bogus"),                  # unknown word
        ("on_rejected", "fix_round:"),             # empty target, right key
        ("on_blocked", "rollback_to:"),            # empty target, allowed key
        ("on_approved", "rollback_to:agent-implementer"),  # right prefix, wrong key
    ],
)
def test_the_same_bad_policy_gives_the_same_text_from_both_paths(key, bad_value):
    stages = _stages(**{key: bad_value})
    source = "pipeline 'x'"  # the write path's label; same for both calls

    with pytest.raises(AwfApiError) as from_stages:
        validate_pipeline_stages(stages, source)
    with pytest.raises(AwfApiError) as from_document:
        validate_pipeline_document({"name": "x", "stages": stages}, source)

    assert str(from_document.value) == str(from_stages.value)


def test_the_write_path_and_the_loader_report_the_same_invalid_policy(tmp_path):
    """Same file-level story: the API refusal equals the loader refusal."""
    proj = _project(tmp_path)
    stages = _stages(on_rejected="bogus")

    with pytest.raises(AwfApiError) as from_write:
        api.write_pipeline(proj, "broken", stages)
    with pytest.raises(AwfApiError) as from_loader:
        validate_pipeline_document(
            {"name": "broken", "stages": stages}, "pipeline 'broken'"
        )

    assert str(from_write.value) == str(from_loader.value)


# ─── what the API writes is what the engine can resolve ─────────────────


def test_a_written_fix_round_reaches_the_transition_resolver(tmp_path):
    """The endpoint of the round-trip: the engine, not just the validator.

    A policy the write path accepts but ``resolve_transition`` cannot turn
    into a stage name is the A33 complaint one layer deeper: the file is
    written, and the first rejection dies with "target not found".
    """
    from awf.transitions import resolve_transition

    proj = _project(tmp_path)
    result = api.write_pipeline(
        proj, "fixes", _stages(on_rejected="fix_round:agent-implementer")
    )

    stages = {stage.name: stage for stage in load_stages(Path(result.file))}

    assert resolve_transition(stages["agent-qa-review"], "rejected") == (
        "fix_round",
        "agent-implementer",
    )


def test_a_written_rollback_to_survives_the_loader_and_resolves(tmp_path):
    """The second prefixed policy round-trips too — not only fix_round."""
    from awf.transitions import resolve_transition

    proj = _project(tmp_path)
    result = api.write_pipeline(
        proj, "rollback", _stages(on_blocked="rollback_to:agent-implementer")
    )

    stages = {stage.name: stage for stage in load_stages(Path(result.file))}

    assert stages["agent-qa-review"].on_blocked == "rollback_to:agent-implementer"
    assert resolve_transition(stages["agent-qa-review"], "blocked") == (
        "rollback",
        "agent-implementer",
    )


@pytest.mark.parametrize(
    ("key", "bad_value", "hint"),
    [
        ("on_rejected", "fix_round:", "fix_round needs a stage name"),
        ("on_blocked", "rollback_to:", "rollback_to needs a stage name"),
    ],
)
def test_write_path_refuses_an_empty_target_and_writes_nothing(
    tmp_path, key, bad_value, hint
):
    """An empty target is refused before the file exists — not a warning."""
    proj = _project(tmp_path)

    with pytest.raises(AwfApiError, match=hint):
        api.write_pipeline(proj, "empty-target", _stages(**{key: bad_value}))

    pipelines = proj / ".agentic" / "pipelines"
    assert not (pipelines / "empty-target.yaml").exists()
