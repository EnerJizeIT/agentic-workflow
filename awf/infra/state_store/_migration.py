"""R1.3: legacy projections + migration into the control snapshot.

``run.yaml`` / ``current.yaml`` stay on disk as READ-ONLY projections: after
migration the snapshot's ``revision`` is the authority and there is no merge
of edits made here back into the snapshot. Migration refuses to guess when
the legacy state is ambiguous (conflicting run/current, or "completed"
inferred from a directory) — it returns
``migration_requires_reconciliation`` instead of auto-healing.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Literal

import yaml

from ... import paths
from ..._errors import AwfApiError, OperationRecoveryRequired
from ..._proc import _pid_alive
from ._errors import MigrationReconciliationRequired, StateCorrupt
from ._io import (
    SCHEMA_VERSION,
    atomic_write_json,
    control_path,
    manifest_path,
    sha256,
    timestamp,
)
from ._snapshot import read_snapshot, require_usable
from ._transact import transact


def _legacy_file(project_dir: str | Path, which: Literal["run", "current"]) -> Path:
    """The legacy projection path; literals live in this one place."""
    name = "run.yaml" if which == "run" else "current.yaml"
    return paths.state_dir(project_dir) / name


def _legacy_yaml(path: Path) -> dict[str, Any] | None:
    """Parse a legacy projection file.

    ``{}`` — the file is absent or empty (a project that never wrote it).
    ``None`` — the file EXISTS but cannot be trusted (unreadable, broken
    YAML, non-object root). The caller must treat that as an ambiguity, not
    as an empty project: migration of a corrupt legacy file would silently
    publish an empty snapshot and lose the old state.
    """
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if data is None:
        return {}
    return data if isinstance(data, dict) else None


def read_projection(project_dir: str | Path, which: Literal["run", "current"]) -> dict[str, Any]:
    """Read a legacy projection (``run.yaml`` / ``current.yaml``) read-only.

    An unreadable file raises :class:`StateCorrupt` — a read adapter must
    not turn corruption into "no state".
    """
    path = _legacy_file(project_dir, which)
    data = _legacy_yaml(path)
    if data is None:
        raise StateCorrupt(
            f"{path}: legacy projection exists but cannot be parsed — never "
            "treated as an empty project"
        )
    return data


def _live_pid(pid: Any) -> bool:
    """Is the legacy ``pipeline_pid`` alive? Delegates to the shared
    ``awf._proc._pid_alive`` probe (OverflowError-safe, zombie-aware) instead
    of re-implementing ``os.kill``: one liveness semantics for the whole
    tool, and an oversized fake pid cannot escape as a raw OverflowError."""
    if not isinstance(pid, int) or pid <= 1:
        return False
    return _pid_alive(pid)


def migration_preview(project_dir: str | Path) -> dict[str, Any]:
    """Describe what a migration WOULD do: legacy files, carried-over fields,
    conflicts and whether the legacy state is ambiguous.

    Ambiguity (``requires_reconciliation``) is never auto-healed:
    * ``run.yaml`` and ``current.yaml`` name different active runs, or
    * a run looks finished only because a completion directory exists.
    """
    run_file = _legacy_file(project_dir, "run")
    current_file = _legacy_file(project_dir, "current")
    run = _legacy_yaml(run_file)
    current = _legacy_yaml(current_file)

    conflicts: list[str] = []
    for name, data in ((run_file.name, run), (current_file.name, current)):
        if data is None:
            conflicts.append(f"{name} exists but cannot be parsed (corrupt legacy state)")
    run = run or {}
    current = current or {}

    run_id = run.get("run_id") or run.get("run_generation")
    cur_id = current.get("run_id") or current.get("run_generation")
    if run and current and run_id is not None and cur_id is not None and run_id != cur_id:
        conflicts.append(f"run.yaml run={run_id!r} vs current.yaml run={cur_id!r}")

    done_dir = paths.done_dir(project_dir)
    done_entries = [p.name for p in done_dir.iterdir() if p.is_dir()] if done_dir.is_dir() else []
    run_finished = bool(run.get("completed_at")) or run.get("status") in {"done", "finished"}
    if done_entries and not run_finished and run:
        conflicts.append(
            f"run.yaml has no completion marker but the done dir holds "
            f"{len(done_entries)} entries"
        )

    return {
        "control_exists": control_path(project_dir).is_file(),
        "legacy_files": [str(p) for p in (run_file, current_file) if p.is_file()],
        "old_fields": {"active_run": run or None, "execution": current or None},
        "conflicts": conflicts,
        "requires_reconciliation": bool(conflicts),
        "expected": (
            "publish control.json schema_version=2 revision=1; keep legacy "
            "files read-only; idempotent repeat returns no_change"
        ),
    }


def migrate(
    project_dir: str | Path,
    *,
    operation_id: str | None = None,
    expected_revision: int = 0,
) -> dict[str, Any]:
    """Migrate legacy state into the snapshot.

    Preconditions: unambiguous legacy state and no live legacy pipeline (a
    live pid refuses). Original files and a manifest are preserved. A repeat
    on an already-migrated project is ``no_change``.
    """
    snap = read_snapshot(project_dir)
    if snap.valid:
        return {
            "status": "no_change",
            "revision": snap.revision,
            "message": "control.json already present and valid — nothing to migrate",
        }
    if snap.status in {"corrupt", "unsupported"}:
        require_usable(snap, project_dir)  # raises the refusal

    preview = migration_preview(project_dir)
    if preview["requires_reconciliation"]:
        raise MigrationReconciliationRequired(
            "ambiguous legacy state — refusing to auto-heal: "
            + "; ".join(preview["conflicts"])
        )

    current = preview["old_fields"]["execution"] or {}
    if _live_pid(current.get("pipeline_pid")):
        raise MigrationReconciliationRequired(
            f"legacy pipeline pid {current.get('pipeline_pid')} is alive — stop "
            "the run before migrating"
        )

    op = operation_id or f"state-migrate-{SCHEMA_VERSION}"
    run = preview["old_fields"]["active_run"] or {}

    def _mutate(new: dict[str, Any]) -> dict[str, Any]:
        if run:
            new["active_run"] = run
        if current:
            new["execution"] = current
        new["run_generation"] = int(run.get("run_generation") or 0)
        return new

    out = transact(
        project_dir,
        expected_revision=expected_revision,
        operation_id=op,
        mutate=_mutate,
        args={"migration": SCHEMA_VERSION, "legacy": preview["legacy_files"]},
    )

    state = paths.state_dir(project_dir)
    try:
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "migrated_at": timestamp(),
            "revision": out["revision"],
            "legacy": {
                name: sha256(p.read_text(encoding="utf-8"))
                for name in ("run.yaml", "current.yaml")
                if (p := state / name).is_file()
            },
        }
        atomic_write_json(manifest_path(project_dir), manifest)
    except Exception as e:
        # The snapshot is already published (the commit point), so ANY fault
        # here — a failed manifest read/write, not only OSError — means the
        # migration landed while its manifest stays unwritten. Same shape T2b
        # closed inside ``transact``: surface recovery_required instead of a
        # raw exception, so the caller reconciles rather than retrying blind.
        raise OperationRecoveryRequired(
            op,
            f"migration {op}: control.json published (revision "
            f"{out['revision']}) but the manifest step failed ({e}) — "
            "recovery_required, the migration landed",
        ) from e
    return {
        "status": "applied",
        "revision": out["revision"],
        "legacy_files": preview["legacy_files"],
        "manifest": str(manifest_path(project_dir)),
    }


def main(argv: list[str] | None = None) -> int:
    """Minimal CLI: ``python -m awf.infra.state_store preview|migrate <dir>``."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] not in {"preview", "migrate"}:
        sys.stderr.write(
            "usage: python -m awf.infra.state_store preview|migrate <project_dir>\n"
        )
        return 2
    action, target = argv
    if action == "preview":
        try:
            text = json.dumps(
                migration_preview(target), ensure_ascii=False, indent=2
            )
        except (OSError, ValueError, TypeError) as e:
            # QA-217: the SAME fault class the ``migrate`` handler below closes
            # also reaches ``preview`` — a legacy field YAML parses into a
            # non-JSON type (``started: 2026-10-17`` -> ``datetime.date``) and
            # ``json.dumps`` raises TypeError. The CLI must always emit a
            # structured outcome, never a traceback, on every subcommand.
            sys.stdout.write(
                json.dumps({"status": "failed", "error": str(e)}, ensure_ascii=False) + "\n"
            )
            return 1
        sys.stdout.write(text + "\n")
        return 0
    try:
        result = migrate(target)
    except AwfApiError as e:
        # ``OperationRecoveryRequired`` is an ``AwfApiError`` but NOT a
        # ``StateStoreError``; catching the shared base surfaces every
        # documented outcome (including recovery_required) as JSON instead of
        # letting one class escape as a traceback.
        outcome = getattr(e, "outcome", "error")
        sys.stdout.write(
            json.dumps({"status": outcome, "error": str(e)}, ensure_ascii=False) + "\n"
        )
        return 1
    except (OSError, ValueError, TypeError) as e:
        # Q1 (REVIEW-фикс 4): a fault on the publish/parse path is NOT an
        # ``AwfApiError`` and ``transact`` re-raises it raw — a write fault on
        # ``control.json`` (OSError), a legacy field YAML parsed into a
        # non-JSON type (TypeError), a non-numeric ``run_generation``
        # (ValueError). The CLI must still emit a structured outcome, never a
        # traceback. The snapshot replace is the commit point, so these faults
        # leave the effect unapplied → ``failed`` (retryable, no double
        # effect), same class as the C fix one layer deeper.
        sys.stdout.write(
            json.dumps({"status": "failed", "error": str(e)}, ensure_ascii=False) + "\n"
        )
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


__all__ = ["read_projection", "migration_preview", "migrate", "main"]
