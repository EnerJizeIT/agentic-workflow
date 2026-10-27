"""Shared command parsing for the suite consumers (AUD-27-3).

The suite gate, the verify pack's flake probe and
``scripts/suite-canonical.sh`` used to run three parsers with three
coverage sets (``uv run``/``poetry run`` only in the verify pack, ``sudo``
only in the gate: AUD-27-1 is that drift). This module is the single
parser: wrappers come off by program basename; the gate keeps its target
policy and ``detect_runner`` owns the artifact's runner name (no fourth copy).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SHELL_NAMES = frozenset({"bash", "sh", "dash", "zsh", "ksh"})
_PYTEST_NAMES = frozenset({"pytest", "py.test"})
_PYTHON_NAME = re.compile(r"(?:python|pypy)(\d+(\.\d+)*)?t?$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_LEAD_WRAPPERS = frozenset({"command", "exec", "nohup", "setsid", "env", "sudo"})
#: runner prefixes: ``<tool> run <program>`` (AUD-27-1).
_LEAD_RUNNERS = frozenset({"uv", "poetry", "pdm", "hatch"})
#: runner families the artifact counts know (``_named_runner``). The same
#: names answer the prefix lookahead's "is this a program?" — one owner, so a
#: new family cannot be known to one question and not the other.
_NPM_NAMES = frozenset({"npm", "npx", "yarn", "pnpm", "jest"})
_GO_CARGO_FAMILIES = ("go", "cargo")
_GO_CARGO_NAMES = frozenset(_GO_CARGO_FAMILIES)
_RUNNER_PROGRAMS = _PYTEST_NAMES | _NPM_NAMES | _GO_CARGO_NAMES
#: options of the ``<tool> run`` prefix whose NEXT word is a value. Other
#: options of the prefix are flags: ``uv run --no-project pytest tests/``.
_RUN_VALUE_OPTIONS = frozenset(
    {
        "--with", "--without", "--python", "--project", "--directory",
        "--env-file", "--index", "--index-url", "--extra-index-url",
        "--group", "--only-group", "--no-group", "--resolution",
        "--prerelease", "--config-setting", "--config-settings", "--package",
        "--extras", "--env",
        "-E", "-C", "-P", "-p", "-e",
    }
)
#: shell reserved words that may start a segment (``then pytest tests/``).
_LEAD_KEYWORDS = frozenset({"then", "do", "else"})
#: wrapper options whose NEXT word is a value, not the runner.
_LEAD_VALUE_OPTIONS = {
    "timeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "exec": frozenset({"-a"}),
    "sudo": frozenset(
        {
            "-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt",
            "-C", "--close-from", "-D", "--chdir", "-r", "--role", "-t",
            "--type", "-U", "--other-user",
        }
    ),
}
#: a short-option cluster that carries a command: ``-c``, ``-lc``, ``-ec``.
_SHELL_CLUSTER_C = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")
#: interpreter options whose NEXT word is a value, not a script path.
_PY_VALUE_OPTIONS = frozenset({"-W", "-X", "-Q"})


@dataclass(frozen=True)
class Program:
    """The program a segment runs after its wrappers come off.

    ``kind``: ``pytest``/``python-pytest``/``python``/``shell``/``other``.
    ``word`` keeps the runner as written; take ``Path(word).name`` yourself.
    """

    kind: str
    word: str
    args: tuple[str, ...]


def unquote(word: str) -> str:
    """A word without the shell quotes ``bash -c '...'`` leaves attached."""
    return word.strip("'\"")


def is_assignment(word: str) -> bool:
    return bool(_ASSIGNMENT.match(word))


def _is_program_word(word: str) -> bool:
    """A word that names a program rather than a wrapper option's value.

    The runner-prefix lookahead (CTX2): in ``uv run --no-project pytest``
    the next word is a program (a flag); in ``uv run --extra dev pytest``
    it is a value. A known runner family counts in both questions.
    """
    name = Path(unquote(word)).name.rpartition(":")[2]
    return (
        name in _RUNNER_PROGRAMS
        or name in _SHELL_NAMES
        or name in _LEAD_RUNNERS
        or bool(_PYTHON_NAME.match(name))
    )


def split_segments(line: str, *, split_single_amp: bool = False) -> list[str]:
    """Split on shell separators that are NOT inside quotes.

    ``&&`` and ``||`` split; a single ``&`` stays glued by default (a named
    blind spot the gate keeps on purpose). The verify pack asks for
    ``split_single_amp=True``: ``echo x & pytest tests/`` is two commands
    and its flake probe must see the pytest run (CTX3). Quote-aware, so
    ``"a|b"`` is one word.
    """
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
        elif ch == "&" and (
            split_single_amp
            or (i + 1 < len(line) and line[i + 1] == "&")
        ):
            parts.append("".join(buf))
            buf = []
            if i + 1 < len(line) and line[i + 1] == "&":
                i += 1
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


def split_words(segment: str) -> list[str]:
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
        elif ch.isspace():
            if buf:
                words.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        words.append("".join(buf))
    return words


def shell_command_index(args: list[str]) -> int | None:
    """Index of the shell option that carries a command (``-c``/``-lc``)."""
    for idx, arg in enumerate(args):
        if arg == "--":
            break
        if _SHELL_CLUSTER_C.match(arg):
            return idx
    return None


def _next_word_is_program(words: list[str], index: int) -> bool:
    """True when the option at *index* is a FLAG, so the next word runs.

    Three answers, all R13-shaped (a missed full run is the costly side):
    the next word names a program; it opens another option (``--frozen
    --python 3.12``); or it is the LAST word left — a prefix of only
    ``option value`` runs nothing, so the trailing word is the program
    (``uv run --no-project ./scripts/run-all.sh``). Any other word is
    assumed to be the option's value.
    """
    if index + 1 >= len(words):
        return False
    return (
        _is_program_word(words[index + 1])
        or unquote(words[index + 1]).startswith("-")
        or index + 2 >= len(words)
    )


def unwrap_prefix(words: list[str]) -> list[str]:
    """Drop wrappers and assignments before the real runner.

    Covers ``cd``/``pushd``, reserved words, ``timeout``/``env``/``sudo``/
    ``command``/``exec``/``nohup``/``setsid`` (option values included) and
    the ``uv/poetry/pdm/hatch run`` prefixes, by program basename.

    Runner options use a program lookahead (CTX2): a known value option
    skips its value; an UNKNOWN option skips the next word only when that
    word is neither a program nor another option — so ``--no-project`` is
    a flag, and an option word is not a value (QA-0280-6). ``+py=3.12`` is
    hatch's matrix spec and ``test:pytest`` its env form: the program is
    after the last ``:``. Both reach pytest.
    """
    i = 0
    while i < len(words):
        word = unquote(words[i])
        name = Path(word).name
        if name in _LEAD_KEYWORDS:
            i += 1
            continue
        if name in ("cd", "pushd"):
            i += 2
            continue
        if name == "timeout":
            i += 1
            valued = _LEAD_VALUE_OPTIONS["timeout"]
            while i < len(words) and unquote(words[i]).startswith("-"):
                i += 2 if unquote(words[i]) in valued else 1
            i += 1  # the duration value
            continue
        if (
            name in _LEAD_RUNNERS
            and i + 1 < len(words)
            and unquote(words[i + 1]) == "run"
        ):
            i += 2
            while i < len(words):
                token = unquote(words[i])
                if is_assignment(token):
                    i += 1
                    continue
                if token.startswith("+") and "=" in token:
                    i += 1  # hatch matrix spec (+py=3.12), not the program
                    continue
                if token == "--":
                    i += 1
                    break
                if not token.startswith("-"):
                    # hatch names an environment as ``<env>:<command>``
                    # (``test:pytest``, ``test:python -m pytest``): the
                    # program is after the LAST ``:``.
                    _, sep, tail = token.rpartition(":")
                    if sep and _is_program_word(tail):
                        words = words[:i] + [tail] + words[i + 1:]
                    break
                if token in _RUN_VALUE_OPTIONS:
                    i += 2
                elif "=" in token or _next_word_is_program(words, i):
                    i += 1
                else:
                    i += 2
            continue
        if name in _LEAD_WRAPPERS:
            # PA-0280-1: ``command -v``/``-V`` ASK whether a program exists;
            # they do not run it. Unwrapping would report the query as a
            # suite run (a wasted flake-probe run, a false gate fact).
            if (
                name == "command"
                and i + 1 < len(words)
                and unquote(words[i + 1]) in ("-v", "-V")
            ):
                break
            i += 1
            valued = _LEAD_VALUE_OPTIONS.get(name, frozenset())
            while i < len(words):
                token = unquote(words[i])
                if is_assignment(token):
                    i += 1
                    continue
                if not token.startswith("-"):
                    break
                i += 2 if token in valued else 1
            continue
        if is_assignment(word):
            i += 1
            continue
        break
    return words[i:]


def _python_module(words: list[str]) -> tuple[str, list[str]] | None:
    """``(module, args after it)`` for ``python* -m <module>``, else ``None``."""
    i = 1
    while i < len(words):
        token = unquote(words[i])
        if token == "-m":
            if i + 1 >= len(words):
                return None
            return unquote(words[i + 1]), words[i + 2:]
        if token.startswith("-m") and len(token) > 2:
            return token[2:], words[i + 1:]
        if token == "-c":
            return None  # an inline script: the interpreter's options are over
        if token in _PY_VALUE_OPTIONS:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        return None  # the script path: the interpreter's options are over
    return None


def parse_words(words: list[str]) -> Program | None:
    """Classify one segment's words after unwrapping its prefixes."""
    rest = unwrap_prefix(words)
    return _classify(rest)


def _classify(rest: list[str]) -> Program | None:
    """The program of words whose prefixes are already off.

    Split out so ``detect_runner`` classifies a segment it has already
    unwrapped instead of unwrapping the same words twice.
    """
    if not rest:
        return None
    word = unquote(rest[0])
    name = Path(word).name
    if name in _SHELL_NAMES:
        return Program("shell", word, tuple(rest[1:]))
    if name in _PYTEST_NAMES:
        return Program("pytest", word, tuple(rest[1:]))
    if _PYTHON_NAME.match(name):
        parsed = _python_module(rest)
        if parsed is None:
            return Program("python", word, tuple(rest[1:]))
        module, args = parsed
        kind = "python-pytest" if module in _PYTEST_NAMES else "python"
        return Program(kind, word, tuple(args))
    return Program("other", word, tuple(rest[1:]))


def detect_runner(command: str) -> str:
    """The suite runner named by a shell command (counts live in the script).

    Whole command words first, so a runner inside ``bash -c '...'`` is still
    named; then the same prefix unwrapping the gate and the verify pack use.
    No known runner → the first word's basename, or ``unknown``.
    """
    segments = split_segments(command)
    raw = [
        Path(unquote(word)).name
        for segment in segments
        for word in split_words(segment)
    ]
    named = _named_runner(raw)
    if named:
        return named
    unwrapped: list[str] = []
    first_unwrapped: str | None = None
    for index, segment in enumerate(segments):
        words = unwrap_prefix(split_words(segment))
        names = [Path(unquote(word)).name for word in words]
        if index == 0 and names:
            first_unwrapped = names[0]
        unwrapped.extend(names)
        # A shell payload is a command again: ``bash -c 'pytest tests/'``.
        program = _classify(words)
        if program is None or program.kind != "shell":
            continue
        args = [unquote(arg) for arg in program.args]
        idx = shell_command_index(args)
        if idx is None:
            continue
        for sub in split_segments(" ".join(args[idx + 1:])):
            unwrapped.extend(Path(unquote(word)).name for word in split_words(sub))
    named = _named_runner(unwrapped)
    if named:
        return named
    # A prefix that came off and left a script is not a runner: naming the
    # wrapper (``uv``) as the counts format would be a false fact (AUD-28-8).
    if first_unwrapped is not None and raw and first_unwrapped != raw[0]:
        return "unknown"
    return raw[0] if raw else "unknown"


def _named_runner(names: list[str]) -> str | None:
    """Map program basenames to the runner families the artifact counts."""
    found = set(names)
    if found & _PYTEST_NAMES:
        return "pytest"
    if found & _NPM_NAMES:
        return "npm"
    if "test" in found:
        for family in _GO_CARGO_FAMILIES:
            if family in found:
                return family
    return None


__all__ = [
    "Program",
    "detect_runner",
    "parse_words",
    "shell_command_index",
    "split_segments",
    "split_words",
    "unquote",
    "unwrap_prefix",
]
