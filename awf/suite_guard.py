"""CARRY IMP2 (R13/K13): the raw-full-suite gate.

A worker that runs the whole suite by hand (``pytest tests/``,
``scripts/run-all.sh``) is visible in its log, but nothing used to record
that the run bypassed the canonical artifact. The gate compares the log
with ``context/SUITE-<tree-sha>-*.json``:

- no full-suite invocation in the log → no check, no warning;
- a matching artifact (same tree sha, same canonical command) → silence;
- no matching artifact → ``SUITE-WARN:`` for the stage log plus the
  ``suite_gate=raw-full-suite`` fact in the handoff.

The stage and the commit are NOT blocked (R13): the point is visibility,
not another wall.

The worker log is opencode's ``--print-logs`` output: every executed
command is echoed as ``\\x1b[0m$ \\x1b[0m<command>``. The ANSI reset and the
prompt are stripped before parsing — otherwise every real command line
looks like prose and the gate never fires (QA-0277 F1). A command is
parsed per shell segment (``&&``, ``;``, ``|``): the runner, not any word
in the line, decides. ``cat scripts/run-all.sh`` reads the script, it
does not run it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import git_utils, paths, suite_command

RAW_SUITE_FACT = "suite_gate=raw-full-suite"

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PYTEST_NAMES = frozenset({"pytest", "py.test"})
_SUITE_TARGETS = frozenset({"tests", "tests/"})
_SHELL_NAMES = frozenset({"bash", "sh", "dash", "zsh", "ksh"})
_PYTHON_NAME = re.compile(r"python(\d+(\.\d+)*)?$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PROMPTS = ("$", "❯", ">")
_LEAD_WRAPPERS = frozenset({"command", "exec", "nohup", "setsid", "env"})
#: pytest options whose NEXT word is a value, not a positional target.
_VALUE_OPTIONS = frozenset(
    {
        "-k", "-m", "-p", "-c", "-o", "-W", "-n",
        "--deselect", "--ignore", "--ignore-glob", "--rootdir",
        "--maxfail", "--tb", "--dist", "--import-mode", "--basetemp",
    }
)
_MAX_UNWRAP = 2


def _unquote(word: str) -> str:
    """A word without the shell quotes ``bash -c '...'`` leaves attached."""
    return word.strip("'\"")


def _is_assignment(word: str) -> bool:
    return bool(_ASSIGNMENT.match(word))


def _is_keyword_filter(word: str) -> bool:
    """``-k``/``--keywords`` — any spelling narrows the run off "full"."""
    word = _unquote(word)
    return (
        word in ("-k", "--keywords")
        or word.startswith("--keywords=")
        or (word.startswith("-k") and len(word) > 2)
    )


def _segments(line: str) -> list[str]:
    """Split on shell separators that are NOT inside quotes."""
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch in ";|":
            parts.append("".join(buf))
            buf = []
        elif ch == "&" and i + 1 < len(line) and line[i + 1] == "&":
            parts.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def _words(segment: str) -> list[str]:
    """Split a segment into words, keeping a quoted value together."""
    words: list[str] = []
    buf: list[str] = []
    quote = ""
    for ch in segment:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch.isspace() or ch in "<>":
            if buf:
                words.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        words.append("".join(buf))
    return words


def _pytest_full(words: list[str]) -> bool:
    """``pytest`` with a positional target exactly ``tests``/``tests/``."""
    if any(_is_keyword_filter(word) for word in words):
        return False
    skip_next = False
    for word in words:
        token = _unquote(word)
        if skip_next:
            skip_next = False
            continue
        if token in _VALUE_OPTIONS:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        if token in _SUITE_TARGETS:
            return True
    return False


def _strip_prefix(words: list[str]) -> list[str]:
    """Drop ``cd``/``timeout``/``env``/assignments before the real runner."""
    i = 0
    while i < len(words):
        word = _unquote(words[i])
        if word in ("cd", "pushd"):
            i += 2
            continue
        if word == "timeout":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
            i += 1  # the duration value
            continue
        if word in _LEAD_WRAPPERS:
            i += 1
            while i < len(words) and (
                words[i].startswith("-") or _is_assignment(words[i])
            ):
                i += 1
            continue
        if _is_assignment(word):
            i += 1
            continue
        break
    return words[i:]


def _shell_full(rest: list[str], depth: int) -> bool:
    """``bash scripts/run-all.sh`` (and ``bash -c '<command>'``)."""
    args = [_unquote(word) for word in rest[1:]]
    if "-c" in args:
        if depth >= _MAX_UNWRAP:
            return False
        payload = " ".join(args[args.index("-c") + 1:])
        return any(
            _segment_full(segment, depth + 1) for segment in _segments(payload)
        )
    if any(arg in ("-n", "--noexec") for arg in args):
        return False  # syntax check only, nothing runs
    return any(
        arg.replace("\\", "/").endswith("scripts/run-all.sh") for arg in args
    )


def _segment_full(segment: str, depth: int) -> bool:
    """True when this one command segment executes the whole suite."""
    rest = _strip_prefix(_words(segment))
    if not rest:
        return False
    name = Path(_unquote(rest[0])).name
    if name in _SHELL_NAMES:
        return _shell_full(rest, depth)
    if name in _PYTEST_NAMES or _PYTHON_NAME.match(name):
        return _pytest_full(rest)
    return _unquote(rest[0]).replace("\\", "/").endswith("scripts/run-all.sh")


def _clean(line: str) -> str:
    """Drop ANSI escapes and the shell prompt opencode prints before a command."""
    line = _ANSI.sub("", line).strip()
    if line.startswith("timestamp="):
        return ""  # opencode's own log line, not an executed command
    for prompt in _PROMPTS:
        if line.startswith(prompt):
            return line[len(prompt):].lstrip()
    return line


def _is_full_suite_line(line: str) -> bool:
    """True when *line* runs the whole suite."""
    line = _clean(line)
    if not line:
        return False
    return any(_segment_full(segment, 0) for segment in _segments(line))


def detect_full_suite(text: str) -> bool:
    """True when any line of *text* runs the full suite."""
    return any(_is_full_suite_line(line) for line in text.splitlines())


def _artifact_matches(context_dir: Path, sha: str, command: str) -> bool:
    for artifact in context_dir.glob(f"SUITE-{sha}-*.json"):
        try:
            data = json.loads(artifact.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("command") == command:
            return True
    return False


def check(project_dir: str | Path, log_text: str) -> str:
    """Return the ``SUITE-WARN:`` line, or ``""`` when the run is canonical."""
    if not log_text or not detect_full_suite(log_text):
        return ""
    canonical = suite_command.resolve(project_dir)
    if canonical is None:
        return ""
    try:
        sha = git_utils.tree_fingerprint(project_dir)
    except RuntimeError:
        return ""
    if _artifact_matches(paths.context_dir(project_dir), sha, canonical.command):
        return ""
    return (
        "SUITE-WARN: raw full-suite run in the worker log without the canonical "
        f"artifact (expected .agentic/context/SUITE-{sha}-*.json for command "
        f"`{canonical.command}`); run scripts/suite-canonical.sh"
    )


__all__ = [
    "RAW_SUITE_FACT",
    "check",
    "detect_full_suite",
]
