"""A37 (TODO-0349): the approve override and every non-blocking pack.

The refusal half lives in ``test_commit_gate_pack_verdict.py``. Here:

- ``failed`` pack + approve with ``override_failed`` and a reason → commits,
  and ``COMMIT-AUDIT-<todo>.json`` carries the section list and the reason;
- the approve refuses an override without a reason, before any side effect;
- a partial override (one section named, another failing) still refuses;
- a forged/hand-edited binding cannot turn a string or a reasonless list
  into an override;
- ``ok`` / missing pack → commits exactly as before;
- the same behaviour inside an active run, where the binding is enforced.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf import commit_plan, run_state
from awf.api import AwfApiError, approve_commit

from .commit_gate_pack_support import (
    TODO,
    audit,
    baseline,
    commit,
    fingerprint,
    head,
    prepared_repo,
    write_binding,
    write_pack,
)


@pytest.fixture
def repo(tmp_git_repo: Path) -> Path:
    return prepared_repo(tmp_git_repo)


class TestOverride:
    def test_override_with_reason_commits_and_audits(self, repo: Path) -> None:
        sha = baseline(repo)
        write_pack(repo, "failed", {"gates": "fail"}, fingerprint(repo))

        result = approve_commit(
            repo,
            TODO,
            override_failed=["gates"],
            override_reason="budget overshoot accepted for this unit",
        )
        assert result.signal_file

        outcome = commit(repo, sha)

        assert outcome.status == "committed", outcome.reason
        record = audit(repo)
        assert record["override_failed"] == ["gates"]
        assert "budget overshoot" in record["override_reason"]

    def test_override_without_reason_is_refused(self, repo: Path) -> None:
        with pytest.raises(AwfApiError, match="override_reason"):
            approve_commit(repo, TODO, override_failed=["gates"])
        assert not (repo / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready").exists()

    def test_partial_override_still_refuses(self, repo: Path) -> None:
        """Overriding ``gates`` must not wave through a failing ``tests``."""
        sha = baseline(repo)
        write_pack(repo, "failed", {"gates": "fail", "tests": "fail"}, fingerprint(repo))
        approve_commit(
            repo,
            TODO,
            override_failed=["gates"],
            override_reason="budget accepted",
        )

        outcome = commit(repo, sha)

        assert outcome.status == "refused", outcome.reason
        assert "section(s) tests" in outcome.reason
        assert head(repo) == sha

    def test_override_of_an_unfailed_section_does_not_open_the_gate(
        self, repo: Path
    ) -> None:
        """A name that matches no failed section is a no-op, not a pass."""
        sha = baseline(repo)
        write_pack(repo, "failed", {"gates": "fail"}, fingerprint(repo))
        approve_commit(
            repo,
            TODO,
            override_failed=["lint"],
            override_reason="lint was clean anyway",
        )

        outcome = commit(repo, sha)

        assert outcome.status == "refused", outcome.reason
        assert "gates" in outcome.reason
        assert head(repo) == sha

    def test_string_override_in_the_binding_is_not_an_override(self, repo: Path) -> None:
        """A forged/hand-edited signal with ``override_failed: "gates"``.

        The reader only accepts a list; a bare string must not turn into
        ``["g","a","t","e","s"]`` or into a silent pass.
        """
        sha = baseline(repo)
        write_pack(repo, "failed", {"gates": "fail"}, fingerprint(repo))
        write_binding(repo, {"override_failed": "gates", "override_reason": "x"})

        outcome = commit(repo, sha)

        assert outcome.status == "refused", outcome.reason
        assert "gates" in outcome.reason

    def test_binding_override_without_reason_is_not_an_override(self, repo: Path) -> None:
        """A signal that names sections but no WHY is refused, like an approve."""
        sha = baseline(repo)
        write_pack(repo, "failed", {"gates": "fail"}, fingerprint(repo))
        write_binding(repo, {"override_failed": ["gates"], "override_reason": "   "})

        outcome = commit(repo, sha)

        assert outcome.status == "refused", outcome.reason
        assert "gates" in outcome.reason


class TestGreenOrMissingPack:
    def test_ok_pack_commits(self, repo: Path) -> None:
        sha = baseline(repo)
        write_pack(repo, "ok", {"gates": "pass"}, fingerprint(repo))

        outcome = commit(repo, sha)

        assert outcome.status == "committed", outcome.reason

    def test_missing_pack_commits(self, repo: Path) -> None:
        sha = baseline(repo)

        outcome = commit(repo, sha)

        assert outcome.status == "committed", outcome.reason


class TestActiveRun:
    """The production path: a unit commit inside an active run.

    Outside a run the gate does not enforce the M2.1 binding, so the
    override tests above run a shallower path than production. These pin
    that the pack check and the override behave the same when the binding
    IS enforced.
    """

    def _bind(self, repo: Path, sha: str, *, generation: int = 3) -> None:
        run_state.write_run(
            repo, queue=[TODO], index=0, active=True, generation=generation
        )
        fp = fingerprint(repo)
        (repo / ".agentic" / "context" / f"VERIFIED-{TODO}.sha").write_text(
            fp + "\n", encoding="utf-8"
        )
        plan = commit_plan.build_commit_plan(repo, TODO, sha)
        write_binding(
            repo,
            {
                "generation": generation,
                "verified_sha": fp,
                "files_digest": commit_plan.files_digest(plan.files),
            },
        )

    def test_override_commits_inside_an_active_run(self, repo: Path) -> None:
        sha = baseline(repo)
        self._bind(repo, sha)
        write_pack(repo, "failed", {"gates": "fail"}, fingerprint(repo))
        binding = json.loads(
            (repo / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready").read_text(
                encoding="utf-8"
            )
        )
        binding["override_failed"] = ["gates"]
        binding["override_reason"] = "budget overshoot accepted"
        write_binding(repo, binding)

        outcome = commit(repo, sha)

        assert outcome.status == "committed", outcome.reason
        record = audit(repo)
        assert record["override_failed"] == ["gates"]
        assert "budget overshoot" in record["override_reason"]

    def test_red_pack_refuses_even_with_an_ack_in_an_active_run(self, repo: Path) -> None:
        """ACK does not authorize a commit inside a run — and never a red pack."""
        sha = baseline(repo)
        run_state.write_run(repo, queue=[TODO], index=0, active=True, generation=3)
        inbox = repo / ".agentic" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"ACK-{TODO}.ready").write_text("", encoding="utf-8")
        write_pack(repo, "failed", {"gates": "fail"}, fingerprint(repo))

        outcome = commit(repo, sha)

        assert outcome.status == "refused", outcome.reason
        assert "ACK signal carries no evidence" in outcome.reason
        assert head(repo) == sha

    def test_override_does_not_bypass_the_run_binding(self, repo: Path) -> None:
        """The override opens only the PACK gate, never the run binding.

        Proves the two tests above run with an ACTIVE run (otherwise the
        binding would be skipped and the override would look unconditional).
        """
        sha = baseline(repo)
        self._bind(repo, sha, generation=3)
        write_pack(repo, "failed", {"gates": "fail"}, fingerprint(repo))
        binding = json.loads(
            (repo / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready").read_text(
                encoding="utf-8"
            )
        )
        binding["override_failed"] = ["gates"]
        binding["override_reason"] = "budget overshoot accepted"
        binding["generation"] = 2  # a dead cycle's approval
        write_binding(repo, binding)

        outcome = commit(repo, sha)

        assert outcome.status == "refused", outcome.reason
        assert "generation" in outcome.reason
        assert head(repo) == sha
