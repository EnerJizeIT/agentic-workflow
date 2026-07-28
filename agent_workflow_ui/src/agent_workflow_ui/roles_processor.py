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

from .opencode_config import delete_custom_role, save_custom_role
from .state import get_project_dir

log = logging.getLogger(__name__)


def _project_roles_dir() -> Path | None:
    """Return <project_dir>/.agentic/roles/ if it exists, else None."""
    project_dir = get_project_dir()
    if project_dir is None:
        return None
    d = project_dir / ".agentic" / "roles"
    if d.is_dir():
        return d
    return None


def _copy_to_project(global_path: Path, filename: str) -> None:
    """Copy a role file from global store to project .agentic/roles/."""
    proj = _project_roles_dir()
    if proj is None:
        return
    dest = proj / filename
    shutil.copy2(global_path, dest)
    log.info("Copied role %s to project %s", filename, proj)


def _delete_from_project(filename: str) -> None:
    """Delete a role file from project .agentic/roles/."""
    proj = _project_roles_dir()
    if proj is None:
        return
    target = proj / filename
    if target.exists():
        target.unlink()
        log.info("Deleted role %s from project %s", filename, proj)


def process_role_saves(data: dict[str, Any]) -> int:
    """Save custom agent .md and supervisor .md files if user requested.

    Reads form fields:
    - team_config (JSON string with [{type, agent, skill_content, save, ...}])
    - supervisor_content (string)
    - save_supervisor ("true" if checkbox checked)

    Returns count of successfully saved roles. Failures are logged and skipped.
    """
    saved = 0

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
                    log.info("Saved custom agent: %s", name)
                    _copy_to_project(saved_path, saved_path.name)
                    saved += 1
                except Exception as e:
                    log.error("Failed to save custom agent %s: %s", name, e)

    # Supervisor variant (from supervisor_content + save_supervisor)
    sv_content = str(data.get("supervisor_content", "")).strip()
    save_sv = data.get("save_supervisor", "") == "true"
    if sv_content and save_sv:
        # Derive role name from the first markdown heading, fallback to "custom".
        first_line = sv_content.split("\n", 1)[0]
        name = first_line.lstrip("# ").strip() or "custom"
        try:
            saved_path = save_custom_role(name, sv_content, role_type="supervisor")
            log.info("Saved custom supervisor: %s", name)
            _copy_to_project(saved_path, saved_path.name)
            saved += 1
        except Exception as e:
            log.error("Failed to save supervisor: %s", e)

    return saved


def process_role_deletions(data: dict[str, Any]) -> int:
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
                _delete_from_project(f"{name}.md")
                deleted += 1
        except Exception as e:
            log.error("Failed to delete %s: %s", name, e)

    return deleted
