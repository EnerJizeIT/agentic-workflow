"""S1 (TODO-0288): the spec author roles ship with awf.

Before this unit a clean project could not materialize the shipped ``spec``
composition: ``agent-system-analyst``, ``agent-requirements-reviewer`` and
``agent-architector`` existed only as skills on the author's machine, so
``awf add-role`` wrote an empty placeholder and the setup form called the
variant unavailable. The three roles now ship as package role templates and
are reachable through both paths.
"""
from __future__ import annotations

from pathlib import Path

from awf import api
from awf.api import roles, setup
from tests.spec_support import init_project_repo

SPEC_ROLES = (
    "agent-system-analyst",
    "agent-requirements-reviewer",
    "agent-architector",
)


def test_spec_role_templates_exist_and_are_registered():
    for role in SPEC_ROLES:
        assert roles._builtin_role_template(role) is not None, role
        assert setup._builtin_role_template(role) is not None, role


def test_add_role_installs_the_template_not_a_placeholder(tmp_path: Path):
    repo = init_project_repo(tmp_path)

    for role in SPEC_ROLES:
        result = api.add_role(repo, role)
        text = Path(result.role_file).read_text(encoding="utf-8")
        assert "new role" not in text, f"{role}: placeholder instead of a protocol"
        assert f"# ROLE: {role}" in text


def test_clean_project_spec_variant_has_no_missing_roles(tmp_path: Path):
    repo = init_project_repo(tmp_path)

    assert setup.variant_unavailable_roles("spec", repo) == []


def test_setup_materializes_the_spec_roles_from_the_package(tmp_path: Path):
    repo = init_project_repo(tmp_path)

    result = api.apply_project_setup(repo, team=[], flow_variant="spec")

    assert result.pipeline_file is not None, result.warnings
    roles_dir = repo / ".agentic" / "roles"
    for role in setup._stage_roles(setup._variant_composition("spec")[0]):
        assert (roles_dir / f"{role}.md").is_file(), (
            f"setup did not materialize {role}"
        )
