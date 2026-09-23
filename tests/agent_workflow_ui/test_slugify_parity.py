"""FU-14: core and plugin must produce ONE slug for the same role name.

Before the fix the plugin transliterated Cyrillic («QA Лид» → qa-lid) while
the core stripped it (→ qa) — role files written by the plugin were missed
by core lookups. Both entry points now share awf.api._helpers.slugify.
"""
from __future__ import annotations

import pytest
from agent_workflow_ui.opencode_config import _slugify

from awf.api._helpers import slugify, slugify_role

# (input, expected) — the expected value pins the stable slug; core and
# plugin must both return it.
PARITY_CASES = [
    ("QA Лид", "qa-lid"),
    ("Тест-Роль 1", "test-rol-1"),
    ("qa-review", "qa-review"),
    ("Worker", "worker"),
    ("Backend Developer", "backend-developer"),
    ("ML_Engineer", "ml_engineer"),
    ("Системный аналитик", "sistemnyy-analitik"),
]


@pytest.mark.parametrize("name,expected", PARITY_CASES)
def test_core_and_plugin_give_one_slug(name: str, expected: str):
    assert slugify(name) == expected
    assert _slugify(name) == expected
    assert slugify_role(name) == expected


@pytest.mark.parametrize("name", [c[0] for c in PARITY_CASES])
def test_core_and_plugin_agree(name: str):
    assert slugify(name) == _slugify(name), (
        f"core slugify({name!r})={slugify(name)!r} != plugin "
        f"_slugify({name!r})={_slugify(name)!r}"
    )


def test_ascii_behavior_unchanged():
    """ASCII input: byte-for-byte the old behavior (no transliteration)."""
    assert slugify("qa-review") == "qa-review"
    assert _slugify("qa-review") == "qa-review"
    assert slugify_role("QA Review") == "qa-review"


def test_cyrillic_only_names_transliterated_not_stripped():
    """The old core bug: «Аудитор» fell back to the raw Cyrillic name."""
    assert slugify_role("Аудитор") == "auditor"
    assert _slugify("Аудитор") == "auditor"


def test_plugin_still_raises_on_unusable_name():
    """Plugin contract preserved: empty result → ValueError (no «.md» file)."""
    with pytest.raises(ValueError, match="no usable characters"):
        _slugify("汉字")
    # Core keeps its fallback (pipeline loader warns about the mismatch).
    assert slugify("汉字") == ""
    assert slugify_role("汉字") == "汉字"
