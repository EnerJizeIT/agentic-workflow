"""R2.2 (roadmap R2): explicit dispatch and the code transfer.

На исходной версии файл КРАСНЫЙ: модулей ``awf.dispatch_policy`` /
``awf.claim_changes`` нет и ``dispatch_todo`` не знает dirty-политики;
импорты ленивые, поэтому падают утверждения, а не сбор.

Строки приёмки R2.2:

- грязное дерево + ``dirty_policy=require_clean`` (default нового
  config-version) → ОТКАЗ, ничего не зарезервировано (нет .ready/baseline);
- legacy-проект (нет ``config_version``) сохраняет прежний выбор (warn) с
  явным предупреждением — default молча не меняет проект;
- ``dirty_policy=isolate`` → snapshot + таблица excluded + предупреждение о
  зависимостях;
- claim_changes — перенос КОДА: план, digest, invalidation при изменении
  дерева после preview; охватывает tracked/untracked/delete/rename/mode;
- claim закрывает грязный путь для require_clean — без claim отказ честный,
  с claim нужный blob принадлежит юниту;
- finding-перенос остаётся за R5 (carry_over_from/include_untracked), в
  claim_changes это kind=finding → отказ.

Все репозитории — disposable в ``tmp_path``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf import api
from awf.api._errors import AwfApiError

SRC = "TODO-0001"
RETRY = "TODO-0002"


# ─── lazy imports: baseline gives a real red, not a broken runner ────────


def _policy_mod():
    try:
        from awf import dispatch_policy
    except ImportError as e:  # pragma: no cover - baseline only
        raise AssertionError(f"awf.dispatch_policy is missing: {e}") from e
    return dispatch_policy


def _claim_mod():
    try:
        from awf import claim_changes
    except ImportError as e:  # pragma: no cover - baseline only
        raise AssertionError(f"awf.claim_changes is missing: {e}") from e
    return claim_changes


# ─── helpers ─────────────────────────────────────────────────────────────


def _git(repo: Path, *args: str, check: bool = True) -> None:
    subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True)


def _commit(repo: Path, message: str = "c") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _write_config(repo: Path, *, version: int | None, policy: str | None) -> None:
    """Rewrite config.yaml with (or without) config_version + dirty_policy."""
    import yaml

    cfg = repo / ".agentic" / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    data.pop("config_version", None)
    data.pop("discipline", None)
    if version is not None:
        data["config_version"] = version
    if policy is not None:
        data["discipline"] = {"dirty_policy": policy}
    cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _project(tmp_path: Path, *, version: int | None = 1, policy: str | None = None) -> Path:
    """A git project with ``awf init`` done and ``.agentic`` committed.

    ``version``/``policy`` shape config.yaml after init; ``version=None``
    simulates a legacy project (no config_version).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    api.init_project(
        repo,
        project_name="R2Disp",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    _write_config(repo, version=version, policy=policy)
    _commit(repo, "awf init")
    return repo


def _add_tracked(repo: Path, name: str, content: str = "v1\n") -> None:
    (repo / name).write_text(content, encoding="utf-8")
    _commit(repo, f"add {name}")


# ─── policy resolution ───────────────────────────────────────────────────


def test_resolve_policy_defaults_and_legacy():
    dp = _policy_mod()
    # versioned project, no explicit policy → require_clean
    d = dp.resolve_dirty_policy({"config_version": dp.CONFIG_VERSION})
    assert d.policy == dp.REQUIRE_CLEAN
    assert not d.migrated_legacy
    # legacy project (no config_version) → warn + migration warning
    legacy = dp.resolve_dirty_policy({})
    assert legacy.policy == dp.LEGACY_POLICY
    assert legacy.migrated_legacy
    assert "require_clean" in legacy.warning
    # explicit isolate is honoured
    iso = dp.resolve_dirty_policy(
        {"config_version": dp.CONFIG_VERSION, "discipline": {"dirty_policy": "isolate"}}
    )
    assert iso.policy == dp.ISOLATE


def test_init_seeds_version_and_safe_isolate(tmp_path):
    """A fresh ``awf init`` project adopts the new config version but seeds the
    pre-R2.2-compatible 'isolate' discipline (uncommitted AWF skeleton must not
    block the first dispatch); the VERSION default stays require_clean."""
    dp = _policy_mod()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    api.init_project(
        repo,
        project_name="R2Init",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    import yaml

    data = yaml.safe_load((repo / ".agentic" / "config.yaml").read_text(encoding="utf-8"))
    assert data.get("config_version") == dp.CONFIG_VERSION
    assert data["discipline"]["dirty_policy"] == "isolate"


# ─── require_clean ───────────────────────────────────────────────────────


def test_require_clean_refuses_dirty_tree_and_reserves_nothing(tmp_path):
    repo = _project(tmp_path, version=1, policy=None)  # default require_clean
    _add_tracked(repo, "core.py")
    (repo / "core.py").write_text("uncommitted change\n", encoding="utf-8")

    with pytest.raises(AwfApiError, match="require_clean"):
        api.dispatch_todo(repo, f"# {SRC}\nTask\n", todo_id=SRC)

    # NOTHING is reserved: no TODO, no ready signal, no baseline.
    assert not (repo / ".agentic" / "inbox" / f"{SRC}.md").exists()
    assert not (repo / ".agentic" / "inbox" / f"{SRC}.ready").exists()
    assert not (repo / ".agentic" / "context" / f"BASELINE-{SRC}.sha").exists()


def test_require_clean_allows_clean_tree(tmp_path):
    repo = _project(tmp_path, version=1, policy=None)
    result = api.dispatch_todo(repo, f"# {SRC}\nTask\n", todo_id=SRC)
    assert result.dirty_policy == "require_clean"
    assert result.dirty_files == []
    assert result.dirty_policy_warning == ""


def test_legacy_project_keeps_warn_with_migration_warning(tmp_path):
    repo = _project(tmp_path, version=None)  # legacy: no config_version
    _add_tracked(repo, "core.py")
    (repo / "core.py").write_text("wip\n", encoding="utf-8")

    result = api.dispatch_todo(repo, f"# {SRC}\nTask\n", todo_id=SRC)
    assert result.dirty_policy == "warn"
    assert result.dirty_policy_warning, "legacy migration must be announced"
    assert "core.py" in result.dirty_files


# ─── isolate ─────────────────────────────────────────────────────────────


def test_isolate_reports_excluded_table_and_dependency_warning(tmp_path):
    repo = _project(tmp_path, version=1, policy="isolate")
    _add_tracked(repo, "core.py")
    (repo / "core.py").write_text("wip\n", encoding="utf-8")

    result = api.dispatch_todo(repo, f"# {SRC}\nTask\n", todo_id=SRC)
    assert result.dirty_policy == "isolate"
    assert "isolate" in result.isolate_warning
    assert "core.py" in result.isolate_warning
    assert result.isolate_dependency_warning, "dependency warning is required"
    assert "depend" in result.isolate_dependency_warning.lower()
    # isolate implies a worktree snapshot
    wt = repo / ".agentic" / "context" / f"BASELINE-{SRC}.worktree"
    assert wt.is_file(), "isolate must snapshot the dirty worktree"


def test_isolate_table_excludes_adopted_path(tmp_path):
    """An ADOPTED path is the unit's own work, so the isolate table must not
    list it as 'stays OUTSIDE the unit' — the same invariant the require_clean
    refusal and the dirty warning already hold (all three surfaces agree)."""
    repo = _project(tmp_path, version=1, policy="isolate")
    (repo / "finding.txt").write_text("finding\n", encoding="utf-8")

    result = api.dispatch_todo(
        repo, f"# {SRC}\nTask\n", todo_id=SRC, include_untracked=["finding.txt"]
    )
    assert result.dirty_policy == "isolate"
    assert result.dirty_files == []
    assert "finding.txt" not in result.isolate_warning
    assert "finding.txt" not in result.isolate_dependency_warning


# ─── managed AWF runtime is not the user's dirt ──────────────────────────


def test_managed_agentic_runtime_paths_are_not_dirty(tmp_path):
    dp = _policy_mod()
    repo = _project(tmp_path, version=1, policy="require_clean")
    # A tracked file under a managed runtime dir, force-added despite the
    # init-created .gitignore, then modified.
    logs = repo / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "managed.log").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "-f", ".agentic/logs/managed.log")
    _git(repo, "commit", "-qm", "tracked runtime log")
    (logs / "managed.log").write_text("b\n", encoding="utf-8")

    report = dp.check_source_tree(repo)
    assert report.paths == ()
    assert ".agentic/logs/managed.log" in report.managed
    assert report.clean

    # And the dispatch is not refused by it.
    result = api.dispatch_todo(repo, f"# {SRC}\nTask\n", todo_id=SRC)
    assert result.dirty_files == []


# ─── claim_changes ───────────────────────────────────────────────────────


def test_claim_plan_is_invalidated_when_tree_moves(tmp_path):
    cc = _claim_mod()
    repo = _project(tmp_path, version=None)
    _add_tracked(repo, "core.py")
    api.create_baseline(repo, SRC)
    (repo / "core.py").write_text("predecessor change\n", encoding="utf-8")

    plan = cc.plan_claim_changes(repo, SRC)
    assert plan.files, "the dirty change must be in the plan"
    assert plan.digest

    # The tree moves after the preview → apply is refused with a reason.
    (repo / "core.py").write_text("moved again\n", encoding="utf-8")
    with pytest.raises(AwfApiError, match="changed|invalidat"):
        cc.apply_claim_changes(repo, plan, confirmed_digest=plan.digest)
    assert not cc.claim_record_path(repo, SRC).exists()


def test_claim_apply_requires_the_confirmed_digest(tmp_path):
    cc = _claim_mod()
    repo = _project(tmp_path, version=None)
    _add_tracked(repo, "core.py")
    api.create_baseline(repo, SRC)
    (repo / "core.py").write_text("x\n", encoding="utf-8")
    plan = cc.plan_claim_changes(repo, SRC)

    with pytest.raises(AwfApiError, match="digest"):
        cc.apply_claim_changes(repo, plan, confirmed_digest="deadbeef")

    path = cc.apply_claim_changes(repo, plan, confirmed_digest=plan.digest)
    assert path.is_file()
    assert cc.read_claim(repo, SRC) is not None


def test_claim_covers_tracked_untracked_delete_rename_and_mode(tmp_path):
    cc = _claim_mod()
    repo = _project(tmp_path, version=None)
    _add_tracked(repo, "tracked.py")
    _add_tracked(repo, "gone.py")
    _add_tracked(repo, "old.py")
    _add_tracked(repo, "exec.py")
    api.create_baseline(repo, SRC)

    (repo / "tracked.py").write_text("edited\n", encoding="utf-8")
    (repo / "newfile.py").write_text("brand new\n", encoding="utf-8")  # untracked
    (repo / "gone.py").unlink()  # delete
    _git(repo, "mv", "old.py", "renamed.py")  # rename (staged)
    (repo / "exec.py").chmod(0o755)  # executable bit

    plan = cc.plan_claim_changes(repo, SRC)
    ops = {f.path: f for f in plan.files}
    assert ops["tracked.py"].op == "modified"
    assert ops["newfile.py"].op == "untracked"
    assert ops["gone.py"].op == "deleted"
    assert "renamed.py" in ops and ops["renamed.py"].op == "renamed"
    assert ops["renamed.py"].old_path == "old.py"
    assert ops["exec.py"].mode == "100755"


def test_claim_refuses_finding_kind(tmp_path):
    cc = _claim_mod()
    repo = _project(tmp_path, version=None)
    api.create_baseline(repo, SRC)
    with pytest.raises(AwfApiError, match="finding|carry_over|include_untracked"):
        cc.plan_claim_changes(repo, SRC, kind="finding")


# ─── scenario 3: retry re-uses a predecessor's tracked change ────────────


def test_retry_refuses_without_claim_and_owns_blob_with_claim(tmp_path):
    cc = _claim_mod()
    repo = _project(tmp_path, version=1, policy="require_clean")
    _add_tracked(repo, "core.py")
    # Predecessor unit baseline, then its untouched tracked change.
    api.create_baseline(repo, SRC)
    (repo / "core.py").write_text("predecessor work\n", encoding="utf-8")

    # Without a claim the candidate honestly refuses (no completeness promise).
    with pytest.raises(AwfApiError, match="require_clean"):
        api.dispatch_todo(repo, f"# {RETRY}\nTask\n", todo_id=RETRY)
    assert not (repo / ".agentic" / "context" / f"BASELINE-{RETRY}.sha").exists()

    # With a claim the needed blob is owned by the retry.
    plan = cc.plan_claim_changes(repo, SRC)
    cc.apply_claim_changes(repo, plan, confirmed_digest=plan.digest)
    result = api.dispatch_todo(
        repo, f"# {RETRY}\nTask\n", todo_id=RETRY, claim_from=SRC
    )
    assert result.claim_from == SRC
    assert result.claimed_files == ["core.py"]
    assert (repo / ".agentic" / "context" / f"BASELINE-{RETRY}.sha").is_file()


def test_dispatch_with_missing_claim_record_refuses(tmp_path):
    repo = _project(tmp_path, version=None)
    _add_tracked(repo, "core.py")
    with pytest.raises(AwfApiError, match="claim"):
        api.dispatch_todo(
            repo, f"# {SRC}\nTask\n", todo_id=SRC, claim_from="TODO-9999"
        )
    assert not (repo / ".agentic" / "inbox" / f"{SRC}.md").exists()


def test_claim_consumption_refuses_when_tree_moved_after_apply(tmp_path):
    """F3 (review-fix): consent is bound to the exact plan — an edit AFTER the
    claim was applied invalidates it at CONSUMPTION time too (dispatch with
    ``claim_from``), not only at apply. Recompute-vs-plan blob digests."""
    cc = _claim_mod()
    repo = _project(tmp_path, version=1, policy="require_clean")
    _add_tracked(repo, "core.py")
    api.create_baseline(repo, SRC)
    (repo / "core.py").write_text("predecessor work\n", encoding="utf-8")

    plan = cc.plan_claim_changes(repo, SRC)
    cc.apply_claim_changes(repo, plan, confirmed_digest=plan.digest)

    # The tree moves after the consent → consumption must refuse, no residue.
    (repo / "core.py").write_text("edited after consent\n", encoding="utf-8")
    with pytest.raises(AwfApiError, match="invalidat|no longer match"):
        api.dispatch_todo(
            repo, f"# {RETRY}\nTask\n", todo_id=RETRY, claim_from=SRC
        )
    assert not (repo / ".agentic" / "context" / f"BASELINE-{RETRY}.sha").exists()
    assert not (repo / ".agentic" / "inbox" / f"{RETRY}.md").exists()


# ─── M1 (review-fix 2): byte-faithful dirty surfaces ─────────────────────


def test_non_ascii_paths_are_byte_faithful_in_dirty_surfaces(tmp_path):
    """An ADOPTED non-ASCII path is the unit's own work on BOTH dirty
    surfaces (the dispatch dirty warning and the isolate table), an un-owned
    one is honestly reported — byte-faithfully.

    REVIEW-fix 2 M1: ``_dirty_tree_classes`` parsed the printed ``git status
    --short``, which C-quotes a non-ASCII name (``"\\321\\204..."``), while
    the adopted paths come from the R2.1 model as raw bytes (``файл.txt``) —
    the exclusion missed and the two surfaces contradicted each other. The
    warning now reads the same R2.1 model as the isolate table.
    """
    repo = _project(tmp_path, version=1, policy="isolate")
    (repo / "файл.txt").write_text("finding\n", encoding="utf-8")   # adopted
    (repo / "грязный.txt").write_text("wip\n", encoding="utf-8")    # un-owned

    result = api.dispatch_todo(
        repo, f"# {SRC}\nTask\n", todo_id=SRC, include_untracked=["файл.txt"]
    )
    assert result.dirty_policy == "isolate"
    # adopted → the unit's own work: on NEITHER surface
    assert "файл.txt" not in result.dirty_files
    assert "файл.txt" not in result.dirty_warning
    assert "файл.txt" not in result.isolate_warning
    # un-owned dirt → reported on BOTH surfaces, the name not C-quoted
    assert "грязный.txt" in result.dirty_files
    assert "грязный.txt" in result.isolate_warning


# ─── M2 (review-fix 2): the claim survives a re-baseline / recompute ─────


def test_claim_exclusion_survives_rebaseline_and_recompute(tmp_path):
    """The CODE channel keeps its exclusion where the carry-over / include
    channels do: a re-baseline re-applies the ``BASELINE-*.claim`` link and a
    baseline recompute (``update_todo``) does not return a claimed path to the
    pre-existing snapshot.

    REVIEW-fix 2 M2: ``dispatch_todo(claim_from=...)`` passed the claim to
    ``create_baseline`` but wrote no link, so ``awf run_next`` re-listed the
    claimed untracked blob in ``BASELINE-*.untracked`` → the commit gate
    judged it pre-existing and silently dropped the claimed code.
    """
    cc = _claim_mod()
    from awf.api.hygiene import transfer_exclusions

    repo = _project(tmp_path, version=1, policy="require_clean")
    api.create_baseline(repo, SRC)
    (repo / "newmod.py").write_text("predecessor blob\n", encoding="utf-8")  # untracked
    plan = cc.plan_claim_changes(repo, SRC)
    cc.apply_claim_changes(repo, plan, confirmed_digest=plan.digest)
    api.dispatch_todo(repo, f"# {RETRY}\nretry\n", todo_id=RETRY, claim_from=SRC)

    untracked_path = repo / ".agentic" / "context" / f"BASELINE-{RETRY}.untracked"
    # the audit link records the CODE-transfer origin; the dispatch baseline
    # already excludes the claimed untracked path
    assert cc.read_claim_link(repo, RETRY) == SRC
    assert "newmod.py" not in untracked_path.read_text(encoding="utf-8")

    # (a) a re-baseline (what awf run_next does) re-applies the claim exclusion
    carry_over, include, claim = transfer_exclusions(repo, RETRY)
    assert claim == {"newmod.py"}
    api.create_baseline(
        repo,
        RETRY,
        carry_over=carry_over or None,
        include=include or None,
        claim=claim or None,
    )
    assert "newmod.py" not in untracked_path.read_text(encoding="utf-8")

    # (b) an include-list recompute keeps the claim exclusion too
    api.update_todo(repo, RETRY, append="\nmore\n", include_untracked=[])
    assert "newmod.py" not in untracked_path.read_text(encoding="utf-8")


# ─── architect-review (iter 5): public name, ownership surfaces, snapshot ──


def test_claim_source_todo_is_validated_as_a_path_name(tmp_path):
    """The source id is interpolated into ``CLAIM-<id>.json`` /
    ``BASELINE-<id>.sha``; an unvalidated input must be refused before it can
    leave ``.agentic/context`` (docs/contracts/public-name-validation.md) — the
    same guard ``resolve_carry_over`` applies to its origin."""
    cc = _claim_mod()
    repo = _project(tmp_path, version=1, policy="require_clean")
    api.create_baseline(repo, SRC)
    for bad in ("../../escape", "not-a-todo", ""):
        with pytest.raises(AwfApiError, match="invalid source"):
            cc.plan_claim_changes(repo, bad)
    with pytest.raises(AwfApiError, match="invalid source"):
        cc.resolve_claim_changes(repo, "../../escape")


def test_secret_like_untracked_is_reported_on_all_dirty_surfaces(tmp_path):
    """One tree, one story: a secret-looking untracked name (``.env``) is named
    by the isolate table — the dirty warning must not omit it. The ownership
    reader (``changes()``) drops a secret name; the policy reader keeps it, so
    the warning must read the untracked listing too."""
    repo = _project(tmp_path, version=1, policy="isolate")
    (repo / ".env").write_text("SECRET=1\n", encoding="utf-8")

    result = api.dispatch_todo(repo, f"# {SRC}\nTask\n", todo_id=SRC)
    assert ".env" in result.dirty_files
    assert ".env" in result.dirty_warning
    assert ".env" in result.isolate_warning


def test_adopted_non_ascii_untracked_is_excluded_from_the_baseline_snapshot(tmp_path):
    """The ``BASELINE-*.untracked`` writer must drop an ADOPTED non-ASCII path:
    git's printed listing C-quotes it (``"\\321\\204..."``) while the adopted
    identity is raw — without the bridge the exclusion silently missed and the
    commit gate would treat the unit's own adopted file as pre-existing
    (the R2.2 ownership promise, not just its surfaces)."""
    repo = _project(tmp_path, version=1, policy="isolate")
    (repo / "файл.txt").write_text("finding\n", encoding="utf-8")   # adopted
    (repo / "грязный.txt").write_text("wip\n", encoding="utf-8")    # un-owned

    api.dispatch_todo(
        repo, f"# {SRC}\nTask\n", todo_id=SRC, include_untracked=["файл.txt"]
    )
    snapshot = (
        repo / ".agentic" / "context" / f"BASELINE-{SRC}.untracked"
    ).read_text(encoding="utf-8")
    lines = [ln for ln in snapshot.splitlines() if ln.strip()]
    assert len(lines) == 1, snapshot
    adopted_printed = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "файл.txt"],
        cwd=repo, capture_output=True, text=True,
    ).stdout.strip()
    assert adopted_printed and adopted_printed not in snapshot


# ─── byte-faithful transfer × re-baseline (QA iter 8) ────────────────────
# M1 (iter 2) made the dispatch-time exclusion byte-faithful; M2 (iter 2) made
# it survive a re-baseline. Neither existing test crosses the two: the M1 test
# adopts via include at DISPATCH only, the M2 test re-baselines an ASCII claim.
# These guard the intersection — an ADOPTED non-ASCII (Cyrillic) path must stay
# out of ``BASELINE-*.untracked`` on a re-baseline of EVERY transfer channel
# (claim / include / carry-over), the same as at dispatch time.

ADOPTED_CLAIM = "предмодуль.py"
ADOPTED_INCLUDE = "включённый.txt"
ADOPTED_CARRY = "перенос.txt"


def _rebaseline(repo, todo_id):
    """Re-baseline exactly as ``awf run_next`` does: resolve the audit links
    through ``transfer_exclusions`` and feed them back to ``create_baseline``."""
    from awf.api.hygiene import transfer_exclusions

    carry_over, include, claim = transfer_exclusions(repo, todo_id)
    api.create_baseline(
        repo, todo_id,
        carry_over=carry_over or None,
        include=include or None,
        claim=claim or None,
    )
    return carry_over, include, claim


def test_claimed_non_ascii_untracked_survives_a_rebaseline(tmp_path):
    """The CODE channel: a claimed non-ASCII predecessor blob stays excluded
    from the snapshot on the re-baseline (claim link → raw path → bridge)."""
    cc = _claim_mod()
    repo = _project(tmp_path, version=1, policy="isolate")
    api.create_baseline(repo, SRC)
    (repo / ADOPTED_CLAIM).write_text("blob\n", encoding="utf-8")  # untracked

    plan = cc.plan_claim_changes(repo, SRC)
    cc.apply_claim_changes(repo, plan, confirmed_digest=plan.digest)
    api.dispatch_todo(repo, f"# {RETRY}\nretry\n", todo_id=RETRY, claim_from=SRC)

    snap = repo / ".agentic" / "context" / f"BASELINE-{RETRY}.untracked"
    assert snap.read_text(encoding="utf-8").strip() == ""

    _carry, _include, claim = _rebaseline(repo, RETRY)
    assert claim == {ADOPTED_CLAIM}
    assert snap.read_text(encoding="utf-8").strip() == ""


def test_included_non_ascii_untracked_survives_a_rebaseline(tmp_path):
    """The FINDING include channel: a re-claimed non-ASCII untracked file
    stays excluded from the snapshot on the re-baseline (include link)."""
    repo = _project(tmp_path, version=1, policy="isolate")
    (repo / ADOPTED_INCLUDE).write_text("finding\n", encoding="utf-8")
    api.dispatch_todo(
        repo, f"# {SRC}\nTask\n", todo_id=SRC, include_untracked=[ADOPTED_INCLUDE]
    )
    snap = repo / ".agentic" / "context" / f"BASELINE-{SRC}.untracked"
    assert snap.read_text(encoding="utf-8").strip() == ""

    _carry, include, _claim = _rebaseline(repo, SRC)
    assert include == {ADOPTED_INCLUDE}
    assert snap.read_text(encoding="utf-8").strip() == ""


def test_carried_over_non_ascii_finding_survives_a_rebaseline(tmp_path):
    """The R5 carry-over channel: a rejected attempt's non-ASCII finding stays
    excluded from the snapshot on the re-baseline (carry-over link)."""
    repo = _project(tmp_path, version=1, policy="isolate")
    api.dispatch_todo(repo, f"# {SRC}\norigin\n", todo_id=SRC)
    (repo / ADOPTED_CARRY).write_text("finding\n", encoding="utf-8")
    from awf import reject_files

    reject_files.reject_files_path(repo, SRC).write_text(
        ADOPTED_CARRY + "\n", encoding="utf-8"
    )
    api.dispatch_todo(repo, f"# {RETRY}\nretry\n", todo_id=RETRY, carry_over_from=SRC)
    snap = repo / ".agentic" / "context" / f"BASELINE-{RETRY}.untracked"
    assert snap.read_text(encoding="utf-8").strip() == ""

    carry_over, _include, _claim = _rebaseline(repo, RETRY)
    assert carry_over == {ADOPTED_CARRY}
    assert snap.read_text(encoding="utf-8").strip() == ""
