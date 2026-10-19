"""R2.5 (roadmap R2) — QA-проба границ, второй блок (agent-qa-review, iter 4).

Продолжение ``test_roadmap_r2_publish_qa.py``: три пробы поверх REVIEW-фиксов
iter 4 (M1/L1/L2). Две КРАСНЫЕ доказывают открытые точки R2.5, одну ЗЕЛЁНУЮ
фиксируют как регресс (ветка resume по совпадению receipt/OID — у неё в
протоколе нет точки инъекции краша, поэтому она проверяется напрямую).
Прод-код здесь НЕ правится: следующий implementer-проход берёт находки по
одной.

Ленивый импорт: на базовом дереве ``awf.publish`` отсутствует.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

TODO = "TODO-0001"


def _publish():
    try:
        from awf import publish
    except ImportError as e:  # pragma: no cover — базовое дерево
        raise AssertionError(f"R2.5 publish module is missing: {e}") from e
    return publish


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, capture_output=True, text=True
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def _logs(repo: Path) -> Path:
    d = repo / ".agentic" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _resume(repo: Path):
    from awf import commit_gate

    return commit_gate.maybe_commit(
        "verify", TODO, "commit_and_report", repo, _logs(repo), auto=False
    )


def _mkplan(files, base):
    from awf import commit_plan

    return commit_plan.CommitPlan(
        todo_id=TODO, generation=0, verified_sha="", files=files, baseline_sha=base
    )


# ─── QA iter 4: open points found ON TOP of the iter-4 REVIEW fixes ───────
#
# The two tests below are RED against the current tree. They are the
# evidence for THIS QA pass (agent-qa-review, iter 4). Prod code is NOT
# fixed here: the next implementer pass takes them one by one.


def test_resume_recognizes_landed_commit_as_ancestor_of_moved_head(tmp_path):
    """RED (requirement 1 / acceptance matrix §8 «не теряет артефакты»).

    The crash-after-``update-ref`` recovery is recognized by ``HEAD ==
    intent.commit_oid`` EXACTLY. If any commit lands on top of the unit's
    own commit before the resume (a second actor, a push round-trip), HEAD
    is a DESCENDANT of ``intent.commit_oid`` — the ref update DID land, the
    unit's work IS in history — yet the resume classifies it as «HEAD moved
    elsewhere», supersedes the intent and returns ``refused``. The
    committed unit then has NO receipt, and the strict archive path (empty
    plan + ``no changes to commit``) refuses to archive it: the artifact
    (the publication fact) is lost even though the commit is reachable.

    Expected: an ancestor check (``commit_oid`` reachable from HEAD)
    completes the receipt instead of abandoning the operation.
    """
    publish = _publish()
    from awf import commit_gate

    repo = _repo(tmp_path)
    base = _head(repo)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")

    with pytest.raises(publish.PublishCrash):
        commit_gate._commit_via_isolated_index(
            repo, _mkplan(("README.md",), base), f"awf(verify): {TODO}",
            _logs(repo), crash_at="update_ref",
        )

    intent = publish.read_intent(repo, TODO)
    assert intent is not None
    unit_oid = intent.commit_oid
    # The crash landed after update-ref but before the index sync: bring the
    # real index onto the new HEAD the way the sync would have.
    _git(repo, "reset", "-q", "HEAD", "--", "README.md")
    # Another actor commits on TOP before the resume.
    (repo / "later.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-qm", "later actor")
    assert (
        _git(repo, "merge-base", "--is-ancestor", unit_oid, "HEAD", check=False).returncode
        == 0
    )

    out = _resume(repo)
    assert out.status == "committed", out.reason
    receipt = publish.read_receipt(repo, TODO)
    assert receipt is not None, "the landed commit left no receipt"
    assert receipt.commit_oid == unit_oid
    assert receipt.disposition in (
        "committed",
        "committed_with_reconciliation_required",
    )
    assert not publish.superseded_file(repo, TODO).exists(), (
        "the unit commit is an ancestor of HEAD — it landed; the operation "
        "must not be abandoned as if the ref never moved"
    )


def test_superseded_fact_is_cleared_by_a_later_successful_publish(tmp_path):
    """RED (requirement 4: commit / receipt / archive as SEPARATE, ACCURATE
    facts in status and RUN-REPORT).

    ``PUBLISH-SUPERSEDED-{todo}.json`` is written when a stale intent is
    abandoned. Nothing clears it, so after the SAME unit id publishes
    successfully later, ``publication_facts`` still reports
    ``superseded=True`` with the old reason — the RUN-REPORT then prints a
    «publish intent superseded» line right next to the unit's live
    ``commit`` / ``receipt committed`` lines: a stale fact contradicting the
    current disposition. The superseded flag must not survive a newer
    successful publication of the same unit.
    """
    publish = _publish()
    from awf import commit_gate

    repo = _repo(tmp_path)
    base0 = _head(repo)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    with pytest.raises(publish.PublishCrash):
        commit_gate._commit_via_isolated_index(
            repo, _mkplan(("README.md",), base0), f"awf(verify): {TODO}",
            _logs(repo), crash_at="commit_object",
        )
    (repo / "other.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-qm", "another actor")

    out1 = commit_gate.maybe_commit(
        "verify", TODO, "commit_and_report", repo, _logs(repo), auto=False
    )
    assert out1.status == "refused", out1.reason
    assert publish.superseded_file(repo, TODO).exists()

    base1 = _head(repo)
    (repo / "README.md").write_text("v3\n", encoding="utf-8")
    out2 = commit_gate.maybe_commit(
        "verify", TODO, "commit_and_report", repo, _logs(repo), auto=False,
        baseline_sha=base1,
    )
    assert out2.status == "committed", out2.reason
    facts = publish.publication_facts(repo, TODO)
    assert facts["receipt"] == "committed"
    assert facts["superseded"] is False, (
        "the unit published successfully after the supersede — a current "
        "fact must not still name the abandoned intent"
    )


def test_resume_completes_when_only_the_intent_removal_crashed(tmp_path):
    """GREEN (requirement 1): the crash that lands BETWEEN the receipt write
    and the intent removal leaves BOTH files. The resume must key on the
    receipt matching the intent's OID (not on "an intent exists"): it drops
    the stray intent and reports the fact — never a second commit, never a
    lost receipt. This branch (``resume_if_pending`` receipt-match) has no
    crash-injection point in the protocol, so it is exercised directly here.
    """
    publish = _publish()
    from awf import commit_gate

    repo = _repo(tmp_path)
    base = _head(repo)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    out = commit_gate._commit_via_isolated_index(
        repo, _mkplan(("README.md",), base), f"awf(verify): {TODO}", _logs(repo)
    )
    assert out.status == "committed", out.reason
    receipt = publish.read_receipt(repo, TODO)
    assert receipt is not None

    # Simulate the gap: the intent file survives next to the matching receipt.
    intent = publish.PublishIntent(
        todo_id=TODO, generation=0, expected_head="", tree_oid="",
        commit_oid=receipt.commit_oid, candidate_tree="", files=("README.md",),
        delta_files=(), degraded=(), message="", index_orig={}, evidence_refs=(),
        transfers=(), residuals=(), created_at="crash-gap",
    )
    publish._write_json(publish.intent_file(repo, TODO), intent.to_dict())
    assert publish.intent_file(repo, TODO).exists()
    # The D2 invariant, on the resume surface (R2.5 architect-review): an
    # abandoned-intent fact from an EARLIER attempt may still sit next to the
    # live committed receipt when the crash landed in this gap. The resume
    # finishes the same tail as a normal success — the superseded fact goes.
    publish._write_json(
        publish.superseded_file(repo, TODO),
        {
            "todo_id": TODO,
            "disposition": "superseded",
            "reason": "an earlier abandoned attempt",
            "created_at": "before-the-crash-gap",
        },
    )
    assert publish.publication_facts(repo, TODO)["superseded"] is True
    count = int(_git(repo, "rev-list", "--count", "HEAD").stdout.strip())

    again = commit_gate.maybe_commit(
        "verify", TODO, "commit_and_report", repo, _logs(repo), auto=False
    )
    assert again.status == "skipped", again.reason
    assert not publish.intent_file(repo, TODO).exists()
    assert int(_git(repo, "rev-list", "--count", "HEAD").stdout.strip()) == count
    kept = publish.read_receipt(repo, TODO)
    assert kept is not None and kept.commit_oid == receipt.commit_oid
    assert not publish.superseded_file(repo, TODO).exists(), (
        "a live committed receipt must not coexist with an abandoned-intent "
        "fact — the resume surface carries the same invariant as success"
    )
    assert publish.publication_facts(repo, TODO)["superseded"] is False
