"""BD-16: per-role pipeline contracts."""
from __future__ import annotations

from awf.skills_contract import (
    DEFAULT_ROLE_CONTRACTS,
    RoleContract,
    get_role_contract,
    render_pipeline_contract,
)


class TestRoleContractRegistry:
    def test_well_known_roles_have_contracts(self) -> None:
        for slug in ("system-analysis", "developer", "qa", "project-auditor", "reviewer", "tester", "worker"):
            assert slug in DEFAULT_ROLE_CONTRACTS, f"{slug} missing from registry"
            c = DEFAULT_ROLE_CONTRACTS[slug]
            assert c.zone
            assert c.receives_hint
            assert c.produces_hint
            assert c.out_of_scope

    def test_get_role_contract_known(self) -> None:
        c = get_role_contract("developer")
        assert "Implement" in c.zone or "implement" in c.zone

    def test_get_role_contract_unknown_falls_back_to_generic(self) -> None:
        c = get_role_contract("totally-made-up-role")
        assert isinstance(c, RoleContract)
        # Generic contract still has the essentials
        assert c.zone
        assert c.receives_hint
        assert c.produces_hint
        # And the fallback's out_of_scope mentions "next role"
        assert any("next role" in item or "previous" in item for item in c.out_of_scope)


class TestRenderPipelineContract:
    def test_first_stage_no_prev_role(self) -> None:
        md = render_pipeline_contract("system-analysis", 1, 4, prev_role=None, next_role="developer")
        assert "Stage 1 of 4" in md
        assert "supervisor TODO" in md
        assert "you are the first agent stage" in md
        assert "`developer`" in md

    def test_last_stage_no_next_role(self) -> None:
        md = render_pipeline_contract("project-auditor", 4, 4, prev_role="qa", next_role=None)
        assert "Stage 4 of 4" in md
        assert "`qa`" in md
        assert "supervisor verify" in md
        assert "you are the last agent stage" in md

    def test_middle_stage_has_both_neighbors(self) -> None:
        md = render_pipeline_contract("developer", 2, 4, prev_role="system-analysis", next_role="qa")
        assert "Stage 2 of 4" in md
        assert "Receives from:** `system-analysis`" in md
        assert "Produces for:** `qa`" in md

    def test_zone_and_out_of_scope_present(self) -> None:
        md = render_pipeline_contract("qa", 3, 4, prev_role="developer", next_role="project-auditor")
        assert "Zone of responsibility" in md
        assert "Out of scope" in md
        # qa must not implement new features
        assert any("New features" in line for line in md.split("\n"))

    def test_handoff_instruction_for_role(self) -> None:
        """The contract tells the role to write PROGRESS/DONE — orchestrator compiles handoff."""
        md = render_pipeline_contract("developer", 2, 4, prev_role="system-analysis", next_role="qa")
        # Should mention PROGRESS/DONE pattern, NOT instruct agent to write handoff itself
        assert "PROGRESS-{TODO-ID}.md" in md
        assert "DONE-{TODO-ID}.md" in md
        assert "do NOT write handoff files yourself" in md
        # Stale instruction (write handoff to <role>.md) must be gone
        assert ".agentic/handoff/developer.md" not in md

    def test_unknown_role_uses_generic_contract(self) -> None:
        md = render_pipeline_contract("custom-role", 2, 3, prev_role="worker", next_role="reviewer")
        # Generic contract is used but pipeline position is still rendered
        assert "Stage 2 of 3" in md
        assert "`worker`" in md
        assert "`reviewer`" in md
        # Generic zone text
        assert "your part" in md.lower() or "your role" in md.lower()

    def test_system_analysis_out_of_scope_mentions_no_code(self) -> None:
        """Critical: system-analysis must NOT write production code."""
        md = render_pipeline_contract("system-analysis", 1, 4, prev_role=None, next_role="developer")
        # Look at out-of-scope items
        assert "production code" in md.lower()
