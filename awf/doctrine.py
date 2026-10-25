"""U9: project doctrine — lessons that enter every role's prompt.

Files in ``.agentic/doctrine/*.md`` are assembled into one
«Доктрина проекта» section and injected between the role's instructions
and the task in every stage's prompt (workers and supervisor alike).
A new lesson lands in every future run without touching role templates.

Determinism (U9 part B): the assembly is a pure function of file names
and contents — sorted by name, no timestamps, no randomness. Two
assemblies with the same input are byte-identical regardless of the
order files were created on disk.

No catalog (or no .md files in it) → the section is empty and the
prompt is unchanged (backward compatible).
"""
from __future__ import annotations

from pathlib import Path

from . import paths
from ._atomic import atomic_write_text
from .infra import managed_fs

DOCTRINE_DIRNAME = "doctrine"
DOCTRINE_ASSEMBLED_NAME = "DOCTRINE.md"
SECTION_HEADING = "## Доктрина проекта"


def doctrine_dir(project_dir: str | Path) -> Path:
    """Return the project's doctrine catalog: .agentic/doctrine/."""
    return paths.agentic_dir(project_dir) / DOCTRINE_DIRNAME


def load_doctrine_files(project_dir: str | Path) -> list[tuple[str, str]]:
    """Return ``(filename, content)`` for every ``*.md`` in the catalog.

    Top-level .md files only, sorted by filename — the deterministic
    order. Subdirectories and non-.md files are ignored. Returns []
    when the catalog is missing or holds no .md files.
    """
    d = doctrine_dir(project_dir)
    if not d.is_dir():
        return []
    files = [p for p in d.iterdir() if p.is_file() and p.suffix == ".md"]
    files.sort(key=lambda p: p.name)
    return [(p.name, p.read_text(encoding="utf-8")) for p in files]


def assemble_doctrine(project_dir: str | Path) -> str:
    """Assemble the «Доктрина проекта» section; '' when nothing to inject."""
    entries = load_doctrine_files(project_dir)
    if not entries:
        return ""
    parts: list[str] = [SECTION_HEADING, ""]
    for name, content in entries:
        parts.append(f"### {name}")
        parts.append("")
        body = content.strip()
        if body:
            parts.append(body)
        parts.append("")
    return "\n".join(parts).rstrip("\n") + "\n"


# W7: built-in doctrine templates shipped with awf (awf/templates/doctrine/).
# ``awf init`` copies the missing ones into the project's catalog so a new
# project starts with the core safety lessons without hand-copying them.
BUILTIN_DOCTRINE_DIR = Path(__file__).resolve().parent / "templates" / "doctrine"


def seed_doctrine(project_dir: str | Path) -> list[str]:
    """Copy built-in doctrine templates missing from the project catalog.

    Never overwrites an existing file — a user-edited doctrine is user
    data (same rule as add_role refusing to replace a role file).
    Returns the copied file names (sorted); [] when nothing was missing
    or the built-in catalog does not exist (old installs).

    M2 (REVIEW виток 4): the copy goes through ``managed_write_text`` — a
    symlinked ``.agentic``/``doctrine`` used to send ``mkdir``+``copy2`` out
    of the project while init reported success. The seed is part of the
    init bootstrap, so a non-POSIX platform keeps the plain atomic writer
    (same policy as ``lifecycle._bootstrap_write``, docs/design.md).
    """
    if not BUILTIN_DOCTRINE_DIR.is_dir():
        return []
    dst = doctrine_dir(project_dir)
    root = Path(project_dir).resolve()
    copied: list[str] = []
    for src in sorted(BUILTIN_DOCTRINE_DIR.glob("*.md")):
        target = dst / src.name
        if target.exists():
            continue
        text = src.read_text(encoding="utf-8")
        mode = src.stat().st_mode & 0o777
        if managed_fs.is_posix():
            managed_fs.managed_write_text(
                target, text, root=root, mode=mode
            )
        else:
            dst.mkdir(parents=True, exist_ok=True)
            atomic_write_text(target, text, mode=mode)
        copied.append(src.name)
    return copied


def materialize_doctrine(project_dir: str | Path) -> Path | None:
    """Write the assembled section to .agentic/context/DOCTRINE.md.

    Single entry point for all prompt consumers (worker stages and the
    supervisor stage): they pass the returned path to opencode as
    ``--file`` between the role file and the task file. Returns None
    (and writes nothing) when there is no doctrine to inject.
    """
    text = assemble_doctrine(project_dir)
    if not text:
        return None
    ctx = paths.context_dir(project_dir)
    ctx.mkdir(parents=True, exist_ok=True)
    out = ctx / DOCTRINE_ASSEMBLED_NAME
    atomic_write_text(out, text)
    return out
