"""REPORTS26 B4: analyze_roles must not eat the tail of the role file.

Source: awf-bug-20261007-analyze-roles-stiraet-sektsii-roli-dopisannye-supervizorom.md.
Sections the supervisor wrote AFTER the BD-31 block ("## Iteration
adaptation", "## Handoff contract") vanished after ``awf_analyze_roles``:
the deletion regex ran from the BD-31 marker to end of file (``.*$`` with
DOTALL), not to the next heading.

Three invariants:
(a) role text + BD-31 block + supervisor sections -> after
    ``analyze_roles`` the sections survive, the block is replaced (one
    fresh block), the text before/after the block is unchanged;
(b) two consecutive runs -> byte-identical file (idempotent);
(c) a legacy BD-31 duplicate -> exactly one block in the result.
"""
from __future__ import annotations

from pathlib import Path

from awf.api.roles import analyze_roles

BD31 = "## BD-31: Pipeline-specific disambiguation (added by awf analyze-roles)"
MARKER = "BD-31: Pipeline-specific disambiguation"
CONTRACT = (
    "**Pipeline contract:** read the handoff from the previous role "
    "before starting. Do NOT redo prior work."
)

HEAD = (
    "# agent-implementer\n"
    "\n"
    "Role body text.\n"
    "\n"
    "More body.\n"
    "\n"
)

OLD_BLOCK = (
    "---\n"
    "\n"
    f"{BD31}\n"
    "\n"
    "**Your zone:** legacy zone from 2020\n"
    "\n"
    f"{CONTRACT}\n"
    "\n"
)

TAIL = (
    "## Iteration adaptation\n"
    "\n"
    "- iteration note one\n"
    "\n"
    "## Handoff contract\n"
    "\n"
    "- receives: TODO unit\n"
    "\n"
    "- produces: code + tests\n"
)


def _make_project(tmp_path: Path, roles: dict[str, str]) -> Path:
    proj = tmp_path / "proj"
    (proj / ".agentic" / "roles").mkdir(parents=True)
    (proj / ".agentic" / "pipelines").mkdir(parents=True)
    (proj / ".agentic" / "roles" / "supervisor.md").write_text("# supervisor\n")
    for slug, content in roles.items():
        (proj / ".agentic" / "roles" / f"{slug}.md").write_text(content)
    (proj / ".agentic" / "config.yaml").write_text(
        'project:\n  name: t\nphases:\n  current: ".agentic/phases/plan.md"\n'
        'default_pipeline: "default"\n'
    )
    stages = "- name: plan\n  role: supervisor\n"
    for slug in roles:
        stages += f"- name: {slug}\n  role: {slug}\n"
    stages += "- name: verify\n  role: supervisor\n"
    (proj / ".agentic" / "pipelines" / "default.yaml").write_text(
        f"name: default\nstages:\n{stages}"
    )
    return proj


def _role_path(proj: Path, slug: str) -> Path:
    return proj / ".agentic" / "roles" / f"{slug}.md"


def _expect_head_and_tail(content: str) -> None:
    """The text before the old block and the sections after it survive."""
    assert content.startswith(HEAD.rstrip("\n") + "\n\n## Iteration adaptation")
    assert "- iteration note one" in content
    assert "- receives: TODO unit" in content
    assert "- produces: code + tests" in content
    # fresh block landed at the end, after the supervisor sections
    assert content.index("## Iteration adaptation") < content.index(MARKER)


class TestTailSurvivesPatch:
    """(a) supervisor sections after the BD-31 block survive the patch."""

    def test_sections_after_bd31_block_survive(self, tmp_path: Path) -> None:
        proj = _make_project(
            tmp_path, {"agent-implementer": HEAD + OLD_BLOCK + TAIL}
        )

        result = analyze_roles(proj)
        assert result.dry_run is False

        content = _role_path(proj, "agent-implementer").read_text(encoding="utf-8")
        _expect_head_and_tail(content)

        # the block is replaced: exactly one, legacy content gone
        assert content.count(MARKER) == 1
        assert "legacy zone from 2020" not in content
        assert content.count("**Your zone:**") == 1


class TestIdempotentWithTail:
    """(b) two consecutive runs -> byte-identical file."""

    def test_second_run_is_byte_identical(self, tmp_path: Path) -> None:
        proj = _make_project(
            tmp_path, {"agent-implementer": HEAD + OLD_BLOCK + TAIL}
        )

        analyze_roles(proj)
        first = _role_path(proj, "agent-implementer").read_bytes()

        analyze_roles(proj)
        second = _role_path(proj, "agent-implementer").read_bytes()

        assert first == second


class TestLegacyDuplicates:
    """(c) duplicated legacy BD-31 blocks -> exactly one block in result."""

    def test_duplicate_blocks_collapsed_to_one(self, tmp_path: Path) -> None:
        proj = _make_project(
            tmp_path, {"agent-implementer": HEAD + OLD_BLOCK + OLD_BLOCK + TAIL}
        )

        analyze_roles(proj)
        content = _role_path(proj, "agent-implementer").read_text(encoding="utf-8")

        assert content.count(MARKER) == 1
        assert "legacy zone from 2020" not in content
        _expect_head_and_tail(content)
