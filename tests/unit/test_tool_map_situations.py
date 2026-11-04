"""TODO-0347 (IMP4): the tool map by situation — data and selection.

The card used to print a wall of all 64 records. D8 splits the two jobs:
`tool_map.yaml` is the single data source, `situational_tools` picks the
≤5 tools of one situation (the advice tool first, `when` as the `why`),
and `inventory_only` records stay in the full list but never in the row.
`map_coverage` keeps both directions of the map/registry check.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api
from awf.brief import (
    SITUATION_ADVICE,
    SITUATION_ALIASES,
    load_tool_map,
    map_coverage,
    situational_tools,
    tool_registry,
)

SITUATIONS = (
    "S0",
    "S1",
    "S1a",
    "S2",
    "S3",
    "S4",
    "S5",
    "S6",
    "S7",
    "S8",
    "S9",
    "S10",
    "S11",
    "S11a",
    "S11b",
    "S12",
    "S13",
)


def _entries() -> list[dict]:
    return [t for g in load_tool_map() for t in g["tools"]]


def _situation_names(situation: str) -> list[str]:
    return [t["tool"] for t in situational_tools(situation)]


# ─── Data shape ──────────────────────────────────────────────────────────


class TestMapData:
    def test_full_inventory_keeps_every_record(self):
        """AC20: the full list is still the 66 records / 8 groups."""
        groups = load_tool_map()
        assert len(groups) == 8
        assert len(_entries()) == 66

    def test_every_entry_has_a_non_empty_why(self):
        """INV3: `when` is the row's `why`; an empty one is a red test."""
        for entry in _entries():
            assert entry["when"].strip(), (
                f"empty 'when' in {entry['mcp'] or entry['cli']}"
            )
            assert entry["mcp"] or entry["cli"], "entry needs mcp or cli name"

    def test_advice_table_covers_every_situation(self):
        for situation in SITUATIONS:
            assert situation in SITUATION_ADVICE, f"{situation} has no advice"
            assert SITUATION_ADVICE[situation].strip()

    def test_s1a_row_exists(self):
        """K2/RR52: the foreign-commit row is data, not a lost branch."""
        assert "S1a" in SITUATION_ADVICE
        assert _situation_names("S1a"), "S1a selects no tools"

    def test_group_situations_are_the_union_of_selectable_entries(self):
        """D8: the group claims exactly the situations it can show rows for.

        Inventory-only entries keep their `situations` (the row they would
        serve) but must not put a heading above an empty row.
        """
        for group in load_tool_map():
            union = {
                s
                for t in group["tools"]
                if not t["inventory_only"]
                for s in t["situations"]
            }
            assert set(group["situations"]) == union, group["title"]

    def test_helpers_are_inventory_only(self):
        """AC20: helper'ы не выводятся, но есть в перечне."""
        helpers = [g for g in load_tool_map() if g["title"] == "helpers"][0]
        assert helpers["hidden"] is True
        for entry in helpers["tools"]:
            assert entry["inventory_only"] is True
            assert entry["situations"] == []

    def test_advice_tool_is_one_of_its_row_entries(self):
        """AC19: the reorder leads only with a tool the row contains.

        A row without its advice tool would silently keep the file order
        first — the card would name one tool and print another.
        """
        for situation, advice in SITUATION_ADVICE.items():
            assert advice in _situation_names(situation), (
                f"{situation}: advice {advice!r} has no entry in the row"
            )

    def test_every_row_has_at_most_five_candidates(self):
        """The cut must not be what decides the row.

        Six candidates would mean the sixth is dropped by ``[:5]`` as a
        side effect of file order, not because the data says so.
        """
        for situation in SITUATIONS:
            candidates = [
                t
                for g in load_tool_map()
                for t in g["tools"]
                if not t["inventory_only"] and situation in t["situations"]
            ]
            assert len(candidates) <= 5, (
                f"{situation}: {len(candidates)} candidates — "
                "mark one inventory_only or drop it"
            )

    def test_each_tool_appears_exactly_once(self):
        """The file header's promise: one entry per registry tool.

        A duplicate would print twice in a row — ``situational_tools``
        hides it with a dedupe, so the data has to be checked here.
        """
        keys = [(t["mcp"], t["cli"]) for g in load_tool_map() for t in g["tools"]]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        assert duplicates == [], duplicates


# ─── Selection ───────────────────────────────────────────────────────────


class TestSituationalTools:
    @pytest.mark.parametrize("situation", SITUATIONS)
    def test_at_most_five_tools(self, situation):
        assert len(situational_tools(situation)) <= 5

    @pytest.mark.parametrize("situation", SITUATIONS)
    def test_advice_tool_goes_first(self, situation):
        tools = _situation_names(situation)
        assert tools, f"{situation} selects no tools"
        assert tools[0] == SITUATION_ADVICE[situation], (
            f"{situation}: first tool {tools[0]!r} is not the advice "
            f"{SITUATION_ADVICE[situation]!r}"
        )

    @pytest.mark.parametrize("situation", SITUATIONS)
    def test_every_row_line_has_a_why(self, situation):
        for entry in situational_tools(situation):
            assert entry["why"].strip(), (
                f"{situation}: empty why for {entry['tool']}"
            )

    def test_inventory_only_never_selected(self):
        inventory = {
            t["mcp"] or f"awf {t['cli']}"
            for t in _entries()
            if t["inventory_only"]
        }
        for situation in SITUATIONS:
            assert not (set(_situation_names(situation)) & inventory), situation

    def test_coarse_advice_names_resolve_to_codes(self):
        """The TODO's vocabulary: setup / salvage / blocked / run /
        verify / hygiene — same rows as their S-codes."""
        for alias, code in SITUATION_ALIASES.items():
            assert situational_tools(alias) == situational_tools(code)

    def test_unknown_situation_is_empty(self):
        assert situational_tools("S99") == []

    def test_explicit_advice_tool_wins(self):
        """The card passes the advice it just computed; that tool leads."""
        tools = _situation_names("S8")
        assert tools[0] == "awf_approve"
        assert situational_tools("S8", "awf_verify_pack")[0]["tool"] == (
            "awf_verify_pack"
        )


# ─── Coverage ────────────────────────────────────────────────────────────


class TestCoverage:
    def test_every_registry_tool_is_described(self):
        """INV3: map and registry agree in both directions."""
        cov = map_coverage()
        assert cov["missing_from_map"] == []
        assert cov["unknown_in_map"] == []

    def test_every_mcp_name_has_a_situation(self):
        """Completeness: all 55 MCP names serve at least one situation."""
        names = {t["mcp"] for t in _entries() if t["mcp"] and t["situations"]}
        missing = [name for name in tool_registry()["mcp"] if name not in names]
        assert missing == [], f"MCP tools without a situation: {missing}"

    def test_every_cli_name_is_described(self):
        """CLI helpers may stay inventory-only (AC20) but must be mapped."""
        names = {t["cli"] for t in _entries() if t["cli"]}
        missing = [name for name in tool_registry()["cli"] if name not in names]
        assert missing == []


# ─── Old card still works (until IMP5) ───────────────────────────────────


class TestOldCard:
    def test_card_prints_the_situational_row(self, tmp_path: Path):
        """IMP5 owns the situational render: ≤5 tools, advice tool first."""
        project = tmp_path / "proj"
        (project / ".agentic" / "state").mkdir(parents=True)
        (project / ".agentic" / "pipelines").mkdir(parents=True)
        (project / ".agentic" / "pipelines" / "default.yaml").write_text(
            "name: default\nstages:\n  - name: plan\n    role: supervisor\n",
            encoding="utf-8",
        )
        done = project / ".agentic" / "done" / "TODO-0001"
        done.mkdir(parents=True)
        (done / "TODO.md").write_text("# TODO-0001\n", encoding="utf-8")
        result = api.brief(project)
        text = result.text
        assert "## Tool map" in text
        assert result.tools and len(result.tools) <= 5
        assert result.tools[0]["tool"] == result.advice_tool
        assert "full inventory" in text
        for title in ("launch", "check", "run", "hygiene"):
            # Group headings belong to the full inventory, not the card.
            assert f"**{title}**" not in text


# ─── QA review (TODO-0347): the renderer contract ────────────────────────
#
# IMP5 renders the situational card from `group["situations"]` and the rows
# of `situational_tools`. The two must agree, otherwise a group heading is
# printed for a situation where it has nothing to show — the "wall of names"
# CTX7 was about, only shorter. These tests pin the agreement at the data
# level, before the renderer exists.


class TestRendererContract:
    def test_every_data_code_is_known(self):
        """A typo (`S1A`, `S14`, a scalar split into `S`,`8`) hides a tool.

        `situational_tools` selects by exact code, so an unknown code keeps
        the entry in the full list but drops it from every row — silently.
        """
        known = set(SITUATION_ADVICE)
        for entry in _entries():
            unknown = [s for s in entry["situations"] if s not in known]
            name = entry["mcp"] or entry["cli"]
            assert unknown == [], f"{name}: unknown situation codes {unknown}"

    def test_group_claims_only_situations_with_rows(self):
        """D8: a group is not rendered where it has no selectable entry.

        The metrics group used to inherit S2/S3 from its inventory-only
        entries: IMP5 would print the heading and zero rows.
        """
        for group in load_tool_map():
            selectable = {
                s
                for t in group["tools"]
                if not t["inventory_only"]
                for s in t["situations"]
            }
            extra = [s for s in group["situations"] if s not in selectable]
            assert extra == [], f"{group['title']}: claims {extra} with no rows"

    def test_inventory_only_group_is_not_situational(self):
        """AC20 + AC21: metrics/helpers live in the full list only.

        A group whose every entry is inventory-only has no row in any
        situation; it may stay visible in the old full map (until IMP5)
        but must not claim a situation.
        """
        for group in load_tool_map():
            if group["tools"] and all(t["inventory_only"] for t in group["tools"]):
                assert group["situations"] == [], group["title"]

    def test_row_names_are_callable(self):
        """The card prints `tool` verbatim: an MCP name or `awf <cli>`.

        A bare CLI subcommand (`supervise`) would read as a missing tool;
        an MCP name outside the registry would be a dead call (INV3).
        """
        registry = tool_registry()
        for situation in SITUATIONS:
            names = _situation_names(situation)
            assert len(set(names)) == len(names), f"{situation}: duplicate row"
            for entry in situational_tools(situation):
                name = entry["tool"]
                if name in registry["mcp"]:
                    continue
                assert name.startswith("awf "), f"{situation}: bare {name!r}"
                assert name[4:] in registry["cli"], f"{situation}: dead {name!r}"

    def test_absent_advice_keeps_the_row_intact(self):
        """AC19 drift guard: a foreign advice tool cannot be prepended.

        The row keeps the file order (the data's own advice stays first);
        the caller must notice the mismatch, not get a silently reordered
        row that contradicts the advice line.
        """
        baseline = situational_tools("S8")
        same = situational_tools("S8", "awf_not_a_tool")
        assert same == baseline
        assert same[0]["tool"] == SITUATION_ADVICE["S8"]
