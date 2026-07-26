"""Process custom role save/delete requests from form submits.

Extracted from http_endpoint.py to keep SubmitHandler thin. This module
contains the business logic; the handler just delegates here after parsing
the form body.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .opencode_config import delete_custom_role, save_custom_role

log = logging.getLogger(__name__)


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
                    save_custom_role(name, content, role_type="agent")
                    log.info("Saved custom agent: %s", name)
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
            save_custom_role(name, sv_content, role_type="supervisor")
            log.info("Saved custom supervisor: %s", name)
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
                deleted += 1
        except Exception as e:
            log.error("Failed to delete %s: %s", name, e)

    return deleted
