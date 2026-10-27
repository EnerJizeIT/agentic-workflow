"""Lifecycle API: init, status, report, reset, orphans.

All functions return a Result dataclass (see :mod:`awf.api._results`)
or raise :class:`awf.api.AwfApiError` with a human-readable message.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .. import budget as budget_mod
from .. import config as cfg_mod
from .. import paths, run_state, todos
from .._atomic import MODE_OWNER_EDITABLE, atomic_write_text
from .._lock import LOCK_NAME
from ..dispatch_policy import CONFIG_VERSION
from ..infra import managed_fs
from ..unit_history import forget_unit_history
from . import _liveness
from ._background import check_pipeline_running
from ._errors import AwfApiError
from ._helpers import (
    managed_write_api_text,
    read_file_text,
    require_agentic,
    require_git_repo,
)
from ._interlock import INTERLOCK_NAME, destructive_section, ensure_interlock_file
from ._lease import EXECUTION_NAME, LEASE_NAME, rotate_execution_id
from ._results import (
    InitResult,
    ReportResult,
    ResetResult,
    RestoreResult,
    StatusResult,
)
from ._stack import derive_project_name, detect_stack
from ._templates import (
    _CONFIG_TEMPLATE,
    awf_templates_dir,
    update_gitignore,
)

# ─── init_project ───────────────────────────────────────────────────────

# M1 (REVIEW виток 4): the dirs a fresh init creates — the CORE. The full
# skeleton below EXTENDS this list, so the seven core names are typed once:
# two hand-copied lists were the same "two sources of one surface" drift the
# init preflight is meant to remove.
_FRESH_DIRS = [
    "roles",
    "pipelines",
    "phases",
    "inbox",
    "outbox",
    "context",
    "logs",
]

# A-11: the full skeleton (fresh-init core + status dirs). A re-init
# without force only re-creates the ones that are missing.
_SKELETON_DIRS = [
    *_FRESH_DIRS,
    "handoff", "done", "state", "dashboards", "inputs",
]


def _init_plan_dirs(project_dir: Path) -> list[str]:
    """Union of every init surface: runtime wipe, skeleton, seed writes.

    M1 (REVIEW виток 4): ``phases``/``pipelines`` are not in
    ``INIT_RUNTIME_DIRS`` and used to be checked only at the skeleton step —
    AFTER the force wipe. ONE plan definition, checked before every first
    effect: a refusal leaves everything (inbox, logs, state) untouched.

    Z1 (REVIEW виток 6) / architect TODO-0231: the seed surfaces are part of
    THIS union, not appended by each caller — three copies of the same
    conditional were the "two sources of one surface" drift the plan exists
    to remove. ``doctrine`` stays unconditional here (the force branch always
    preflighted it); the checkpoint surface is appended only when its seed
    would write.
    """
    from ..doctrine import DOCTRINE_DIRNAME
    from ._destructive import INIT_RUNTIME_DIRS

    return list(
        dict.fromkeys(
            [
                *INIT_RUNTIME_DIRS,
                *_SKELETON_DIRS,
                DOCTRINE_DIRNAME,
                *_pending_seed_surfaces(project_dir),
            ]
        )
    )


def _pending_seed_surfaces(project_dir: Path) -> list[str]:
    """The ``.agentic``-relative surfaces the seed steps will write NOW.

    Per-branch by design (Q3): a seed that writes nothing must not refuse a
    branch over a symlink on its surface. ONE definition — the full plan and
    the re-init branch read the same list, so a new seed cannot be checked
    in one place and forgotten in the other.
    """
    surfaces: list[str] = []
    if _doctrine_seed_pending(project_dir):
        surfaces.append("doctrine")
    if _checkpoint_seed_pending(project_dir):
        surfaces.append(paths.CHECKPOINT_SURFACE)
    return surfaces


def _refuse_symlinked_dirs(
    project_dir: Path, dirs: list[str] | tuple[str, ...], *, what: str
) -> None:
    """No-follow preflight for an init plan, BEFORE the first effect.

    C1/C2/M1 (REVIEW): ONE plan definition — ``_init_plan_dirs()`` unions
    the runtime wipe (``_clean_runtime``), the skeleton creation and the
    doctrine seed. Copies were a second place for the same invariant to
    drift; ``what`` only keeps the refusal text specific ("runtime" /
    "skeleton"). The caller runs the whole plan BEFORE its own first effect
    (the force branch: before the wipe and again before the writes; the
    non-force branch: before the missing-dir mkdir), so a refusal leaves
    the disk untouched — in particular a symlinked ``.agentic`` (or a
    component such as ``roles``) never sends ``mkdir``/``rmtree``/``write``
    out of the project (QA F1, Q1).
    """
    agentic = project_dir / ".agentic"
    try:
        managed_fs.assert_managed_dir(agentic, root=project_dir)
        for d in dirs:
            managed_fs.assert_managed_dir(agentic / d, root=project_dir)
    except managed_fs.ManagedPathError as exc:
        raise AwfApiError(f"init refused — unsafe {what} path: {exc}") from exc


def _clean_runtime(project_dir: Path) -> list[str]:
    """A-11: destructive path only. Remove runtime dirs, preserve config.

    R1.4 (QA F-B): ``state/`` чистится ПО СОДЕРЖИМОМУ, файлы синхронизации
    ``.interlock`` и ``.lock`` сохраняются. ``rmtree`` каталога удаляет
    inode, на котором ``init_project(force=True)`` держит flock: конкурентный
    запуск открывает новый файл, берёт flock и публикует lease в окне
    сноса — секция перестаёт закрывать окно (требование 4). Та же логика
    для ``.lock`` (RMW-блокировка Store/run_state): снос inode осиротил бы
    flock живого писателя, новый открыл бы другой файл и прошёл мимо.
    Оба — стабильные точки синхронизации, а не runtime-данные.
    """
    agentic = project_dir / ".agentic"
    cleaned: list[str] = []
    # F-R2: one source with the destructive preview — ``INIT_RUNTIME_DIRS``
    # lives in ``_destructive`` and the force-init preview reads it too, so
    # "what will be removed" cannot drift from what is actually removed.
    from ._destructive import INIT_RUNTIME_DIRS

    # The no-follow preflight runs in the caller over the WHOLE init plan
    # (runtime ∪ skeleton ∪ doctrine), before the first effect — see
    # ``_refuse_symlinked_dirs``. A local check would be a second copy and
    # would still miss ``phases``/``pipelines`` (QA Q1).
    for name in INIT_RUNTIME_DIRS:
        d = agentic / name
        if not d.exists():
            continue
        if name == "state":
            _clean_state_dir(d)
        else:
            shutil.rmtree(d)
        cleaned.append(name)
    return cleaned


#: Файлы ``.agentic/state/``, которые НЕ runtime-данные, а стабильные
#: точки синхронизации: секция запуска/разрушения (``.interlock``) и
#: RMW-блокировка Store/run_state (``.lock``). Их снос осиротил бы flock
#: живого конкурента — новый писатель открыл бы НОВЫЙ inode и прошёл мимо
#: (QA F-B для ``.interlock``, класс F-B для ``.lock``). Имена берём у
#: владельцев файлов, не повторяем рядом.
_PRESERVED_STATE_FILES = frozenset({INTERLOCK_NAME, LOCK_NAME})


def _clean_state_dir(state_dir: Path) -> None:
    """Снести содержимое ``state``, сохранив ``.interlock`` и ``.lock``.

    R1.5 (REVIEW A5): the wipe goes through the Store's facade
    (``delete_state_tree``) — the api layer no longer touches state bytes
    (or their inodes) itself. Symlink/file removed in place, directory
    recursively; the preserved synchronisation files are never descended
    into.
    """
    from ..infra import state_store

    state_store.delete_state_tree(state_dir, preserve=_PRESERVED_STATE_FILES)


def _rotate_launch_identity(project_dir: Path) -> str:
    """F-1/F-X: свежий ExecutionId — общий хвост ЛЮБОГО сноса runtime.

    Метка ``logs/awf-launch.execution`` — не runtime-данные, а страховка
    handshake: ребёнок сверяет свой id с ней, и «нет метки = работай» —
    legacy-правило. После сноса проекту нужна ЖИВАЯ свежая метка, иначе
    crash-orphan ребёнок прежнего запуска (его launch умер, поэтому
    destructive-preflight его пропустил) проходит handshake и поднимает
    второй пайплайн поверх заново инициализированного проекта.

    Обе разрушительные двери сносят ``logs`` (reset — пофайлово,
    ``init_project(force=True)`` — ``rmtree``) и обе обязаны закончить этим
    шагом. Единый locus — третья дверь сноса не заведёт свою копию и не
    забудет ротацию. Зовётся ПОД ``destructive_section``, чтобы новый
    запуск не опубликовал заявку в окне между сносом и ротацией.
    """
    return rotate_execution_id(project_dir)


def _read_vision_excerpt(vision_path: Path | None) -> tuple[str, str | None]:
    """REPORTS33 (TODO-0207): excerpt from the ACTUAL vision anchor.

    Returns ``(excerpt, reason)``. ``reason`` is ``None`` when the excerpt
    is real; otherwise the excerpt is ``""`` and ``reason`` is the honest
    line the caller appends to ``warnings`` (no anchor, anchor gone between
    lookup and read, unreadable file) — no more silent empty excerpt.
    utf-8 with errors=replace; first 4000 chars with the same truncation
    marker as :func:`read_file_text`, so a readable file yields byte-
    identical output to the old fresh-init read.
    """
    if vision_path is None:
        return "", "vision file not found — no excerpt available"
    if not vision_path.is_file():
        return "", (
            f"vision file not found — {vision_path.name} used as anchor "
            "without excerpt"
        )
    try:
        text = vision_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return "", f"vision file unreadable — {vision_path.name}: {e}"
    if len(text) > 4000:
        text = text[:4000] + "\n...[truncated]"
    return text, None


def _bootstrap_write(
    path: Path,
    content: str,
    *,
    operation: str,
    mode: int | None = None,
    root: Path | None = None,
) -> None:
    """One write primitive for the init skeleton.

    TODO-0234 REVIEW (architect): on POSIX the skeleton is written through
    the managed barrier (a pre-existing symlinked ``.agentic`` must refuse
    BEFORE the first byte — the force path can meet one). Off POSIX the
    barrier refuses by policy, but ``awf init`` is a safe bootstrap and
    must not fail there (F-P1) — the plain atomic writer keeps the old
    platform behavior.

    For a leaf under ``.agentic`` the root is derived from the leaf
    (``managed_fs.agentic_root_for``), so a separately passed spelling of
    the same root could only disagree with it (the C4/Q4 class). ``root``
    is for the one skeleton file OUTSIDE ``.agentic``
    (``scripts/suite-canonical.sh``): there is no anchor component to
    walk up to, and the leaf-derived root would start at the leaf's own
    parent — a symlinked ``scripts/`` would slip through unexamined.
    """
    if not managed_fs.is_posix():
        atomic_write_text(path, content, mode=mode)
        return
    if root is None:
        managed_write_api_text(
            path,
            content,
            operation=operation,
            mode=0o600 if mode is None else mode,
        )
        return
    try:
        managed_fs.managed_write_text(
            path,
            content,
            root=root,
            mode=0o600 if mode is None else mode,
        )
    except managed_fs.ManagedPathError as exc:
        raise AwfApiError(
            f"{operation} refused — unsafe managed path: {exc}"
        ) from exc


def _seed_doctrine(project_dir: Path) -> list[str]:
    """Seed the built-in doctrine, translating a barrier refusal.

    One entry point for both init branches (fresh/force and re-init): the
    lazy import and the error translation used to be repeated at each call
    site. ``seed_doctrine`` writes through the managed barrier, which can
    still refuse on a swap after the preflight; the API's error matrix keeps
    ``AwfApiError``, not a raw ``ManagedPathError`` (same rule as K3).
    """
    from ..doctrine import seed_doctrine

    try:
        return seed_doctrine(project_dir)
    except managed_fs.ManagedPathError as exc:
        raise AwfApiError(
            f"init refused — unsafe doctrine path: {exc}"
        ) from exc


def _seed_checkpoint_template(project_dir: Path) -> list[str]:
    """W7 (TODO-0231): copy the checkpoint template into the project.

    The init branches share this: the file lands at
    ``paths.CHECKPOINT_TEMPLATE`` (managed barrier), and an
    existing copy — or a dangling leaf — is the USER's file and is never
    touched. A missing/unreadable vendor template skips silently: the
    report line then simply does not render (RUN-REPORT prints the path
    only when the file is there).
    """
    from ._templates import awf_templates_dir

    template = awf_templates_dir() / paths.CHECKPOINT_TEMPLATE
    dest = paths.agentic_dir(project_dir) / paths.CHECKPOINT_TEMPLATE
    if not template.is_file() or os.path.lexists(dest):
        return []
    try:
        body = template.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        _bootstrap_write(
            dest,
            body,
            operation="init checkpoint-template.md",
            mode=MODE_OWNER_EDITABLE,
        )
    except managed_fs.ManagedPathError as exc:
        raise AwfApiError(
            f"init refused — unsafe checkpoint-template path: {exc}"
        ) from exc
    return [f".agentic/{paths.CHECKPOINT_TEMPLATE}"]


def _checkpoint_seed_pending(project_dir: Path) -> bool:
    """True when the W7 seed would write the checkpoint template.

    Q3's lesson (REVIEW виток 5) applied to the coproduct: preflight
    exactly the surface this init branch touches — the seed exists only
    when the vendor template is present and the project has no copy yet.
    """
    from ._templates import awf_templates_dir

    return (
        (awf_templates_dir() / paths.CHECKPOINT_TEMPLATE).is_file()
        and not os.path.lexists(
            paths.agentic_dir(project_dir) / paths.CHECKPOINT_TEMPLATE
        )
    )


def _doctrine_seed_pending(project_dir: Path) -> bool:
    """True when ``seed_doctrine`` would write at least one template.

    Q3 (REVIEW виток 5): the init preflight is per-branch — doctrine is
    checked only when the branch really writes templates. A re-init over an
    existing catalog touches nothing there, so a symlink on that path must
    not refuse a branch that writes elsewhere.
    """
    from ..doctrine import BUILTIN_DOCTRINE_DIR, doctrine_dir

    if not BUILTIN_DOCTRINE_DIR.is_dir():
        return False
    dst = doctrine_dir(project_dir)
    return any(
        not (dst / src.name).exists()
        for src in BUILTIN_DOCTRINE_DIR.glob("*.md")
    )


def init_project(
    project_dir: Path,
    *,
    force: bool = False,
    project_name: str | None = None,
    test_cmd: str | None = None,
    lint_cmd: str | None = None,
    typecheck_cmd: str | None = None,
    build_cmd: str | None = None,
    dry_run: bool = False,
    confirm_digest: str | None = None,
) -> InitResult:
    """Initialize ``.agentic/`` skeleton in project_dir.

    All command parameters are auto-detected via :func:`detect_stack` when
    not provided explicitly. Project name is derived from directory when
    not provided.

    **A-11 (audit 2026-09-25):** re-init is non-destructive. When
    ``.agentic/`` already exists and ``force=False``, nothing is deleted
    or overwritten — active TODOs, the done/ archive, state and logs stay
    byte-for-byte as they were; only missing empty skeleton directories
    are created. ``force=True`` is the explicit destructive path (cleans
    runtime + rewrites the skeleton) and is REFUSED with
    :class:`AwfApiError` while a pipeline is live (shared resolver
    :func:`awf.api._liveness.resolve`, same as ``start_pipeline``).
    ``dry_run=True`` is a pure read and changes nothing.

    Returns :class:`InitResult` with supervisor.md, vision excerpt, plan.md
    content — caller (CLI/MCP) has everything needed to assume the supervisor
    role without further file reads.
    """
    project_dir = Path(project_dir).resolve()
    require_git_repo(project_dir)

    agentic = project_dir / ".agentic"
    if agentic.exists():
        if dry_run:
            # AUD05-01 + A-11 QA pin: dry-run is a pure read in EVERY
            # combination — including force=True (the CLI passes both
            # flags together, awf/cmd_init.py:43). Must return BEFORE the
            # liveness check and before _clean_runtime, so force+dry-run
            # deletes nothing.
            config = cfg_mod.load(project_dir)
            project_name_val = config.get("project", {}).get("name", project_dir.name)
            vision_path = paths.find_vision_file(project_dir)
            warnings: list[str] = []
            vision_excerpt, _reason = _read_vision_excerpt(vision_path)
            if _reason:
                warnings.append(_reason)
            return InitResult(
                project_name=project_name_val,
                project_dir=str(project_dir),
                stack="(preserved)",
                vision_path=str(vision_path) if vision_path else None,
                vision_excerpt=vision_excerpt,
                supervisor_md="",
                plan_md="",
                pipeline_configured=bool(config.get("default_pipeline")),
                next_action="[dry-run] .agentic/ already exists — nothing written, runtime untouched.",
                warnings=warnings,
            )
        if force:
            # A-11 (audit 2026-09-25): force is the ONLY destructive path.
            # R1.4 (F-H1): it is a destructive op like reset — under the SAME
            # critical section (destructive_section) and the SAME preflight
            # (destructive_blockers). Without them a force-init could slip in
            # while a launch publishes its claim (the project torn down under
            # a live run), and _clean_runtime would wipe control.json,
            # resetting the durable run generation (S3-03 via a third door).
            from ..infra import state_store

            with destructive_section(
                project_dir, label="init_project(force=True)"
            ):
                _liveness.refuse_if_in_use(
                    project_dir, label="init_project(force=True)"
                )
                # F-R2: the force wipe is a destruction — require the
                # confirmed plan digest (recomputed here, under the section)
                # before anything is deleted. None = legacy trusted call.
                from . import _destructive

                _destructive.verify_confirm(project_dir, "init", confirm_digest)
                # The durable generation memory has to survive the wipe:
                # _clean_runtime removes state/control.json, so read the
                # counter BEFORE and seed it back AFTER, keeping it
                # monotone across the re-init.
                carried_generation = state_store.current_run_generation(
                    project_dir
                )
                # M1 (REVIEW виток 4): ONE preflight over the union — the
                # runtime wipe, the skeleton and the doctrine seed — BEFORE
                # the first rmtree. A symlinked ``phases``/``pipelines``
                # used to refuse only after inbox/logs/state were gone.
                # Z1 (REVIEW виток 6): W7 added the fourth write surface
                # (``.agentic/docs``) — without it in the plan the force
                # wipe ran first and only the managed barrier refused.
                _refuse_symlinked_dirs(
                    project_dir,
                    _init_plan_dirs(project_dir),
                    what="runtime",
                )
                _clean_runtime(project_dir)
                state_store.seed_run_generation(project_dir, carried_generation)
                # F-X: the ExecutionId marker has to survive the wipe exactly
                # like reset's does — _clean_runtime rmtree's logs, so without
                # this a crash-orphan child of the pre-force-init launch passes
                # the "no marker = work" handshake and raises a second
                # pipeline. One shared step with reset (_rotate_launch_identity).
                _rotate_launch_identity(project_dir)
            # fall through: rewrite the skeleton below (config.yaml,
            # supervisor.md, plan.md) — the explicit destructive path
        else:
            # A-11 (audit 2026-09-25): re-init without force is
            # non-destructive — active TODOs, the done/ archive, state and
            # logs stay byte-for-byte as they were. Only missing empty
            # skeleton directories are re-created.
            created_dirs: list[str] = []
            # Q3 (REVIEW виток 5): this branch only creates MISSING skeleton
            # dirs — it never writes an existing one. Checking the whole union
            # refused a live symlink (e.g. ``logs``) that the branch skips,
            # breaking a routine ``awf init``. Preflight exactly what the
            # branch touches: ``.agentic`` itself (every write crosses it),
            # the missing dirs, and doctrine only when the seed has templates
            # to write.
            # ONE list drives both the preflight and the mkdir: re-testing
            # ``exists()`` in the loop was a second answer to the same
            # question (the "two sources of one surface" drift this unit
            # removes), and could create a directory the preflight never saw.
            missing = [
                d for d in _SKELETON_DIRS if not (agentic / d).exists()
            ]
            _refuse_symlinked_dirs(
                project_dir,
                missing + _pending_seed_surfaces(project_dir),
                what="skeleton",
            )
            for d in missing:
                (agentic / d).mkdir(parents=True, exist_ok=True)
                created_dirs.append(f".agentic/{d}/")
            ensure_interlock_file(project_dir)
            # W7: re-init also seeds missing doctrine (non-destructive,
            # same rule as the skeleton dirs above — existing files are
            # user data and are never overwritten).
            for name in _seed_doctrine(project_dir):
                created_dirs.append(f".agentic/doctrine/{name}")
            for rel in _seed_checkpoint_template(project_dir):
                created_dirs.append(rel)
            config = cfg_mod.load(project_dir)
            project_name_val = config.get("project", {}).get("name", project_dir.name)
            vision_path = paths.find_vision_file(project_dir)
            warnings: list[str] = []
            vision_excerpt, _reason = _read_vision_excerpt(vision_path)
            if _reason:
                warnings.append(_reason)
            phases_file = cfg_mod.get(config, "phases.current", ".agentic/phases/plan.md")
            supervisor_md_path = project_dir / ".agentic" / "roles" / "supervisor.md"
            plan_md = ""
            plan_path = project_dir / phases_file if not Path(phases_file).is_absolute() else Path(phases_file)
            if plan_path.is_file():
                plan_md = plan_path.read_text(encoding="utf-8")
            # SMO: detect phase for compact prompt + next_action
            try:
                from ..phase import detect_phase, get_phase_prompt
                phase_r1 = detect_phase(project_dir)
                supervisor_md = get_phase_prompt(phase_r1, project_dir)
            except Exception:
                phase_r1 = "goal"
                supervisor_md = supervisor_md_path.read_text(encoding="utf-8") if supervisor_md_path.is_file() else ""

            _NEXT_ACTIONS_R1 = {
                "goal": "Спроси пользователя о цели сессии. После ответа — awf_set_goal.",
                "form": "Открой project-setup форму через awf_open_project_setup_form.",
                "normalize": "Выполни normalize checklist, затем awf_confirm_normalized.",
                "brief": "Изучи vision и план, напиши TODO-NNNN.md (awf_dispatch_todo) и запусти пайплайн.",
                "run": "Проверь awf_status, при необходимости dispatch_todo + awf_start.",
                "verify": "Проверь handoffs + git diff, реши ACK или REVIEW.",
                "done": "Pipeline завершён. Спроси пользователя о следующем шаге.",
            }
            next_action_r1 = _NEXT_ACTIONS_R1.get(
                phase_r1,
                "Project already initialized — runtime untouched. "
                "Pipeline ready — use awf_dispatch_todo to start next iteration.",
            )
            return InitResult(
                project_name=project_name_val,
                project_dir=str(project_dir),
                stack="(preserved)",
                vision_path=str(vision_path) if vision_path else None,
                vision_excerpt=vision_excerpt,
                supervisor_md=supervisor_md,
                plan_md=plan_md,
                pipeline_configured=bool(config.get("default_pipeline")),
                next_action=next_action_r1,
                warnings=warnings,
                created_files=created_dirs,
            )

    if project_name is None:
        project_name = derive_project_name(project_dir)

    stack_info = detect_stack(project_dir)
    if test_cmd is None:
        test_cmd = stack_info["test_cmd"]
    if lint_cmd is None:
        lint_cmd = stack_info["lint_cmd"]
    if typecheck_cmd is None:
        typecheck_cmd = stack_info["typecheck_cmd"]
    if build_cmd is None:
        build_cmd = stack_info["build_cmd"]

    vision_path = paths.find_vision_file(project_dir)

    warnings: list[str] = []
    if vision_path is None:
        warnings.append("Vision/README not found in project root")

    if dry_run:
        # REPORTS33 (TODO-0207): the preview reports the same excerpt the
        # real init would return — one producer for every InitResult path.
        # The None case is already covered by the warning above.
        dry_excerpt, _reason = _read_vision_excerpt(vision_path)
        if _reason and vision_path is not None:
            warnings.append(_reason)
        return InitResult(
            project_name=project_name,
            project_dir=str(project_dir),
            stack=stack_info["stack"],
            vision_path=str(vision_path) if vision_path else None,
            vision_excerpt=dry_excerpt,
            supervisor_md="",
            plan_md="",
            pipeline_configured=False,
            next_action="[dry-run] no files written",
            warnings=warnings,
            created_files=[],
        )

    created_files: list[str] = []
    # Z1 (REVIEW виток 6): the same augmented plan as the force branch —
    # the docs surface preflights whenever the checkpoint seed would write.
    _refuse_symlinked_dirs(
        project_dir,
        _init_plan_dirs(project_dir),
        what="skeleton",
    )
    for d in _FRESH_DIRS:
        (agentic / d).mkdir(parents=True, exist_ok=True)
        created_files.append(f".agentic/{d}/")
    ensure_interlock_file(project_dir)

    config_content = _CONFIG_TEMPLATE.format(
        project_name=project_name,
        config_version=CONFIG_VERSION,
        test_cmd=test_cmd,
        lint_cmd=lint_cmd,
        typecheck_cmd=typecheck_cmd,
        build_cmd=build_cmd,
    )
    # TODO-0234 REVIEW (architect): API-layer writers share ONE barrier
    # (``managed_write_api_text``) so a refusal is an AwfApiError here too,
    # not a raw ManagedPathError leaking out of the API.
    _bootstrap_write(
        agentic / "config.yaml",
        config_content,
        operation="init config.yaml",
    )
    created_files.append(".agentic/config.yaml")

    # Copy supervisor.md template from the awf package. TODO-0230 (Y2):
    # the package directory is the single source — a checkout and a wheel
    # read the same path next to ``__file__``; there is no root mirror.
    templates_dir = awf_templates_dir()
    supervisor_template = templates_dir / "roles" / "supervisor.md"
    supervisor_dest = agentic / "roles" / "supervisor.md"
    if supervisor_template.is_file():
        # TODO-0234 REVIEW (architect): ``shutil.copy2`` wrote
        # ``.agentic/roles/supervisor.md`` outside the barrier — the same
        # surface the DONE table claimed covered via setup's writer. Route
        # the copy through the managed writer; mode stays the template's.
        _bootstrap_write(
            supervisor_dest,
            supervisor_template.read_text(encoding="utf-8"),
            operation="init supervisor.md",
            mode=supervisor_template.stat().st_mode & 0o777,
        )
        created_files.append(".agentic/roles/supervisor.md")
    else:
        warnings.append(f"supervisor.md template not found at {supervisor_template}")

    # W7: seed the built-in project doctrine (missing only, never
    # overwrite) — a new project starts with the core safety lessons.
    for name in _seed_doctrine(project_dir):
        created_files.append(f".agentic/doctrine/{name}")

    # W7 (TODO-0231 виток 5): the checkpoint template ships with the
    # project the same way the doctrine does — copied (missing only,
    # never overwritten) so the RUN-REPORT's path is a real file, not a
    # dead link to the installed package.
    for rel in _seed_checkpoint_template(project_dir):
        created_files.append(rel)

    # plan.md stub — point to vision if found (atomic per H5 invariant)
    if vision_path is not None:
        rel = Path("..") / ".." / vision_path.name
        plan_body = (
            f"# {project_name} — Plan\n\n"
            f"> Контекст проекта: прочитай `{rel}` перед планированием.\n\n"
            f"Steps:\n"
            f"- [ ] (supervisor заполнит после изучения vision)\n"
        )
    else:
        plan_body = (
            f"# {project_name} — Plan\n\n"
            f"> Vision/README не найден в корне проекта. Спроси пользователя "
            f"о контексте перед планированием.\n\n"
            f"Steps:\n"
            f"- [ ] (supervisor заполнит)\n"
        )
    plan_path = agentic / "phases" / "plan.md"
    _bootstrap_write(
        plan_path,
        plan_body,
        operation="init plan.md",
        mode=MODE_OWNER_EDITABLE,
    )
    created_files.append(".agentic/phases/plan.md")

    # M1: the canonical-suite helper ships with the project, so the role
    # instruction works outside this source tree.
    suite_template = templates_dir / "scripts" / "suite-canonical.sh"
    suite_dest = project_dir / "scripts" / "suite-canonical.sh"
    if suite_template.is_file():
        # lexists: a dangling symlink leaf is "already there" too — following
        # it would create the file OUTSIDE the project (QA F6b).
        if os.path.lexists(suite_dest) and not suite_dest.exists():
            # M6 (Q3): a dangling symlink is not a working helper — say so,
            # the role instruction points at it. Not touched: following it
            # would write outside the project.
            warnings.append(
                "scripts/suite-canonical.sh is a broken symlink/leaf — "
                f"not touched (template: {suite_template})"
            )
        elif os.path.lexists(suite_dest):
            # TODO-0230 (M6): a project may already own this helper (custom
            # stack, tuned flags). init copies only missing files — a
            # force-init must not clobber the owner's script.
            warnings.append(
                f"scripts/suite-canonical.sh already exists — kept as-is "
                f"(template: {suite_template})"
            )
        else:
            # REVIEW T2 (QA F6): the same bootstrap primitive as the rest of
            # the skeleton — a symlinked ``scripts/`` must refuse, not write
            # outside. ``root`` is the project: the leaf-derived anchor would
            # start at ``scripts/`` and never check it.
            # M6 REVIEW (architect): an unreadable template is a warning, not
            # an executable file with "(error reading ...)" as its body.
            try:
                suite_body = suite_template.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                warnings.append(
                    f"suite-canonical.sh template unreadable at "
                    f"{suite_template} — not copied: {exc}"
                )
            else:
                _bootstrap_write(
                    suite_dest,
                    suite_body,
                    operation="init suite-canonical.sh",
                    mode=0o755,
                    root=project_dir,
                )
                created_files.append("scripts/suite-canonical.sh")
    else:
        warnings.append(
            f"suite-canonical.sh template not found at {suite_template}"
        )

    update_gitignore(project_dir)

    supervisor_md_full = read_file_text(supervisor_dest) if supervisor_dest.is_file() else ""
    plan_md = read_file_text(plan_path)
    # REPORTS33 (TODO-0207): one producer for every branch — an unreadable
    # anchor degrades to "" + the honest line, not a placeholder in the
    # excerpt. The None case is already covered by the warning above.
    vision_excerpt, _reason = _read_vision_excerpt(vision_path)
    if _reason and vision_path is not None:
        warnings.append(_reason)

    # SMO: return compact phase prompt instead of full 639-line supervisor.md.
    # Dogfood finding: supervisor reads full supervisor.md and ignores phase system.
    # Compact prompt tells supervisor to call awf_current_step for phase guidance.
    try:
        from ..phase import detect_phase, get_phase_prompt
        phase = detect_phase(project_dir)
        supervisor_md = get_phase_prompt(phase, project_dir)
    except Exception:
        phase = "goal"
        supervisor_md = supervisor_md_full  # fallback to full on any error

    # SMO: next_action must match the detected phase — not hardcoded.
    # Dogfood #2: weak model followed next_action ("open form") instead of
    # phase prompt ("ask for goal"). Now both fields say the same thing.
    _NEXT_ACTIONS = {
        "goal": (
            "Спроси пользователя: «Какая цель на эту сессию?» "
            "(фича, багфик, аудит, рефакторинг). После ответа — awf_set_goal."
        ),
        "form": "Открой project-setup форму через awf_open_project_setup_form.",
        "normalize": "Выполни normalize checklist, затем awf_confirm_normalized.",
        "brief": "Изучи vision и план, напиши TODO-NNNN.md (awf_dispatch_todo) и запусти пайплайн.",
        "run": "Проверь awf_status, при необходимости dispatch_todo + awf_start.",
        "verify": "Проверь handoffs + git diff, реши ACK или REVIEW.",
        "done": "Pipeline завершён. Спроси пользователя о следующем шаге.",
        "init": (
            "Спроси пользователя: «Какая цель на эту сессию?» "
            "(фича, багфик, аудит, рефакторинг). После ответа — awf_set_goal."
        ),
    }
    next_action = _NEXT_ACTIONS.get(phase, _NEXT_ACTIONS["goal"])

    return InitResult(
        project_name=project_name,
        project_dir=str(project_dir),
        stack=stack_info["stack"],
        vision_path=str(vision_path) if vision_path else None,
        vision_excerpt=vision_excerpt,
        supervisor_md=supervisor_md,
        plan_md=plan_md,
        pipeline_configured=False,
        next_action=next_action,
        warnings=warnings,
        created_files=created_files,
    )


# ─── get_status ─────────────────────────────────────────────────────────


def _read_ack(inbox: Path, todo_id: str) -> str | None:
    """Read ACK decision from inbox, or None if no decision found.

    Returns the decision string (e.g. ``"decision: rollback"``) if found.
    Returns None if no ACK file exists, or if file lacks ``decision`` line.
    """
    ack_file = inbox / f"ACK-{todo_id}.ready"
    if not ack_file.exists():
        return None
    text = ack_file.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "decision" in line:
            return line.strip()
    return None


def _read_progress(outbox: Path, todo_id: str) -> dict[str, Any]:
    """Parse PROGRESS-{todo_id}.md for task counts and last entry."""
    progress_file = outbox / f"PROGRESS-{todo_id}.md"
    if not progress_file.exists():
        return {}
    text = progress_file.read_text(encoding="utf-8")
    task_lines = [line for line in text.splitlines() if line.startswith("## Task")]
    total = len(task_lines)
    done = sum(1 for ln in task_lines if "[x]" in ln)
    failed = sum(1 for ln in task_lines if "[!]" in ln)
    last = task_lines[-1] if task_lines else ""
    return {"total": total, "done": done, "failed": failed, "last": last}


def _count_done_blocked(inbox: Path, outbox: Path, done: Path | None = None) -> tuple[int, int, list[str]]:
    """Count DONE/BLOCKED TODOs. Returns (done_count, blocked_count, blocked_ids).

    DF6-4: also counts archived TODOs in done/ directory.
    """
    from ..signals import find_signal_file

    done_count = 0
    blocked_count = 0
    blocked_ids: list[str] = []

    if not inbox.exists():
        # DF6-4: even if inbox is empty, count done/ archives
        if done and done.is_dir():
            done_count = sum(1 for d in done.iterdir() if d.is_dir())
        return done_count, blocked_count, blocked_ids

    for ready_file in sorted(inbox.glob("TODO-*.ready")):
        if not ready_file.is_file():
            continue
        todo_id = ready_file.stem
        done_sig = find_signal_file(outbox, "DONE", todo_id, ".ready")
        blocked = find_signal_file(outbox, "BLOCKED", todo_id, ".ready")
        if done_sig:
            done_count += 1
        elif blocked:
            blocked_count += 1
            blocked_ids.append(todo_id)

    # DF6-4: count archived TODOs in done/ directory
    if done and done.is_dir():
        for d in done.iterdir():
            if d.is_dir():
                todo_id = d.name  # e.g. "TODO-0001"
                # Only count if not already counted from outbox (back-compat)
                done_sig = find_signal_file(outbox, "DONE", todo_id, ".ready")
                if not done_sig:
                    done_count += 1

    return done_count, blocked_count, blocked_ids


def blocked_closure_ids(project_dir: Path) -> list[str]:
    """The BLOCKED closures of the project — the counter every surface reads.

    ``awf_status`` and the run status both need this one fact; the second
    surface used to reach into the private counter and assemble the three
    paths itself, which is the "written next to" shape the shared-owner
    rule forbids. The derivation (inbox ready + outbox closure, done/
    fallback) stays in ``_count_done_blocked``; this is its public door.
    """
    _, _, blocked_ids = _count_done_blocked(
        paths.inbox(project_dir),
        paths.outbox(project_dir),
        paths.done_dir(project_dir),
    )
    return blocked_ids


def _run_brief(project_dir: Path) -> dict | None:
    """SPEC A-run: compact run state for the status payload (None when none)."""
    from .run import run_brief

    return run_brief(project_dir)


def restore_todo(project_dir: Path, todo_id: str) -> RestoreResult:
    """Bring an archived TODO back from done/{id}/ to the inbox (active again).

    NEG-2026-09-19 R2a safety net: the manual recovery path for TODOs
    archived without work. Restores TODO.md and re-creates .ready.
    PROGRESS/DONE reports stay in done/ as history — and so does the
    handoff tail (TODO-0191): the re-run's stages write their own
    handoffs, and the previous attempt's tail under the canonical
    ``{stage}-{todo_id}.md`` names in the live dir looked like fresh work
    to the next stage (resume from a later stage, the salvage prompt for
    a stage that died without writing its handoff). The tail is audit
    evidence — done/{id}/handoff/ is where it stays.
    """
    import re as _re
    import shutil

    if not _re.match(r"^TODO-\d{4,}$", todo_id or ""):
        raise AwfApiError(f"invalid todo_id {todo_id!r}, expected TODO-NNNN")
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    done_dir = paths.done_dir(project_dir) / todo_id
    if not done_dir.is_dir():
        raise AwfApiError(f"No archived TODO at done/{todo_id} — nothing to restore.")
    md = done_dir / "TODO.md"
    if not md.is_file():
        raise AwfApiError(f"done/{todo_id}/ has no TODO.md — nothing to restore.")

    inbox = paths.inbox(project_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    # A-12 (audit 2026-09-25): a restore must never overwrite an active
    # TODO — the archived copy and the live task are different units
    # sharing a number. Refuse before any move; both stay byte-for-byte.
    target = inbox / f"{todo_id}.md"
    if target.exists():
        raise AwfApiError(
            f"inbox/{todo_id}.md already exists — {todo_id} is active. "
            "Refusing to overwrite it with the archived copy."
        )
    shutil.move(str(md), str(target))
    (inbox / f"{todo_id}.ready").touch()

    # CTX2: a restored id is about to run again — the previous attempt's
    # CLOSER (first verdict/attempts) and CHECKLIST (verdict cache) must
    # not be inherited by the re-run. Same helper as the rollbacks.
    forget_unit_history(project_dir, todo_id)

    # TODO-0191 (REPORTS30): the handoff tail is NOT moved back to the
    # live .agentic/handoff/ — the bare shutil.move there silently
    # overwrote any live copy of the same name (the archive's A-12
    # "never overwrite" rule had no counterpart on the restore side) and
    # left the previous attempt's files under the canonical
    # {stage}-{todo_id}.md names, where the re-run's next stage read them
    # as fresh. The tail stays in the archive as audit evidence, exactly
    # like the PROGRESS/DONE reports above.
    handoff_kept = 0
    handoff_src = done_dir / "handoff"
    if handoff_src.is_dir():
        handoff_kept = sum(1 for f in handoff_src.iterdir() if f.is_file())

    remaining = sorted(p.name for p in done_dir.iterdir())
    if not remaining:
        try:
            done_dir.rmdir()
        except OSError:
            pass

    return RestoreResult(
        todo_id=todo_id,
        message=(
            f"{todo_id} restored to inbox (active)."
            + (
                f" Handoff history kept in done/{todo_id}/handoff/ "
                f"({handoff_kept} file(s)) — the re-run sees only its own handoffs."
                if handoff_kept
                else ""
            )
            + (f" History kept in done/{todo_id}/: {', '.join(remaining)}." if remaining else "")
        ),
    )


def _zombie_state_hint(project_dir: Path, state: dict[str, Any]) -> str | None:
    """TODO-0196: dead pipeline + stage state on disk + no decision signal
    for the unit → the resume hint. None when a signal lies (another
    surface owns that case) or the state names no unit."""
    todo_id = str(state.get("todo_id") or "")
    if not todo_id:
        return None
    outbox = paths.outbox(project_dir)
    inbox = paths.inbox(project_dir)
    for p in (
        outbox / f"DONE-{todo_id}.ready",
        outbox / f"BLOCKED-{todo_id}.ready",
        outbox / f"REVIEW-{todo_id}.md",
        inbox / f"APPROVE-{todo_id}.ready",
    ):
        if p.is_file():
            return None
    stage = str(state.get("stage_name") or "?")
    return (
        f"stage state lingers after engine death ({stage} for {todo_id}, "
        f"no signal) → awf_continue(todo_id='{todo_id}')"
    )


def get_status(project_dir: Path) -> StatusResult:
    """Get current workflow state — active TODOs, progress, blocked, conflicts."""
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)

    config_data = cfg_mod.load(project_dir)
    project_name = (
        cfg_mod.get(config_data, "project.name", "Project") or "Project"
    )

    active_ids = todos.list_active_todos(inbox, outbox)
    done_count, blocked_count, blocked_ids = _count_done_blocked(
        inbox, outbox, paths.done_dir(project_dir)
    )

    # TODO-0197 (REPORTS32 п.4): the "active" list shows ONLY the units of
    # the active run's queue; other active inbox units (ready to start
    # standalone / closing a previous cycle) are reported separately so
    # the run's queue is not mixed with foreign units.
    from ..run_state import read_run

    _run = read_run(project_dir)
    run_queue_ids: set[str] = set()
    if _run and _run.get("active"):
        for item in _run.get("queue") or []:
            tid = item.get("todo_id") if isinstance(item, dict) else str(item)
            if tid:
                run_queue_ids.add(tid)

    def _todo_entry(todo_id: str) -> dict[str, Any]:
        entry: dict[str, Any] = {"todo_id": todo_id}
        ack = _read_ack(inbox, todo_id)
        if ack is not None:
            entry["ack"] = ack
        progress = _read_progress(outbox, todo_id)
        entry["progress"] = progress if progress else None
        # TODO-0262: the review-pass budget of the unit — ``passes: N/max``
        # on awf_status/awf_brief and the explicit exhaustion flag.
        entry.update(budget_mod.status_fields(project_dir, todo_id, config_data))
        return entry

    active_todos_list: list[dict[str, Any]] = []
    standalone_todos_list: list[dict[str, Any]] = []
    for todo_id in active_ids:
        entry = _todo_entry(todo_id)
        if run_queue_ids and todo_id not in run_queue_ids:
            standalone_todos_list.append(entry)
        else:
            active_todos_list.append(entry)

    conflict_warning: str | None = None
    if len(active_ids) > 1:
        # RUN6 #3: inside an active run the queue is NORMAL — the items wait
        # for their turn and each todo_id is pinned by awf_run_next. The old
        # "stale — rollback or reset" text was a false alarm on every run
        # status call. Outside a run the old warning stands.
        if _run and _run.get("active"):
            conflict_warning = (
                f"Run queue: {len(active_ids)} items wait for a turn — "
                "normal in an active run (each TODO is pinned by "
                "awf_run_next)."
            )
        else:
            newest = active_ids[0]
            conflict_warning = (
                f"{len(active_ids)} active TODOs detected. "
                f"Orchestrator will run: {newest} (highest NNNN). "
                f"Others are stale — rollback or 'awf reset --orphans'."
            )

    suggestion: str | None = None
    if not active_ids and blocked_count == 0:
        # SMO: suggestion must be phase-aware — don't suggest "create TODO"
        # during early phases (goal/form/normalize/brief).
        try:
            from ..phase import detect_phase
            current_phase = detect_phase(project_dir)
        except Exception:
            current_phase = "unknown"

        _PHASE_SUGGESTIONS = {
            "goal": "Спроси пользователя о цели сессии → awf_set_goal.",
            "form": "Открой project-setup форму → awf_open_project_setup_form.",
            "normalize": "Выполни normalize checklist → awf_confirm_normalized.",
            "brief": "Напиши TODO-NNNN.md (awf_dispatch_todo) → .ready сигнал.",
            "init": "Спроси пользователя о цели сессии → awf_set_goal.",
        }
        if current_phase in _PHASE_SUGGESTIONS:
            suggestion = _PHASE_SUGGESTIONS[current_phase]
        elif current_phase in ("run", "verify", "done"):
            suggestion = (
                "No active tasks. Supervisor should create the next TODO, "
                "then run: awf start"
            )
        else:
            suggestion = "No active tasks. Check awf_current_step for guidance."

    # RUN3 #1: named pipelines — the active one (config) + how many exist.
    # Same rules as resolve_pipeline_file, so status never disagrees with
    # the file the engine would load.
    from ..pipeline import active_pipeline_name as _active_name
    from ..pipeline import list_pipeline_names as _list_names

    active_pipeline = _active_name(project_dir, config_data)
    pipeline_count = len(_list_names(project_dir))

    # REPORTS26 F6 (TODO-0166): workflow definitions not yet in git — the
    # dry-run list commit_workflow would commit (roles/pipelines/doctrine
    # added mid-project that read-only units never commit). Never raises:
    # a status call must not fall over on git.
    from .workflow_commit import uncommitted_workflow_files as _wf_files

    uncommitted_wf = _wf_files(project_dir)

    pipeline_running, pipeline_pid, log_tail = check_pipeline_running(project_dir)

    # Dogfood-2/6: stage visibility + expected action (only when pipeline running)
    current_stage_name: str | None = None
    next_stage_role: str | None = None
    last_signal: str | None = None
    current_stage_kind: str | None = None
    expected_action: str | None = None
    checkpoint_pending = False
    checkpoint_port: int | None = None
    checkpoint_form_url: str | None = None
    if pipeline_running:
        from .context import (
            _compute_expected_action,
            _compute_stage_kind,
            _extract_stage_info,
        )

        (
            current_stage_name,
            next_stage_role,
            last_signal,
            _log_tail_alt,
            checkpoint_pending,
            checkpoint_port,
            checkpoint_form_url,
        ) = _extract_stage_info(project_dir)
        log_tail = log_tail or _log_tail_alt
        current_stage_kind = _compute_stage_kind(project_dir, current_stage_name)
        expected_action = _compute_expected_action(
            pipeline_running=pipeline_running,
            current_stage_kind=current_stage_kind,
            checkpoint_pending=checkpoint_pending,
            has_active_todos=bool(active_ids),
            done_count=done_count,
            last_signal=last_signal,
        )

    # DF5-4: salvage state — read from state file even if pipeline not running
    # (orchestrator may have crashed after setting salvage_needed)
    salvage_needed = False
    salvage_stage: str | None = None
    from ..pipeline_state import read_state as _read_state
    _state = _read_state(project_dir)
    if _state:
        salvage_needed = bool(_state.get("salvage_needed", False))
        salvage_stage = _state.get("salvage_stage")
        salvage_attempt = _state.get("salvage_count") or 1
        # If state exists but pipeline not detected as running, enrich
        # stage info from state (crash recovery)
        if not pipeline_running:
            from .context import _compute_expected_action, _compute_stage_kind
            if not current_stage_name:
                current_stage_name = _state.get("stage_name")
            current_stage_kind = _compute_stage_kind(project_dir, current_stage_name)
        if salvage_needed:
            # B5 (dogfood-11): salvage overrides the computed action even when
            # the orchestrator process is alive — it waits for the supervisor,
            # it is NOT running a worker. The old text ("worker stage running —
            # auto-transition on worker DONE. Do NOT intervene") contradicted
            # salvage_needed: true.
            expected_action = (
                f"Salvage (attempt {salvage_attempt}): worker didn't signal. "
                f"Review git diff, then ACK, retry, or split the task."
            )

    # TODO-0241: the engine-death line — state says a stage runs, but the
    # PID is dead or the heartbeat is stale. Read-only; salvage owns its
    # own surface.
    #
    # The "no verdict while the resolver says running" rule lives in
    # status_engine_dead (the launch window would otherwise put
    # ``pipeline_running: True`` next to ``engine_dead``), the resume
    # wording in engine_dead_hint. Neither is rebuilt here.
    #
    # No ``expected_action`` prefill: the advice ORDER has one owner —
    # ``next_action_from_status`` (blocked above engine_dead; a BLOCKED
    # closure asks the owner to answer the worker, not to resume it). The
    # fields below carry the fact; the surfaces derive the sentence.
    engine_dead: str | None = None
    engine_dead_todo: str | None = None
    from .._engine_liveness import status_engine_dead

    # The salvage guard, the launch-window guard and the death verdict all
    # live in status_engine_dead — one owner, the same answer for run_status.
    _dead = status_engine_dead(
        project_dir, _state, config_data, pipeline_running=pipeline_running
    )
    if _dead is not None:
        engine_dead = _dead.line
        engine_dead_todo = _dead.todo_id or None

    # TODO-0262 REVIEW (CTX1): an exhausted unit reads its BUDGET note, not
    # a resume hint — the advice owner (next_action_from_status) ranks this
    # fact above engine_dead, so the surfaces never send the owner to
    # ``awf_continue`` for a unit that cannot continue. ONE computation for
    # the flag and the zombie guard: the budget fact has a single owner here.
    budget_exhausted_todo: str | None = None
    _budget_candidates: list[str] = []
    if _state:
        _state_todo = str(_state.get("todo_id") or "")
        if _state_todo:
            _budget_candidates.append(_state_todo)
    # The run-stop path (awf_run_next) stops BEFORE a launch, so no stage
    # state names the unit — the BUDGET note in the outbox is the durable
    # fact (budget.note_exists). Read it for the active units too, or the
    # status/brief would send the owner to awf_start for an exhausted unit.
    for _todo_id in active_ids:
        if _todo_id not in _budget_candidates:
            _budget_candidates.append(_todo_id)
    for _todo_id in _budget_candidates:
        if budget_mod.stop_active(project_dir, _todo_id, config_data):
            budget_exhausted_todo = _todo_id
            break

    # TODO-0196: zombie-state — the engine died with the stage state still
    # on disk and no signal for the unit: without the hint the run limps in
    # every status until someone notices. Salvage and a lying signal own
    # their own surfaces; the running pipeline is normal. The
    # engine-death line above names the same fact more precisely when it
    # has the pid, so this hint is the narrower fallback (single owner).
    # TODO-0262 REVIEW (CTX1): a budget-stopped unit is not a zombie to
    # resume — the budget flag above owns that answer.
    zombie_state: str | None = None
    if (
        _state
        and not pipeline_running
        and not salvage_needed
        and not engine_dead
        and not budget_exhausted_todo
    ):
        zombie_state = _zombie_state_hint(project_dir, _state)

    # REPORTS32 (TODO-0202): the "office" — who is on the team and on which
    # model, one call. Never raises: a status call must not fall over on a
    # malformed role file (the helper degrades per-role).
    from .roles import role_model_office

    try:
        office = role_model_office(project_dir)
    except Exception:  # noqa: BLE001 — status must never fall over
        office = []

    # TODO-0242 (F8): ``run_active`` and ``run_state`` come from ONE read —
    # a second read could disagree with the record the advice owner checks,
    # and the two fields are consumed together ("closed run ≠ no run").
    run_brief_state = _run_brief(project_dir)

    return StatusResult(
        project_name=project_name,
        active_todos=active_todos_list,
        done_count=done_count,
        blocked_count=blocked_count,
        blocked_ids=blocked_ids,
        conflict_warning=conflict_warning,
        suggestion=suggestion,
        pipeline_running=pipeline_running,
        pipeline_pid=pipeline_pid,
        log_tail=log_tail,
        current_stage_name=current_stage_name,
        next_stage_role=next_stage_role,
        last_signal=last_signal,
        current_stage_kind=current_stage_kind,
        expected_action=expected_action,
        checkpoint_pending=checkpoint_pending,
        checkpoint_port=checkpoint_port,
        checkpoint_form_url=checkpoint_form_url,
        salvage_needed=salvage_needed,
        salvage_stage=salvage_stage,
        zombie_state=zombie_state,
        engine_dead=engine_dead,
        engine_dead_todo=engine_dead_todo,
        budget_exhausted_todo=budget_exhausted_todo,
        active_pipeline=active_pipeline,
        pipeline_count=pipeline_count,
        run_state=run_brief_state,
        run_active=bool(run_brief_state and run_brief_state.get("active")),
        uncommitted_workflow_files=uncommitted_wf,
        standalone_todos=standalone_todos_list,
        office=office,
    )


# ─── get_report ─────────────────────────────────────────────────────────


def get_report(project_dir: Path) -> ReportResult:
    """Generate workflow report — task statuses, git diff, latest test log."""
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    done_d = paths.done_dir(project_dir)

    config_data = cfg_mod.load(project_dir)
    project_name = (
        cfg_mod.get(config_data, "project.name", "Project") or "Project"
    )

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    items: list[dict[str, str]] = []
    # KAUD-1: reuse _count_done_blocked for consistent counting with get_status
    done_count, blocked_count, _ = _count_done_blocked(inbox, outbox, done_d)

    # Build items list from active TODOs + archived TODOs
    if inbox.exists():
        from ..signals import find_signal_file

        for ready_file in sorted(inbox.glob("TODO-*.ready")):
            if not ready_file.is_file():
                continue
            todo_id = ready_file.stem
            done = find_signal_file(outbox, "DONE", todo_id, ".ready")
            blocked = find_signal_file(outbox, "BLOCKED", todo_id, ".ready")
            if done:
                items.append({"todo_id": todo_id, "status": "OK"})
            elif blocked:
                items.append({"todo_id": todo_id, "status": "BLK"})
            else:
                items.append({"todo_id": todo_id, "status": "..."})

    # KAUD-1: include archived TODOs in items
    if done_d.is_dir():
        for d in sorted(done_d.iterdir()):
            if d.is_dir():
                items.append({"todo_id": d.name, "status": "OK"})

    diff_result = subprocess.run(
        ["git", "diff", "--stat"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        timeout=30,
    )
    git_diff = diff_result.stdout if diff_result.returncode == 0 else ""

    # AUD15-07: an archived task carries its TEST-RESULTS log in done/{id}/ —
    # look there too, and read only the tail (the file can be many MB).
    latest_test_log_tail: str | None = None
    candidates: list[Path] = []
    if outbox.exists():
        candidates.extend(p for p in outbox.glob("TEST-RESULTS-*.log") if p.is_file())
    if done_d.is_dir():
        for d in sorted(done_d.iterdir()):
            if d.is_dir():
                candidates.extend(
                    p for p in d.glob("TEST-RESULTS-*.log") if p.is_file()
                )
    if candidates:
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        try:
            from .._log_reader import read_tail_lines

            lines = read_tail_lines(newest, max_lines=5)
            if lines:
                latest_test_log_tail = "\n".join(lines)
        except OSError:
            pass

    return ReportResult(
        project_name=project_name,
        generated_at=now,
        items=items,
        done_count=done_count,
        blocked_count=blocked_count,
        git_diff=git_diff,
        latest_test_log_tail=latest_test_log_tail,
    )


# ─── reset_runtime ──────────────────────────────────────────────────────


def reset_runtime(
    project_dir: Path,
    *,
    tasks_only: bool = False,
    full: bool = False,
    orphans: bool = False,
    confirm_digest: str | None = None,
) -> ResetResult:
    """Clean runtime data, refusing while a launch or destructive peer runs.

    R1.4 (требование 4): preflight и удаление — под ОДНОЙ критической
    секцией с захватом lease. Неблокирующий вход: если секция занята,
    запуск уже в полёте — это отказ, а не ожидание.

    R1.4 (F-R2): ``confirm_digest`` — digest плана из
    :func:`awf.api.preview_destruction`; при несовпадении (в т.ч. устаревшем
    плане) reset отвергается ДО удаления. ``None`` — legacy-путь.
    """
    project_dir = Path(project_dir).resolve()
    # ``destructive_section`` yields free for a project without ``.agentic``
    # (no runtime to protect, no lock file to create); ``_reset_runtime_impl``
    # then returns the no-op result — one decision, not two checks.
    with destructive_section(project_dir, label="reset"):
        return _reset_runtime_impl(
            project_dir,
            tasks_only=tasks_only,
            full=full,
            orphans=orphans,
            confirm_digest=confirm_digest,
        )


def _reset_runtime_impl(
    project_dir: Path,
    *,
    tasks_only: bool = False,
    full: bool = False,
    orphans: bool = False,
    confirm_digest: str | None = None,
) -> ResetResult:
    """Clean runtime data.

    Modes (mutually exclusive). Every mode also clears ``state/current.yaml``
    and (when present) ``state/run.yaml`` — a leftover state file is a stale
    "running" banner / a ghost run (AUD05-02):

    - ``tasks_only``: clean inbox + outbox.
    - ``full``: clean inbox/outbox/context/logs + handoff/inputs/dashboards
      (iteration artifacts).
    - ``orphans``: convenience mode — list + remove orphans in one call.
      Prefer :func:`list_orphans` + :func:`remove_orphans` two-step protocol
      when confirmation is needed.
    - default: clean inbox/outbox/context/logs (keep phases, handoff,
      inputs, dashboards).
    """
    project_dir = Path(project_dir).resolve()
    agentic = project_dir / ".agentic"
    if not agentic.is_dir():
        return ResetResult(cleaned_dirs=[], orphan_ids=[], mode="noop")

    # R1.4 (S9-02): destructive reset must not delete state a LIVE owner
    # owns. The preflight asks the same facts the launch path publishes —
    # live pipeline, live launch lease, reserved long action — and refuses
    # BEFORE the first unlink. A dead/stale owner yields no blocker, so the
    # ghost-run reconciliation below still happens (AUD05-02 preserved).
    _liveness.refuse_if_in_use(project_dir, label="reset")

    if orphans:
        # T1.6: use two-step protocol (list + remove) instead of legacy
        # _reset_orphans one-shot. Single computation path, no duplication.
        ids = list_orphans(project_dir)
        if not ids:
            return ResetResult(cleaned_dirs=[], orphan_ids=[], mode="orphans")
        return remove_orphans(project_dir, ids)

    from . import _destructive

    # F-R2: confirm BEFORE the first unlink, under the section.
    _destructive.verify_confirm(
        project_dir,
        "reset",
        confirm_digest,
        tasks_only=tasks_only,
        full=full,
    )

    mode = _destructive.reset_mode(tasks_only=tasks_only, full=full)
    dirs_to_clean = _destructive.reset_dirs(tasks_only=tasks_only, full=full)

    cleaned: list[str] = []
    partial_failure = False
    notes = ""

    # R1.1/S9-01: preflight the WHOLE plan before the first unlink. A
    # symlinked ``.agentic``, runtime directory, state directory or
    # fallback log aborts with zero side effects — the failure of the
    # LAST directory must not leave the FIRST already deleted.
    try:
        managed_fs.assert_managed_dir(agentic, root=project_dir)
        for d in dirs_to_clean:
            managed_fs.assert_managed_dir(agentic / d, root=project_dir)
        managed_fs.assert_managed_dir(agentic / "state", root=project_dir)
        # S9-03 fallback leaf — a symlinked worker-output.out is refused
        # here too, so a reset never resolves it through the link.
        managed_fs.assert_no_symlink_escape(
            agentic / "logs" / paths.WORKER_FALLBACK_LOG, root=project_dir
        )
        # State files are unlinked, not followed — a symlinked leaf aborts
        # the plan before any directory is cleaned. The pair comes from the
        # destructive preview's list (one source), not a second copy of the
        # same two names written next to it.
        for rel in _destructive.RESET_STATE_FILES:
            managed_fs.assert_no_symlink_escape(
                agentic / rel, root=project_dir
            )
    except managed_fs.ManagedPathError as exc:
        raise AwfApiError(f"reset refused — unsafe runtime path: {exc}") from exc

    for d in dirs_to_clean:
        dirpath = agentic / d
        if not dirpath.is_dir():
            continue
        try:
            for f in dirpath.iterdir():
                # The launch lease and the ExecutionId marker are not
                # runtime data — they are the current owner's claim and the
                # identity the child handshake checks (R1.1 reset must not
                # touch lock/lease; F-1 keeps the identity file in the same
                # class — it is rotated below, never left absent).
                if d == "logs" and f.name in (LEASE_NAME, EXECUTION_NAME):
                    continue
                # is_symlink() first: a symlinked entry is unlinked itself,
                # never followed (no rmtree through the link).
                if f.is_symlink() or f.is_file():
                    f.unlink()
                elif f.is_dir():
                    shutil.rmtree(f)
            cleaned.append(d)
        except OSError as exc:
            # Post-preflight I/O error: journal the applied subset and stop.
            # No rollback of what is already gone is promised; a repeat
            # finishes the leftover plan.
            partial_failure = True
            notes = f"partial: {d} ({type(exc).__name__}: {exc})"
            break

    # Clear pipeline state file so stale "running" doesn't persist
    if not partial_failure:
        from .. import pipeline_state

        state_cleared = False
        # R1.5 (arch-review): the path is spelled by its owner
        # (``paths.state_dir`` + ``pipeline_state.STATE_FILE_NAME``), not
        # re-written next to the owner — the class the iter-3 accessors
        # closed. Same string, one source.
        state_file = paths.state_dir(project_dir) / pipeline_state.STATE_FILE_NAME
        try:
            # R1.5: state files are removed through their owners (the Store
            # is the single writer), never by a raw unlink from the api layer.
            if state_file.is_file():
                pipeline_state.clear_state(project_dir)
                state_cleared = True
            # AUD05-02: a leftover state/run.yaml is a ghost run — it blocks
            # the next run_start with "Run already active". Clear it too.
            if run_state.read_run(project_dir) is not None:
                run_state.clear_run(project_dir)
                state_cleared = True
        except OSError as exc:
            partial_failure = True
            notes = (
                f"partial: state ({type(exc).__name__}: {exc})"
            )
        if state_cleared:
            cleaned.append("state")
        # R1.4 (S3-03): the Store mirror of the active run is cleared too,
        # but its run_generation counter is NOT — that is the durable
        # generation memory a stale close is compared against.
        try:
            from ..infra import state_store

            state_store.clear_active_run(project_dir)
        except Exception as exc:  # noqa: BLE001 — report, not crash a reset
            partial_failure = True
            notes = f"partial: state store ({type(exc).__name__}: {exc})"
        # F-1 (REVIEW-фикс 9): ROTATE the ExecutionId marker. Deleting it
        # (the pre-fix behavior — the wipe spared only LEASE_NAME) reopened
        # the "no marker = work" legacy rule for a crash-orphan of the
        # launch that wrote it; freezing the old value would not stop that
        # same orphan either (its id would still match). A fresh id refuses
        # every pre-reset orphan while the file stays present. Shared with
        # force-init (F-X) through _rotate_launch_identity — one locus.
        if not partial_failure:
            try:
                _rotate_launch_identity(project_dir)
            except Exception as exc:  # noqa: BLE001 — report, not crash a reset
                partial_failure = True
                notes = (
                    f"partial: execution marker ({type(exc).__name__}: {exc})"
                )

    # Regenerate dashboard so user sees clean state
    try:
        from .dashboard import generate_dashboard as _gen_dash
        _gen_dash(project_dir)
    except Exception:
        pass

    return ResetResult(
        cleaned_dirs=cleaned, orphan_ids=[], mode=mode,
        partial_failure=partial_failure, notes=notes,
    )


# ─── list_orphans / remove_orphans (two-step protocol) ──────────────────


def list_orphans(project_dir: Path) -> list[str]:
    """Return orphan TODO ids — active without progress.

    Pure read-only function. Use :func:`remove_orphans` to delete them,
    or :func:`reset_runtime` with ``orphans=True`` for the one-step legacy mode.

    TODO-0242 (B.9): a unit still in the NOT-STARTED region of an active
    run slot (main or service) is NOT an orphan to offer — ``remove_orphans``
    refuses it as a dangling queue reference. The list and the remover ask
    the same predicate (``run_state.queued_todo_slot``): a plan never names
    units the one-step ``reset --orphans`` would reject.
    """
    project_dir = Path(project_dir).resolve()
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    active_ids = todos.list_active_todos(inbox, outbox)
    return [
        tid
        for tid in active_ids
        if not todos.has_progress(outbox, tid)
        and not run_state.queued_todo_slot(project_dir, tid)
    ]


def remove_orphans(
    project_dir: Path, orphan_ids: list[str], *, confirm_digest: str | None = None
) -> ResetResult:
    """Remove specific orphan TODOs (pre-computed via :func:`list_orphans`).

    Use this two-step protocol when caller needs to confirm with user
    before deletion:

        ids = api.list_orphans(project_dir)
        if user_confirms(ids):
            api.remove_orphans(project_dir, ids)

    R1.4 (QA F3): the guard is the SAME destructive preflight reset uses
    (:func:`awf.api._liveness.destructive_blockers`) — a live pipeline, a
    live launch lease or a held Store reservation refuses. A second,
    narrower liveness guard would drift and leave one destructive call
    unprotected exactly where the other refuses.

    R1.4 (F-R2): ``confirm_digest`` — digest плана из
    :func:`awf.api.preview_destruction` (kind ``remove-orphans``); при
    несовпадении удаление отвергается. ``None`` — legacy-путь.
    """
    project_dir = Path(project_dir).resolve()
    # A project without ``.agentic`` yields free without a lock (see
    # ``destructive_section``); the impl below is safe on it.
    with destructive_section(project_dir, label="remove_orphans"):
        return _remove_orphans_impl(project_dir, orphan_ids, confirm_digest)


def _remove_orphans_impl(
    project_dir: Path, orphan_ids: list[str], confirm_digest: str | None = None
) -> ResetResult:
    # R1.1/S9-04: build the inbox via the canonical ``paths.inbox()``. The
    # root is already canonical and ``paths.agentic_dir`` appends ``.agentic``
    # WITHOUT resolving it, so a symlinked ``.agentic`` stays a visible
    # component and the lstat-based preflight below refuses it. The earlier
    # inline copy (and its claim that ``paths.inbox`` resolves the link) was
    # wrong on both counts — it just drifted from the path layout.
    inbox = paths.inbox(project_dir)
    # AUD05-06: a fresh worker may not have written PROGRESS yet, so its
    # TODO looks like an orphan. Refuse to delete while the project is in
    # use — wait for verify/salvage, or kill first (no force flag).
    # R1.4 (QA F3): the same preflight reset uses, not a second narrower one.
    _liveness.refuse_if_in_use(project_dir, label="remove_orphans")

    from . import _destructive

    # F-R2: confirm BEFORE the first unlink, under the section.
    _destructive.verify_confirm(
        project_dir, "remove-orphans", confirm_digest, orphan_ids=orphan_ids
    )

    # R1.1/S9-04: validate EVERY id and build EVERY path BEFORE the first
    # unlink. A traversal like ``../../../disposable`` (which previously
    # deleted an external .md) rejects the whole list, so a valid TODO
    # listed before an invalid one is NOT removed either.
    plan: list[tuple[str, Path, Path]] = []
    try:
        if os.path.lexists(inbox):
            managed_fs.assert_managed_dir(inbox, root=project_dir)
        for tid in orphan_ids:
            managed_fs.validate_todo_id(tid)
            ready = inbox / f"{tid}.ready"
            md = inbox / f"{tid}.md"
            plan.append((tid, ready, md))
    except managed_fs.ManagedPathError as exc:
        raise AwfApiError(f"remove_orphans refused — {exc}") from exc

    # TODO-0240 REVIEW Y2: the dangling-reference guard is the same fact for
    # every remover. An orphan with a stale ``.ready`` may still sit in the
    # NOT-STARTED region of an active run slot (main or service) — deleting
    # the file would leave the queue naming a unit that cannot launch.
    # Checked before the first unlink. The predicate lives with the queue it
    # reads (``run_state``), not in a sibling API module.
    held = [
        tid for tid in orphan_ids
        if run_state.queued_todo_slot(project_dir, tid)
    ]
    if held:
        raise AwfApiError(
            f"remove_orphans refused — {', '.join(held)} still in the active "
            "run queue — replace it first (awf_run_revise with {'from': ..., "
            "'to': ...}) or finish the run (a service slot has no run_revise: "
            "close it with awf_run_service_finish)."
        )

    removed: list[str] = []
    for tid, ready, md in plan:
        deleted_any = False
        # unlink never follows the final symlink — safe for a symlinked leaf.
        if ready.exists() or ready.is_symlink():
            ready.unlink()
            deleted_any = True
        if md.exists() or md.is_symlink():
            md.unlink()
            deleted_any = True
        if deleted_any:
            removed.append(tid)
    return ResetResult(cleaned_dirs=[], orphan_ids=removed, mode="orphans")


__all__ = [
    "init_project",
    "get_status",
    "get_report",
    "reset_runtime",
    "list_orphans",
    "remove_orphans",
    # AUD05-09: restore_todo was exported by awf.api but missing from the
    # module's own __all__ — the "star import of the declaring module"
    # contract was broken.
    "restore_todo",
]
