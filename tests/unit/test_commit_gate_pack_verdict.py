"""A37 (TODO-0349): a red verify pack must stop the commit.

On TODO-0339 the pack returned ``verdict: failed`` (section ``gates``) and
the unit still committed. These tests pin the REFUSAL half of the gate:

- ``failed`` pack on the current tree → refused, no commit, reason in the log;
- the unit's work stays in the tree (not archived, nothing moved);
- advisory sections (``spec_budget``) alone do not block;
- a stale-tree pack describes another commit and does not block;
- an inconsistent report (``failed`` but no failing section) still refuses;
- the writer's own render round-trips through the gate.

The approve override, the ``COMMIT-AUDIT`` fact and the green/missing pack
live in ``test_commit_gate_override.py`` (ratchet: < 400 lines per file).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf import commit_plan, verify_pack

from .commit_gate_pack_support import (
    TODO,
    baseline,
    commit,
    fingerprint,
    head,
    prepared_repo,
    write_pack,
    write_pack_body,
)


@pytest.fixture
def repo(tmp_git_repo: Path) -> Path:
    return prepared_repo(tmp_git_repo)


class TestFailedPack:
    def test_failed_gates_section_refuses_commit(self, repo: Path) -> None:
        sha = baseline(repo)
        write_pack(repo, "failed", {"gates": "fail"}, fingerprint(repo))

        outcome = commit(repo, sha)

        assert outcome.status == "refused", outcome.reason
        assert "gates" in outcome.reason
        assert head(repo) == sha, "a red pack must not produce a commit"
        log = (repo / ".agentic" / "logs" / "orchestrator.log").read_text(
            encoding="utf-8"
        )
        assert "A37" in log and TODO in log
        # the unit's work is still in the tree for the next attempt
        assert (repo / "src" / "a.py").is_file()

    def test_failed_pack_keeps_the_unit_pending(self, repo: Path) -> None:
        """No commit → the unit stays in the working tree, not archived."""
        sha = baseline(repo)
        state = repo / ".agentic" / "state" / "current.yaml"
        state.parent.mkdir(parents=True)
        state.write_text(f"stage_name: implement\ntodo_id: {TODO}\n", encoding="utf-8")
        write_pack(repo, "failed", {"gates": "fail"}, fingerprint(repo))

        outcome = commit(repo, sha)

        assert outcome.status == "refused"
        assert head(repo) == sha
        assert TODO in state.read_text(encoding="utf-8")
        assert (repo / "src" / "a.py").is_file()

    def test_advisory_section_alone_does_not_block(self, repo: Path) -> None:
        sha = baseline(repo)
        write_pack(repo, "failed", {"spec_budget": "fail"}, fingerprint(repo))

        outcome = commit(repo, sha)

        assert outcome.status == "committed", outcome.reason

    def test_stale_pack_does_not_block(self, repo: Path) -> None:
        sha = baseline(repo)
        write_pack(repo, "failed", {"gates": "fail"}, "0" * 64)

        outcome = commit(repo, sha)

        assert outcome.status == "committed", outcome.reason


class TestInconsistentPack:
    """The corners an acceptance test does not reach."""

    def test_failed_verdict_without_failing_section_is_refused(self, repo: Path) -> None:
        """A report that says failed but names no failing section is refused.

        The verdict is the contract; an inconsistent (truncated, hand-edited)
        block must not read as "nothing failed". A section override cannot
        clear it either — there is no section to name.
        """
        sha = baseline(repo)
        body = (
            f"# GATES-{TODO} — verify-pack report\n\n"
            "## Verdict: **failed** (exit 1) — 1 section(s) measured\n\n"
            "## 1. gates: PASS\n\n"
            "---\n\n```json\n"
            + json.dumps(
                {
                    "todo_id": TODO,
                    "verdict": "failed",
                    "sections": {"gates": "pass"},
                    "tree_sha": fingerprint(repo),
                }
            )
            + "\n```\n"
        )
        write_pack_body(repo, body)

        outcome = commit(repo, sha)

        assert outcome.status == "refused", outcome.reason
        assert "names no failing section" in outcome.reason
        assert head(repo) == sha

    def test_pack_without_tree_sha_is_refused(self, repo: Path) -> None:
        """A pack that names no tree cannot be shown to describe this commit."""
        sha = baseline(repo)
        write_pack(repo, "failed", {"gates": "fail"}, "")

        outcome = commit(repo, sha)

        assert outcome.status == "refused", outcome.reason
        assert "gates" in outcome.reason
        assert head(repo) == sha

    def test_real_writer_report_round_trips_through_the_gate(self, repo: Path) -> None:
        """The writer's own render must be the shape the gate reads.

        The other tests build the pack by hand; this one renders it with
        ``verify_pack._render_report`` so a parser/render drift (heading,
        verdict spelling, JSON block) fails HERE, not in production.
        """
        from awf.verify_pack import Section, _render_report

        sha = baseline(repo)
        report = _render_report(
            repo,
            TODO,
            [Section("gates", "fail", ["- `pytest`: FAIL (rc=1) — 1 failed"])],
            "failed",
            1,
            1,
        )
        write_pack_body(repo, report)

        outcome = commit(repo, sha)

        assert outcome.status == "refused", outcome.reason
        assert "gates" in outcome.reason
        assert "FAIL (rc=1)" in outcome.reason
        assert head(repo) == sha

    def test_unreadable_tree_keeps_the_fail_closed_posture(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fingerprint that cannot be taken must not turn a red pack green.

        The pack writer treats a hung/unreadable git as "no fingerprint"
        (``OSError``/``TimeoutExpired``, not only ``RuntimeError``). The gate
        reads the tree the same way: with no current tree there is nothing to
        prove the pack is stale, so a red pack still refuses.
        """

        def boom(_project_dir: object) -> str:
            raise OSError("git is not readable")

        monkeypatch.setattr("awf.commit_plan.git_utils.tree_fingerprint", boom)
        sha = baseline(repo)
        write_pack(repo, "failed", {"gates": "fail"}, "a" * 64)

        outcome = commit(repo, sha)

        assert outcome.status == "refused", outcome.reason
        assert "gates" in outcome.reason
        assert head(repo) == sha


class TestAdvisorySectionNames:
    """The advisory list is tied to the names the writer actually emits.

    ``PACK_ADVISORY_SECTIONS`` is a plain string list. If ``verify_pack``
    renames a section, the gate silently starts BLOCKING on a failure the
    policy calls advisory (or keeps a dead name forever). Both directions
    are pinned: behaviour per name, and existence of each name in the
    writer's source.
    """

    @pytest.mark.parametrize("section", commit_plan.PACK_ADVISORY_SECTIONS)
    def test_advisory_section_alone_does_not_block(
        self, repo: Path, section: str
    ) -> None:
        sha = baseline(repo)
        write_pack(repo, "failed", {section: "fail"}, fingerprint(repo))

        outcome = commit(repo, sha)

        assert outcome.status == "committed", outcome.reason

    def test_every_advisory_name_is_a_real_pack_section(self) -> None:
        source = Path(verify_pack.__file__).read_text(encoding="utf-8")
        missing = [
            name
            for name in commit_plan.PACK_ADVISORY_SECTIONS
            if f'name="{name}"' not in source
        ]
        assert not missing, (
            "PACK_ADVISORY_SECTIONS names a section the pack writer never "
            f"emits: {missing} — a renamed section becomes a commit blocker"
        )
