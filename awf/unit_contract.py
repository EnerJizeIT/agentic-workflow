"""U3: unit contract — optional machine-readable block in a TODO + DONE.json.

TODO front-matter (optional, at the very top of the TODO file, between two
``---`` lines; leading ``<!-- ... -->`` comment lines and blank lines are
skipped when locating the block)::

    verify: ["python3 -m pytest tests/unit/test_x.py -q"]
    flake: off
    gates: ["contracts", "ratchet"]
    prove_red: ["tests/unit/test_x.py::test_y"]
    prove_red_pin: ["tests/unit/test_x.py::test_y"]
    prove_red_cmd: "npm test -- --run"
    prove_red_kind: "coverage"
    prove_red_note: "tests cover behavior implemented in an earlier unit"
    files: ["awf/x.py", "tests/unit/test_x.py"]
    pipeline: audit-llm
    replan_for: TODO-0204

``dispatch_todo`` validates the block; a TODO without a block works exactly
as before. Broken YAML / wrong types / empty lists / unknown gate names are
hard errors (AwfApiError at the API level); unknown keys are warnings only.

``replan_for`` (TODO-0213, BD-30): the declared marker that a new TODO is
the replan answer for the unit being replaced (its id). The interactive
replan wait (``awf/supervisor.py::wait_for_supervisor_signal``,
kind="replan") picks up a fresh TODO ONLY when its front-matter carries
``replan_for: <escalated unit>`` — an ordinary mid-run dispatch (no marker)
must not switch a live pipeline. ``dispatch_todo(replan_for=...)`` writes
the marker into the block; a hand-written line works the same way.

``pipeline`` (RUN3 #2): the pipeline name for run (забег) launches — when
``awf_run_next`` starts this TODO without a queue-level pipeline, it reads
the name from here. Validated as a plain name (letters, digits, ``_``,
``.``, ``-``); an empty pipeline = the config default.

``flake`` (M1): the contract's flake-probe switch. ``off`` runs every
``verify:`` command once (the probe is skipped); ``on`` (the default) keeps
the probe for test-like commands — ``pytest``/``py.test``/``python -m
pytest`` run twice, everything else once. Only ``on``/``off`` are valid.

``prove_red_pin`` (REPORTS29): a list of regression shields of EXISTING
behavior — on the baseline they must be GREEN, the teeth are proven by
mutation outside the tool. It is mutually exclusive with ``prove_red``:
declaring both is a contract error (one TODO either proves a fix or pins a
behavior).

``prove_red_kind`` + ``prove_red_note`` (TODO-0201, TODO-0231): the
declared kind of the prove-red check. Kinds:

- ``coverage`` — a coverage/validation unit whose tests check behavior
  implemented EARLIER (other units), so on this unit's baseline they are
  green by construction and prove-red honestly returns ``not-red``. With
  the kind declared, a ``not-red`` verdict is a PASS of the verify-pack
  ``prove_red`` section (annotated, with the note in the report) instead
  of a failure.
- ``new-api`` — a new module/symbol unit: the test falls on the baseline
  only because the symbol does not exist yet (prove-red reports that as
  its ``new_code_acceptable`` fact). With the kind declared that verdict
  is ``new-api-ok`` — a PASS with a warning section; a real broken runner
  (collection error outside the project, no tests, timeout) stays a fail.

Every other verdict and every other section are unaffected by the
declaration. The kind requires ``prove_red`` (the kind modifies that
check) and ``prove_red_note`` (a one-line justification); the two keys are
a pair — either both or neither is a contract error. The kind is mutually
exclusive with ``prove_red_pin`` (both expect green on the baseline, for
different reasons). An UNdeclared ``not-red`` on a tests-only diff gets a
hint line in the GATES report suggesting the declaration — the verdict
itself is never auto-substituted.

DONE.json (optional, written by the worker to ``.agentic/outbox/``)::

    {"files_changed": [...],
     "tests_run": [{"cmd": "...", "result": "..."}],
     "gates": [...], "notes": "...",
     "stage": {"stage_id": "impl", "attempt": 1}}

``stage`` (ORCH M4.3, block and every key optional): the stage identity the
worker was given in its prompt (``stage_id``/``role``/``model`` non-empty
strings, ``attempt`` an int); a violation skips the whole file like any
other. The engine fills the handoff's "Stage facts" section from its own
knowledge regardless of DONE.json and cross-checks a declared block there —
a note, never an error.

``collect_handoff`` folds a valid DONE.json into the handoff as a
"Machine facts (DONE.json)" section; a broken or schema-violating file is a
log warning and the handoff continues without the section (never a crash).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from ._log import log as _log
from .signals import find_signal_file

#: Gate names ``scripts/run-all.sh`` can run: the fixed checks plus the
#: project-commands entries, under their short names.
KNOWN_GATES: frozenset[str] = frozenset(
    {"contracts", "ratchet", "instructions", "tests", "lint", "mutations"}
)

#: ``files`` is the declared touch-list: verify-pack cross-checks the git
#: diff against it (changed-but-undeclared files are reported). FU-21 D2:
#: it was read by verify_pack but missing here, so declaring it produced
#: an "unknown key" warning and no str-list validation.
#: ``pipeline`` (RUN3 #2): the run launch's pipeline name (a string, not a
#: list); run_next reads it for items without a queue-level pipeline.
#: ``prove_red_cmd`` (REPORTS26-B6): an optional shell command for the
#: prove-red check on non-pytest projects (a string, not a list); it
#: overrides the pytest default, the files from ``prove_red:`` are still
#: copied into the baseline worktree.
#: ``prove_red_pin`` (REPORTS29): the pin-ok — a list of regression shields
#: of EXISTING behavior that must be GREEN on the baseline (mutually
#: exclusive with ``prove_red``); the teeth are proven by mutation, outside
#: the tool.
#: ``prove_red_kind`` / ``prove_red_note`` (TODO-0201, TODO-0231): the
#: declared kind (``coverage`` or ``new-api``) + its one-line
#: justification — a pair: either both or neither; the kind requires a
#: ``prove_red`` list and is mutually exclusive with ``prove_red_pin``.
#: ``replan_for`` (TODO-0213): the replan marker — the id of the unit this
#: TODO replaces (a string, not a list); the interactive replan wait picks
#: the TODO up only when the marker names the escalated unit.
#: ``checklist_classes`` / ``wave`` (TODO-0244): the DoD composition the
#: unit DECLARES — classes are merged only when named here (boundary hints
#: are printed, never merged); a wave names a project wave file.
_CONTRACT_KEYS: tuple[str, ...] = (
    "verify", "flake", "gates", "prove_red", "prove_red_pin", "prove_red_cmd",
    "prove_red_kind", "prove_red_note",
    "files", "pipeline", "replan_for",
    "checklist_classes", "wave",
)

#: Same shape rule the engine enforces (awf/pipeline.py::resolve_pipeline_file,
#: AUD14-05) — a pipeline name is letters, digits, ``_``, ``.``, ``-``.
_PIPELINE_NAME_RE = re.compile(r"[\w.-]+")


def check_pipeline_name(value: object) -> str:
    """Validate the contract's ``pipeline`` value. Returns the stripped name.

    It is a single non-empty name (NOT a list — one TODO runs one pipeline),
    and it must satisfy the engine's name rule, so a typo fails at dispatch
    time instead of silently falling back to the config default at launch.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "'pipeline' must be a non-empty pipeline name string, "
            "e.g. pipeline: audit-llm"
        )
    name = value.strip()
    if not _PIPELINE_NAME_RE.fullmatch(name):
        raise ValueError(
            f"invalid pipeline name {name!r}: use letters, digits, '_', '.' or "
            "'-' (no path separators), e.g. 'default' or 'audit-llm'"
        )
    return name


def check_replan_for(value: object) -> str:
    """TODO-0213: validate the contract's ``replan_for`` value.

    It is the id of the unit being replaced (the escalated TODO) — a
    single ``TODO-NNNN`` id (4+ digits), so a typo fails at dispatch time
    instead of silently marking a TODO the replan wait never matches.
    """
    from .todo_ids import is_valid_todo_id

    if not isinstance(value, str) or not is_valid_todo_id(value.strip()):
        raise ValueError(
            f"'replan_for' must be a TODO id of the unit being replaced, "
            f"e.g. replan_for: TODO-0204 — got {value!r}"
        )
    return value.strip()


def inject_replan_for(content: str, target: str) -> str:
    """TODO-0213: write ``replan_for: <id>`` into the TODO's contract block.

    Mirrors :func:`inject_pipeline_key`: creates the block when absent
    (inserted after the leading comment/blank lines), replaces an existing
    ``replan_for:`` line when present (the explicit argument wins over a
    copy-pasted block). Returns the new content — the trailing newline is
    preserved as found.
    """
    line = f"replan_for: {check_replan_for(target)}"
    lines = content.splitlines()
    open_i, close_j, error = _find_contract_block(content)
    if error:
        raise ValueError(error)
    if open_i is None or close_j is None:
        i = 0
        while i < len(lines):
            s = lines[i].strip()
            if not s or s.startswith("<!--"):
                i += 1
                continue
            break
        lines = lines[:i] + ["---", line, "---"] + lines[i:]
    else:
        body = lines[open_i + 1 : close_j]
        replaced = False
        for k, existing in enumerate(body):
            if existing.strip().startswith("replan_for:"):
                body[k] = line
                replaced = True
                break
        if not replaced:
            body.insert(0, line)
        lines = lines[: open_i + 1] + body + lines[close_j:]
    return "\n".join(lines) + ("\n" if content.endswith("\n") else "")


def replan_for_of(content: str | None) -> str | None:
    """TODO-0213: the replan marker declared by a TODO, or ``None``.

    Reads the ``replan_for:`` key of the front-matter block ONLY (body text
    is documentation and never marks a unit — a TODO about the replan
    feature itself may quote the marker without being one). No block, no
    key, an unreadable block or an invalid value → ``None`` (never raises:
    the caller decides what an unmarked TODO means). Other keys are not
    validated — a broken ``verify:`` list must not mask a valid marker.
    """
    if not content:
        return None
    yaml_lines, error = _find_contract_lines(content)
    if error or yaml_lines is None:
        return None
    try:
        data = yaml.safe_load("\n".join(yaml_lines))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict) or "replan_for" not in data:
        return None
    try:
        return check_replan_for(data["replan_for"])
    except ValueError:
        return None


def _yaml_body_is_contract(body: list[str]) -> bool:
    """The title-following leniency is only for a real contract body.

    A markdown horizontal rule after the title also starts with ``---``; its
    body is prose, not a YAML mapping. Treating it as a broken contract would
    refuse a TODO the baseline dispatched.
    """
    try:
        data = yaml.safe_load("\n".join(body))
    except yaml.YAMLError:
        return False
    return data is None or isinstance(data, dict)


def _find_contract_block(content: str) -> tuple[int | None, int | None, str | None]:
    """Locate the contract block. Returns (open_line, close_line, error).

    (None, None, None) — no block; (i, j, None) — the block spans lines
    ``i`` (``---``) and ``j`` (``---``) with YAML between; (i, None, error)
    — block opened but broken.

    The title may precede the block: a TODO usually opens with
    ``# TODO — <title>``, and a contract written just below it is still the
    contract (one heading only, and only when ``---`` follows it directly;
    a horizontal rule deeper in the body is not a block).
    """
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or s.startswith("<!--"):
            i += 1
            continue
        break
    after_title = False
    if i < len(lines) and re.match(r"^#\s+\S", lines[i].strip()):
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j < len(lines) and lines[j].strip() == "---":
            i = j
            after_title = True
    if i >= len(lines) or lines[i].strip() != "---":
        return None, None, None
    for j in range(i + 1, len(lines)):
        if lines[j].strip() == "---":
            if after_title and not _yaml_body_is_contract(lines[i + 1 : j]):
                return None, None, None
            return i, j, None
    if after_title:
        # F16 (TODO-0244): an ``---`` under the title with no closing rule is
        # a markdown horizontal rule, not a broken contract — the baseline
        # answered "no block", and the title-following leniency exists only
        # for a REAL body between two rules.
        return None, None, None
    return i, None, (
        "contract block opened with `---` at the top but no closing `---` "
        "found — close the block (one `---` line after the last key)"
    )


def _find_contract_lines(content: str) -> tuple[list[str] | None, str | None]:
    """Locate the contract block. Returns (yaml_lines, error).

    (None, None) — no block; (lines, None) — block found; (None, error) —
    block opened but broken.
    """
    open_i, close_j, error = _find_contract_block(content)
    if error:
        return None, error
    if open_i is None or close_j is None:
        return None, None
    lines = content.splitlines()
    return lines[open_i + 1 : close_j], None


def inject_pipeline_key(content: str, pipeline: str) -> str:
    """RUN3 #2 (Part B): write ``pipeline: <name>`` into the TODO's block.

    Creates the block when absent (inserted after the leading comment/blank
    lines, so a role-hint comment stays above it); replaces an existing
    ``pipeline:`` line when present (the explicit argument wins over a
    copy-pasted block). Returns the new content — the trailing newline is
    preserved as found.
    """
    name = check_pipeline_name(pipeline)
    line = f"pipeline: {name}"
    lines = content.splitlines()
    open_i, close_j, error = _find_contract_block(content)
    if error:
        raise ValueError(error)
    if open_i is None or close_j is None:
        i = 0
        while i < len(lines):
            s = lines[i].strip()
            if not s or s.startswith("<!--"):
                i += 1
                continue
            break
        lines = lines[:i] + ["---", line, "---"] + lines[i:]
    else:
        body = lines[open_i + 1 : close_j]
        replaced = False
        for k, existing in enumerate(body):
            if existing.strip().startswith("pipeline:"):
                body[k] = line
                replaced = True
                break
        if not replaced:
            body.insert(0, line)
        lines = lines[: open_i + 1] + body + lines[close_j:]
    return "\n".join(lines) + ("\n" if content.endswith("\n") else "")


def _check_str_list(value: object, field: str) -> None:
    """Field must be a non-empty list of non-empty strings."""
    if not isinstance(value, list):
        raise ValueError(
            f"'{field}' must be a list of command strings, "
            f'e.g. {field}: ["python3 -m pytest tests/ -q"]'
        )
    if not value:
        raise ValueError(
            f"'{field}' must not be empty — drop the key if there is nothing to declare"
        )
    for k, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"'{field}[{k}]' must be a non-empty string, got {item!r}")


#: A4 (REVIEW виток 2): shell operator characters that a ``verify:`` command
#: must not carry OUTSIDE quotes. The declared commands run as argv
#: (``shlex.split``, no shell — one launch mechanism shared by
#: candidate_verify and verify_pack), so an operator would silently become a
#: literal argument — a command that means something else than people expect.
#: F3 (REVIEW виток 3) adds command substitution — ``$(`` and backticks — and
#: the background ``&``: the same silent-literal class, and unlike ``VAR=val``
#: or globbing (both pass through argv as plain arguments) they promise
#: execution semantics the argv runner does not deliver.
_SHELL_OPS = ("&&", "||", ";", "|", ">", "<", "$(", "&", "`")


def _first_shell_op_outside_quotes(cmd: str) -> str | None:
    """The first shell operator at a quoted-agnostic position, or ``None``.

    A single pass tracks single- and double-quote (with ``\\`` escapes in
    double quotes); operators only count OUTSIDE any quote — a command like
    ``grep -n "pid > 1" f.py`` stays legal, and so does any operator inside
    quotes (an ``$(...)`` in a quoted text is a literal argument for shlex
    too). A backslash-escaped operator outside quotes (``a\\;b``) is a
    literal argument for ``shlex`` too, so it is not reported.
    ``&&``/``||`` are checked before the single-char forms so ``&&`` is
    not reported as its ``&``-less neighbour.
    """
    quote = ""
    escaped = False
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            if escaped:
                escaped = False
            elif quote == '"' and ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            i += 1
            continue
        if ch == "\\":
            # Outside quotes a backslash escapes the next character: shlex
            # keeps ``a\;b`` as ONE argument, so the escaped operator is not
            # a shell operator here either.
            i += 2
            continue
        if ch in {"'", '"'}:
            quote = ch
            i += 1
            continue
        for op in _SHELL_OPS:
            if cmd.startswith(op, i):
                return op
        i += 1
    return None


def shell_operator_refusal(cmd: str) -> str:
    """The A4 refusal text for a command carrying an unquoted shell operator.

    The ONE wording of the shell-operator policy (REVIEW виток 6, I1): the
    contract validation renders it with the ``verify[k]`` address, and every
    argv runner (config ``verification.*`` commands, candidate check,
    verify-pack) renders it as its own typed refusal — one predicate, one
    message. An empty string means the command is clean.

    K1 (REVIEW виток 8): the SAME predicate owns the H1 newline rule, so a
    config ``test_cmd``/``baseline_cmd`` cannot smuggle a second command past
    the argv runner either. A trailing ``\\r\\n``/``\\n`` (a YAML block scalar
    always appends one) is normalized away — one command; an INTERIOR newline
    means multiple commands on one surface and is refused.
    """
    text = str(cmd).rstrip("\r\n")
    if "\n" in text or "\r" in text:
        return (
            "multi-line verify — one command per entry: verify runs argv "
            "without a shell; use a script or separate commands"
        )
    op = _first_shell_op_outside_quotes(text)
    if not op:
        return ""
    return (
        f"contains a shell operator ({op!r}) outside quotes — commands run as "
        "argv without a shell, so use a script or separate commands (or quote "
        'the operator into a `sh -c "..."` argument)'
    )


def _check_verify_shell_operators(value: list) -> None:
    """A4 (REVIEW виток 2) + F3 (REVIEW виток 3) + H1 (REVIEW виток 5):
    ``verify:`` commands are argv, not shell lines.

    An unquoted ``&&``/``||``/``;``/``|``/``>``/``<``/``&``/backtick/
    ``$(``  would pass through the argv runner as a literal argument — a
    silent behavior change the writer did not declare. ``VAR=val`` and
    globbing stay legal: argv passes them through as plain arguments. The
    contract refuses the command up front; a pipeline step is a script
    file or separate commands.

    H1: a trailing ``\\r\\n``/``\\n`` (a YAML block scalar always appends
    one) is normalized away; INTERIOR newlines make the command
    multi-line — refused, for the same reason. K1 (виток 8): both the
    operator and the newline rule live in :func:`shell_operator_refusal`
    so the contract and every config runner give the same verdict.
    """
    for k, raw in enumerate(value):
        refusal = shell_operator_refusal(raw)
        if refusal:
            raise ValueError(f"'verify[{k}]' ({raw!r}) {refusal}")


def parse_todo_contract(content: str) -> tuple[dict | None, list[str]]:
    """Parse + validate the optional contract block at the top of a TODO.

    Returns ``(contract, unknown_keys)``: ``contract`` is ``None`` when the
    TODO has no block, otherwise a dict (possibly empty). Raises
    ``ValueError`` with a message naming the problem and the fix.
    """
    yaml_lines, error = _find_contract_lines(content)
    if error:
        raise ValueError(error)
    if yaml_lines is None:
        return None, []

    try:
        data = yaml.safe_load("\n".join(yaml_lines))
    except yaml.YAMLError as e:
        raise ValueError(f"contract block is not valid YAML: {e}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(
            f"contract block must be a YAML mapping with keys {list(_CONTRACT_KEYS)}, "
            f"got {type(data).__name__}"
        )

    unknown = [k for k in data if k not in _CONTRACT_KEYS]
    if "verify" in data:
        _check_str_list(data["verify"], "verify")
        _check_verify_shell_operators(data["verify"])
    if "flake" in data:
        value = data["flake"]
        if isinstance(value, bool):
            # YAML 1.1 reads ``off``/``on`` as booleans.
            data["flake"] = "on" if value else "off"
        elif isinstance(value, str) and value.strip().lower() in {"on", "off"}:
            data["flake"] = value.strip().lower()
        else:
            raise ValueError(
                "'flake' must be 'on' or 'off' — 'off' runs every `verify:` "
                "command once (the flake probe is skipped)"
            )
    if "gates" in data:
        _check_str_list(data["gates"], "gates")
        for g in data["gates"]:
            if g.strip() not in KNOWN_GATES:
                raise ValueError(
                    f"unknown gate '{g.strip()}' — allowed: "
                    + ", ".join(sorted(KNOWN_GATES))
                )
    if "prove_red" in data:
        _check_str_list(data["prove_red"], "prove_red")
    if "prove_red_pin" in data:
        _check_str_list(data["prove_red_pin"], "prove_red_pin")
    if "prove_red" in data and "prove_red_pin" in data:
        raise ValueError(
            "'prove_red' and 'prove_red_pin' are mutually exclusive — one "
            "TODO either proves a fix (red on the baseline, green now) or "
            "pins existing behavior (green on the baseline, teeth by "
            "mutation); declare exactly one of the two"
        )
    if "prove_red_cmd" in data:
        v = data["prove_red_cmd"]
        if not isinstance(v, str) or not v.strip():
            raise ValueError(
                "'prove_red_cmd' must be a non-empty shell command string, "
                'e.g. prove_red_cmd: "npm test -- --run"'
            )
    if "prove_red_kind" in data:
        v = data["prove_red_kind"]
        if not isinstance(v, str) or not v.strip():
            raise ValueError(
                "'prove_red_kind' must be a non-empty kind string, "
                "e.g. prove_red_kind: coverage"
            )
        kind = v.strip()
        if kind not in ("coverage", "new-api"):
            raise ValueError(
                f"unknown prove_red_kind {kind!r} — the declared kinds are "
                "'coverage' (tests check already-implemented behavior, so "
                "not-red is expected) and 'new-api' (the test falls on the "
                "baseline only because the new symbol does not exist yet)"
            )
        if "prove_red_pin" in data:
            raise ValueError(
                "'prove_red_kind' and 'prove_red_pin' are mutually "
                "exclusive — both expect green on the baseline, for "
                "different reasons (declared coverage vs a regression "
                "shield); declare exactly one of the two"
            )
        note = data.get("prove_red_note")
        if not isinstance(note, str) or not note.strip():
            raise ValueError(
                "'prove_red_kind' requires 'prove_red_note' — a one-line "
                "justification of the declared kind (why not-red is "
                "expected for `coverage`, or which symbol is new for "
                "`new-api`); the note goes into the GATES report"
            )
        pr = data.get("prove_red")
        if not isinstance(pr, list) or not pr:
            raise ValueError(
                "'prove_red_kind' requires a 'prove_red' list — the kind "
                "modifies the prove-red check, so the tests must be "
                "declared too"
            )
    if "prove_red_note" in data and "prove_red_kind" not in data:
        raise ValueError(
            "'prove_red_note' without 'prove_red_kind' — the note "
            "justifies the declared kind; declare both keys or neither"
        )
    if "files" in data:
        _check_str_list(data["files"], "files")
    if "pipeline" in data:
        check_pipeline_name(data["pipeline"])
    if "replan_for" in data:
        check_replan_for(data["replan_for"])
    if "checklist_classes" in data:
        value = data["checklist_classes"]
        if not isinstance(value, list) or not value:
            raise ValueError(
                "'checklist_classes' must be a non-empty list of class names — "
                "drop the key when the unit declares no classes"
            )
        bad = [c for c in value if not isinstance(c, str) or not c.strip()]
        if bad:
            raise ValueError(
                f"'checklist_classes' has a non-string or empty entry: {bad!r}"
            )
        data["checklist_classes"] = [c.strip() for c in value]
    if "wave" in data:
        value = data["wave"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "'wave' must be a non-empty wave name (a slug) — drop the key "
                "when the unit belongs to no wave"
            )
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", value.strip()):
            raise ValueError(
                f"'wave' must be a slug (letters, digits, '.', '_', '-'), got {value!r}"
            )
        data["wave"] = value.strip()
    return data, unknown


def parse_done_json(text: str) -> dict | None:
    """Parse + validate DONE.json. Returns a dict, or None when broken.

    Schema (all keys optional): ``files_changed: [str]``,
    ``tests_run: [{cmd: str, result: str}]``, ``gates: [str]``,
    ``notes: str``, ``stage: {stage_id: str, attempt: int, role: str,
    model: str}`` (ORCH M4.3, block and keys optional).
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("files_changed", "gates"):
        v = data.get(key)
        if v is not None and (
            not isinstance(v, list) or not all(isinstance(x, str) for x in v)
        ):
            return None
    tests_run = data.get("tests_run")
    if tests_run is not None:
        if not isinstance(tests_run, list):
            return None
        for item in tests_run:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("cmd"), str)
                or not isinstance(item.get("result"), str)
            ):
                return None
    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        return None
    # ORCH M4.3: the stage identity block — a violation skips the file.
    stage = data.get("stage")
    if stage is not None:
        if not isinstance(stage, dict):
            return None
        for key in ("stage_id", "role", "model"):
            v = stage.get(key)
            if v is not None and (not isinstance(v, str) or not v.strip()):
                return None
        attempt = stage.get("attempt")
        if attempt is not None and (
            not isinstance(attempt, int) or isinstance(attempt, bool)
        ):
            return None
    return data


def render_done_json(data: dict) -> list[str]:
    """Render a validated DONE.json dict as handoff markdown lines."""
    lines: list[str] = []
    files_changed = data.get("files_changed")
    if files_changed:
        listed = ", ".join(f"`{p}`" for p in files_changed[:10])
        more = f" … +{len(files_changed) - 10}" if len(files_changed) > 10 else ""
        lines.append(f"- files_changed: {len(files_changed)} — {listed}{more}")
    tests_run = data.get("tests_run")
    if tests_run:
        lines.append("- tests_run:")
        for item in tests_run:
            lines.append(f"  - `{item['cmd']}` → {item['result']}")
    gates = data.get("gates")
    if gates:
        lines.append(f"- gates: {', '.join(gates)}")
    notes = data.get("notes")
    if notes:
        lines.append(f"- notes: {notes}")
    stage = data.get("stage")
    if isinstance(stage, dict):
        parts = [
            f"{key}=" + fmt.format(stage[key])
            for key, fmt in (("stage_id", "`{}`"), ("attempt", "{}"),
                             ("role", "`{}`"), ("model", "`{}`"))
            if stage.get(key) is not None
        ]
        if parts:
            lines.append(f"- stage: {', '.join(parts)}")
    return lines


def render_stage_facts(
    stage_id: str, attempt: int, role: str, model: str | None = None,
    declared: dict | None = None,
) -> list[str]:
    """ORCH M4.3: the handoff's "Stage facts" section lines.

    The engine fills the identity itself — always present, even without
    DONE.json. ``declared`` is the DONE.json ``stage`` block (None = absent):
    a cross-check line follows, matched/mismatched — a note, never an error;
    only declared fields are compared.
    """
    lines: list[str] = []
    if stage_id:
        lines.append(f"- stage_id: `{stage_id}`")
    lines.append(f"- attempt: {attempt}")
    if role:
        lines.append(f"- role: `{role}`")
    if model:
        lines.append(f"- model: `{model}`")
    if declared:
        mismatches: list[str] = []
        decl_id = declared.get("stage_id")
        if decl_id is not None and stage_id and decl_id != stage_id:
            mismatches.append(f"stage_id declared `{decl_id}` (engine `{stage_id}`)")
        decl_attempt = declared.get("attempt")
        if decl_attempt is not None and decl_attempt != attempt:
            mismatches.append(f"attempt declared {decl_attempt} (engine {attempt})")
        decl_role = declared.get("role")
        if decl_role is not None and role and decl_role != role:
            mismatches.append(f"role declared `{decl_role}` (engine `{role}`)")
        if mismatches:
            lines.append(
                "- check vs DONE.json: mismatched — " + "; ".join(mismatches)
            )
        else:
            lines.append("- check vs DONE.json: matched")
    return lines


def collect_done_facts(
    outbox: Path, todo_id: str, logs_dir: Path
) -> tuple[str, list[str], dict | None]:
    """Read + validate DONE-{todo_id}.json for the handoff.

    Returns ``(fact_suffix, machine_facts_lines, declared_stage)``: the
    Run-facts suffix (``", DONE-json=present"``/``", DONE-json=absent"``),
    the rendered machine-facts lines (empty when absent, empty, or broken)
    and the DONE.json ``stage`` block (ORCH M4.3) or None — the handoff's
    Stage facts cross-checks it. A broken or schema-violating file logs a
    warning and never raises.
    """
    fact = ", DONE-json=absent"
    path = find_signal_file(outbox, "DONE", todo_id, ".json")
    if path is None:
        return fact, [], None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        # NEG-3: binary garbage in the DONE json degrades to absent facts,
        # the same as an unreadable file.
        _log(logs_dir, f"U3: {path.name} unreadable ({e}) — handoff skips machine facts")
        return fact, [], None
    if not text.strip():
        return fact, [], None
    data = parse_done_json(text)
    if data is None:
        _log(
            logs_dir,
            f"U3: {path.name} is broken or violates the DONE.json schema — "
            "handoff continues without the machine-facts section",
        )
        return fact, [], None
    lines = render_done_json(data)
    return (fact if not lines else ", DONE-json=present"), lines, data.get("stage")


__all__ = [
    "KNOWN_GATES",
    "parse_todo_contract",
    "check_pipeline_name",
    "check_replan_for",
    "inject_pipeline_key",
    "inject_replan_for",
    "replan_for_of",
    "parse_done_json",
    "render_done_json",
    "render_stage_facts",
    "collect_done_facts",
]
