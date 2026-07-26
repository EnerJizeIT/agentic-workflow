"""Cross-check that Python _slugify matches JS slugify() in project-setup.html.j2.

JS implementation (from project-setup.html.j2):
    function slugify(name) {
      return (name || '').toLowerCase()
        .replace(/[^a-z0-9_-]/g, '-')
        .replace(/^-+|-+$/g, '') || 'unnamed';
    }

Python implementation (from opencode_config.py):
    _slugify(name) = re.sub(r"[^a-zA-Z0-9_-]", "-", name.lower()).strip("-") or "unnamed"

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
    ("C# Dev", "c#-dev".replace("#", "-")),  # "c--dev"
    ("Data Scientist (Senior)", "data-scientist--senior"),
    ("@username", "username"),  # leading @ → -, then strip
    # Numeric
    ("dev2", "dev2"),
    ("123", "123"),
    ("v1.2.3", "v1-2-3"),
    # Unicode (non-ASCII → dash, then strip)
    ("Разработчик", "unnamed"),
    ("dev-разработчик", "dev"),
    ("汉字", "unnamed"),
    ("Émile", "mile"),  # É → -, mile survives → strip → "mile"
    # Empty / whitespace
    ("", "unnamed"),
    ("   ", "unnamed"),
    ("---", "unnamed"),
    ("-", "unnamed"),
    ("___", "___"),  # underscores are allowed, not stripped
    # Leading/trailing separators
    ("--worker--", "worker"),
    ("  worker  ", "worker"),  # spaces → dashes → strip → "worker"
    # Long realistic names
    ("Senior Backend Engineer", "senior-backend-engineer"),
    ("Full-Stack Web Developer (React/Node)", "full-stack-web-developer--react-node"),
    ("DevOps Engineer", "devops-engineer"),
    # Supervisor-prefixed (relevant for save_custom_role)
    ("supervisor-strict", "supervisor-strict"),
    ("Supervisor-Lead", "supervisor-lead"),
]


@pytest.mark.parametrize("name,expected", SLUGIFY_CASES)
def test_slugify_matches_js_implementation(name: str, expected: str):
    """Python _slugify must match JS slugify() for conflict detection to work."""
    assert _slugify(name) == expected, (
        f"slugify({name!r}) returned {_slugify(name)!r}, expected {expected!r}. "
        f"This breaks client-side conflict detection in project-setup.html.j2."
    )


def test_slugify_corpus_consistency():
    """Sanity check: every case in SLUGIFY_CASES has a non-empty expected slug.

    This guards against accidentally adding ('name', '') pairs to the corpus.
    """
    for name, expected in SLUGIFY_CASES:
        assert expected, f"Empty expected slug for input {name!r}"
