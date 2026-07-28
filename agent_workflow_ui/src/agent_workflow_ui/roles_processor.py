"""Process custom role save/delete requests from form submits.

Extracted from http_endpoint.py to keep SubmitHandler thin. This module
contains the business logic; the handler just delegates here after parsing
the form body.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from . import opencode_config as _oc
from .opencode_config import delete_custom_role, save_custom_role
from .state import get_project_dir

log = logging.getLogger(__name__)


def _project_roles_dir(project_dir: Path | None = None) -> Path | None:
    """Return <project_dir>/.agentic/roles/ if it exists, else None.

    When project_dir is None, falls back to cwd-based detection (back-compat
    for non-HOME launches).

    .. note::
        When opencode supports setting env vars for MCP subprocess, switch to
        ``AWF_PROJECT_DIR`` env var as primary source. Until then,
        agent-passed ``project_dir`` is canonical.
    """
    if project_dir is None:
        project_dir = get_project_dir()
    if project_dir is None:
        return None
    d = project_dir / ".agentic" / "roles"
    if d.is_dir():
        return d
    return None


def _copy_to_project(global_path: Path, filename: str, project_dir: Path | None = None) -> None:
    """Copy a role file from global store to project .agentic/roles/."""
    proj = _project_roles_dir(project_dir)
    if proj is None:
        return
    dest = proj / filename
    shutil.copy2(global_path, dest)
    log.info("Copied role %s to project %s", filename, proj)


def _delete_from_project(filename: str, project_dir: Path | None = None) -> None:
    """Delete a role file from project .agentic/roles/."""
    proj = _project_roles_dir(project_dir)
    if proj is None:
        return
    target = proj / filename
    if target.exists():
        target.unlink()
        log.info("Deleted role %s from project %s", filename, proj)


_DEFAULT_AGENT_IDS = {"worker", "reviewer", "tester"}


def _copy_existing_role_to_project(role_id: str, project_dir: Path | None = None) -> bool:
    """Copy an existing global role .md to project .agentic/roles/ if it exists.

    Returns True if copied, False otherwise (missing, default sentinel, or no project).
    """
    if not role_id or role_id == "default":
        return False
    src = _oc.GLOBAL_ROLES_DIR / f"{role_id}.md"
    if not src.exists():
        log.debug("Role %s not in global store, skip copy to project", role_id)
        return False
    _copy_to_project(src, src.name, project_dir=project_dir)
    return True


def process_role_saves(data: dict[str, Any], project_dir: Path | None = None) -> int:
    """Save custom agent .md and supervisor .md files if user requested.

    Reads form fields:
    - team_config (JSON string with [{type, agent, skill_content, save, ...}])
    - supervisor_content (string)
    - save_supervisor ("true" if checkbox checked)
    - supervisor_role (string id of selected existing supervisor variant)
    - agent[] (list of selected agent ids)

    Returns count of successfully saved/copied roles. Failures are logged and skipped.
    """
    saved = 0
    saved_agent_ids: set[str] = set()

    # Custom agents (from team_config JSON)
    team_json = data.get("team_config", "")
    if team_json:
        try:
            team = json.loads(team_json)
        except (json.JSONDecodeError, TypeError):
            team = []
        if isinstance(team, list):
            for member in team:
                if not isinstance(member, dict) or member.get("type") != "custom":
                    continue
                name = str(member.get("agent", "")).strip()
                content = str(member.get("skill_content", "")).strip()
                save_flag = member.get("save", False)
                if not (name and content and save_flag):
                    continue
                try:
                    saved_path = save_custom_role(name, content, role_type="agent")
                    slug = saved_path.stem
                    saved_agent_ids.add(slug)
                    log.info("Saved custom agent: %s", name)
                    _copy_to_project(saved_path, saved_path.name, project_dir=project_dir)
                    saved += 1
                except Exception as e:
                    log.error("Failed to save custom agent %s: %s", name, e)

    # Supervisor variant (new content + save_supervisor)
    sv_content = str(data.get("supervisor_content", "")).strip()
    save_sv = data.get("save_supervisor", "") == "true"
    if sv_content and save_sv:
        first_line = sv_content.split("\n", 1)[0]
        name = first_line.lstrip("# ").strip() or "custom"
        try:
            saved_path = save_custom_role(name, sv_content, role_type="supervisor")
            log.info("Saved custom supervisor: %s", name)
            _copy_to_project(saved_path, saved_path.name, project_dir=project_dir)
            saved += 1
        except Exception as e:
            log.error("Failed to save supervisor: %s", e)

    # BD-5-B: copy existing supervisor variant selected from dropdown
    if not sv_content:
        sv_role = str(data.get("supervisor_role", "")).strip()
        if _copy_existing_role_to_project(sv_role, project_dir=project_dir):
            saved += 1

    # BD-5-C: copy existing custom agents selected from agent[]
    agent_list = data.get("agent", [])
    if isinstance(agent_list, str):
        agent_list = [agent_list]
    for agent_id in agent_list:
        agent_id = str(agent_id).strip()
        if not agent_id:
            continue
        if agent_id in _DEFAULT_AGENT_IDS:
            continue
        if agent_id in saved_agent_ids:
            continue
        if _copy_existing_role_to_project(agent_id, project_dir=project_dir):
            saved += 1

    # BD-9: write pipeline.yaml from team order
    if team_json and project_dir:
        try:
            team = json.loads(team_json)
            if isinstance(team, list):
                from .pipelines_writer import write_pipeline

                written = write_pipeline(team, project_dir)
                if written:
                    log.info("Pipeline written to %s", written)
        except (json.JSONDecodeError, TypeError) as e:
            log.warning("Failed to parse team_config for pipeline write: %s", e)

    return saved


def process_role_deletions(data: dict[str, Any], project_dir: Path | None = None) -> int:
    """Delete custom role .md files if user requested.

    Reads form field:
    - delete_agent (comma-separated slug names)

    Returns count of successfully deleted roles. Missing files are not errors.
    """
    deleted = 0

    delete_str = str(data.get("delete_agent", "")).strip()
    if not delete_str:
        return 0

    for name in delete_str.split(","):
        name = name.strip()
        if not name:
            continue
        try:
            if delete_custom_role(name):
                log.info("Deleted custom role: %s", name)
                _delete_from_project(f"{name}.md", project_dir=project_dir)
                deleted += 1
        except Exception as e:
            log.error("Failed to delete %s: %s", name, e)

    return deleted
