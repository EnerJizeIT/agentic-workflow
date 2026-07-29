"""BD-16/29: per-role pipeline contracts (generic only — no role registry)."""
from __future__ import annotations

from awf.skills_contract import (
    RoleContract,
    get_role_contract,
    render_pipeline_contract,
)


class TestRoleContractGeneric:
    """BD-29: every role gets the generic contract — no role-specific rules."""

    def test_unknown_role_gets_generic_contract(self) -> None:
        c = get_role_contract("totally-made-up-role")
        assert isinstance(c, RoleContract)
        assert c.zone
        assert c.receives_hint
        assert c.produces_hint
        assert c.out_of_scope

    def test_well_known_roles_also_get_generic(self) -> None:
        """No special-casing — every role identical contract."""
        roles = ("system-analysis", "developer", "qa", "project-auditor",
                 "reviewer", "tester", "worker", "data-scientist", "architect")
        contracts = {r: get_role_contract(r) for r in roles}
        # All identical — same dataclass instance (frozen)
        first = contracts[roles[0]]
        for r in roles[1:]:
            assert contracts[r] is first, f"{r} got different contract"


class TestRenderPipelineContract:
    def test_first_stage_no_prev_role(self) -> None:
        md = render_pipeline_contract("data-scientist", 1, 4, prev_role=None, next_role="qa")
        assert "Stage 1 of 4" in md
        assert "supervisor plan" in md
        assert "you are the first agent stage" in md
        assert "`qa`" in md

    def test_last_stage_no_next_role(self) -> None:
        md = render_pipeline_contract("qa", 4, 4, prev_role="developer", next_role=None)
        assert "Stage 4 of 4" in md
        assert "`developer`" in md
        assert "supervisor verify" in md
        assert "you are the last agent stage" in md

    def test_middle_stage_has_both_neighbors(self) -> None:
        md = render_pipeline_contract("developer", 2, 4, prev_role="system-analyst", next_role="qa")
        assert "Stage 2 of 4" in md
        assert "Receives from:** `system-analyst`" in md
        assert "Produces for:** `qa`" in md

    def test_zone_and_out_of_scope_present(self) -> None:
        md = render_pipeline_contract("qa", 3, 4, prev_role="developer", next_role="project-auditor")
        assert "Zone of responsibility" in md
        assert "Out of scope" in md

    def test_handoff_instruction_for_role(self) -> None:
        """The contract tells the role to write PROGRESS/DONE — orchestrator compiles handoff."""
        md = render_pipeline_contract("developer", 2, 4, prev_role="system-analyst", next_role="qa")
        assert "PROGRESS-{TODO-ID}.md" in md
        assert "DONE-{TODO-ID}.md" in md
        assert "do NOT write handoff files yourself" in md
        assert ".agentic/handoff/developer.md" not in md
