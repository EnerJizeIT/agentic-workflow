"""TODO-0119 (wave 8.1): doc structure and relative links stay healthy.

Wave 8 moved ``vision/*.md`` and ``protocols/communication.md`` into
``docs/`` (``git mv``, links updated). This guard pins the new tree:
  * the old ``vision/`` and ``protocols/`` directories are gone;
  * the new docs exist (3 moved + the ``docs/README.md`` map;
    ``supervisor-flow.md`` was merged into ``design.md`` in wave 8.2);
  * every relative markdown link in the live docs resolves to an
    existing file.

Live docs = root ``*.md`` + ``docs/**/*.md``, EXCEPT ``docs/audit-*`` —
that directory is a frozen audit history: its links describe the
2026-09-25 tree and are deliberately not updated (owner rule: history
text is not rewritten). http(s) targets and pure anchors (``#...``)
are ignored; fenced code blocks and inline code are stripped so code
examples are not checked.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

NEW_DOCS = [
    REPO_ROOT / "docs" / "vision.md",
    REPO_ROOT / "docs" / "design.md",
    REPO_ROOT / "docs" / "file-bus.md",
    REPO_ROOT / "docs" / "README.md",
]

# [text](target) — target up to the first ')', optional "title" after it.
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^()]*)\)")
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
# URI schemes (http:, https:, mailto:, ...) — not relative paths.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _live_docs() -> list[Path]:
    """Root *.md + docs/**/*.md, minus the audit-* history."""
    docs = list(REPO_ROOT.glob("*.md"))
    for path in REPO_ROOT.glob("docs/**/*.md"):
        if any(part.startswith("audit-") for part in path.relative_to(REPO_ROOT).parts):
            continue
        docs.append(path)
    return sorted(docs)


def _strip_code(text: str) -> str:
    """Drop fenced code blocks and inline code so examples are unchecked."""
    lines, in_block = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_block = not in_block
            continue
        if not in_block:
            lines.append(_INLINE_CODE_RE.sub("", line))
    return "\n".join(lines)


def test_old_directories_removed():
    """vision/ and protocols/ must be gone after the wave 8.1 move."""
    for name in ("vision", "protocols"):
        assert not (REPO_ROOT / name).exists(), f"{name}/ still exists"


def test_new_docs_exist():
    missing = [str(p.relative_to(REPO_ROOT)) for p in NEW_DOCS if not p.is_file()]
    assert not missing, f"missing docs: {missing}"


def test_all_relative_links_resolve():
    broken = []
    for doc in _live_docs():
        text = _strip_code(doc.read_text(encoding="utf-8"))
        for raw in _LINK_RE.findall(text):
            target = raw.strip().split()[0] if raw.strip() else ""
            if not target or target.startswith("#") or _SCHEME_RE.match(target):
                continue
            path = target.split("#", 1)[0]
            if not path:
                continue
            if not (doc.parent / path).exists():
                broken.append(f"{doc.relative_to(REPO_ROOT)}: {target}")
    assert not broken, "broken relative doc links:\n" + "\n".join(broken)
