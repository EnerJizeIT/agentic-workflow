"""CARRY IMP2 (R13/K13): the raw-full-suite gate.

A worker that runs the whole suite by hand (``pytest tests/``,
``scripts/run-all.sh``) is visible in its log, but nothing used to record
that the run bypassed the canonical artifact. The gate compares the log
with ``context/SUITE-<tree-sha>-*.json``:

- no full-suite invocation in the log → no check, no warning;
- no canonical command (``verification.test_cmd`` unset/broken) → silence:
  there is nothing to compare an artifact with, and no remedy to name;
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

Three more "not a command" shapes (AUD1–AUD3, owner review 0277):

- a bare ``pytest`` with no positional target is a full run only when
  pytest config declares a non-empty ``testpaths`` (owner's AUD1 rule;
  ``suite_command.testpaths_configured`` reads it).
  Known blind spot, kept on purpose: in a repo WITHOUT ``testpaths`` a
  bare ``pytest`` also collects the whole tree from the rootdir, so that
  raw run stays invisible — the rule is the contract, not an accident.
  ``-k``/``-m`` selectors do NOT cancel the ``testpaths`` fullness:
  pytest still resolves the same default targets; they only narrow what
  runs inside them. An explicit target still decides by itself —
  ``pytest tests -k smoke`` stays narrow (R13),
  ``pytest tests/unit/test_x.py`` is never full. Only ``-k`` narrows an
  explicit target: R13 names ``-k`` alone, so ``pytest tests -m slow``
  stays "full" — the louder side, which R13 prefers;
- ``python* -m <module>`` is pytest only when ``<module>`` is
  ``pytest``/``py.test`` — ``-m compileall`` or ``-m ruff`` run a tool,
  not the suite;
- markdown quotes (``> pytest tests/``) and heredoc bodies are text, not
  executed commands; only a prompted line (opencode's ``$ `` echo) opens a
  heredoc, so a printed script listing cannot swallow the commands after it.

A wrapper option takes its own value too (QA-0277 F7): ``timeout -k 5 300
pytest tests/`` and ``env -u FOO pytest tests/`` still name ``pytest`` after
the wrapper comes off, so the raw run stays visible. A short shell cluster
that carries the command (``bash -lc 'pytest tests/'``) is ``-c`` as well.
``env -S '...'`` is not unwrapped: its value is a whole command string, so a
raw run hidden inside it stays invisible — a named blind spot, not a guess.

Redirects and spaced option values are not positional targets (AUD11):
``pytest -q 2>&1``, ``pytest -q > out.txt`` and ``pytest --timeout 600``
show no target, so the ``testpaths`` rule above decides. A redirect glued
to a word keeps the word: ``pytest tests>out.txt`` still names ``tests``.
The same rule covers an option whose name the gate does not know (CTX1):
only a path-shaped word or a node-id is a target, so ``--cov awf``,
``--junitxml report.xml`` and ``--durations 10`` add none, while
``--cov awf tests/`` still names ``tests/``.
A command substitution in the *target set* (``pytest $(find tests -name
'*.py')``, AUD10) hides an unknown set — the gate does not claim "full" for
it. In an option value or a redirect target it does not erase an explicit
target: ``pytest tests/ -n $(nproc)`` and ``pytest tests/ > $(mktemp)`` still
name ``tests/`` (QA-0277 F5).

Known false alarm, kept on purpose (AUD13, R13 prefers it over a missed
raw run): a printed output line WITHOUT the ``$ `` prompt can look like a
command. Only ``cat``-style listings are affected; the gate does not try to
recognise them.
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
_PROMPTS = ("$", "❯")
_LEAD_WRAPPERS = frozenset({"command", "exec", "nohup", "setsid", "env", "sudo"})
#: shell reserved words that may start a command segment (CTX4, volut 4):
#: ``if …; then pytest tests/`` and ``for …; do pytest tests/`` run the suite.
_LEAD_KEYWORDS = frozenset({"then", "do", "else"})
#: wrapper options whose NEXT word is a value, not the runner (QA-0277 F7):
#: ``timeout -k 5 300 pytest tests/`` and ``env -u FOO pytest tests/`` run the
#: suite, but the value looked like the runner. ``-S``/``--split-string`` is
#: left out on purpose: its value is a whole command, not a runner prefix.
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
_HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
#: pytest options whose NEXT word is a value, not a positional target.
_VALUE_OPTIONS = frozenset(
    {
        "-k", "-m", "-p", "-c", "-o", "-W", "-n",
        "--deselect", "--ignore", "--ignore-glob", "--rootdir",
        "--maxfail", "--tb", "--dist", "--import-mode", "--basetemp",
        "--timeout",
    }
)
#: a short-option cluster that carries a command: ``-c``, ``-lc``, ``-ec``.
_SHELL_CLUSTER_C = re.compile(r"^-[A-Za-z]*c[A-Za-z]*$")
#: a bare redirect operator: its target is the next word (AUD11).
_REDIRECT_OP = re.compile(r"^(?:\d*>>?|\d*<|&>>?)$")
_MAX_UNWRAP = 2
#: interpreter options whose NEXT word is a value, not a script path.
_PY_VALUE_OPTIONS = frozenset({"-W", "-X", "-Q"})
#: a positional word must name a path/node-id to be a target (CTX1).
_PATHLIKE = re.compile(r"[\\/]")
_NODE_ID = "::"


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
        elif ch.isspace():
            if buf:
                words.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        words.append("".join(buf))
    return words


def _split_redirect(word: str) -> tuple[str, str] | None:
    """Split a word at its first unquoted redirect operator (AUD11).

    ``tests>out.txt`` → ``("tests", ">out.txt")``; ``>/dev/null`` →
    ``("", ">/dev/null")``; no operator → ``None``. The operator is part of
    the tail, so the caller can tell a bare operator from a glued one.
    """
    quote = ""
    for i, ch in enumerate(word):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch in "<>":
            return word[:i], word[i:]
    return None


def _strip_redirects(words: list[str]) -> list[str]:
    """Drop redirect operators and their targets (AUD11).

    A redirect glued to a word (``tests>out.txt``) keeps the word — only the
    redirect half goes. A bare operator (``>``) also swallows its own next
    word, the target file. A heredoc marker (``<<EOF``) is not a redirect,
    but it is not a positional target either, so it goes the same way.
    """
    out: list[str] = []
    skip_next = False
    for word in words:
        if skip_next:
            skip_next = False
            continue
        split = _split_redirect(word)
        if split is None:
            out.append(word)
            continue
        head, tail = split
        if head and not head.isdigit() and not head.endswith("&"):
            out.append(head)  # digits and ``&`` are the redirect's own fd
        if _REDIRECT_OP.match(_unquote(tail)):
            skip_next = True
    return out


def _positionals(words: list[str]) -> list[str]:
    """Non-option words pytest treats as targets.

    Option values are not targets: ``pytest --timeout 600`` names none
    (AUD11). Redirects are already gone — see ``_strip_redirects``.
    """
    targets: list[str] = []
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
        targets.append(token)
    return targets


def _is_target(token: str) -> bool:
    """True when a positional word names a path or a node-id (CTX1).

    An unknown option's spaced value is data, not a target: ``--cov awf``,
    ``--junitxml report.xml`` and ``--durations 10`` name no target, so the
    ``testpaths`` rule below decides. A bare word counts only when it looks
    like a path (a separator, a leading dot, a ``.py`` file) or carries a
    node-id (``file.py::test``) or is the suite target itself. Existence is
    not required: an explicit file that the stage is about to create is
    still a narrow target.
    """
    return (
        token in _SUITE_TARGETS
        or _NODE_ID in token
        or bool(_PATHLIKE.search(token))
        or token.startswith(".")
        or token.endswith(".py")
    )


def _normalize_target(token: str) -> str:
    """A path target without shell sugar (CTX3, volut 4).

    ``./tests``, ``./tests/``, ``tests/.``, ``$PWD/tests`` and
    ``${PWD}/tests`` all name the same directory as ``tests``. Only the
    suite target benefits: ``./tests/unit`` normalizes to a longer path and
    stays narrow.
    """
    text = token.replace("\\", "/").replace("${PWD}", "").replace("$PWD", "")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    return "/".join(parts)


def _is_root_target(token: str) -> bool:
    """``pytest .`` — the rootdir, i.e. the whole tree (CTX3)."""
    return token in (".", "./", "$PWD", "${PWD}", "$PWD/", "${PWD}/")


def _pytest_full(words: list[str], testpaths: bool) -> bool:
    """``pytest`` with a positional target exactly ``tests``/``tests/``.

    *words* are the arguments after the runner, redirects already stripped
    by ``_segment_full`` — the one owner of that normalization (AUD11).

    No positional target is "full" only when pytest config declares
    ``testpaths`` (AUD1); a keyword filter narrows an explicit target
    (R13) but does not undo the default targets of ``testpaths``. A command
    substitution names an unknown set, never "full" (AUD10).
    """
    positional = _positionals(words)
    # AUD10: only the target set is unknown. A substitution in an option
    # value or a redirected stream (``-n $(nproc)``, ``> $(mktemp)``) is
    # not a target and must not erase the explicit one (QA-0277 F5).
    if any("$(" in token or "`" in token for token in positional):
        return False
    targets = [token for token in positional if _is_target(token)]
    if not targets:
        return testpaths
    # CTX3: shell spellings of the same directory (``./tests``, ``$PWD/tests``)
    # and the rootdir (``pytest .``) are the whole suite too.
    named = any(_normalize_target(token) == "tests" for token in targets)
    if not named and not any(_is_root_target(token) for token in targets):
        return False
    return not any(_is_keyword_filter(word) for word in words)


def _python_module(words: list[str]) -> tuple[str, list[str]] | None:
    """``(module, args after it)`` for ``python* -m <module>``, else ``None``.

    Only a ``-m`` BEFORE the first positional word is the interpreter's own
    ``-m``: in ``python3 script.py -m pytest`` the flag belongs to the
    script, and the script is what runs (AUD2).
    """
    i = 1
    while i < len(words):
        token = _unquote(words[i])
        if token == "-m":
            if i + 1 >= len(words):
                return None
            return _unquote(words[i + 1]), words[i + 2:]
        if token.startswith("-m") and len(token) > 2:
            return token[2:], words[i + 1:]
        if token == "-c":
            return None  # an inline script: the interpreter's options are over
        if token in _PY_VALUE_OPTIONS:
            i += 2  # the option and its value: neither is a positional
            continue
        if token.startswith("-"):
            i += 1
            continue
        return None  # the script path: the interpreter's options are over
    return None


def _strip_prefix(words: list[str]) -> list[str]:
    """Drop ``cd``/``timeout``/``env``/assignments before the real runner."""
    i = 0
    while i < len(words):
        word = _unquote(words[i])
        if word in _LEAD_KEYWORDS:
            i += 1
            continue
        if word in ("cd", "pushd"):
            i += 2
            continue
        if word == "timeout":
            i += 1
            valued = _LEAD_VALUE_OPTIONS["timeout"]
            while i < len(words) and _unquote(words[i]).startswith("-"):
                i += 2 if _unquote(words[i]) in valued else 1
            i += 1  # the duration value
            continue
        if word in _LEAD_WRAPPERS:
            i += 1
            valued = _LEAD_VALUE_OPTIONS.get(word, frozenset())
            while i < len(words):
                token = _unquote(words[i])
                if _is_assignment(token):
                    i += 1
                    continue
                if not token.startswith("-"):
                    break
                i += 2 if token in valued else 1
            continue
        if _is_assignment(word):
            i += 1
            continue
        break
    return words[i:]


def _shell_command_index(args: list[str]) -> int | None:
    """Index of the shell option that carries a command (QA-0277 F7).

    ``-c`` is the standalone spelling; ``-lc``/``-ec``/``-ic`` are the same
    option inside a letter cluster (a login or errexit shell is common in
    worker logs). Only a cluster of short options counts — an argument like
    ``--command`` is not a shell spelling.
    """
    for idx, arg in enumerate(args):
        if arg == "--":
            break
        if _SHELL_CLUSTER_C.match(arg):
            return idx
    return None


def _shell_full(rest: list[str], depth: int, testpaths: bool) -> bool:
    """``bash scripts/run-all.sh`` (and ``bash -c '<command>'``)."""
    args = [_unquote(word) for word in rest[1:]]
    command_idx = _shell_command_index(args)
    if command_idx is not None:
        if depth >= _MAX_UNWRAP:
            return False
        payload = " ".join(args[command_idx + 1:])
        return any(
            _segment_full(segment, depth + 1, testpaths)
            for segment in _segments(payload)
        )
    if any(arg in ("-n", "--noexec") for arg in args):
        return False  # syntax check only, nothing runs
    return any(
        arg.replace("\\", "/").endswith("scripts/run-all.sh") for arg in args
    )


def _segment_full(segment: str, depth: int, testpaths: bool) -> bool:
    """True when this one command segment executes the whole suite."""
    rest = _strip_prefix(_strip_redirects(_words(segment)))
    if not rest:
        return False
    name = Path(_unquote(rest[0])).name
    if name in _SHELL_NAMES:
        return _shell_full(rest, depth, testpaths)
    if name in _PYTEST_NAMES:
        return _pytest_full(rest[1:], testpaths)
    if _PYTHON_NAME.match(name):
        parsed = _python_module(rest)
        if parsed is None:
            return False
        module, args = parsed
        return module in _PYTEST_NAMES and _pytest_full(args, testpaths)
    return _unquote(rest[0]).replace("\\", "/").endswith("scripts/run-all.sh")


def _is_command_echo(line: str) -> bool:
    """A prompted line is an executed command; printed output is not (F4)."""
    stripped = _ANSI.sub("", line).strip()
    return any(stripped.startswith(prompt) for prompt in _PROMPTS)


def _clean(line: str) -> str:
    """Drop ANSI escapes and the shell prompt opencode prints before a command."""
    line = _ANSI.sub("", line).strip()
    if line.startswith("timestamp="):
        return ""  # opencode's own log line, not an executed command
    for prompt in _PROMPTS:
        if line.startswith(prompt):
            return line[len(prompt):].lstrip()
    return line


def _is_full_suite_line(line: str, testpaths: bool) -> bool:
    """True when *line* runs the whole suite."""
    line = _clean(line)
    if not line or line.startswith(">"):
        return False  # a markdown quote, not an executed command
    return any(_segment_full(s, 0, testpaths) for s in _segments(line))


def _heredoc_start(line: str) -> tuple[str, bool] | None:
    """``(delimiter, strip_tabs)`` when *line* opens a heredoc.

    Only an unquoted ``<<`` opens one: in ``git commit -m "a << b"`` the
    ``<<`` is text, and treating it as a heredoc would swallow the real
    command lines that follow.
    """
    quote = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if line.startswith("<<", i):
            match = _HEREDOC.match(line, i)
            return (match.group(3), match.group(1) == "-") if match else None
        i += 1
    return None


def _heredoc_end(line: str, delimiter: str, strip_tabs: bool) -> bool:
    return (line.lstrip("\t") if strip_tabs else line).rstrip() == delimiter


def detect_full_suite(text: str, *, testpaths: bool = False) -> bool:
    """True when any executed line of *text* runs the full suite.

    Heredoc bodies are data (AUD3), and only a prompted command opens one:
    a ``<<'PY'`` in printed output is text, not a shell construct (F4).
    """
    delimiter: str | None = None
    strip_tabs = False
    for line in text.splitlines():
        if delimiter is not None:
            if _heredoc_end(line, delimiter, strip_tabs):
                delimiter = None
            continue
        if _is_full_suite_line(line, testpaths):
            return True
        if not _is_command_echo(line):
            continue  # printed output: it cannot open a heredoc (F4)
        opened = _heredoc_start(_clean(line))
        if opened is not None:
            delimiter, strip_tabs = opened
    return False


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
    if not log_text:
        return ""
    if not detect_full_suite(
        log_text,
        testpaths=suite_command.testpaths_configured(project_dir),
    ):
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
