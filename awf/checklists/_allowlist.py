"""The ``command_rc`` argv allowlist: a CLOSED list per binary.

A probe only reads. The closed list is the guard, not a deny-list of
known-bad flags: ``git diff --output=<path>`` writes a file and
``rg --pre <cmd>`` runs a command, while their argv carries no shell token
and the binary is allowlisted. So every option token must be named here,
and a positional must stay inside the project (no absolute path, no
``..``).

Two layers use it: :func:`argv_structure_refusal` (the shape every parse
enforces — no shell, an allowlisted binary) and :func:`argv_refusal` (the
full verdict: loaders refuse a data file, the block parser refuses a
hand-edited TODO, the gate refuses right before exec).
"""
from __future__ import annotations

import re
from collections.abc import Callable

#: argv allowlist for ``command_rc`` probes. The probe never writes: the
#: commands read state only, and ``python3 -m pytest --collect-only`` is the
#: one pytest form that collects without running.
#:
#: The check is a CLOSED list per binary: every option token must be named
#: here. A deny-list of known-bad flags is exactly how ``git --output`` and
#: ``rg --pre`` slip through — one writes a file, the other runs a command.
_ALLOWED_BINS = ("grep", "rg", "ls", "test", "git", "python3")
_ALLOWED_GIT_SUBCMDS = ("diff", "status", "ls-files", "show")
_SHELL_TOKENS = {"|", "||", "&&", ";", ">", ">>", "<", "<<", "&", "2>", "2>&1"}
_SHELL_BINS = ("sh", "bash", "zsh", "dash", "cmd", "powershell", "pwsh")
_FORBIDDEN_BINS = ("env", "xargs", "find", "make")

#: Boolean option tokens allowed per binary (for ``git``: per subcommand).
_BOOL_FLAGS: dict[str, object] = {
    "grep": frozenset({"-q", "-r", "-R", "-n", "-i", "-F", "-E", "-l"}),
    "rg": frozenset({"-q", "-n", "-i", "-F", "-l"}),
    "ls": frozenset({"-l", "-a", "-d", "-1"}),
    "git": {
        "status": frozenset(
            {"--porcelain", "-s", "--short", "--branch", "-b", "--long", "-uno"}
        ),
        "diff": frozenset(
            {
                "--stat",
                "--numstat",
                "--shortstat",
                "--name-only",
                "--name-status",
                "--quiet",
                "--exit-code",
                "--cached",
                "--staged",
                "--check",
                "--no-color",
                "--no-ext-diff",
                "--no-renames",
            }
        ),
        "ls-files": frozenset(
            {
                "--cached",
                "--others",
                "--modified",
                "--deleted",
                "--error-unmatch",
                "-z",
                "-m",
                "-o",
                "-d",
                "-c",
            }
        ),
        "show": frozenset(
            {
                "--stat",
                "--numstat",
                "--shortstat",
                "--name-only",
                "--name-status",
                "--quiet",
                "--no-patch",
                "-s",
                "--no-color",
            }
        ),
    },
}

#: Option tokens that consume the NEXT token as a value (the value is data:
#: a pattern, not a path and not a command).
_VALUE_FLAGS = {
    "grep": frozenset({"-e"}),
    "rg": frozenset({"-e"}),
}

#: ``--opt=value`` forms: option → allowed values (None = an integer). An
#: unknown option, or a value outside the set, is a refusal.
_EQUALS_FLAGS: dict[str, dict[str, tuple[str, ...] | None]] = {
    "git:status": {
        "--porcelain": ("v1", "v2", "z"),
        "--untracked-files": ("no", "normal", "all"),
    },
    "git:diff": {"--unified": None},
    "git:show": {"--unified": None},
}

#: The one shape a ``test`` probe may take: ``test -e|-f|-d <path>``.
_TEST_FLAGS = frozenset({"-e", "-f", "-d"})
_PYTEST_BOOL_FLAGS = frozenset({"--collect-only", "-q", "--quiet", "-s"})
_PYTEST_VALUE_FLAGS = frozenset({"-p"})


def _pytest_plugin_disabled(value: str) -> bool:
    """``-p`` may only DISABLE a plugin (``no:<name>``); ``-p <module>`` imports it.

    ``python3 -m pytest --collect-only -p <module>`` imports the module at
    collection time and runs its top-level code (``sys.path[0]`` is the
    project), which is the same exec vector as ``rg --pre``. The ``no:``
    prefix is pytest's own "disable" form and imports nothing.
    """
    return value.startswith("no:")


def _bare_binary_refusal(token: str) -> str:
    """``argv[0]`` must be a bare program name, never a path.

    The allowlist matches a program NAME; ``subprocess.run`` executes the
    token as a path. ``/tmp/x/grep`` or ``./grep`` would therefore run
    whatever file sits there while passing a basename comparison — the
    same "executes an arbitrary program" class as ``rg --pre``.
    """
    if "/" in token or "\\" in token or token in (".", ".."):
        return (
            f"probe binary {token!r} must be a bare name, not a path — the "
            "allowlist matches a program name, and a path would run whatever "
            "file sits there"
        )
    return ""


def argv_structure_refusal(argv: object) -> str:
    """The shape check: a list of strings, no shell, an allowlisted binary.

    This is the half every parse enforces (a hand-edited line is refused
    early). The flags themselves are checked by :func:`argv_refusal` — the
    full verdict used where a probe could actually run.
    """
    if not isinstance(argv, list) or not argv:
        return "argv must be a non-empty list of strings"
    if not all(isinstance(a, str) and a for a in argv):
        return "argv must be a non-empty list of strings"
    for token in argv:
        if token in _SHELL_TOKENS:
            return (
                f"shell operator {token!r} in argv — probes run argv without "
                "a shell; pipe/redirect are refused"
            )
    bare = _bare_binary_refusal(argv[0])
    if bare:
        return bare
    binary = argv[0]
    if binary in _SHELL_BINS:
        return (
            f"shell interpreter {binary!r} is not allowed — probes run argv "
            "directly, no `sh -c` workaround"
        )
    if binary in _FORBIDDEN_BINS:
        return (
            f"{binary!r} is not allowed — probes run argv directly, without a "
            "shell/wrapper"
        )
    if binary not in _ALLOWED_BINS:
        return (
            f"{binary!r} is not in the probe allowlist — allowed: "
            + ", ".join(_ALLOWED_BINS)
        )
    if binary == "git":
        sub = argv[1] if len(argv) > 1 else ""
        if sub not in _ALLOWED_GIT_SUBCMDS:
            return (
                f"git {sub!r} is not in the probe allowlist — allowed: "
                + ", ".join(_ALLOWED_GIT_SUBCMDS)
            )
    return ""


def _path_refusal(token: str) -> str:
    """A positional must stay inside the project: no absolute, no ``..``."""
    norm = token.replace("\\", "/")
    if norm.startswith(("/", "~")) or re.match(r"^[A-Za-z]:/", norm):
        return (
            f"absolute path {token!r} is refused — probes read only inside "
            "the project tree"
        )
    if ".." in norm.split("/"):
        return (
            f"path {token!r} leaves the project (`..`) — probes read only "
            "inside the project tree"
        )
    return ""


def path_refusal(token: str) -> str:
    """Public form of the path guard, for probes whose path is not in argv.

    ``grep_*``/``file_*``/``json_field`` carry their target as a named field,
    not as a positional, so the argv verdict never sees it. The data loaders
    and the block parser call this instead — the same "inside the project"
    rule, one owner.
    """
    return _path_refusal(token)


def _equals_ok(value: str, allowed: tuple[str, ...] | None) -> bool:
    if allowed is None:
        return value.isdigit()
    return value in allowed


def _parse_options(
    args: list[str],
    bools: frozenset[str],
    values: frozenset[str],
    equals: dict[str, tuple[str, ...] | None],
    value_guards: dict[str, Callable[[str], bool]] | None = None,
) -> tuple[str, list[str]]:
    """Check options from a CLOSED list; return ``(refusal, positionals)``."""
    positionals: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            positionals.extend(args[index + 1 :])
            break
        if token.startswith("-") and token != "-":
            if token in values:
                if index + 1 >= len(args):
                    return f"option {token!r} needs a value", positionals
                value = args[index + 1]
                guard = (value_guards or {}).get(token)
                if guard is not None and not guard(value):
                    return (
                        f"option {token!r} value {value!r} is refused — only "
                        "the disabling form `no:<plugin>` is allowed (a "
                        "module import would execute its code)"
                    ), positionals
                index += 2
                continue
            if token in bools:
                index += 1
                continue
            if "=" in token:
                option, value = token.split("=", 1)
                if option in equals and _equals_ok(value, equals[option]):
                    index += 1
                    continue
            return (
                f"option {token!r} is not in the probe allowlist — a probe "
                "reads only; flags that write or execute are refused"
            ), positionals
        positionals.append(token)
        index += 1
    return "", positionals


def _option_spec(
    binary: str, argv: list[str]
) -> tuple[
    list[str],
    frozenset[str],
    frozenset[str],
    dict[str, tuple[str, ...] | None],
    dict[str, Callable[[str], bool]],
]:
    """The option slice of ``argv`` and the closed flags allowed for it.

    ONE resolution of «which flags apply to this binary/subcommand», shared
    by the verdict (:func:`argv_refusal`) and by the positional extraction
    (:func:`_argv_positionals`): the gate checks the paths of the very
    tokens the verdict parsed, so the two can never disagree about where
    the argv splits.
    """
    if binary == "python3":
        return (
            argv[3:],
            _PYTEST_BOOL_FLAGS,
            _PYTEST_VALUE_FLAGS,
            {},
            {"-p": _pytest_plugin_disabled},
        )
    if binary == "git":
        sub = argv[1] if len(argv) > 1 else ""
        spec = _BOOL_FLAGS["git"]
        bools = spec.get(sub, frozenset()) if isinstance(spec, dict) else frozenset()
        return argv[2:], bools, frozenset(), _EQUALS_FLAGS.get(f"git:{sub}", {}), {}
    raw_bools = _BOOL_FLAGS.get(binary, frozenset())
    bools = raw_bools if isinstance(raw_bools, frozenset) else frozenset()
    return argv[1:], bools, _VALUE_FLAGS.get(binary, frozenset()), {}, {}


def _argv_positionals(argv: list[str]) -> list[str]:
    """The positional tokens of an allowlisted argv (``[]`` on a refusal)."""
    binary = argv[0]
    if binary == "test":
        return [argv[2]] if len(argv) == 3 and argv[1] in _TEST_FLAGS else []
    tokens, bools, values, equals, guards = _option_spec(binary, argv)
    refusal, positionals = _parse_options(tokens, bools, values, equals, guards)
    return [] if refusal else positionals


def argv_refusal(argv: object) -> str:
    """The full allowlist verdict for a ``command_rc`` argv — empty is clean.

    Runs the shape check, then a per-binary closed option list and a path
    check on positionals. Used by the data loaders (dispatch refusal), by
    the block parser (a hand-edited TODO is the same surface) and right
    before execution in the gate (defence in depth).
    """
    structure = argv_structure_refusal(argv)
    if structure:
        return structure
    assert isinstance(argv, list)
    binary = argv[0]
    if binary == "test":
        if len(argv) != 3 or argv[1] not in _TEST_FLAGS:
            return "test probes are limited to `test -e|-f|-d <path>`"
        return _path_refusal(argv[2])
    if binary == "python3":
        tail = argv[1:]
        if tail[:2] != ["-m", "pytest"] or "--collect-only" not in tail[2:]:
            return (
                "python3 probes are limited to `python3 -m pytest "
                "--collect-only ...` (collection only, nothing runs)"
            )
    tokens, bools, values, equals, guards = _option_spec(binary, argv)
    refusal, positionals = _parse_options(tokens, bools, values, equals, guards)
    if refusal:
        return refusal
    for token in positionals:
        path_refusal = _path_refusal(token)
        if path_refusal:
            return path_refusal
    return ""


def argv_positionals(argv: object) -> list[str]:
    """Positional path/revision tokens of an allowlisted argv (for ``_inside``)."""
    if argv_refusal(argv):
        return []
    assert isinstance(argv, list)
    return _argv_positionals(argv)
