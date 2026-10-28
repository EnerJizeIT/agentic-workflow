"""TODO dispatch: atomic create + baseline + ready signal.

Replaces the 3-step manual workflow:
  1. Write inbox/TODO-NNNN.md
  2. awf_baseline TODO-NNNN (snapshot)
  3. touch inbox/TODO-NNNN.ready (signal)

With one call. Auto-picks next NNNN, creates baseline snapshot, dispatches
the signal. Supervisor gets back the todo_id + baseline_sha to track.
"""
from __future__ import annotations

import json
import os
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path

from .. import closer_stage, git_utils, paths
from .._atomic import MODE_OWNER_EDITABLE
from .._markdown import (
    append_block,
    fence_flags,
    find_headings,
    first_heading_index,
    lines_below,
    section_body,
    skip_leading_trivia,
)
from ..dispatch_policy import (
    ISOLATE,
    REQUIRE_CLEAN,
    SourceTreeReport,
    check_source_tree,
    format_dirty_refusal,
    format_isolate_report,
    log_policy_decision,
    resolve_dirty_policy,
)
from ..infra import managed_fs
from ..todo_ids import is_valid_todo_id
from . import _liveness
from ._errors import AwfApiError
from ._helpers import refuse_unsafe_surfaces, require_awf_project
from ._results import BaselineResult, DispatchTodoResult
from .pipeline import create_baseline

#: TODO-0195: the dirty-tree warning lists at most this many files
#: (the rest as "…N more") — the same cap convention as the untracked one.
_DIRTY_WARNING_CAP = 10

#: TODO-0261 (RB-4): the dispatch-time baseline is a SMOKE snapshot, not a
#: gate. It gets its own timeout so a hanging test command cannot hold the
#: dispatch (the incident: a second dispatch hung on baseline tests, was
#: killed by the caller and left TODO-0255 without ``.ready``). Overridable
#: per project via ``verification.baseline_timeout`` (seconds).
DEFAULT_BASELINE_TIMEOUT_SECONDS = 60.0
BASELINE_TIMEOUT_CONFIG_KEY = "verification.baseline_timeout"

#: The write-ahead journal of an in-flight dispatch. An ATTEMPT-UNIQUE temp is
#: written before the id is reserved and promoted to ``DISPATCH-<id>.json``
#: after the reservation is won; it is removed right after ``.ready`` — a
#: crash/kill anywhere in between leaves a journal, so ``resume_dispatch`` can
#: finish the unit instead of leaving a TODO without its signal. The unique
#: temp is what keeps two parallel auto dispatches from overwriting each
#: other's journal before either owns the id.
_JOURNAL_PREFIX = "DISPATCH-"


def _baseline_timeout_seconds(project_dir: Path) -> float:
    """The dispatch-time baseline timeout: config value or the 60 s default."""
    from .. import config as cfg_mod

    raw = cfg_mod.get(cfg_mod.load(project_dir), BASELINE_TIMEOUT_CONFIG_KEY, "")
    try:
        value = float(str(raw).strip()) if str(raw).strip() else 0.0
    except (TypeError, ValueError):
        return DEFAULT_BASELINE_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_BASELINE_TIMEOUT_SECONDS


def _journal_path(project_dir: Path, todo_id: str) -> Path:
    return paths.context_dir(project_dir) / f"{_JOURNAL_PREFIX}{todo_id}.json"


def _journal_tmp_path(project_dir: Path, todo_id: str) -> Path:
    """An attempt-unique journal path (QA TODO-0261: collision ownership).

    Concurrent dispatches racing for the same AUTO id both wrote the canonical
    ``DISPATCH-<id>.json`` before the id was reserved, so the loser's body
    replaced the winner's — a killed winner then resumed with a foreign body
    (or with the loser's ``needs_manual_resume`` flag, silently dropping its
    exclusions). The unique name makes the pre-reservation write harmless:
    only the winner publishes it, the loser deletes its own file.
    """
    token = f"{os.getpid()}.{os.urandom(4).hex()}"
    return paths.context_dir(project_dir) / f"{_JOURNAL_PREFIX}{todo_id}.{token}.json"


def _write_journal(
    project_dir: Path,
    todo_id: str,
    body: str,
    *,
    needs_manual_resume: bool = False,
) -> Path:
    """Write the attempt-unique write-ahead journal; return its path."""
    payload = json.dumps(
        {
            "todo_id": todo_id,
            "content": body,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "needs_manual_resume": needs_manual_resume,
        },
        ensure_ascii=False,
    )
    tmp_path = _journal_tmp_path(project_dir, todo_id)
    managed_fs.managed_write_text(
        tmp_path,
        payload,
        root=project_dir,
        mode=MODE_OWNER_EDITABLE,
    )
    return tmp_path


def _publish_journal(project_dir: Path, todo_id: str, tmp_path: Path) -> None:
    """Promote the attempt-unique journal to the canonical resume path.

    Called only AFTER the id reservation is won, so a loser can never clobber
    the winner's journal. The temp file is removed once the canonical file is
    in place; a kill before this point leaves the temp, which
    ``_read_journal`` still finds.
    """
    try:
        payload = tmp_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    managed_fs.managed_write_text(
        _journal_path(project_dir, todo_id),
        payload,
        root=project_dir,
        mode=MODE_OWNER_EDITABLE,
    )
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError:
        pass


def _discard_journal_tmp(tmp_path: Path) -> None:
    """Best-effort removal of a loser's own journal temp (never canonical)."""
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError:
        pass


def _journal_candidates(project_dir: Path, todo_id: str) -> list[Path]:
    """Canonical journal first, then any attempt-unique leftovers."""
    context_dir = paths.context_dir(project_dir)
    candidates = [_journal_path(project_dir, todo_id)]
    try:
        candidates.extend(
            sorted(
                context_dir.glob(f"{_JOURNAL_PREFIX}{todo_id}.*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        )
    except OSError:
        pass
    return candidates


def _read_journal(project_dir: Path, todo_id: str) -> dict | None:
    for path in _journal_candidates(project_dir, todo_id):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _remove_journal(project_dir: Path, todo_id: str) -> None:
    for path in _journal_candidates(project_dir, todo_id):
        try:
            refuse_unsafe_surfaces(
                project_dir, "dispatch journal cleanup", path.parent, path
            )
            path.unlink(missing_ok=True)
        except (OSError, AwfApiError):
            pass


def _dispatch_baseline(
    project_dir: Path,
    todo_id: str,
    *,
    baseline_timeout: float,
    no_baseline_tests: bool,
    carry_over: str | None = None,
    include: set[str] | None = None,
    claim: set[str] | None = None,
) -> BaselineResult:
    """The dispatch smoke snapshot — ONE home for its rules (TODO-0261, RB-4).

    ``dispatch_todo`` and ``resume_dispatch`` both call this: the timeout, the
    ``--no-baseline-tests`` skip and the ``timeout`` status must not drift
    between a fresh dispatch and a resumed one.
    """
    return create_baseline(
        project_dir,
        todo_id,
        carry_over=carry_over,
        include=include,
        claim=claim,
        test_timeout=baseline_timeout,
        run_tests=not no_baseline_tests,
        timeout_status="timeout",
    )


def _write_ready(project_dir: Path, todo_id: str) -> None:
    """Publish the unit's ``.ready`` signal (no-follow), one refusal form."""
    ready_path = paths.inbox(project_dir) / f"{todo_id}.ready"
    try:
        managed_fs.managed_write_text(  # .ready — no-follow
            ready_path, "", root=project_dir, mode=MODE_OWNER_EDITABLE
        )
    except managed_fs.ManagedPathError as e:
        raise AwfApiError(f"dispatch refused — unsafe inbox path: {e}") from e


# REPORTS26 F5: a TODO number in the TITLE heading (a copied template keeps
# its old title). The title is the first H1 heading after the leading comments
# and the front matter — the same notion the source-ref insert resolves
# through ``_title_line_index`` and the commit subject reads in
# ``commit_gate._todo_title``. A TODO-NNNN after the title is ordinary
# text about another unit.
_HEADING_TODO_NUM_RE = re.compile(r"^#{1,6}\s+TODO-(\d+)")


def _heading_todo_number(content: str) -> int | None:
    """The number from the title heading, else None."""
    if not content:
        return None
    lines = content.splitlines()
    index = _title_line_index(lines)
    if index is None:
        return None
    m = _HEADING_TODO_NUM_RE.match(lines[index])
    return int(m.group(1)) if m else None


def _renumber_heading(content: str, todo_id: str) -> str:
    """Replace the number in the title heading with ``todo_id``'s.

    Only the title line is touched; the rest of the content stays byte-
    for-byte. The regex matched ``#... TODO-<digits>`` at the start of the
    line, so the first ``TODO-<digits>`` occurrence in it IS the heading's.
    """
    lines = content.splitlines(keepends=True)
    index = _title_line_index(lines)
    if index is None:
        return content
    lines[index] = re.sub(r"TODO-\d+", todo_id, lines[index], count=1)
    return "".join(lines)


# TODO-0186: file extensions that mark a token as a path even without a
# separator. Deliberately short — the heuristic stays conservative
# ("in doubt, don't warn"), so only the extensions that actually appear in
# verify commands are listed.
_PATH_EXTS = (".py", ".sh", ".md", ".yaml", ".yml", ".j2")

#: Flags that take their value as the NEXT token (not ``--flag=value``). A
#: path-like token right after one of these is the flag's argument, not a
#: positional path — skipped (in doubt, don't warn).
_VALUE_TAKING_FLAGS = frozenset({
    "-k", "--keyword",
    "-m", "--marker",
    "-p", "--plug-in", "--plugin",
    "-c", "--config",
    "-W", "--pythonwarnings",
    "--timeout", "--rootdir", "--confcutdir",
    "--ds", "--durations", "--maxfail",
    "-n",
})


def _shlex_tokens(cmd: str) -> list[str]:
    """Shell-split a verify command, tolerating broken quoting.

    ``shlex.split`` handles the quote wrappers (``-k "stack or init"`` is
    one token, not fragments); a ValueError from unbalanced quotes falls
    back to a plain whitespace split (the heuristic is best-effort and must
    never raise).
    """
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:
        return cmd.split()


def _looks_like_path(token: str, prev: str) -> bool:
    """Whether a verify-command token is a file-path candidate.

    A candidate contains ``/`` or ends with a known extension, and is not a
    flag (leading ``-``), a URL (``://``), a shell variable (``$``), or the
    separate value of a value-taking flag (``-k EXPR``). In doubt the token
    is NOT a path — the heuristic has no right to warn falsely.
    """
    if not token or token.startswith("-"):
        return False
    if "://" in token or "$" in token:
        return False
    if prev in _VALUE_TAKING_FLAGS:
        return False
    return "/" in token or token.endswith(_PATH_EXTS)


def _verify_path_tokens(commands: list[str]) -> list[str]:
    """Extract the path-like tokens from verify commands (TODO-0186).

    Runs the :func:`_looks_like_path` heuristic over the shell-split tokens
    of every command. Returns the unique candidates in first-seen order —
    the caller checks their existence relative to the project.
    """
    found: list[str] = []
    seen: set[str] = set()
    for cmd in commands:
        tokens = _shlex_tokens(cmd)
        prev = ""
        for token in tokens:
            if _looks_like_path(token, prev) and token not in seen:
                seen.add(token)
                found.append(token)
            prev = token
    return found


def _missing_verify_paths(project_dir: Path, tokens: list[str]) -> list[str]:
    """The path tokens that do NOT exist relative to ``project_dir``.

    A relative token is joined onto the project root; an absolute token
    keeps its own root (pathlib semantics).
    """
    return [t for t in tokens if not (project_dir / t).exists()]


def _dirty_tree_classes(project_dir: Path) -> dict[str, str]:
    """TODO-0195 / TODO-0204 / TODO-0221: the paths that are NOT clean at
    dispatch time, with their class: ``staged`` (the index differs from
    HEAD), ``tracked-dirty`` (a worktree-only change of a tracked path —
    the pre-dispatch WIP the delta mechanism covers), ``untracked``.

    Read through the R2.1 git model — ONE git parser, byte-faithful paths:
    the printed ``git status --short`` C-quotes a non-ASCII name
    (``"\\321\\204..."``), so an adopted path the model gives as raw bytes
    (``файл.txt``) no longer compared equal and the exclusion missed
    (TODO-0221 M1). First-seen order, duplicates dropped. ``{}`` on a clean
    tree,     and on a tree the model refuses too (best effort — this feeds a
    warning, it must never refuse a dispatch).

    The untracked side comes from :meth:`GitRepository.untracked_paths`, the
    SAME listing the policy check reads: ``changes()`` drops a secret-looking
    untracked name (``.env``) from the ownership operations, so the warning
    would omit a path the require_clean refusal / isolate table name — the
    three surfaces must describe one tree.
    """
    from ..dispatch_policy import is_managed_runtime_path
    from ..infra.git_model import GitModelError, GitRepository, display_path

    if not git_utils.is_git_repo(project_dir):
        return {}
    try:
        repo = GitRepository(project_dir)
        records = repo.changes()
        untracked = repo.untracked_paths()
    except GitModelError:
        return {}  # best effort: the warning must never refuse a dispatch
    classes: dict[str, str] = {}
    for r in records:
        # R2.2: managed AWF runtime artifacts are never the USER's dirt — the
        # same documented exceptions the source-tree policy check applies.
        if is_managed_runtime_path(r.path):
            continue
        path = display_path(r.path)
        if not path or path in classes:
            continue
        if r.kind == "untracked":
            classes[path] = "untracked"
        elif _change_is_staged(r):
            classes[path] = "staged"
        else:
            classes[path] = "tracked-dirty"
    for raw in untracked:
        if is_managed_runtime_path(raw):
            continue
        path = display_path(raw)
        if path:
            classes.setdefault(path, "untracked")
    return classes


def _change_is_staged(record) -> bool:
    """True when the R2.1 change sits in the INDEX (staged vs HEAD).

    The record's index facts moved from the base ones, or the path is NEW in
    the index (added/renamed/copied — the base has no entry). Everything
    else is a worktree-only change of a tracked path (``tracked-dirty``),
    the class the dispatch-time worktree snapshot and the delta mechanism
    cover.
    """
    if record.kind in ("added", "renamed", "copied"):
        return True
    return (
        record.index_blob != record.base_blob
        or record.index_mode != record.base_mode
    )


def _group_list(paths_list: list[str], cap: int = 5) -> str:
    """The first ``cap`` paths for a warning group (…N more when cut).

    Names render through :func:`escape_str_for_ui` (F3) — a non-UTF-8 byte in
    a path must not break the strict-UTF-8 sink the warning is emitted
    through.
    """
    from ..infra.git_model import escape_str_for_ui

    shown = [escape_str_for_ui(p) for p in paths_list[:cap]]
    more = len(paths_list) - len(shown)
    return ", ".join(shown) + (f" …({more} more)" if more else "")


def _format_dirty_warning(
    todo_id: str,
    files: list[str],
    classes: dict[str, str],
    snapshot_available: bool,
    snapshot_reason: str = "",
) -> str:
    """TODO-0195 / TODO-0204: the dispatch warning for a dirty tree —
    honest about what the commit gate will actually do. ``""`` when the
    tree is clean (nothing to say).

    Tracked-dirty files (the pre-dispatch WIP): with the dispatch-time
    worktree snapshot (``BASELINE-{todo}.worktree``) the gate commits
    only the unit's own edit — the pre-dispatch change STAYS OUTSIDE the
    unit commit. Without the snapshot (not a git repo, a failed or absent
    snapshot) the gate cannot split the two — the pre-dispatch change
    WILL ENTER the commit as-is, and the warning says so. Staged-at-
    baseline and untracked files never enter the commit (claim them via
    include_untracked / carry_over_from) — in every case.
    """
    if not files:
        return ""
    from ..infra.git_model import escape_str_for_ui

    shown = [escape_str_for_ui(f) for f in files[:_DIRTY_WARNING_CAP]]
    more = len(files) - len(shown)
    tail = f" (…{more} more)" if more else ""
    claim = (
        "staged-at-dispatch and untracked files do not enter the commit "
        "(to claim them: include_untracked / carry_over_from)."
    )
    tracked = [f for f in files if classes.get(f) == "tracked-dirty"]
    if not tracked:
        # Untracked/staged only — the legacy promise holds as-is.
        return (
            f"⚠️ the tree is dirty at dispatch: {len(files)} file(s) "
            f"({', '.join(shown)}{tail}) will NOT enter the {todo_id} commit — "
            "these changes pre-date the unit. To claim them into the unit: "
            "include_untracked (untracked files) / carry_over_from (a rejected "
            "retry); otherwise they stay WIP for whoever commits them."
        )
    td = _group_list(tracked)
    if snapshot_available:
        return (
            f"⚠️ the tree is dirty at dispatch: {len(files)} file(s) "
            f"({', '.join(shown)}{tail}). pre-dispatch changes to "
            f"tracked-dirty files ({td}) stay OUTSIDE the unit commit — "
            "only the unit's own edits to them will land; " + claim
        )
    reason = snapshot_reason or "no worktree snapshot available"
    return (
        f"⚠️ the tree is dirty at dispatch: {len(files)} file(s) "
        f"({', '.join(shown)}{tail}). pre-dispatch changes to "
        f"tracked-dirty files ({td}) WILL ENTER the unit commit as-is — "
        f"{reason}, so the commit gate cannot exclude them; " + claim
    )


# TODO-0229 (M5): the source-ref section extractor. A list item is a
# bullet or a numbered item; headings come from the shared markdown helper.
_MD_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
#: An item that is already a task-list checkbox — boxing it again would
#: render ``- [ ] [x] …`` and turn the original mark into literal text.
_MD_CHECKBOX_RE = re.compile(r"^\[[ xX]\]\s")


def _split_source_ref(ref: str, label: str) -> tuple[str, str]:
    """Split ``<file>#<section>``; both halves are required.

    ONE parser for the legacy ``source_ref`` and the package links (K4):
    two copies would drift on the edges (a missing ``#``, an empty half),
    and this refusal is the unit's public message. ``label`` is the text
    the refusal shows (the caller's original value).
    """
    if "#" not in ref:
        raise AwfApiError(
            f"source_ref {label!r}: expected '<file>#<section>' "
            "(e.g. 'R2-GIT-OWNERSHIP.md#R2.5')"
        )
    file_part, section = ref.rsplit("#", 1)
    file_part, section = file_part.strip(), section.strip()
    if not file_part or not section:
        raise AwfApiError(
            f"source_ref {label!r}: both the file and the section are "
            "required ('<file>#<section>')"
        )
    return file_part, section


def _resolve_source_file(project_dir: Path, source_ref: str) -> tuple[Path, str]:
    """Resolve ``<file>#<section>`` to an existing file + the section name.

    A relative path is joined onto the project root and must stay inside
    it; an absolute path is taken as-is (explicitly passed — never guessed).
    Refuses BEFORE any side effect: a missing/unreadable file or an empty
    half of the reference is an error, not a silent no-op.
    """
    ref = str(source_ref or "").strip()
    file_part, section = _split_source_ref(ref, str(source_ref))

    candidate = Path(file_part)
    if candidate.is_absolute():
        path = candidate.resolve()
    else:
        path = (project_dir / candidate).resolve()
        try:
            path.relative_to(project_dir.resolve())
        except ValueError:
            raise AwfApiError(
                f"source_ref {source_ref!r}: the file must stay inside the "
                f"project (got '{file_part}')"
            ) from None
    if not path.is_file():
        raise AwfApiError(
            f"source_ref {source_ref!r}: no such file '{file_part}' "
            f"(resolved to {path})"
        )
    return path, section


def _read_section_body(
    path: Path, text: str, section: str, label: str
) -> list[str]:
    """Body lines of the exact heading ``section`` in ``text``.

    ONE rule for the legacy ``source_ref`` (INV2) and the package links
    (K4). Only an EXACT heading text (case-insensitive) matches — no
    first-word fallback. Several headings carrying the same exact text are
    ambiguous: the caller gets a refusal naming the line numbers instead of
    a silent first hit. The body runs until the next heading of the same or
    a higher level, so nested sub-sections stay inside. An unknown section
    is a refusal (no TODO, no baseline).
    """
    hits = find_headings(text, section)
    if not hits:
        raise AwfApiError(
            f"source_ref {label!r}: section '{section}' not found in {path}"
        )
    if len(hits) > 1:
        places = ", ".join(f"line {index + 1}" for index, _ in hits)
        raise AwfApiError(
            f"source_ref {label!r}: section '{section}' is ambiguous — "
            f"{len(hits)} headings match exactly ({places}) in {path}; make "
            "the heading unique or reference another section"
        )
    index, level = hits[0]
    return lines_below(text, index, level)


def _extract_source_section(
    path: Path, section: str, source_ref: str
) -> list[str]:
    """Read ``path`` and return the body of its exact heading ``section``."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise AwfApiError(f"source_ref {source_ref!r}: cannot read {path}: {e}") from e
    return _read_section_body(path, text, section, source_ref)


def _source_ref_block(section: str, body: list[str]) -> str:
    """The ``## Первоисточник`` block: one unchecked box per list item.

    Everything that is not a list item keeps its bytes: prose stays prose,
    a multi-line item's continuation lines follow their box untouched, and
    a fenced code block keeps its fences — a ``- key: value`` line inside
    it is an example, not a requirement. The fence rule is the same shared
    predicate the section reader uses (``awf._markdown.fence_flags``), so the
    two cannot drift apart. Nothing from the section is dropped silently.
    A section without list items still reaches the worker verbatim.
    """
    out: list[str] = []
    for line, in_fence in zip(body, fence_flags(body)):
        if not in_fence:
            m = _MD_LIST_ITEM_RE.match(line)
            if m:
                rest = m.group(1)
                if not _MD_CHECKBOX_RE.match(rest):
                    indent = line[: len(line) - len(line.lstrip())]
                    line = f"{indent}- [ ] {rest}"
                out.append(line)
                continue
        out.append(line)
    payload = "\n".join(out).strip() or "_раздел пуст_"
    return f"## Первоисточник ({section})\n\n{payload}\n"


def _spec_package_transfer(
    project_dir: Path, package: str, refs: list[str]
) -> str:
    """K4: the ``## Пакет спецификации`` + ``## Первоисточник`` blocks.

    The norm is a SNAPSHOT: the package summary (path, ``Пакет-sha``, six
    revisions, every ID, the residual risks) plus one verbatim body per
    link, in the declared order. Every refusal happens here, before any
    side effect, and names the link that failed.
    """
    from ..spec_package import (
        ROLES,
        SpecHeader,
        parse_coverage_matrix,
        parse_spec_header,
    )
    from ..unit_contract import resolve_spec_package

    package_dir, label = resolve_spec_package(project_dir, package)

    layout_docs = {
        role: package_dir / name
        for role, name in _spec_document_names(package_dir).items()
    }
    texts: dict[str, str] = {}
    headers: dict[str, SpecHeader] = {}
    for role in ROLES:
        path = layout_docs.get(role)
        if path is None or not path.is_file():
            raise AwfApiError(
                f"spec_package {package!r}: no {role} document in {label}"
            )
        try:
            texts[role] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise AwfApiError(
                f"spec_package {package!r}: cannot read {path}: {e}"
            ) from None
        try:
            headers[role] = parse_spec_header(
                texts[role], role, path.relative_to(package_dir).as_posix()
            )
        except ValueError as e:
            raise AwfApiError(f"spec_package {package!r}: {e}") from None

    try:
        rows = parse_coverage_matrix(texts["readiness"], f"{label}/readiness")
    except ValueError as e:
        raise AwfApiError(f"spec_package {package!r}: {e}") from None

    readiness = headers["readiness"]
    revisions = ", ".join(
        f"`{headers[role].path}`={headers[role].revision}" for role in ROLES
    )
    block_lines = [
        "## Пакет спецификации",
        "",
        f"- Путь: `{label}`",
        f"- Пакет-sha: `{readiness.package_sha}`",
        f"- Ревизии: {revisions}",
        f"- ID: {', '.join(row.id for row in rows)}",
    ]
    risks = _residual_risks(texts)
    if risks:
        block_lines.extend(["- Остаточные риски:", ""])
        block_lines.extend(f"  {line}" for line in risks.splitlines())
    else:
        block_lines.append("- Остаточные риски: не заявлены")
    blocks = ["\n".join(block_lines)]

    for ref in refs:
        path, section, body = _resolve_package_source(
            project_dir, package_dir, label, ref
        )
        covered = _covered_ids(rows, package_dir, path, section)
        if not covered:
            raise AwfApiError(
                f"source_ref {ref!r}: no coverage-matrix row names this "
                f"source — the link passes no norm ID"
            )
        payload = "\n".join(body).strip("\n")
        blocks.append(
            f"## Первоисточник ({section})\n\n"
            f"Покрытые ID: {', '.join(covered)}\n\n{payload}\n"
        )
    return "\n\n".join(blocks)


def _spec_document_names(package_dir: Path) -> dict[str, str]:
    from ..spec_package import discover_documents

    layout = discover_documents(package_dir)
    if layout.problems:
        raise AwfApiError(
            "spec_package: " + "; ".join(layout.problems)
        )
    return {
        role: path.relative_to(layout.root).as_posix()
        for role, path in layout.documents.items()
    }


def _resolve_package_source(
    project_dir: Path, package_dir: Path, label: str, ref: str
) -> tuple[Path, str, list[str]]:
    """Resolve one package link; every failure is a locator-rich refusal."""
    file_part, section = _split_source_ref(ref, ref)
    candidate = Path(file_part)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AwfApiError(
            f"source_ref {ref!r}: the link must stay inside the package "
            f"({label}) — no absolute path, no '..'"
        )
    path = (project_dir / candidate).resolve()
    try:
        path.relative_to(package_dir.resolve())
    except ValueError:
        raise AwfApiError(
            f"source_ref {ref!r}: the file is outside the package ({label})"
        ) from None
    if not path.is_file():
        raise AwfApiError(
            f"source_ref {ref!r}: no such file '{file_part}' "
            f"(resolved to {path})"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise AwfApiError(f"source_ref {ref!r}: cannot read {path}: {e}") from None
    body = _read_section_body(path, text, section, ref)
    if not any(line.strip() for line in body):
        raise AwfApiError(
            f"source_ref {ref!r}: section '{section}' in {path} has an "
            "empty body — nothing to transfer"
        )
    return path, section, body


def _covered_ids(
    rows: list, package_dir: Path, path: Path, section: str
) -> list[str]:
    """Matrix IDs whose ``Источник`` is exactly this file + section (K4/D8).

    The matrix names a section TOKEN (``pkg.md#R1``) while a link names the
    exact heading (``pkg.md#R1 — Первое правило``); the token matches the
    heading as a whole word — the ONE rule lives in
    :func:`spec_package.section_token_matches` (K1/D8), so the gate and the
    transfer can never disagree about which section an ID names.
    """
    from ..spec_package import section_token_matches

    covered: list[str] = []
    for row in rows:
        raw = str(row.source or "")
        if "#" not in raw:
            continue
        rel, row_section = raw.rsplit("#", 1)
        if not section_token_matches(section, row_section.strip()):
            continue
        try:
            row_path = (package_dir / rel.strip()).resolve()
        except (OSError, ValueError):
            continue
        if row_path == path.resolve():
            covered.append(row.id)
    return covered


def _residual_risks(texts: dict[str, str]) -> str:
    from ..spec_package import ROLES

    for role in ROLES:
        body = section_body(texts.get(role, ""), "Остаточные риски")
        if body.strip():
            return body.strip()
    return ""


def _title_line_index(lines: list[str]) -> int | None:
    """Index of the TODO title line, or None when the content has none.

    The title is the first H1 heading after the leading comments / front
    matter / blank lines — the SAME notion ``commit_gate._todo_title`` uses
    for the commit subject, so a unit cannot have a title in the git log
    that dispatch did not see (or the other way around). A deeper heading
    is a body section, never the title: resolving one as the title would
    let the REPORTS26 F5 guards renumber a section, and the source-ref
    insert would land the block in the middle of the body. Fenced lines do
    not count either — an example's ``#``-line is a comment.

    The REPORTS26 F5 guards and the source-ref insert resolve the title
    through this helper, so a leading comment cannot make one of them read
    a different line.
    """
    start = skip_leading_trivia(lines)
    return first_heading_index(lines, level=1, start=start)


def _insert_at(content: str, index: int, block: str) -> str:
    """Insert ``block`` (followed by a blank line) before line ``index``."""
    lines = content.splitlines(keepends=True)
    head = "".join(lines[:index])
    tail = "".join(lines[index:])
    if head and not head.endswith("\n"):
        head += "\n"
    return head + block + "\n" + tail


def _insert_after_title(content: str, block: str) -> str:
    """Insert ``block`` right after the TODO title heading.

    The title must stay the first heading: the REPORTS26 F5 guards resolve
    it through ``_title_line_index``, so a block placed before it would
    blind them. The front matter and leading comments stay above the title;
    content without a title keeps the old position (right after the
    contract block — the contract must stay the first thing a parser sees).
    """
    lines = content.splitlines(keepends=True)
    title = _title_line_index(lines)
    if title is None:
        return _insert_at(content, skip_leading_trivia(lines), block)
    insert = title + 1
    while insert < len(lines) and not lines[insert].strip():
        insert += 1
    return _insert_at(content, insert, block)


_SIZE_REQUIREMENTS_LIMIT = 6
_SIZE_FILES_LIMIT = 12


def unit_size_warning(content: str) -> str:
    """TODO-0242 (B.13): the soft mega-unit lock — a warning, not a refusal.

    Size was never visible at dispatch time: a TODO with sixteen numbered
    requirements and twenty files went into one unit and the worker had to
    hold all of it. Count the two cheap structural signals — numbered
    requirements (``## Требования``) and named files (``## Границы``).
    """
    # The section reader is the shared walker (``awf._markdown.section_body``):
    # a body heading inside a fence is an example, and a nested ``###``
    # stays inside the section — the same rules the other section readers
    # use. A second, regex-only reader here would answer differently.
    requirements = len(
        re.findall(
            r"^[ \t]*\d+[.)][ \t]+",
            section_body(content, "Требования"),
            re.MULTILINE,
        )
    )
    boundaries = section_body(content, "Границы")
    files = {
        token
        for token in re.findall(r"`([^`]+)`", boundaries)
        if "/" in token or "." in token
    }
    if requirements > _SIZE_REQUIREMENTS_LIMIT:
        return (
            f"looks like a mega-unit: {requirements} numbered requirements "
            f"(>{_SIZE_REQUIREMENTS_LIMIT}) — split it before dispatch"
        )
    if len(files) > _SIZE_FILES_LIMIT:
        return (
            f"looks like a mega-unit: {len(files)} files in the boundaries "
            f"block (>{_SIZE_FILES_LIMIT}) — split it before dispatch"
        )
    return ""


def _next_todo_id(project_dir: Path) -> str:
    """Pick next TODO-NNNN id (max existing + 1).

    Scans inbox, outbox, AND done/ for any TODO-NNNN.* files to avoid
    collisions with already-completed/archived TODOs.
    """
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    done = paths.done_dir(project_dir)

    max_num = 0
    for d in (inbox, outbox, done):
        if not d.is_dir():
            continue
        for f in d.glob("TODO-*.md"):
            m = re.match(r"^TODO-(\d+)$", f.stem)
            if m:
                num = int(m.group(1))
                if num > max_num:
                    max_num = num
        # Also check done/ subdirectories (done/TODO-NNNN/)
        if d == done:
            for sub in d.iterdir():
                if sub.is_dir():
                    m = re.match(r"^TODO-(\d+)$", sub.name)
                    if m:
                        num = int(m.group(1))
                        if num > max_num:
                            max_num = num

    return f"TODO-{max_num + 1:04d}"


def dispatch_todo(
    project_dir: Path,
    content: str,
    *,
    role: str | None = None,
    todo_id: str | None = None,
    pipeline: str | None = None,
    carry_over_from: str | None = None,
    include_untracked: list[str] | None = None,
    allow_mismatch: bool = False,
    replan_for: str | None = None,
    claim_from: str | None = None,
    source_ref: str | None = None,
    source_file: str | None = None,
    no_baseline_tests: bool = False,
    force: bool = False,
) -> DispatchTodoResult:
    """Atomically create a TODO, baseline it, dispatch the signal.

    Three-step manual workflow compressed into one call:
      1. Write ``.agentic/inbox/TODO-NNNN.md`` (atomic).
      2. Create ``.agentic/context/BASELINE-NNNN.{sha,status,tests.log,env.log}``.
      3. Write ``.agentic/inbox/TODO-NNNN.ready`` signal (trigger for plan stage).

    Args:
        project_dir: awf project root (must contain .agentic/).
        content: TODO markdown body (the task description).
        role: optional role hint for logging/debugging (does not affect
            pipeline routing — that's determined by stage order in
            pipeline.yaml). Useful for supervisor context: "this TODO
            is for agent-system-analyst".
        todo_id: override auto-generated id (e.g. "TODO-0007"). If None,
            auto-picks next available NNNN by scanning inbox + outbox.
        pipeline: RUN3 #2 (Part B) — optional pipeline name, written into
            the TODO's contract block as ``pipeline: <name>`` (the block is
            created when absent). When an ``awf_run_next`` launch has no
            queue-level pipeline, it uses this one.
        carry_over_from: RUN5 #1 (leak-gate) — the id of a REJECTED TODO
            whose files this retry re-claims. The paths recorded in
            ``.agentic/context/REJECT-<origin>.files`` (the attempt's new
            untracked files AND its tracked delta) are excluded from the new
            baseline's untracked snapshot and adopted by the retry's commit
            plan, so the retry's candidate and commit include them. Refused
            (before any side effect) when the origin TODO or its REJECT file
            does not exist.
        include_untracked: RUN10 #4 (TODO-0074) — pre-existing untracked
            files to include in this unit's commit. Each path must exist,
            be untracked, be not gitignored, and stay inside the project;
            ANY invalid path refuses the dispatch before any side effect
            (no TODO, no baseline). The paths are excluded from the
            baseline's untracked snapshot, so the commit gate commits them
            with the unit. The trace lives in
            ``.agentic/context/BASELINE-<id>.include``. Independently, the
            answer always lists the files that STAY excluded
            (``pre_existing_untracked`` + ``untracked_warning``).
        allow_mismatch: REPORTS26 F5 — heading/title mismatch policy. The
            title heading (the first heading after the leading comments and
            the front matter) carries a ``TODO-NNNN`` number:

             - auto number (``todo_id`` is None) and the number differs from
               the issued one → the heading is renumbered to the issued id,
               the result carries ``renumbered=True`` (rest of the text
               untouched);
             - explicit ``todo_id`` and the number differs → the dispatch is
               refused BEFORE any side effect (the message names both
               numbers); ``allow_mismatch=True`` accepts the mismatch and
               writes the content as-is (``renumbered=False``).
        replan_for: TODO-0213 (BD-30) — optional marker that this TODO is
            the replan answer for the unit being replaced (its id). The
            marker ``replan_for: <id>`` is written into the TODO's contract
            block (created when absent; an explicit argument wins over a
            copy-pasted block, like ``pipeline=``). Only a TODO carrying the
            marker is picked up by the interactive replan wait — an
            ordinary mid-run dispatch (no marker) does not switch a live
            pipeline. The referenced unit must exist and be live: refused
            (before any side effect) when it has no inbox TODO-NNNN.md, is
            archived in done/, or is closed as done (DONE closure).

            Example (mid-run replan of the blocked unit TODO-0204)::

                dispatch_todo(project, "# TODO — refined task\\n...",
                              replan_for="TODO-0204")

            A hand-written ``replan_for: TODO-0204`` line in the content's
            contract block works the same way (the parameter then wins).
        claim_from: R2.2 (roadmap R2) — the id of a REJECTED/INTERRUPTED unit
            whose CODE this retry deliberately owns. The applied claim record
            (``.agentic/context/CLAIM-<source>.json``, written by
            ``claim_changes``) is read; its paths are excluded from the new
            baseline's untracked snapshot exactly like ``include_untracked``.
            This is the code channel — the R5 finding channel stays
            ``carry_over_from`` / ``include_untracked``. Refused (before any
            side effect) when no claim record exists or a claimed change is no
            longer in the tree.
        source_ref: TODO-0229 (M5) — the source of truth for this unit, as
            ``<file>#<section>`` (e.g. ``R2-GIT-OWNERSHIP.md#R2.5 Ownership``).
            The file is read, the section (its heading + body up to the next
            heading of the same or a higher level) is inserted into the TODO
            as a ``## Первоисточник (<section>)`` checklist built from the
            section's list items; the heading must match the section text
            EXACTLY (no first-word fallback), and several identical headings
            are an explicit refusal. Prose, item continuations and fenced
            code blocks stay as they are. A relative path is resolved
            against the project root and must stay inside it; an absolute
            path is used as passed. A missing/unreadable file or an unknown
            section refuses BEFORE any side effect (no TODO, no baseline).
        source_file: PA-0284-3 — the file the content was read from, when
            the caller is a CLI route (``--content-file``). It prefixes a
            contract parse error so the message names the suspect file
            without the caller matching on message text. ``None`` for a
            direct content string — the message is unchanged.
        no_baseline_tests: TODO-0261 (RB-4) — skip the baseline test command
            entirely. The snapshot still records sha/status/env; the tests
            log says ``skipped`` and ``.ready`` is written immediately. For
            projects whose baseline command is slow by design.
        force: TODO-0261 (RB-4) — allow a re-dispatch to overwrite an
            existing ``inbox/<id>.md`` for an EXPLICIT ``todo_id``. Refused
            while a pipeline is live (the running unit must not be rewritten
            under it) and still bound by the archive/DONE-closure checks.
            Without it an existing TODO is never overwritten — the previous
            refusal is unchanged.

    The dispatch is journaled: ``context/DISPATCH-<id>.json`` carries the
    TODO body and is written before the id is reserved. A crash or kill
    during the baseline leaves that journal (and possibly the md) behind —
    ``resume_dispatch(project_dir, todo_id)`` finishes the unit: restores a
    missing/empty md, re-runs the smoke baseline under the same timeout and
    writes ``.ready``. A normal failure cleans both — no orphan TODO.

    The dirty-source-tree policy (R2.2) governs the dispatch: a project with
    ``config_version`` and ``dirty_policy=require_clean`` (the version
    default) refuses a dirty source tree BEFORE reserving the id — no TODO,
    no baseline. ``dirty_policy=isolate`` proceeds and reports the excluded
    operations plus a dependency warning. A legacy project (no
    ``config_version``) keeps the pre-R2.2 ``warn`` behaviour and the result
    carries the migration warning. A dirty path covered by an ADOPTED channel
    — ``claim_from`` (code) or ``carry_over_from`` / ``include_untracked``
    (finding) — is owned, not refused.

    Re-dispatch of a number with stale BLOCKED/ACK closures (RUN3 #4)
    clears them automatically; a DONE closure refuses the dispatch
    (use ``awf restore`` for an archived TODO).

    Returns:
        DispatchTodoResult with todo_id, baseline_sha, files written,
        ``renumbered`` (REPORTS26 F5: True when the auto-issued
        number replaced a foreign one in the title heading),
        ``verify_path_warnings`` (TODO-0186: the contract's verify: path
        tokens missing from the project — a warning, never a refusal) and
        ``dirty_files``/``dirty_warning`` (TODO-0195: the tracked-dirty /
        staged / untracked paths present at dispatch — they will NOT enter
        the unit's commit; a warning, never a refusal). R2.2 adds
        ``dirty_policy`` (the effective policy), ``dirty_policy_warning``
        (legacy migration), ``isolate_warning``/``isolate_dependency_warning``
        (the isolate artefacts) and ``claim_from``/``claimed_files`` (the code
        transfer applied).

    Raises:
        AwfApiError: if .agentic/ missing, content empty, ``todo_id`` is
            already archived in done/, a DONE closure for ``todo_id`` is
            still in the outbox, a ``carry_over_from`` origin cannot be
            validated, an ``include_untracked`` path is invalid, an
            explicit ``todo_id`` disagrees with the title heading's
            number without ``allow_mismatch`` (REPORTS26 F5),
            ``replan_for`` is not a TODO id / references a unit that does
            not exist or is closed as done (TODO-0213), the dirty-source-tree
            policy is ``require_clean`` and the tree is dirty (R2.2), or
            ``claim_from`` has no applied claim / its confirmed plan is
            invalidated by a later tree change (R2.2).
    """
    if not content or not content.strip():
        raise AwfApiError("content is required (non-empty TODO body)")

    # RUN3 #2 (Part B): an explicit pipeline= wins over a copy-pasted block
    # — inject first, so the parse below validates the FINAL content.
    if pipeline is not None and str(pipeline).strip():
        from ..unit_contract import inject_pipeline_key

        try:
            content = inject_pipeline_key(content, str(pipeline))
        except ValueError as e:
            raise AwfApiError(f"pipeline: {e}") from None

    # TODO-0213 (BD-30): same pattern — the explicit replan_for= writes the
    # marker into the contract block (created when absent) before the parse
    # below validates the FINAL content. check_replan_for inside
    # inject_replan_for refuses a value that is not a TODO-NNNN id.
    if replan_for is not None and str(replan_for).strip():
        from ..unit_contract import inject_replan_for

        try:
            content = inject_replan_for(content, str(replan_for))
        except ValueError as e:
            raise AwfApiError(f"replan_for: {e}") from None

    # U3: optional unit-contract block at the top of the TODO (--- yaml ---)
    # is validated here, before anything is written. No block = no-op.
    from ..unit_contract import parse_todo_contract
    try:
        _contract, unknown_keys = parse_todo_contract(content)
    except ValueError as e:
        # PA-0284-3: the file name is CARRIED here, not sniffed from the
        # message text by the caller — a contract error and a project
        # precondition must not be told apart by a substring.
        prefix = f"{source_file}: " if source_file else ""
        raise AwfApiError(f"{prefix}TODO contract block: {e}") from None

    # REPORTS26 B2: write entry point — a bare .agentic/ without
    # config.yaml (stale directory) is refused before any side effect.
    # The resolved path lands in the result (resolved_project_dir).
    project_dir = require_awf_project(project_dir)

    # SPEC IMP2/IMP4 (K3/K4): the readiness gate and the norm transfer, both
    # BEFORE any side effect (no TODO, no baseline, no .ready). The gate is
    # the single shared predicate — the start/continue points call the same
    # one. Without ``spec_package`` the legacy path is untouched.
    package = ""
    if _contract:
        from ..unit_contract import spec_package_gate

        gate_message = spec_package_gate(project_dir, _contract)
        if gate_message:
            raise AwfApiError(
                f"spec gate refused the dispatch: {gate_message}"
            )
        package = _contract.get("spec_package")
        if package:
            refs = list(_contract.get("source_refs") or [])
            # K4: with a package declared, the legacy ``source_ref`` parameter
            # is a package link too — resolved, checked and transferred by the
            # same rules (inside the package, unique heading, matrix coverage),
            # never as an arbitrary outside file. Without a package it keeps
            # the old INV2 behavior (the branch below).
            legacy_ref = str(source_ref).strip() if source_ref else ""
            if legacy_ref and legacy_ref not in refs:
                refs.append(legacy_ref)
            content = _insert_after_title(
                content,
                _spec_package_transfer(
                    project_dir,
                    str(package),
                    refs,
                ),
            )

    # TODO-0229 (M5): the source of truth for this unit. Read and validate
    # BEFORE any side effect: a missing file or section refuses here, so no
    # TODO / .ready / baseline appears. The section lands right AFTER the
    # title heading (the REPORTS26 F5 guards resolve the same line).
    if source_ref is not None and str(source_ref).strip() and not package:
        _src_path, _src_section = _resolve_source_file(project_dir, str(source_ref))
        _body = _extract_source_section(_src_path, _src_section, str(source_ref))
        content = _insert_after_title(
            content, _source_ref_block(_src_section, _body)
        )

    # TODO-0244: the Definition-of-done block. Composed ONCE, here, from the
    # base set + the classes the unit DECLARES + its wave + its acceptance
    # items; frozen into the TODO so a wave edited mid-unit cannot change a
    # live unit. Boundary hints are printed, never merged. An unknown class,
    # a missing wave, a duplicate id or a bad probe refuses the dispatch
    # BEFORE any side effect.
    checklist_suggested: list[str] = []
    from ..checklists import (
        ChecklistError,
        block_has_items,
        block_of,
        compose_block,
        validate_carried_block,
        validate_readable_block,
    )

    if not block_has_items(content):
        # A heading-only block is not a frozen block (Q2/Q3 on the WRITER
        # surface): composing would append a second heading and leave the
        # empty one authoritative, so the unit would be born failing. Refuse
        # before any side effect instead.
        if block_of(content) is not None:
            raise AwfApiError(
                "checklist: the carried '## Чек-лист завершения' block carries "
                "no items — a heading is not a block; remove it or let "
                "dispatch compose the real block"
            )
        try:
            _composition, block = compose_block(project_dir, content, _contract)
        except ChecklistError as e:
            raise AwfApiError(f"checklist: {e}") from None
        checklist_suggested = _composition.suggested
        content = append_block(content, block)
    else:
        # A carried TODO (a replan copy) brings its own frozen block — it is
        # NOT recomposed, but the contract's declaration is still validated:
        # an unknown class, a missing wave file or a declared class whose
        # items the block lacks refuses the dispatch (never a silent drop).
        try:
            validate_carried_block(project_dir, content, _contract)
        except ChecklistError as e:
            raise AwfApiError(f"checklist: {e}") from None

    # F14/F15 (TODO-0244): the block is only good if the FINAL text reads it
    # back — a block appended after an unterminated code fence is invisible
    # to the gate, and two level-2 blocks let the gate execute the first
    # while a human reads two. Refuse BEFORE any side effect.
    try:
        validate_readable_block(content)
    except ChecklistError as e:
        raise AwfApiError(f"checklist: {e}") from None

    # R2.2 (roadmap R2): the explicit dirty-source-tree policy. Resolved from
    # config.yaml; a legacy project (no config_version) keeps the pre-R2.2
    # 'warn' behaviour and announces the migration. Read BEFORE any side
    # effect: require_clean refuses a dirty tree before the id is reserved —
    # nothing is written (no TODO, no baseline).
    from .. import config as _cfg_mod

    policy_decision = resolve_dirty_policy(_cfg_mod.load(project_dir))
    source_report = check_source_tree(project_dir)

    # R2.2 claim_changes: the applied CODE transfer this unit owns. Validated
    # before any side effect — a missing record, or a claimed change whose blob
    # moved since the plan (F3: consent is bound to the EXACT plan), refuses.
    # The validation lives in ``claim_changes`` (its own channel), the parallel
    # of ``resolve_carry_over`` / ``resolve_include_untracked`` below.
    claimed: list[str] = []
    if claim_from is not None and str(claim_from).strip():
        from ..claim_changes import resolve_claim_changes

        claimed = resolve_claim_changes(project_dir, str(claim_from))

    # RUN5 #1 (leak-gate): validate the carry-over BEFORE the policy gate and
    # the id reservation — a refused carry-over must leave no side effects, and
    # its paths are the FINDING channel's adopted dirt (F2).
    carry_over: set[str] = set()
    carry_over_files: list[str] = []
    if carry_over_from:
        from ..reject_files import resolve_carry_over

        carry_over_files = resolve_carry_over(project_dir, carry_over_from)
        carry_over = set(carry_over_files)

    # RUN10 #4 (TODO-0074): validate include_untracked BEFORE the policy gate
    # and the id reservation — a refused path must leave no side effects, and
    # its paths are the FINDING channel's adopted dirt (F1): a tree whose ONLY
    # dirt is an explicitly adopted untracked file must not be refused.
    include_files: list[str] = []
    if include_untracked is not None:
        from ..include_untracked import resolve_include_untracked

        include_files = resolve_include_untracked(project_dir, include_untracked)

    # R2.2: the adopted (owned) paths — the CODE transfer (claim_from) plus the
    # R5 FINDING transfer (carry_over_from / include_untracked). They are the
    # unit's own work, not un-owned dirt: excluded from the require_clean
    # refusal, from the dirty warning below AND from the isolate table (the
    # three surfaces describe one thing and must agree).
    adopted: set[str] = set(claimed) | carry_over | set(include_files)

    # The UN-OWNED part of the source tree — dirt no transfer channel adopted.
    # The SAME report feeds the require_clean refusal and the isolate table:
    # an adopted path is the unit's work, so it is neither refused nor listed
    # as "stay OUTSIDE the unit".
    unowned_report = SourceTreeReport(
        paths=tuple(p for p in source_report.paths if p not in adopted),
        managed=source_report.managed,
        error=source_report.error,
    )

    if policy_decision.policy == REQUIRE_CLEAN and not unowned_report.clean:
        raise AwfApiError(format_dirty_refusal(unowned_report))

    # TODO-0213 (BD-30): the marker must reference a real, live unit — the
    # escalated (blocked) TODO the replan replaces. A unit that does not
    # exist, is archived, or is closed as done could never be picked up
    # (there is no escalation waiting for it) — refuse BEFORE any side
    # effect (no TODO, no baseline).
    if replan_for is not None and str(replan_for).strip():
        target = str(replan_for).strip()
        if (paths.done_dir(project_dir) / target).is_dir():
            raise AwfApiError(
                f"replan_for {target}: the unit is already archived in "
                f"done/{target}/ — a replan replaces a unit that is still "
                "waiting (blocked), not a finished one"
            )
        if not (paths.inbox(project_dir) / f"{target}.md").is_file():
            raise AwfApiError(
                f"replan_for {target}: no such TODO — the marker must "
                "reference the unit being replaced (its inbox TODO-NNNN.md)"
            )
        from .hygiene import has_done_closure

        if has_done_closure(project_dir, target):
            raise AwfApiError(
                f"replan_for {target}: the unit is closed as done (DONE "
                "closure in outbox) — a replan replaces a blocked unit, "
                "not a finished one"
            )

    # TODO-0186: verify-path validation at dispatch. A verify: command may
    # reference a file that does not exist (a typo, a renamed test) — that
    # would surface later as a red contract gate. Extract the path tokens
    # now and collect the missing ones as a WARNING (never a refusal: the
    # heuristic has no right to block a dispatch).
    verify_path_warnings: list[str] = []
    if _contract is not None:
        verify_cmds = _contract.get("verify")
        if isinstance(verify_cmds, list):
            verify_path_warnings = _missing_verify_paths(
                project_dir, _verify_path_tokens(verify_cmds)
            )

    # RUN5 #1 (leak-gate) / RUN10 #4 (TODO-0074): the carry-over and the
    # include_untracked lists were validated ABOVE (before the policy gate),
    # since their paths are the adopted dirt the gate must not refuse. They are
    # re-used here to build the baseline exclusions.

    # Whether the CALLER fixed the id (explicit collisions must raise,
    # auto-picked ones may retry with the next free id).
    explicit_id = todo_id is not None
    if todo_id is None:
        todo_id = _next_todo_id(project_dir)
    elif not is_valid_todo_id(todo_id):
        raise AwfApiError(
            f"invalid todo_id '{todo_id}' — expected format 'TODO-NNNN' (4+ digits)"
        )

    # REPORTS26 F5: an explicit id + a foreign number in the title heading
    # would land a unit titled after someone else's number. Refuse
    # BEFORE any side effect (no reservation, no baseline); allow_mismatch
    # is the conscious opt-out (content written as-is).
    heading_num = _heading_todo_number(content)
    if (
        explicit_id
        and not allow_mismatch
        and heading_num is not None
        and heading_num != int(todo_id.split("-", 1)[1])
    ):
        raise AwfApiError(
            f"the content heading says TODO-{heading_num:04d} but "
            f"todo_id is {todo_id} — the unit would land with a foreign "
            "number in its title. Fix the heading to match, or pass "
            "allow_mismatch=True to write the content as-is."
        )

    # A-12 (audit 2026-09-25): an archived id is occupied — a re-dispatch
    # of done/<id>/ would let the next archive overwrite the history.
    # Refuse BEFORE the O_EXCL reservation: no file created, no baseline.
    # Auto numbering already skips done/<id>/ (see _next_todo_id), so this
    # check bites on explicit todo_id.
    if (paths.done_dir(project_dir) / todo_id).is_dir():
        raise AwfApiError(
            f"{todo_id} is already archived in done/{todo_id}/ — the id is "
            "occupied. Restore it with awf restore or use a different todo_id."
        )

    inbox = paths.inbox(project_dir)
    # R1.5 (добор 0215): the inbox lives under ``.agentic`` — a symlinked
    # ``.agentic``/``inbox`` used to send the unit file and its ``.ready``
    # signal OUTSIDE the project (S9-01 class). Refuse before the first side
    # effect; the writes below go through the no-follow managed helpers too.
    #
    # REVIEW G1: the barrier covers every managed surface the dispatch can
    # reach — its own writes (inbox md/.ready, context baselines) and the
    # helpers it calls (clear_stale_closures reads inbox/outbox and writes
    # ``context/unblock-*``) — plus ``done``, the archive surface of the
    # unit being created. It runs BEFORE the id reservation: with a
    # symlinked ``done`` the old order wrote the md and five baseline files
    # and only then refused in ``clear_stale_closures``, leaving a
    # half-dispatch (md+baselines, no ``.ready``). Now nothing is written.
    refuse_unsafe_surfaces(
        project_dir,
        f"dispatch {todo_id}",
        inbox,
        paths.outbox(project_dir),
        paths.context_dir(project_dir),
        paths.done_dir(project_dir),
    )
    # F-0219-2: no ``inbox.mkdir`` here. A plain ``Path.mkdir(parents=True)``
    # after the preflight re-follows ``.agentic``: a swap to an outward
    # symlink between the check and the mkdir created an empty ``inbox``
    # OUTSIDE the project (F-B class, same as F-0219-1 for logs). The chain
    # is created safely below by ``open_managed(create=True)`` — ``mkdirat``
    # on the already-verified parent fd, which refuses a swapped component
    # (ELOOP → ManagedPathError) before any directory appears.

    # AUD14-01: reserve the ID atomically before writing content. The old
    # exists() → write() window let two parallel dispatches pick the same
    # TODO-NNNN and the second silently overwrote the first's content.
    # O_CREAT|O_EXCL makes the claim one syscall: the first writer wins,
    # concurrent callers get FileExistsError and retry with the next id.
    def _composed_body(for_id: str) -> tuple[str, bool]:
        """The exact text written for *for_id* (+ whether the heading moved)."""
        text = content
        moved = False
        if (
            not explicit_id
            and heading_num is not None
            and heading_num != int(for_id.split("-", 1)[1])
        ):
            text = _renumber_heading(text, for_id)
            moved = True
        if role:
            text = f"<!-- role_hint: {role} -->\n" + text
        return text, moved

    # TODO-0261 (RB-4): an explicit --force re-dispatch overwrites an existing
    # unit. Never under a live pipeline — the engine reads that md; and the
    # old ``.ready`` is dropped so the engine cannot pick up a half-baselined
    # unit while the snapshot is being rebuilt. Liveness comes from the SHARED
    # resolver (both pid sources) — a single-file read is exactly how the two
    # liveness codes diverged before it was unified (AUD04-07).
    force_existing = force and explicit_id and (inbox / f"{todo_id}.md").is_file()
    if force_existing:
        running, _pid, _source = _liveness.resolve(project_dir)
        if running:
            raise AwfApiError(
                f"{todo_id} cannot be force-re-dispatched while a pipeline is "
                "live — stop it first (awf kill), then retry."
            )
        stale_ready = inbox / f"{todo_id}.ready"
        # PA4 (TODO-0269): a re-issued unit is a DIFFERENT unit. The closer's
        # first verdict and attempt counter belong to the old one — keeping
        # the file would report the old wave/attempts and (IMP4) refuse the
        # retry for a budget it never spent. Nothing re-writes this file
        # (unlike the baseline snapshot, which the dispatch re-creates), so
        # the stale record is removed here. The leaf joins the barrier next
        # to ``stale_ready``: the branch's no-follow preflight covers
        # exactly the files it unlinks (the ``context`` directory itself is
        # already in the dispatch-wide preflight above).
        closer_state = closer_stage.state_path(project_dir, todo_id)
        refuse_unsafe_surfaces(
            project_dir, f"dispatch {todo_id}", inbox, stale_ready, closer_state
        )
        stale_ready.unlink(missing_ok=True)
        closer_state.unlink(missing_ok=True)

    # A plain repeat of an explicit id is refused BEFORE the write-ahead
    # journal exists: writing ours first would overwrite the journal of a
    # still-pending dispatch of that id, and a later ``--resume`` would finish
    # it with the wrong body. Auto ids never reach this check — the loop below
    # skips taken numbers.
    if explicit_id and not force and (inbox / f"{todo_id}.md").is_file():
        raise AwfApiError(
            f"{todo_id}.md already exists in inbox. Use a different todo_id or "
            "remove the existing file first (or pass force=True to re-dispatch "
            "it)."
        )

    md_path: Path | None = None
    created_md = True
    # ``resume_dispatch`` rebuilds the unit from the journal, which carries only
    # the TODO body. A dispatch with leak-gate / include / claim exclusions
    # would lose them (baseline rebuilt WITHOUT the exclusions, audit links
    # skipped) — mark it for a manual ``force`` re-dispatch instead of a silent
    # wrong resume.
    resume_needs_manual = bool(carry_over or include_files or claimed)
    if force_existing:
        md_path = inbox / f"{todo_id}.md"
        created_md = False
        tmp_journal = _write_journal(
            project_dir,
            todo_id,
            _composed_body(todo_id)[0],
            needs_manual_resume=resume_needs_manual,
        )
        _publish_journal(project_dir, todo_id, tmp_journal)
    else:
        for _attempt in range(16):
            candidate = inbox / f"{todo_id}.md"
            # TODO-0261 (RB-4): the write-ahead journal lands BEFORE the
            # reservation. A kill between the two leaves only a journal (the
            # resume recreates the md from it); a kill after it leaves a
            # journal + md — never a TODO without a way to finish it.
            #
            # The write is ATTEMPT-UNIQUE and is promoted to the canonical
            # ``DISPATCH-<id>.json`` only after this attempt wins the id: two
            # parallel auto dispatches used to write the same canonical path
            # before either reserved, so the loser's body replaced the
            # winner's and a killed winner resumed with a foreign body.
            tmp_journal = _write_journal(
                project_dir,
                todo_id,
                _composed_body(todo_id)[0],
                needs_manual_resume=resume_needs_manual,
            )
            try:
                # R1.5: no-follow all along — a swapped intermediate component
                # fails closed (ManagedPathError), never a write outside.
                fd = managed_fs.open_managed(
                    candidate,
                    flags=os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    mode=MODE_OWNER_EDITABLE,
                    root=project_dir,
                )
            except FileExistsError:
                # Another caller won this id. Drop only OUR temp journal — the
                # winner's canonical file is not ours to touch.
                _discard_journal_tmp(tmp_journal)
                if explicit_id:
                    raise AwfApiError(
                        f"{candidate.name} already exists in inbox. Use a "
                        "different todo_id or remove the existing file first "
                        "(or pass force=True to re-dispatch it)."
                    ) from None
                todo_id = _next_todo_id(project_dir)
                # desynchronize the herd of parallel callers
                import random as _random
                import time as _time
                _time.sleep(_random.uniform(0, 0.02))
                continue
            except managed_fs.ManagedPathError as e:
                _discard_journal_tmp(tmp_journal)
                raise AwfApiError(f"dispatch refused — unsafe inbox path: {e}") from e
            except BaseException:
                # The temp journal stays: the resume can find and finish this id.
                raise
            os.close(fd)
            md_path = candidate
            _publish_journal(project_dir, todo_id, tmp_journal)
            break
        if md_path is None:
            # Every attempt lost its id and dropped its own temp journal —
            # nothing canonical was ever written, so there is nothing to clean.
            raise AwfApiError(
                f"could not reserve a free TODO id (16 consecutive collisions, "
                f"last tried {todo_id}) — check .agentic/inbox for stray TODO files"
            )

    # REPORTS26 F5: auto number + a foreign number in the title heading
    # (a copied template kept its old title) — renumber the
    # heading to the ISSUED id (the reservation loop may have moved past
    # the first candidate on collisions, hence here, not earlier). The
    # rest of the content stays untouched; an explicit id never reaches
    # here (mismatch refused above, matching number is a no-op).
    body, renumbered = _composed_body(todo_id)

    # RUN3 #4: never (re)issue a number whose DONE closure is still in the
    # outbox — the fresh TODO would be invisible (todos.is_closed). The
    # archived TODO comes back via awf restore, not a silent re-dispatch.
    from .hygiene import clear_stale_closures, has_done_closure

    if has_done_closure(project_dir, todo_id):
        if created_md:
            md_path.unlink(missing_ok=True)
        _remove_journal(project_dir, todo_id)
        raise AwfApiError(
            f"outbox has a DONE closure for {todo_id} — the TODO is closed as "
            "finished. Bring it back with awf restore or dispatch a new number."
        )

    # Pre-dispatch check: grep code for key identifiers from TODO content.
    # Warns if patterns already exist in codebase (task may be already done).
    pre_check_warnings: list[str] = []
    try:
        import re as _re
        # Extract identifiers: backtick-quoted, camelCase, snake_case
        identifiers = set()
        for m in _re.finditer(r'`([a-zA-Z_][a-zA-Z0-9_]{2,})`', content):
            identifiers.add(m.group(1))
        for m in _re.finditer(r'\b([a-z][a-zA-Z0-9]*_[a-z][a-zA-Z0-9_]*)\b', content):
            identifiers.add(m.group(1))
        identifiers.discard("todo")  # too generic

        if identifiers:
            import subprocess as _sp
            for ident in list(identifiers)[:5]:  # check max 5
                try:
                    result = _sp.run(
                        ["grep", "-rl", "--include=*.ts", "--include=*.js",
                         "--include=*.py", "--include=*.go", "--include=*.rs",
                         "--include=*.java", "--include=*.rb",
                         "-d", "skip",
                         # AUD15-05: don't scan VCS/dependency dirs — on a
                         # 25k-file tree the old unfiltered grep took up to
                         # 5s per identifier (5 identifiers → ~25s dispatch).
                         "--exclude-dir=.git", "--exclude-dir=node_modules",
                         "--exclude-dir=.venv", "--exclude-dir=venv",
                         "--exclude-dir=vendor",
                         ident, str(project_dir)],
                        capture_output=True, text=True, timeout=2, check=False,
                    )
                    # Filter out .agentic/ matches
                    matches = [line for line in result.stdout.strip().splitlines()
                               if ".agentic/" not in line and "node_modules/" not in line]
                    if matches:
                        pre_check_warnings.append(
                            f"'{ident}' already found in {len(matches)} file(s). "
                            f"Task may be already implemented — verify before running pipeline."
                        )
                except (OSError, _sp.SubprocessError):
                    pass
    except Exception:
        pass  # pre-check is best-effort, never blocks dispatch

    # U3: unknown keys in the contract block are a typo risk — warn, don't fail.
    from ..unit_contract import _CONTRACT_KEYS

    for key in unknown_keys:
        pre_check_warnings.append(
            f"contract block: unknown key '{key}' — known keys: "
            + ", ".join(_CONTRACT_KEYS)
        )

    # TODO-0244: boundary hints are printed as `checklist_suggested:` and are
    # NEVER merged on their own — the unit declares its classes explicitly.
    if checklist_suggested:
        pre_check_warnings.append(
            "checklist_suggested: "
            + ", ".join(checklist_suggested)
            + " — declare in checklist_classes to merge (hints are not merged)"
        )

    # TODO-0186: surface the missing verify paths as one warning line (the
    # structured field is verify_path_warnings on the result). The dispatch
    # still succeeds — a false positive would be worse than a missed typo.
    if verify_path_warnings:
        pre_check_warnings.append(
            "verify: command(s) reference path(s) not found in the project — "
            + ", ".join(verify_path_warnings)
            + " (warning only; the contract gate may go red)"
        )

    # Step 1: fill the reserved TODO-NNNN.md (atomic temp+rename over the
    # placeholder — the id is already claimed, no one else can take it).
    # R1.5: no-follow write (same class as the reservation open above).
    managed_fs.managed_write_text(md_path, body, root=project_dir, mode=MODE_OWNER_EDITABLE)

    # Step 2: create baseline snapshot (sha + tests.log + env.log + status).
    # RUN10 #4: include files are excluded from the untracked snapshot by a
    # SEPARATE parameter — the leak-gate carry-over stays its own mechanism.
    baseline_timeout = _baseline_timeout_seconds(project_dir)
    try:
        baseline = _dispatch_baseline(
            project_dir,
            todo_id,
            baseline_timeout=baseline_timeout,
            no_baseline_tests=no_baseline_tests,
            carry_over=carry_over,
            include=set(include_files) or None,
            claim=set(claimed) or None,
        )
    except Exception:
        # Rollback: remove TODO .md if baseline fails (prevents orphan TODO).
        # A force re-dispatch keeps the md (it pre-dated this call) and its
        # journal, so ``resume_dispatch`` can finish that unit.
        if created_md:
            md_path.unlink(missing_ok=True)
            _remove_journal(project_dir, todo_id)
        raise

    # RUN5 #1 (leak-gate): audit trail — which rejected origin this retry's
    # baseline carried over. Best-effort: a failed write must not undo the
    # dispatch (the baseline already excluded the paths). The link lifecycle is
    # owned by ``reject_files`` (the leak-gate channel), the same as the
    # include/claim links, instead of being re-spelled inline here.
    if carry_over:
        from ..reject_files import write_carry_over_link

        write_carry_over_link(project_dir, todo_id, str(carry_over_from))
        from .._log import log as _log

        _log(
            paths.logs_dir(project_dir),
            f"dispatch: {todo_id} — carried over {len(carry_over_files)} "
            f"file(s) from rejected {carry_over_from} (leak-gate)",
        )
    else:
        # RUN5 #1 (leak-gate): a non-carry-over (re-)dispatch rewrites the
        # baseline WITHOUT the exclusion — drop any stale link from an earlier
        # carry-over dispatch of the same id, so a later re-baseline (awf
        # run_next) does not re-apply an exclusion that no longer holds.
        from ..reject_files import clear_carry_over_link

        clear_carry_over_link(project_dir, todo_id)

    # RUN10 #4 (TODO-0074): audit trail — which pre-existing untracked files
    # this unit re-claims; a (re-)dispatch without include_untracked drops
    # the stale link (same lifecycle as the carry-over link above).
    from ..include_untracked import clear_include_audit, write_include_audit

    if include_files:
        write_include_audit(project_dir, todo_id, include_files)
    else:
        clear_include_audit(project_dir, todo_id)

    # R2.2 (TODO-0221 M2): CODE-channel audit link — the claim parallel to the
    # carry_over/include links above. Without it a later re-baseline
    # (awf run_next) drops the claimed paths' exclusion and the commit gate
    # treats a claimed predecessor blob as pre-existing (silently out of the
    # unit commit). A (re-)dispatch without claim_from drops a stale link.
    from ..claim_changes import clear_claim_link, write_claim_link

    if claimed and claim_from is not None and str(claim_from).strip():
        write_claim_link(project_dir, todo_id, str(claim_from).strip())
    else:
        clear_claim_link(project_dir, todo_id)

    # RUN3 #4: re-dispatch of the same number must not stay hidden behind
    # stale BLOCKED/ACK closures — clear them via the shared helper.
    # (Only reachable when inbox had no .md for this id, i.e. a re-issue;
    # a running pipeline keeps its own .md in the inbox, so its signals
    # are never moved out from under the engine.)
    cleared, _trace = clear_stale_closures(project_dir, todo_id)
    if cleared:
        from .._log import log as _log

        _log(
            paths.logs_dir(project_dir),
            f"dispatch: {todo_id} — stale closure cleared on re-dispatch: "
            f"{', '.join(cleared)}",
        )

    # Step 3: dispatch signal
    # R1.5: the `.ready` touch is a write under `.agentic` too — no-follow.
    _write_ready(project_dir, todo_id)
    # TODO-0261 (RB-4): the unit is complete (md + baseline + .ready) — the
    # journal has done its job. Leftover journal = an unfinished dispatch,
    # which ``resume_dispatch`` owns.
    _remove_journal(project_dir, todo_id)

    # RUN10 #4 (TODO-0074): visibility — the files the commit gate will
    # exclude (still untracked, recorded in the fresh baseline snapshot,
    # NOT re-claimed via carry_over/include_untracked).
    from ..include_untracked import format_untracked_warning, pre_existing_untracked

    pre_existing = pre_existing_untracked(project_dir, todo_id)
    untracked_warning = format_untracked_warning(todo_id, pre_existing)

    # TODO-0195 (рана C) / TODO-0204 (REPORTS33): a dirty tree at dispatch
    # (tracked-dirty / staged / untracked, gitignored excluded) is LOUD, not
    # a refusal. Same moment as the baseline's own .status snapshot, so the
    # warning describes the tree the baseline actually saw — and it must
    # promise what the commit gate WILL do: with the dispatch-time worktree
    # snapshot the pre-dispatch change of a tracked-dirty file stays OUTSIDE
    # the unit commit (only the unit's own edit lands); without it the
    # warning says the change will enter the commit as-is.
    # One R2.1 model read serves both the warning's file list and its class
    # map (they describe the same tree): ``dirty_files`` is the keys of
    # ``dirty_classes`` in the model's first-seen order.
    dirty_classes = {
        p: c for p, c in _dirty_tree_classes(project_dir).items() if p not in adopted
    }
    dirty_files = list(dirty_classes)
    snapshot_available = (
        paths.context_dir(project_dir) / f"BASELINE-{todo_id}.worktree"
    ).is_file()
    snapshot_reason = ""
    if not snapshot_available and dirty_classes:
        snapshot_reason = (
            "not a git repo"
            if not baseline.is_git_repo
            else "the worktree snapshot was not taken"
        )
    dirty_warning = _format_dirty_warning(
        todo_id, dirty_files, dirty_classes, snapshot_available, snapshot_reason
    )

    # R2.2 (roadmap R2): the isolate artefacts — what is excluded and what may
    # depend on it. Only the explicit isolate policy reports them (legacy
    # 'warn' keeps the plain dirty warning above).
    isolate_warning = ""
    isolate_dependency_warning = ""
    if policy_decision.policy == ISOLATE:
        isolate_warning, isolate_dependency_warning = format_isolate_report(
            todo_id, unowned_report
        )
    log_policy_decision(project_dir, todo_id, policy_decision)

    # F3: the path lists in the RESULT are a display surface too — the MCP
    # answer serializes them (json.dumps ... ensure_ascii=False). Escape each
    # name for the UI so a non-UTF-8 byte cannot break that sink. The internal
    # lists (adopted-filtered ``dirty_files``, ``claimed``) stay on the raw
    # display form the ownership decisions above used.
    from ..infra.git_model import escape_str_for_ui

    return DispatchTodoResult(
        todo_id=todo_id,
        baseline_sha=baseline.sha,
        resolved_project_dir=str(project_dir),
        role_hint=role,
        files_written=[
            f".agentic/inbox/{todo_id}.md",
            f".agentic/inbox/{todo_id}.ready",
            *baseline.files_created,
        ],
        pre_check_warnings=pre_check_warnings,
        size_warning=unit_size_warning(content),
        carry_over_from=carry_over_from,
        carry_over_files=[escape_str_for_ui(p) for p in carry_over_files],
        pre_existing_untracked=[escape_str_for_ui(p) for p in pre_existing],
        untracked_warning=untracked_warning,
        renumbered=renumbered,
        verify_path_warnings=verify_path_warnings,
        dirty_files=[escape_str_for_ui(p) for p in dirty_files],
        dirty_warning=dirty_warning,
        dirty_policy=policy_decision.policy,
        dirty_policy_warning=policy_decision.warning,
        isolate_warning=isolate_warning,
        isolate_dependency_warning=isolate_dependency_warning,
        claim_from=(str(claim_from).strip() if claim_from else None),
        claimed_files=[escape_str_for_ui(p) for p in claimed],
        baseline_test_status=baseline.test_status,
        baseline_timeout=baseline_timeout,
    )


def resume_dispatch(
    project_dir: Path,
    todo_id: str,
    *,
    no_baseline_tests: bool = False,
) -> DispatchTodoResult:
    """Finish a dispatch that was killed mid-baseline (TODO-0261, RB-4).

    Reads the write-ahead journal ``context/DISPATCH-<id>.json``, restores a
    missing/empty ``inbox/<id>.md`` from it, re-runs the smoke baseline under
    the same rules as :func:`dispatch_todo` (dispatch timeout, optional skip)
    and writes ``.ready``. Idempotent: a unit whose ``.ready`` already exists
    only drops the stale journal. Refuses when there is no journal at all
    (nothing to resume) or the id is archived / closed as done.
    """
    if not todo_id or not is_valid_todo_id(todo_id):
        raise AwfApiError(
            f"invalid todo_id '{todo_id}' — expected format 'TODO-NNNN' (4+ digits)"
        )
    project_dir = require_awf_project(project_dir)
    inbox = paths.inbox(project_dir)
    ready_path = inbox / f"{todo_id}.ready"
    md_path = inbox / f"{todo_id}.md"
    refuse_unsafe_surfaces(
        project_dir,
        f"resume dispatch {todo_id}",
        inbox,
        paths.context_dir(project_dir),
        paths.done_dir(project_dir),
    )
    journal = _read_journal(project_dir, todo_id)
    if journal is None:
        raise AwfApiError(
            f"{todo_id}: no pending dispatch journal "
            f"({_journal_path(project_dir, todo_id)}) — nothing to resume"
        )
    if (paths.done_dir(project_dir) / todo_id).is_dir():
        _remove_journal(project_dir, todo_id)
        raise AwfApiError(
            f"{todo_id} is already archived in done/{todo_id}/ — nothing to resume"
        )
    from .hygiene import has_done_closure

    if has_done_closure(project_dir, todo_id):
        _remove_journal(project_dir, todo_id)
        raise AwfApiError(
            f"outbox has a DONE closure for {todo_id} — nothing to resume"
        )

    if ready_path.is_file():
        # Crashed AFTER the signal: the unit is complete, the journal is stale.
        _remove_journal(project_dir, todo_id)
        sha = ""
        sha_path = paths.context_dir(project_dir) / f"BASELINE-{todo_id}.sha"
        try:
            sha = sha_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        return DispatchTodoResult(
            todo_id=todo_id,
            baseline_sha=sha,
            role_hint=None,
            # Nothing is written in this branch: the unit was complete
            # before the journal went stale, so the answer must not claim
            # the .md/.ready were created (AUD-0283-3).
            files_written=[],
            resolved_project_dir=str(project_dir),
            resumed=True,
        )

    if journal.get("needs_manual_resume"):
        raise AwfApiError(
            f"{todo_id}: the killed dispatch carried carry-over / include / "
            "claim exclusions that this resume cannot replay — re-dispatch "
            "with force=True instead of resuming."
        )

    body = str(journal.get("content") or "")
    if not body.strip():
        raise AwfApiError(
            f"{todo_id}: the pending dispatch journal carries no content — "
            "re-dispatch the unit instead"
        )
    try:
        existing = md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        existing = ""
    written: list[str] = []
    if not existing.strip():
        managed_fs.managed_write_text(
            md_path, body, root=project_dir, mode=MODE_OWNER_EDITABLE
        )
        written.append(f".agentic/inbox/{todo_id}.md")

    baseline_timeout = _baseline_timeout_seconds(project_dir)
    baseline = _dispatch_baseline(
        project_dir,
        todo_id,
        baseline_timeout=baseline_timeout,
        no_baseline_tests=no_baseline_tests,
    )
    _write_ready(project_dir, todo_id)
    written.append(f".agentic/inbox/{todo_id}.ready")
    written.extend(baseline.files_created)
    _remove_journal(project_dir, todo_id)
    return DispatchTodoResult(
        todo_id=todo_id,
        baseline_sha=baseline.sha,
        role_hint=None,
        files_written=written,
        resolved_project_dir=str(project_dir),
        baseline_test_status=baseline.test_status,
        baseline_timeout=baseline_timeout,
        resumed=True,
    )


__all__ = ["dispatch_todo", "resume_dispatch", "_next_todo_id"]
