"""TODO-0348 (IMP5): the card's budget, wait rule and full phase prompt.

Keeps the contract's verify filename; the entry/situation and verify-fact
halves live in ``test_brief_card_entry.py`` / ``test_brief_card_verify.py``
(the ratchet splits any file over 400 lines).
"""
from __future__ import annotations

from awf import api
from awf.brief import (
    MAX_WORDS,
    WAIT_RULE_REACTIVE,
    WAIT_RULE_RUN,
    build_brief,
    render_brief,
)
from awf.phase import get_phase_prompt

from .brief_card_support import (
    heavy_project,
    heavy_status,
    mask_date,
    project,
    record,
    status,
)

# ─── Waiting rule block ──────────────────────────────────────────────────


class TestWaitBlock:
    def test_reactive_rule_has_no_loop(self, tmp_path):
        r = build_brief(project(tmp_path), status=status())
        assert r.wait["rule"] == WAIT_RULE_REACTIVE
        assert r.wait["loop"] is False
        assert r.wait["tool"] == ""
        assert not r.wait["timeout"]

    def test_run_rule_loops(self, tmp_path):
        r = build_brief(
            project(tmp_path),
            status=status(pipeline_running=True, expected_action="wait"),
            run_record=record(active=True),
        )
        assert r.wait["rule"] == WAIT_RULE_RUN
        assert r.wait["loop"] is True
        assert r.wait["tool"] == "awf_wait_for_event"
        assert isinstance(r.wait["timeout"], int)

    def test_card_prints_the_wait_rule(self, tmp_path):
        r = build_brief(project(tmp_path), status=status(), run_record=record(active=True))
        assert WAIT_RULE_RUN in r.text or WAIT_RULE_RUN in r.next_action

    def test_run_rule_is_printed_once_with_its_timeout(self, tmp_path):
        """AUD8: the bullet used to read ``- wait: wait only inside …``."""
        r = build_brief(project(tmp_path), status=status(), run_record=record(active=True))
        line = next(ln for ln in r.text.splitlines() if WAIT_RULE_RUN in ln)
        assert not line.startswith("- wait:")
        assert f"~{r.wait['timeout']}s" in line


# ─── Full phase prompt (AC48/AC49) ───────────────────────────────────────


class TestPhasePrompt:
    def test_field_is_the_full_prompt(self, tmp_path):
        root = project(tmp_path)
        r = api.brief(root)
        assert r.phase_prompt
        assert r.phase_prompt == get_phase_prompt(r.phase, root)
        assert r.phase_prompt.splitlines() == get_phase_prompt(r.phase, root).splitlines()
        assert "awf_current_step" not in r.phase_prompt

    def test_empty_project_has_no_prompt(self, tmp_path):
        r = api.brief(tmp_path / "fresh")
        assert r.situation == "S0"
        assert r.phase_prompt == ""

    def test_setup_project_with_agentic_also_has_no_prompt(self, tmp_path):
        """D3: S0 is the setup chain even when ``.agentic/`` exists.

        The review-fix split dropped this regression: a project initialized
        but without work history used to carry the setup prompt in
        ``phase_prompt`` (the gate is the situation, not ``has_agentic``).
        """
        root = tmp_path / "setup"
        (root / ".agentic" / "state").mkdir(parents=True)
        r = api.brief(root)
        assert r.situation == "S0"
        assert r.phase_prompt == ""

    def test_card_points_at_the_field(self, tmp_path):
        r = api.brief(project(tmp_path))
        assert "field `phase_prompt`" in r.text
        assert "awf_current_step" not in r.text
        assert len(r.text.split()) <= MAX_WORDS


# ─── Budget and determinism (AC30–AC33) ──────────────────────────────────


class TestBudgetAndDeterminism:
    def test_heavy_state_under_budget(self, tmp_path):
        r = build_brief(heavy_project(tmp_path), status=heavy_status())
        assert len(r.text.split()) <= MAX_WORDS
        assert r.budget["limit"] == MAX_WORDS
        assert r.budget["words"] == len(r.text.split())
        for header in ("## What's next", "## State", "## Tool map"):
            assert header in r.text
        assert r.next_action in r.text or r.next_action.split()[0] in r.text
        assert "verify:" in r.text

    def test_clipped_sections_are_named(self, tmp_path):
        r = build_brief(heavy_project(tmp_path), status=heavy_status())
        if r.budget["clipped"]:
            assert r.budget["pointer"]
            for section in r.budget["clipped"]:
                assert section in r.budget["pointer"]
            assert "awf brief --json" in r.text

    def test_budget_drops_verbose_sections_not_state(self, tmp_path):
        r = build_brief(heavy_project(tmp_path), status=heavy_status())
        r.what_new = "лишнее слово " * 700
        text = render_brief(r)
        assert len(text.split()) <= MAX_WORDS
        assert "What's new" in r.budget["clipped"]
        assert "awf brief --json" in text
        # D9: the unclipped surfaces survive the cut.
        assert "## State" in text
        assert "## Tool map" in text
        assert r.next_action.split()[0] in text
        assert "verify:" in text

    def test_two_calls_are_identical(self, tmp_path):
        root = heavy_project(tmp_path)
        first = build_brief(root, status=heavy_status())
        second = build_brief(root, status=heavy_status())
        assert mask_date(first.text) == mask_date(second.text)
        assert first.budget == second.budget

    def test_empty_project_does_not_crash(self, tmp_path):
        r = build_brief(tmp_path / "fresh")
        assert r.situation == "S0"
        assert len(r.text.split()) <= MAX_WORDS
        assert r.entry["single"] is True

    def test_pointer_counts_towards_the_limit(self, tmp_path):
        """AUD4: the pointer was appended after the cut loop (909/900)."""
        root = project(tmp_path)
        clipped_seen = 0
        for extra in range(600, 1000, 10):
            r = build_brief(
                root,
                status=status(pipeline_running=True, expected_action="wait"),
            )
            r.what_new = "word " * extra
            text = render_brief(r)
            assert len(text.split()) <= MAX_WORDS, (extra, len(text.split()))
            assert r.budget["words"] == len(text.split())
            clipped_seen += bool(r.budget["clipped"])
        assert clipped_seen, "the sweep must actually exercise the cut"

    def test_entry_is_a_copy_not_the_module_constant(self, tmp_path):
        """AUD6: ``dict(ENTRY_FIELD)`` shared the ``recall_when`` list.

        QA: the split called ``project(tmp_path)`` twice — the second call
        hit ``FileExistsError``, so the audit regression was red in the tree.
        """
        from awf.brief import ENTRY_FIELD

        root = project(tmp_path)
        r = build_brief(root, status=status())
        r.entry["recall_when"].append("MUTATED")
        assert "MUTATED" not in ENTRY_FIELD["recall_when"]
        assert "MUTATED" not in build_brief(root, status=status()).entry["recall_when"]
