"""Internal helpers for awf.api: precondition checks + safe file reads.

All functions here are private (``_`` prefix). Public API functions in
:mod:`awf.api.lifecycle`, :mod:`awf.api.pipeline`, :mod:`awf.api.roles`
use them as building blocks.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import git_utils, paths
from ._errors import AwfApiError

# Cyrillic transliteration for role slugs. Canonical home (FU-14): the
# plugin's _slugify imports slugify() from here, so core and plugin
# produce one slug for the same name. The JS slugify() in
# project-setup.html.j2 must keep the same map (cross-check:
# tests/agent_workflow_ui/test_slugify.py).
_CYRILLIC_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d",
    "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}


def slugify(name: str) -> str:
    """Transliterate Cyrillic to Latin, then reduce to a filesystem slug.

    Lowercases, maps Cyrillic via ``_CYRILLIC_MAP``, replaces every
    remaining character outside ``[a-z0-9_-]`` with a dash (no collapse),
    strips edge dashes. Returns ``""`` when nothing usable remains.
    """
    lower = name.strip().lower()
    transliterated = "".join(_CYRILLIC_MAP.get(c, c) for c in lower)
    return re.sub(r"[^a-z0-9_-]", "-", transliterated).strip("-")


def slugify_role(name: str) -> str:
    """Normalize a role name to its saved file slug.

    Single normalization point for role names (AUD06-01): the pipeline
    stages, the config.yaml ``models.<role>`` keys, and the role-file
    lookup must all agree on the same slug, or runtime lookups miss.

    Custom roles are saved by the plugin via the same transliteration
    (FU-14: the plugin's ``_slugify`` delegates to :func:`slugify`), so
    both sides produce one slug — ``QA Лид`` → ``qa-lid`` everywhere.
    Falls back to the raw name when nothing usable remains (the pipeline
    loader warns about the mismatch).
    """
    slug = slugify(name)
    return slug or name.strip()


def require_agentic(project_dir: Path) -> Path:
    """Ensure .agentic/ exists. Raise AwfApiError if not.

    Returns the resolved .agentic/ Path on success.
    """
    agentic = paths.agentic_dir(project_dir)
    if not agentic.is_dir():
        raise AwfApiError(
            f"No .agentic/ found at {project_dir}. Run 'awf init' first."
        )
    return agentic


def require_git_repo(project_dir: Path) -> None:
    """Ensure project is a git repo. Raise AwfApiError if not."""
    if not git_utils.is_git_repo(project_dir):
        raise AwfApiError(
            f"Not a git repository: {project_dir}. Run 'git init' first."
        )


def read_file_text(path: Path, max_chars: int | None = None) -> str:
    """Read file as text, optionally truncated to ``max_chars``.

    Returns a placeholder string on OSError (never raises) — used by
    init_project which prefers degraded content over total failure.
    AUD06-17: non-UTF-8 bytes are replaced (errors="replace") instead of
    raising UnicodeDecodeError, so the "never raises" promise holds.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"(error reading {path.name}: {e})"
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars] + "\n...[truncated]"
    return text


def read_log_tail(log_file: Path, n: int) -> str | None:
    """Read last N lines of a log file. Returns None if file is absent.

    dogfood-11: consecutive duplicate lines are collapsed to ``line  (×N)`` —
    retry loops repeat the same warning verbatim and flood the tail that the
    supervisor reads.
    """
    if not log_file.is_file():
        return None
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines:
        return ""
    tail = lines[-n:] if len(lines) > n else lines
    collapsed: list[tuple[str, int]] = []
    for line in tail:
        if collapsed and collapsed[-1][0] == line:
            collapsed[-1] = (line, collapsed[-1][1] + 1)
        else:
            collapsed.append((line, 1))
    out = [f"{line}  (×{count})" if count > 1 else line for line, count in collapsed]
    return "\n".join(out)


__all__ = [
    "require_agentic",
    "require_git_repo",
    "read_file_text",
    "read_log_tail",
]
