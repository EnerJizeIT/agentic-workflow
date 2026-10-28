"""S14 (TODO-0288) QA: the producer rule is narrow and back-compatible.

A producer is a package-WRITING unit: ``spec_package`` AND the ``spec`` /
``spec-<slug>`` pipeline (the TODO's own key or the launch name). Everything
else keeps the consumer contract — the rule must not become a silent bypass
for a dev unit or for a name that merely starts with the letters ``spec``.
"""
from __future__ import annotations

import pytest

from awf.unit_contract import is_spec_producer, parse_todo_contract


def _block(extra: str = "") -> str:
    return "---\nspec_package: spec/pkg\n" + extra + "---\n# TODO-0001 — unit\n"


@pytest.mark.parametrize("name", ["spec", "spec-pkg", "spec-audit"])
def test_spec_pipelines_are_producers(name: str):
    assert is_spec_producer({"spec_package": "spec/pkg"}, name) is True


@pytest.mark.parametrize(
    "name", ["", "default", "special", "specks", "Spec", "spec_pkg"]
)
def test_names_that_only_look_like_spec_are_consumers(name: str):
    assert is_spec_producer({"spec_package": "spec/pkg"}, name) is False


def test_a_unit_without_a_package_is_never_a_producer():
    assert is_spec_producer({}, "spec") is False
    assert is_spec_producer(None, "spec") is False


def test_the_todo_key_alone_marks_a_producer():
    contract = {"spec_package": "spec/pkg", "pipeline": "spec-pkg"}
    assert is_spec_producer(contract, "") is True


def test_a_todo_naming_a_dev_pipeline_stays_a_consumer():
    contract = {"spec_package": "spec/pkg", "pipeline": "default"}
    assert is_spec_producer(contract, "default") is False


def test_producer_may_omit_source_refs_with_the_launch_hint():
    contract, _unknown = parse_todo_contract(_block(), pipeline="spec-pkg")
    assert contract == {"spec_package": "spec/pkg"}


def test_producer_may_omit_source_refs_with_its_own_key():
    contract, _unknown = parse_todo_contract(_block("pipeline: spec-pkg\n"))
    assert contract is not None


def test_consumer_still_must_name_source_refs():
    with pytest.raises(ValueError, match="source_refs"):
        parse_todo_contract(_block(), pipeline="default")


def test_consumer_without_a_hint_still_must_name_source_refs():
    with pytest.raises(ValueError, match="source_refs"):
        parse_todo_contract(_block())
