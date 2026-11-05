"""K12: the one rule for secrets in a stage text (R2, arch §9, AC27).

A secret never removes its block from the text — it removes its VALUE: the
reader must still see that a token existed, not read it. Two spellings:

- a named value (``token``, ``key``, ``secret``, ``password``, ``auth``,
  ``cookie`` in the name) becomes ``<redacted: name>``;
- a secret FILE (``.env*``, ``*.pem``, ``*.key``, ``id_rsa*``,
  ``*credential*``, ``*secret*``) keeps its name and loses its content: a
  diff hunk is dropped as a whole, a stat line becomes
  ``[redacted: файл-секрет] <path>``.

The owner is here alone: ``worker_tree`` masks a process cmdline in its own
format (``***``) and takes the WORDS from :data:`SECRET_NAME_KEYWORDS` — a
second typed list is how two spellings of "secret" start to drift.
"""
from __future__ import annotations

import re

#: Name keywords: a value under such a name is never printed.
SECRET_NAME_KEYWORDS = ("token", "key", "secret", "password", "passwd", "auth", "cookie")

#: File patterns of :mod:`fnmatch` semantics (arch §9).
SECRET_FILE_PATTERNS = (
    ".env*",
    "*.env",
    "*.env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "*credential*",
    "*secret*",
)

#: ``[redacted: файл-секрет]`` — the marker of a secret file whose text is gone.
FILE_MARKER = "[redacted: файл-секрет]"

_NAME = r"[A-Za-z0-9_.-]*(?:" + "|".join(SECRET_NAME_KEYWORDS) + r")[A-Za-z0-9_.-]*"

#: ``name=value`` / ``name: value`` / ``"name": "value"`` — the value group
#: stops at whitespace or a list separator, so one replacement never eats the
#: rest of the line.
_ASSIGN = re.compile(
    rf"(?P<name>{_NAME})\s*(?P<sep>[:=])\s*(?P<val>\"[^\"]*\"|'[^']*'|[^\s,;)'\"]+)",
    re.IGNORECASE,
)

_DIFF_HEADER = re.compile(r"^diff --git (?P<a>\S+) (?P<b>\S+)$")


def is_secret_name(name: str) -> bool:
    """True when *name* carries one of :data:`SECRET_NAME_KEYWORDS`."""
    lowered = name.lower()
    return any(word in lowered for word in SECRET_NAME_KEYWORDS)


def is_secret_file(path: str) -> bool:
    """True when *path* names a file whose CONTENT is secret (arch §9).

    The name must look like a file at all: a value such as ``abc-SECRET`` is
    a secret VALUE, not a path, and is masked by name instead.
    """
    import fnmatch

    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    lowered = name.lower()
    looks_like_file = "." in lowered or "/" in path or lowered.startswith(("id_rsa", "credential"))
    if not looks_like_file:
        return False
    return any(fnmatch.fnmatch(lowered, p) for p in SECRET_FILE_PATTERNS)


def redact_value(name: str) -> str:
    """The marker a named secret's value is replaced with."""
    return f"<redacted: {name}>"


def _diff_paths(line: str) -> tuple[str, str] | None:
    match = _DIFF_HEADER.match(line)
    if match is None:
        return None
    return match.group("a")[2:] if match.group("a").startswith(("a/", "b/")) else match.group("a"), (
        match.group("b")[2:] if match.group("b").startswith(("a/", "b/")) else match.group("b")
    )


def _strip_secret_hunks(lines: list[str]) -> tuple[list[str], int]:
    """Drop a secret file's whole diff hunk, keeping its name."""
    out: list[str] = []
    replacements = 0
    skipping = False
    for line in lines:
        paths = _diff_paths(line)
        if paths is not None:
            match = next((p for p in paths if is_secret_file(p)), None)
            if match is not None:
                out.append(f"{FILE_MARKER} {match}")
                replacements += 1
                skipping = True
                continue
            skipping = False
        if skipping:
            continue
        out.append(line)
    return out, replacements


def _redact_named(line: str) -> tuple[str, int]:
    replacements = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal replacements
        value = match.group("val")
        if value.startswith("<redacted:"):
            return match.group(0)
        replacements += 1
        return f"{match.group('name')}{match.group('sep')}{redact_value(match.group('name'))}"

    return _ASSIGN.sub(_replace, line), replacements


def redact(text: str) -> tuple[str, int]:
    """Mask secrets in *text*; return ``(masked, replacements)``.

    Secret file hunks disappear as a whole; every other secret value becomes
    ``<redacted: name>``. The number counts one per replaced hunk or value.
    """
    if not text:
        return text, 0
    lines, count = _strip_secret_hunks(text.split("\n"))
    redacted: list[str] = []
    for line in lines:
        secret_path = next(
            (token for token in re.split(r"[\s|+\"'()]+", line) if token and is_secret_file(token)),
            None,
        )
        if secret_path is not None and not line.lstrip().startswith(FILE_MARKER):
            redacted.append(f"{FILE_MARKER} {secret_path}")
            count += 1
            continue
        line, replaced = _redact_named(line)
        count += replaced
        redacted.append(line)
    return "\n".join(redacted), count


__all__ = [
    "FILE_MARKER",
    "SECRET_FILE_PATTERNS",
    "SECRET_NAME_KEYWORDS",
    "is_secret_file",
    "is_secret_name",
    "redact",
    "redact_value",
]
