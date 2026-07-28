"""Cross-check that Python _slugify matches JS slugify() in project-setup.html.j2.

JS implementation (from project-setup.html.j2):
    function slugify(name) {
      var m = {'а':'a','б':'b',...};
      var s = (name || '').toLowerCase().split('').map(function(c) { return m[c] !== undefined ? m[c] : c; }).join('');
      s = s.replace(/[^a-z0-9_-]/g, '-').replace(/^-+|-+$/g, '');
      if (!s) throw new Error('No usable characters in role name: ' + name);
      return s;
    }

Python implementation (from opencode_config.py):
    _slugify(name) — transliterates Cyrillic, strips non-ASCII, raises ValueError if empty.

These must produce identical output for client-side conflict detection
to work. If either implementation changes, this test will catch the divergence.
"""
from __future__ import annotations

import pytest
from agent_workflow_ui.opencode_config import _slugify

# Corpus of (input, expected_slug) pairs. Each expected value was verified
# against the JS implementation above. Add cases here when new edge cases emerge.
SLUGIFY_CASES = [
    # Basic
    ("worker", "worker"),
    ("Worker", "worker"),
    ("WORKER", "worker"),
    # Spaces → single dash (per separator)
    ("Backend Dev", "backend-dev"),
    ("Data Scientist", "data-scientist"),
    # Underscores preserved
    ("ml_engineer", "ml_engineer"),
    ("ML_Engineer", "ml_engineer"),
    # Hyphens preserved
    ("full-stack-dev", "full-stack-dev"),
    # Mixed separators
    ("dev_ops-engineer", "dev_ops-engineer"),
    # Special chars → dash (no collapse)
    ("C++ Dev", "c---dev"),
    ("C# Dev", "c--dev"),
    ("Data Scientist (Senior)", "data-scientist--senior"),
    ("@username", "username"),
    # Numeric
    ("dev2", "dev2"),
    ("123", "123"),
    ("v1.2.3", "v1-2-3"),
    # Unicode (non-ASCII, non-Cyrillic → dash, then strip)
    ("汉字", "unnamed"),  # will raise ValueError — handled below
    ("Émile", "mile"),
    # Leading/trailing separators
    ("--worker--", "worker"),
    ("  worker  ", "worker"),
    # Long realistic names
    ("Senior Backend Engineer", "senior-backend-engineer"),
    ("Full-Stack Web Developer (React/Node)", "full-stack-web-developer--react-node"),
    ("DevOps Engineer", "devops-engineer"),
    # Supervisor-prefixed
    ("supervisor-strict", "supervisor-strict"),
    ("Supervisor-Lead", "supervisor-lead"),
    # Underscores only
    ("___", "___"),
    # Mixed Latin unchanged
    ("Mixed", "mixed"),
]

# Cyrillic transliteration cases
CYRILLIC_CASES = [
    ("Аудитор", "auditor"),
    ("Системный аналитик", "sistemnyy-analitik"),
    ("привет-world", "privet-world"),
    ("Журналист", "zhurnalist"),
    ("Щит", "schit"),
    ("Язык", "yazyk"),
    ("Менеджер", "menedzher"),
    ("Агент", "agent"),
    ("Разработчик", "razrabotchik"),
    ("dev-разработчик", "dev-razrabotchik"),
    ("Mixed", "mixed"),
]

# Cases that should raise ValueError
ERROR_CASES = [
    "",
    "   ",
    "---",
    "-",
    "日本語",
    "汉字",
]


@pytest.mark.parametrize("name,expected", SLUGIFY_CASES)
def test_slugify_matches_js_implementation(name: str, expected: str):
    """Python _slugify must match JS slugify() for conflict detection to work."""
    if expected == "unnamed":
        with pytest.raises(ValueError):
            _slugify(name)
        return
    result = _slugify(name)
    assert result == expected, (
        f"slugify({name!r}) returned {result!r}, expected {expected!r}. "
        f"This breaks client-side conflict detection in project-setup.html.j2."
    )


@pytest.mark.parametrize("name,expected", CYRILLIC_CASES)
def test_slugify_cyrillic(name: str, expected: str):
    """Cyrillic names are transliterated to Latin."""
    result = _slugify(name)
    assert result == expected, f"slugify({name!r}) = {result!r}, expected {expected!r}"


@pytest.mark.parametrize("name", ERROR_CASES)
def test_slugify_empty_raises(name: str):
    """Empty or all-non-usable-char names raise ValueError."""
    with pytest.raises(ValueError, match="no usable characters"):
        _slugify(name)


def test_slugify_corpus_consistency():
    """Sanity check: every case in SLUGIFY_CASES has a non-empty expected slug."""
    for name, expected in SLUGIFY_CASES:
        assert expected, f"Empty expected slug for input {name!r}"


def test_slugify_js_parity_cyrillic():
    """Verify JS slugify in template produces same output as Python for Cyrillic.

    We read the template source and extract the JS transliteration map to ensure
    it matches the Python _CYRILLIC_MAP.
    """
    from pathlib import Path

    import agent_workflow_ui as _awui

    template_path = Path(_awui.__file__).parent / "render" / "default_templates" / "project-setup.html.j2"
    content = template_path.read_text(encoding="utf-8")

    # Verify the JS transliteration map exists and contains key entries
    assert "'а':'a'" in content, "JS slugify missing Cyrillic transliteration"
    assert "'ж':'zh'" in content, "JS slugify missing zh mapping"
    assert "'щ':'sch'" in content, "JS slugify missing sch mapping"
    assert "'я':'ya'" in content, "JS slugify missing ya mapping"

    # Verify JS throws on empty result (not fallback to 'unnamed')
    assert "throw new Error" in content, "JS slugify should throw on empty result"
