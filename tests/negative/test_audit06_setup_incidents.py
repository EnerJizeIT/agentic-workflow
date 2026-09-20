"""AUD06-07 + AUD06-12: project-setup form incidents (audit-fixes, FU-14).

S5: ``add_role`` used to silently overwrite an existing role file with the
placeholder template (``atomic_write_text`` on top, no existence check).
A hand-edited role was lost without a trace, a warning, or a backup —
unlike pipeline/config, which do get .bak backups.

S6: an empty-team submit (context only, "re-configure existing project
without changing team") left the project stuck in the ``form`` phase and
the warning did not explain why — no pipeline was written, so the phase
cannot advance, and nothing told the user that.
"""
from __future__ import annotations

import pytest

from awf import api
from awf.api._errors import AwfApiError


@pytest.fixture
def awf_project(tmp_git_repo):
    """Project with .agentic/ initialized (config.yaml + roles/)."""
    api.init_project(tmp_git_repo, project_name="SetupIncidents")
    return tmp_git_repo


class TestAddRoleOverwrite:
    """AUD06-07: an existing role file is user data, not a template slot."""

    def test_existing_role_file_not_overwritten(self, awf_project):
        role_file = awf_project / ".agentic" / "roles" / "developer.md"
        original = "# developer\n\nHand-edited content, keep it.\n"
        role_file.write_text(original, encoding="utf-8")

        with pytest.raises(AwfApiError, match="already exists"):
            api.add_role(awf_project, "developer")

        # the hand-edited role survives byte-for-byte
        assert role_file.read_text(encoding="utf-8") == original

    def test_error_points_to_editing_the_file(self, awf_project):
        """The error must say what to do instead of 'role already exists' alone."""
        (awf_project / ".agentic" / "roles" / "qa.md").write_text("# qa\n", encoding="utf-8")
        with pytest.raises(AwfApiError, match="edit"):
            api.add_role(awf_project, "qa")

    def test_new_role_still_created(self, awf_project):
        """The guard only blocks EXISTING files — fresh roles work."""
        result = api.add_role(awf_project, "brand-new")
        assert (awf_project / ".agentic" / "roles" / "brand-new.md").is_file()
        assert result.role_name == "brand-new"


class TestEmptyTeamPhaseStuck:
    """AUD06-12: empty team + context — the warning must explain the consequence.

    Before the fix the warning was "team is empty — pipeline + role mapping
    skipped": true but useless — it did not say the phase stays in 'form'
    and no pipeline file exists for the supervisor to run.
    """

    def test_warning_explains_stuck_phase(self, awf_project):
        from awf import phase
        from awf.pipeline_state import write_state

        write_state(awf_project, goal="test feature")
        assert phase.detect_phase(awf_project) == "form"

        result = api.apply_project_setup(
            awf_project, team=[], context_message="ctx"
        )

        # still stuck in form — nothing created a pipeline
        assert phase.detect_phase(awf_project) == "form"
        assert result.pipeline_file is None

        warning = next(w for w in result.warnings if "team is empty" in w)
        # the user sees the reason: no pipeline, phase stays 'form'
        assert "pipeline" in warning
        assert "form" in warning

    def test_warning_is_first_warning(self, awf_project):
        """Back-compat: existing callers read warnings[0] for the empty-team case."""
        result = api.apply_project_setup(
            awf_project, team=[], context_message="x"
        )
        assert "team is empty" in result.warnings[0]
