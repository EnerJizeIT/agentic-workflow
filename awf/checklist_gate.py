"""The definition-of-done gate: execute the TODO checklist's probes.

One module, two callers (TODO-0244): ``verify_pack`` runs it as a section,
the close stage (TODO-0245) runs it before closing a unit. The result is
pinned to a tree fingerprint: the same tree and the same block reuse the
recorded answer; a changed tree re-runs every probe.

DONE ``facts`` are never trusted as the answer. A claim that disagrees
with the actual probe run is a warning on the result — the discrepancy is
visible, not silently accepted.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import git_utils, paths, todos
from .checklists import (
    MAX_PROBE_TIMEOUT,
    PROPOSALS_NAME,
    ChecklistError,
    Item,
    argv_positionals,
    argv_refusal,
    block_hash,
    item_ids,
    parse_block,
    section_lines,
)
from .infra import managed_fs
from .signals import find_signal_file
from .unit_contract import parse_done_json

MAX_OUTPUT_TAIL = 2048


class ChecklistGateError(ValueError):
    """A gate-level refusal (bad project/todo, unreadable block)."""


# ─── probes ─────────────────────────────────────────────────────────────


def _inside(project: Path, rel: str) -> Path:
    """Resolve a probe path inside the project; refuse an escape."""
    try:
        candidate = (project / rel).resolve()
    except (RuntimeError, OSError, ValueError) as e:
        # ``resolve`` raises RuntimeError on a symlink loop, OSError on an
        # unreadable path, and ValueError on a NUL byte; all are about ONE
        # probe target, not the whole gate (callers catch ChecklistError only).
        raise ChecklistError(f"probe path {rel!r} cannot be resolved: {e}") from None
    try:
        candidate.relative_to(project)
    except ValueError:
        raise ChecklistError(
            f"probe path {rel!r} escapes the project — probes read only "
            "inside the project tree"
        ) from None
    return candidate


def _tail(text: str, limit: int = MAX_OUTPUT_TAIL) -> str:
    return text[-limit:] if len(text) > limit else text


#: Ambient variables a probe must never inherit. Three families, one rule:
#: a read-only allowlisted probe must not acquire an exec hook or a
#: different input tree from the environment around it (TODO-0244 F21/F22).
#:
#: - ``AWF_*`` — awf secrets.
#: - git: ``GIT_CONFIG*``/``GIT_EXTERNAL_DIFF``/``GIT_PAGER`` turn a plain
#:   ``git diff``/``show`` into an external program or a pager; ``GIT_DIR``,
#:   ``GIT_WORK_TREE`` and ``GIT_INDEX_FILE`` silently point the probe at a
#:   different repository than the project it was dispatched for.
#: - python: ``PYTEST_*`` (``PYTEST_PLUGINS`` is exactly the ``-p <module>``
#:   the allowlist refuses, ``PYTEST_ADDOPTS`` appends arbitrary flags);
#:   ``PYTHONPATH``/``PYTHONHOME``/``PYTHONSTARTUP``/``PYTHONINSPECT``
#:   replace or execute interpreter modules.
#: - ``RIPGREP_CONFIG_PATH`` gives ``rg`` a config that can set ``--pre``.
_BLOCKED_ENV_PREFIXES = ("AWF_", "GIT_CONFIG", "PYTEST_")
_BLOCKED_ENV_EXACT = frozenset(
    {
        "GIT_DIR",
        "GIT_EXTERNAL_DIFF",
        "GIT_INDEX_FILE",
        "GIT_PAGER",
        "GIT_WORK_TREE",
        "PAGER",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "RIPGREP_CONFIG_PATH",
    }
)


def _sanitized_env() -> dict[str, str]:
    """The environment minus secrets and exec/redirect hooks.

    ``AWF_*`` is the secret channel; the git and python families let an
    allowlisted read-only probe run an arbitrary program or import a module
    the argv never named. The probe is read-only, so it never needs a pager
    or an ambient interpreter path either.
    """
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(_BLOCKED_ENV_PREFIXES) and k not in _BLOCKED_ENV_EXACT
    }


def _run(argv: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            timeout=timeout,
            env=_sanitized_env(),
            check=False,
        )
    except FileNotFoundError:
        return 127, f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s: {' '.join(argv)}"
    except OSError as e:
        return 126, f"{type(e).__name__}: {e}"
    except ValueError as e:
        # ``subprocess.run`` raises ValueError on a NUL byte in an argv token
        # — the same per-item verdict as an unusable binary (126), never a
        # traceback out of the gate.
        return 126, f"{type(e).__name__}: {e}"
    # Decode the bytes ourselves: a probe's output is untrusted and may carry
    # a non-UTF-8 byte (a tracked filename, a latin-1 locale). ``text=True``
    # decodes strictly and would raise ``UnicodeDecodeError`` out of the
    # gate, killing the whole verify-pack; ``backslashreplace`` keeps the
    # tail valid text for the record and the report.
    raw = (proc.stdout or b"") + (proc.stderr or b"")
    output = raw.decode("utf-8", errors="backslashreplace")
    return proc.returncode, _tail(output.strip())


def _json_field(data: object, dotted: str) -> tuple[bool, object]:
    node = data
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return False, None
    return True, node


def run_probe(
    project: Path, item: Item, *, todo_id: str = ""
) -> tuple[bool, str]:
    """Execute one item's probe. Returns ``(ok, detail)``."""
    assert item.probe is not None
    probe = item.probe
    kind = probe["type"]
    expect = item.expect

    def _arg(value: str) -> str:
        return value.replace("{todo_id}", todo_id)

    if kind in ("grep_present", "grep_absent"):
        try:
            target = _inside(project, _arg(probe["file"]))
        except ChecklistError as e:
            # One escaping item is a failed ITEM, not a dead run: the other
            # probes still execute and report.
            return False, str(e)
        argv = ["grep", "-q", "-e", probe["pattern"], "--", str(target)]
        try:
            is_dir = target.is_dir()
        except OSError as e:
            # F20: a closed parent directory makes ``is_dir`` raise EACCES.
            # That is this item's verdict, not a dead gate — the other
            # probes still run.
            return False, f"grep {probe['file']}: unreadable ({e})"
        if is_dir:
            argv = ["grep", "-rq", "-e", probe["pattern"], "--", str(target)]
        rc, out = _run(argv, project, MAX_PROBE_TIMEOUT)
        if rc > 1:
            # grep 2 = cannot read the target (missing file, no permission).
            # That is not "the pattern is absent": mapping it to a pass turns
            # deleting the file into a green check.
            detail = f"grep {probe['file']}: cannot read target (rc={rc})"
            if out:
                detail += f" ({out})"
            return False, detail
        found = rc == 0
        ok = found == bool(expect)
        detail = "" if ok else f"grep {probe['file']}: pattern {probe['pattern']!r} " + (
            "found" if found else "not found"
        )
        if not ok and out:
            detail += f" ({out})"
        return ok, detail

    if kind in ("file_exists", "file_absent"):
        try:
            target = _inside(project, _arg(probe["path"]))
        except ChecklistError as e:
            return False, str(e)
        try:
            exists = target.exists()
        except OSError as e:
            # F20: ``exists`` re-raises EACCES when a parent directory denies
            # traversal (Python swallows only ENOENT/ENOTDIR). The item is
            # unreadable — a verdict, not a crash.
            return False, f"{probe['path']}: unreadable ({e})"
        ok = exists == bool(expect)
        detail = "" if ok else f"{probe['path']}: " + (
            "exists" if exists else "missing"
        )
        return ok, detail

    if kind == "json_field":
        try:
            target = _inside(project, _arg(probe["file"]))
        except ChecklistError as e:
            return False, str(e)
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except OSError as e:
            return False, f"{probe['file']}: unreadable ({e})"
        except ValueError as e:
            return False, f"{probe['file']}: not JSON ({e})"
        found, value = _json_field(data, probe["path"])
        if not found:
            return False, f"{probe['file']}: no field {probe['path']!r}"
        ok = value == item.expect
        detail = "" if ok else (
            f"{probe['file']}:{probe['path']} is {value!r}, expected {item.expect!r}"
        )
        return ok, detail

    # command_rc
    argv = [_arg(str(a)) for a in probe["argv"]]
    # Defence in depth: the data loaders and the block parser already refuse
    # a bad argv, but the gate is the last step before exec — an argv that
    # fails the allowlist is never run (a write/exec flag cannot slip in
    # through a cache or a future caller).
    refusal = argv_refusal(argv)
    if refusal:
        return False, f"{' '.join(argv)}: refused — {refusal}"
    for token in argv_positionals(argv):
        try:
            _inside(project, token)
        except ChecklistError as e:
            return False, f"{' '.join(argv)}: refused — {e}"
    timeout = int(probe.get("timeout") or MAX_PROBE_TIMEOUT)
    # F21: the allowlist refuses ``--ext-diff``, but the ambient environment
    # can still turn a diff into an external program. The env is stripped;
    # ``--no-ext-diff`` is belt and braces for a repo-level config we do not
    # control. Added AFTER validation, so the closed flag list is untouched.
    run_argv = list(argv)
    if (
        len(run_argv) >= 2
        and run_argv[0] == "git"
        and run_argv[1] in ("diff", "show")
        and "--no-ext-diff" not in run_argv
    ):
        run_argv.insert(2, "--no-ext-diff")
    rc, out = _run(run_argv, project, timeout)
    ok = rc == int(expect)
    detail = "" if ok else f"{' '.join(argv)}: rc={rc}, expected {expect}"
    if not ok and out:
        detail += f" — {out}"
    return ok, detail


# ─── cache / run ────────────────────────────────────────────────────────


def _sha_file(path: Path) -> str:
    """Content hash, or an ``unreadable:<errno>`` marker.

    The cache key is a best-effort fingerprint, never a reason to kill the
    gate: an unreadable target (mode 000, a file gone between listing and
    open) is a state of the target. The marker changes the key on a mode
    change, and the verdict comes from the probe itself — not from here.
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as e:
        return f"unreadable:{e.errno if e.errno is not None else '?'}"
    return digest.hexdigest()


def _sha_tree(root: Path, skip: Path) -> str:
    """Content hash of a directory, ignoring ``.git`` and the cache itself."""
    rows: list[str] = []
    try:
        entries = sorted(root.rglob("*"))
    except OSError as e:
        return f"unreadable:{e.errno if e.errno is not None else '?'}"
    for p in entries:
        if p == skip:
            continue
        rel = p.relative_to(root)
        if ".git" in rel.parts:
            continue
        try:
            is_file = p.is_file()
        except OSError:
            is_file = False
        if is_file:
            rows.append(f"{rel.as_posix()}\t{_sha_file(p)}")
    return hashlib.sha256(
        "\n".join(rows).encode("utf-8", errors="surrogateescape")
    ).hexdigest()


def _target_state(project: Path, rel: str, *, content: bool, skip: Path) -> str:
    """What a probe reads at ``rel`` — the cache must see gitignored targets."""
    try:
        target = _inside(project, rel)
    except ChecklistError:
        return "escape"
    try:
        if not target.exists():
            return "absent"
        if target.is_file():
            return "file:" + (_sha_file(target) if content else "")
        if target.is_dir():
            return "dir:" + (_sha_tree(target, skip) if content else "")
    except OSError as e:
        # F20: a mode-000 PARENT directory makes ``exists``/``is_file``
        # raise EACCES here — the cache key must still be computable.
        return f"unreadable:{e.errno if e.errno is not None else '?'}"
    return "other"


def _probe_paths(item: Item) -> list[str]:
    probe = item.probe
    if not probe:
        return []
    kind = probe["type"]
    if kind in ("grep_present", "grep_absent", "json_field"):
        return [probe["file"]]
    if kind in ("file_exists", "file_absent"):
        return [probe["path"]]
    return list(argv_positionals(probe["argv"]))


def _probe_fingerprint(
    project: Path, items: list[Item], todo_id: str, skip: Path
) -> str:
    """Fingerprint of everything the probes READ.

    ``tree_fingerprint`` excludes ``.agentic/`` by construction (awf
    gitignores it), yet most base probes read those paths. The cache key
    must therefore carry the probe targets themselves: content for the
    readers (grep/json/command paths), existence for ``file_*`` — a
    deleted baseline file changes the key and forces a re-run.
    """
    rows: list[str] = []
    for item in items:
        if not item.probe:
            continue
        kind = item.probe["type"]
        content = kind not in ("file_exists", "file_absent")
        for rel in _probe_paths(item):
            rel = rel.replace("{todo_id}", todo_id)
            state = _target_state(project, rel, content=content, skip=skip)
            rows.append(f"{kind}:{rel}:{state}")
    return hashlib.sha256(
        "\n".join(rows).encode("utf-8", errors="surrogateescape")
    ).hexdigest()


def state_path(project: Path, todo_id: str) -> Path:
    """``.agentic/context/CHECKLIST-<todo>.json`` — the gate's verdict cache.

    Public: the unit-history cleanup (``awf.unit_history``) must find the
    same file the gate writes, or a re-issued unit id inherits a stale
    verdict.
    """
    return paths.context_dir(project) / f"CHECKLIST-{todo_id}.json"


def _read_cache(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _tree_sha(project: Path) -> str:
    try:
        return git_utils.tree_fingerprint(project)
    except (RuntimeError, OSError):
        return ""


def _done_claims(project: Path, todo_id: str) -> tuple[dict[str, bool], int]:
    """DONE.json checklist claims + the proposal count from DONE.md.

    Claims are evidence to CONTRAST, not truth: the caller turns a
    disagreement with the actual probe run into a warning.
    """
    claims: dict[str, bool] = {}
    outbox = paths.outbox(project)
    # ONE DONE.json reader for the handoff and the gate: ``find_signal_file``
    # also finds the legacy short name, ``parse_done_json`` refuses a file
    # that violates the schema. Re-reading the JSON here would let the gate
    # trust a file the handoff already skipped.
    jf = find_signal_file(outbox, "DONE", todo_id, ".json")
    data = None
    if jf is not None:
        try:
            data = parse_done_json(jf.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            data = None
    if isinstance(data, dict):
        raw = data.get("checklist")
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(key, str) and isinstance(value, bool):
                    claims[key] = value
        elif isinstance(raw, list):
            for key in raw:
                if isinstance(key, str):
                    claims[key] = True
        checked = data.get("checklist_checked")
        if isinstance(checked, list):
            for key in checked:
                if isinstance(key, str):
                    claims[key] = True

    proposals = 0
    md = find_signal_file(outbox, "DONE", todo_id, ".md")
    try:
        text = md.read_text(encoding="utf-8", errors="surrogateescape") if md else ""
    except OSError:
        text = ""
    if text:
        proposals = sum(
            1
            for line in section_lines(text, PROPOSALS_NAME)
            if line.strip().startswith(("- ", "* ", "+ "))
        )
    return claims, proposals


def _claim_warnings(claims: dict[str, bool], records: list[dict]) -> list[str]:
    """DONE claims against the actual records — a claim never overrides a run."""
    out: list[str] = []
    for item_id, claimed in claims.items():
        record = next((r for r in records if r["id"] == item_id), None)
        if record is None:
            out.append(
                f"DONE claims checklist item {item_id!r} which is not in the unit block"
            )
        elif claimed and record["status"] == "missing":
            out.append(
                f"DONE claims checklist item {item_id!r} as done, but its probe "
                "failed — the run is the truth, the claim is ignored"
            )
    return out


def _proposal_warning(proposals: int) -> str:
    return f"proposals: {proposals} — supervisor triages, never auto-applied"


def _static_record(
    todo_id: str, *, block_sha: str, missing: list[str], warnings: list[str]
) -> dict:
    """A verdict that needs no probe run: an unreadable or empty block."""
    return {
        "todo_id": todo_id,
        "status": "checked",
        "ok": False,
        "tree_sha": "",
        "block_sha": block_sha,
        "items": [],
        "missing": missing,
        "manual": [],
        "warnings": warnings,
        "proposals": 0,
        "cached": False,
    }


def run(
    project_dir: str | Path,
    todo_id: str,
    *,
    tree_sha: str | None = None,
    use_cache: bool = True,
) -> dict:
    """Run the unit's checklist and return the verdict record.

    Returns ``{todo_id, status: "checked"|"none", ok, tree_sha, block_sha,
    probe_sha, items: [...], missing: [...], manual: [...], warnings: [...],
    proposals, generated, cached}``. A unit without the block is
    ``status="none"`` — old units do not break. A verdict that needs no probe
    run (an unreadable or empty block, ``_static_record``) is never cached,
    so it carries no ``probe_sha``/``generated``.
    """
    project = Path(project_dir).resolve()
    if not paths.agentic_dir(project).is_dir():
        raise ChecklistGateError(f"{project} is not an awf project (no .agentic/)")

    text = todos.read_todo_text(project, todo_id)
    if not text:
        raise ChecklistGateError(f"{todo_id}: no TODO found (inbox or archive)")
    try:
        items = parse_block(text)
    except ChecklistError as e:
        # A block the strict parser refuses is a failed block, not a dead
        # run: every item it carries is named as missing.
        return _static_record(
            todo_id,
            block_sha=block_hash(text),
            missing=item_ids(text) or ["(unreadable block)"],
            warnings=[f"unreadable checklist block: {e}"],
        )
    if items is None:
        return {
            "todo_id": todo_id,
            "status": "none",
            "ok": True,
            "tree_sha": "",
            "block_sha": "",
            "items": [],
            "missing": [],
            "manual": [],
            "warnings": [],
            "proposals": 0,
            "cached": False,
        }
    if not items:
        # A heading with no items is not a passed definition of done: zero
        # executed checks is not a gate. Distinct from "no block at all",
        # which stays ``status="none"`` for legacy units.
        return _static_record(
            todo_id,
            block_sha=block_hash(text),
            missing=["(empty block)"],
            warnings=["the checklist block carries no items — nothing was checked"],
        )

    sha = tree_sha if tree_sha is not None else _tree_sha(project)
    bsha = block_hash(text)
    cache_file = state_path(project, todo_id)
    probe_sha = _probe_fingerprint(project, items, todo_id, cache_file)
    # DONE facts are re-read on EVERY call: they live outside the tree
    # fingerprint, so a cached probe answer must not freeze an older
    # proposal count or an older claim. The probe verdicts are cached, the
    # contrast against DONE is always fresh — and it is the ONLY source of
    # warnings on a finished run (the cache stores no static ones), so the
    # warnings are rebuilt, not filtered.
    claims, proposals = _done_claims(project, todo_id)
    if use_cache and sha:
        cached = _read_cache(cache_file)
        if (
            cached
            and cached.get("tree_sha") == sha
            and cached.get("block_sha") == bsha
            and cached.get("probe_sha") == probe_sha
        ):
            cached = dict(cached)
            cached["cached"] = True
            cached["proposals"] = proposals
            warnings = _claim_warnings(claims, cached.get("items", []))
            if proposals:
                warnings.append(_proposal_warning(proposals))
            cached["warnings"] = warnings
            return cached

    records: list[dict] = []
    missing: list[str] = []
    manual: list[str] = []
    warnings: list[str] = []
    for item in items:
        if item.manual:
            manual.append(item.id)
            records.append(
                {"id": item.id, "text": item.text, "status": "manual", "detail": ""}
            )
            continue
        ok, detail = run_probe(project, item, todo_id=todo_id)
        records.append(
            {
                "id": item.id,
                "text": item.text,
                "status": "ok" if ok else "missing",
                "detail": detail,
            }
        )
        if not ok:
            missing.append(item.id)

    warnings += _claim_warnings(claims, records)
    if proposals:
        warnings.append(_proposal_warning(proposals))

    record = {
        "todo_id": todo_id,
        "status": "checked",
        "ok": not missing,
        "tree_sha": sha,
        "block_sha": bsha,
        "probe_sha": probe_sha,
        "items": records,
        "missing": missing,
        "manual": manual,
        "warnings": warnings,
        "proposals": proposals,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cached": False,
    }
    if use_cache and sha:
        try:
            managed_fs.managed_write_text(
                cache_file,
                json.dumps(record, indent=2, ensure_ascii=False) + "\n",
                root=project,
            )
        except (managed_fs.ManagedPathError, OSError):
            pass
    return record


__all__ = [
    "ChecklistGateError",
    "run",
    "run_probe",
]
