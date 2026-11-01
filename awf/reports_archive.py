"""A2: a stage report is never overwritten — the previous one is archived.

Every stage writes a handoff and most stages overwrite the unit's DONE
report. Without an archive the trail of an earlier stage (or an earlier
attempt of the same stage) simply disappears: the audit reads the last
report and believes it is the only one.

The archive lives under ``.agentic/context/reports/`` — runtime, gitignored,
like the rest of ``context/``. Names are unique by construction
(``<name>-<stage>-a<attempt>-<UTC timestamp>.md``), so an archive can never
overwrite another archive; a missing source is a no-op, and a failed copy is
logged and never stops the stage (the report is a record, not a gate).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def reports_dir(project_dir: str | Path) -> Path:
    from . import paths

    return paths.context_dir(project_dir) / "reports"


def archive_report(
    project_dir: str | Path,
    source: Path,
    *,
    stage: str = "",
    attempt: int | None = None,
    logs_dir: Path | None = None,
) -> Path | None:
    """Copy ``source`` into the reports archive; ``None`` when there is none.

    The copy is taken BEFORE the caller overwrites or removes the source, so
    the archive always holds the state the next reader would have lost.
    """
    if not source.is_file():
        return None
    target_dir = reports_dir(project_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    suffix = source.suffix or ".md"
    stem = source.stem
    parts = [stem]
    if stage:
        parts.append(stage)
    if attempt is not None:
        parts.append(f"a{attempt}")
    parts.append(stamp)
    target = target_dir / ("-".join(parts) + suffix)
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes()
        # ``xb``: an archive never replaces another archive, even on a clock
        # collision — the name carries microseconds AND the copy is exclusive.
        with open(target, "xb") as fh:
            fh.write(data)
    except FileExistsError:
        return None
    except OSError as e:
        if logs_dir is not None:
            from ._log import log as _log

            _log(logs_dir, f"A2: report archive failed for {source.name}: {e}")
        return None
    return target


__all__ = ["archive_report", "reports_dir"]
