"""R2.5 (roadmap R2) — QA-проба границ, третий блок (agent-qa-review, iter 5).

Независимые тесты поверх ``test_roadmap_r2_publish.py`` и ``..._qa2.py``:
проход по КЛЕТКАМ таблицы покрытия инварианта (поверхность x исход), не
покрытым приёмкой. Каждый тест красный на сломанном поведении (проверен
мутационной пробой), зелёный на текущем дереве.

Ядро проверок:
* crash в UNBORN-HEAD репозитории (нет ни одного коммита) на стадиях
  commit_object / update_ref / index_sync — повтор делает РОВНО один
  первый коммит (CAS zero-OID), артефакт не теряется;
* resume признаёт лёгший коммит предком уехавшего HEAD (D1) и НЕ создаёт
  второй коммит;
* архив: durable факт ``ARCHIVE-{todo}.json`` и ``publication_facts``
  согласованы по одной форме пути; повтор архива не теряет артефакты.
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


def _empty_repo(tmp_path: Path) -> Path:
    """A repo with NO commits (unborn HEAD) — the supported git init state."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _repo(tmp_path: Path) -> Path:
    repo = _empty_repo(tmp_path)
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


def _count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD").stdout.strip())


def _plan(files, base: str = ""):
    from awf import commit_plan

    return commit_plan.CommitPlan(
        todo_id=TODO, generation=0, verified_sha="", files=files, baseline_sha=base
    )


def _commit(repo: Path, files, crash_at: str = "", base: str = ""):
    from awf import commit_gate

    return commit_gate._commit_via_isolated_index(
        repo, _plan(files, base), f"awf(verify): {TODO}", _logs(repo), crash_at=crash_at
    )


def _resume(repo: Path, base: str = ""):
    from awf import commit_gate

    return commit_gate.maybe_commit(
        "verify", TODO, "commit_and_report", repo, _logs(repo), auto=False,
        baseline_sha=base,
    )


# ─── unborn HEAD: crash matrix on a repo with no commits ──────────────────
#
# The acceptance matrix (§8) uses a repo that already has an init commit.
# A repo with an UNBORN HEAD is the other supported shape (AUD05-08): the
# first unit commit has no parent and its CAS old-value must be the ZERO
# OID. A crash anywhere in that first publication must resume to EXACTLY
# one first commit — never two roots, never a lost artifact.


@pytest.mark.parametrize("stage", ["commit_object", "update_ref", "index_sync"])
def test_unborn_head_crash_then_resume_makes_one_first_commit(tmp_path, stage):
    publish = _publish()
    repo = _empty_repo(tmp_path)
    (repo / "unit.txt").write_text("u\n", encoding="utf-8")

    with pytest.raises(publish.PublishCrash):
        _commit(repo, ("unit.txt",), crash_at=stage)

    out = _resume(repo)
    assert out.status == "committed", out.reason
    # Exactly ONE commit overall: the resumable protocol did not create a
    # second root commit nor leave the object dangling.
    assert _count(repo) == 1
    assert _git(repo, "log", "-1", "--format=%s").stdout.strip()
    receipt = publish.read_receipt(repo, TODO)
    assert receipt is not None and receipt.disposition == "committed"
    assert not publish.intent_file(repo, TODO).exists()


def test_unborn_head_first_commit_has_no_parent(tmp_path):
    """The first commit in an unborn-HEAD repo is a ROOT commit (no -p)."""
    repo = _empty_repo(tmp_path)
    (repo / "unit.txt").write_text("u\n", encoding="utf-8")
    out = _commit(repo, ("unit.txt",))
    assert out.status == "committed", out.reason
    parents = _git(repo, "rev-list", "--parents", "-1", "HEAD").stdout.split()
    assert len(parents) == 1  # just the commit, no parent listed


# ─── D1 (iter 5): descendant HEAD must not create a second commit ─────────


def test_resume_on_descendant_head_creates_no_second_commit(tmp_path):
    """The D1 fix recognizes a landed commit as an ANCESTOR of a moved HEAD
    and finishes the receipt. The strict claim is not only «committed» — it
    is also «no second commit»: the ref already carries the unit's work, so
    resume must NOT add another commit on top of the later actor's one."""
    publish = _publish()
    repo = _repo(tmp_path)
    base = _head(repo)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")

    with pytest.raises(publish.PublishCrash):
        _commit(repo, ("README.md",), crash_at="update_ref", base=base)
    unit_oid = publish.read_intent(repo, TODO).commit_oid
    # The sync the crash skipped, then an unrelated commit on top.
    _git(repo, "reset", "-q", "HEAD", "--", "README.md")
    (repo / "later.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-qm", "later actor")
    count = _count(repo)
    head_after_actor = _head(repo)

    out = _resume(repo)
    assert out.status == "committed", out.reason
    assert _count(repo) == count, "resume added a commit on top of a landed one"
    assert _head(repo) == head_after_actor, "resume moved HEAD"
    receipt = publish.read_receipt(repo, TODO)
    assert receipt is not None and receipt.commit_oid == unit_oid


# ─── archive fact: one shape, consistent with publication_facts ───────────


def test_archive_fact_and_publication_facts_agree(tmp_path):
    """Requirement 4: the archive is ONE fact. ``archive_after_receipt``
    writes ``ARCHIVE-{todo}.json`` with a project-relative archive directory;
    ``publication_facts`` must report the SAME locus (not an absolute path,
    not a file), and an idempotent re-archive must keep it and lose nothing."""
    publish = _publish()
    repo = _repo(tmp_path)
    inbox = repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{TODO}.md").write_text("# TODO\n", encoding="utf-8")
    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    _commit(repo, ("README.md",))

    archived = publish.archive_after_receipt(repo, TODO)
    assert archived is not None

    fact = publish._read_json(publish.archive_fact_file(repo, TODO))
    assert fact is not None
    assert fact["archived"] and not Path(fact["archived"]).is_absolute()
    assert fact["disposition"] == "committed"

    facts = publish.publication_facts(repo, TODO)
    assert facts["archived"] == fact["archived"]
    assert facts["archived"] == str(Path(".agentic") / "done" / TODO)

    # Idempotent re-archive: the tree stays, the fact stays, nothing lost.
    again = publish.archive_after_receipt(repo, TODO)
    assert again is not None
    assert (repo / ".agentic" / "done" / TODO / "TODO.md").is_file()
    assert publish.publication_facts(repo, TODO)["archived"] == fact["archived"]


def test_crash_after_archive_records_fact_on_resume(tmp_path):
    """Столбец «archive» x «crash после шага»: the move may have landed while
    the durable ``ARCHIVE-{todo}.json`` fact did not. Resume must write the
    fact (no artifact lost) and stay idempotent."""
    publish = _publish()
    repo = _repo(tmp_path)
    inbox = repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{TODO}.md").write_text("# TODO\n", encoding="utf-8")
    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    _commit(repo, ("README.md",))

    with pytest.raises(publish.PublishCrash):
        publish.archive_after_receipt(repo, TODO, crash_at="archive")
    assert not publish.archive_fact_file(repo, TODO).exists()

    archived = publish.archive_after_receipt(repo, TODO)
    assert archived is not None
    assert publish.archive_fact_file(repo, TODO).exists()
    facts = publish.publication_facts(repo, TODO)
    assert facts["archived"] == str(Path(".agentic") / "done" / TODO)


# ─── QA iter 5: the "no state" catch-list is NOT delivered to every caller ──
#
# L1 typed the state-file failures as ``_STATE_WRITE_FAILURES`` on the WRITER
# (``_mutate_state_locked``) and on ``clear_state``'s own log line. But
# ``clear_state`` PROPAGATES (by design — ``reset`` needs ``partial_failure``),
# and it now raises ``ManagedPathError`` where it used to raise ``OSError`` for
# the very same symlinked ``.agentic/state``. Every caller that caught
# ``except OSError`` around ``clear_state`` therefore lost its degradation path:
# ``kill_pipeline`` (and ``_clear_state_keep_salvage``) now crash the kill
# instead of clearing what they can and reporting. Same for ``clear_run``.


def _symlinked_state_project(tmp_path: Path) -> Path:
    """A project whose ``.agentic/state`` is a symlink OUT of the project.

    The managed no-follow barrier refuses every write/remove there — the
    scenario the state writers degrade on ("no state").
    """
    project = tmp_path / "project"
    (project / ".agentic").mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text("project:\n  name: p\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".agentic" / "state").symlink_to(outside, target_is_directory=True)
    (outside / "current.yaml").write_text(
        "todo_id: TODO-0001\nstage_name: agent-implementer\n", encoding="utf-8"
    )
    return project


def test_clear_state_keep_salvage_degrades_on_symlinked_state(tmp_path):
    """RED: ``_clear_state_keep_salvage`` documents "best-effort" and catches
    ``OSError``, but the R1 carry made ``clear_state`` raise
    ``ManagedPathError`` (a plain ``Exception``) for a symlinked
    ``.agentic/state``. The kill path (``kill_pipeline`` → this helper) now
    dies on the barrier instead of clearing what it can."""
    from awf.api import pipeline as api_pipeline

    project = _symlinked_state_project(tmp_path)
    api_pipeline._clear_state_keep_salvage(project)  # must not raise


def test_kill_pipeline_survives_symlinked_state(tmp_path, monkeypatch):
    """RED: the same hole one level up — ``awf_kill`` on a project whose
    ``.agentic/state`` is a symlink. The kill must answer (``killed=False``:
    no live pid), not raise the barrier out of the MCP tool."""
    from awf.api import pipeline as api_pipeline

    project = _symlinked_state_project(tmp_path)
    out = api_pipeline.kill_pipeline(project)
    assert out["killed"] is False


def test_clear_run_degrades_on_symlinked_state(tmp_path):
    """RED: ``run_state.clear_run`` has no catch at all — the soft reset of a
    run whose ``.agentic/state`` is a symlink raises ``ManagedPathError``
    where ``clear_state``'s callers expect the OSError family. The documented
    contract is «a state removal that cannot proceed is reported, not an
    unhandled barrier»."""
    from awf import run_state

    project = _symlinked_state_project(tmp_path)
    try:
        run_state.clear_run(project)
    except OSError:
        pass  # the pre-R1 family — callers already handle it


def test_finish_facts_degrades_on_symlinked_state(tmp_path):
    """RED: the run-close gate (``_finish_facts``) catches ``OSError`` around
    ``clear_state`` and records ``stage_cleared=False`` on a real failure.
    After the R1 carry the same refusal is ``ManagedPathError`` — it escapes
    the close gate, so ``awf_run_finish`` raises instead of refusing with the
    «stale stage state» answer. Same family as the kill path above."""
    from awf.api import run as api_run

    project = _symlinked_state_project(tmp_path)
    done = project / ".agentic" / "done" / TODO
    done.mkdir(parents=True)
    (done / "TODO.md").write_text("# unit\n", encoding="utf-8")

    facts = api_run._finish_facts(project, TODO)
    assert facts["archived"] is True
    assert facts["stage_cleared"] is False


def test_clean_exit_state_clear_degrades_on_symlinked_state(tmp_path):
    """RED: the pipeline's clean-exit path (``orchestrator``) also catches
    only ``OSError`` around ``clear_state`` — the same barrier escapes it and
    a completed run crashes at its last step instead of writing phase=done."""
    import awf.orchestrator as orchestrator

    project = _symlinked_state_project(tmp_path)
    # The guard is the catch-list itself: exercise the same call shape the
    # clean-exit block uses, with the barrier armed.
    from awf.pipeline_state import clear_state

    try:
        clear_state(project, logs_dir=None)
    except OSError:
        pass
    except Exception as e:  # noqa: BLE001 — the escape this test proves
        raise AssertionError(
            "clear_state raised a non-OSError on a symlinked .agentic/state: "
            f"{type(e).__name__} — every `except OSError` caller "
            f"({orchestrator.__name__} clean exit) loses its degradation path"
        ) from e


def test_service_rollback_degrades_on_symlinked_state(tmp_path):
    """RED: ``_rollback_service_side_effects`` promises «a cleanup failure is
    not raised (the refusal still stands)» and calls ``clear_run`` bare. With
    the R1 carry that call raises ``ManagedPathError`` on a symlinked
    ``.agentic/state`` — a refused service start now dies inside its own
    rollback instead of returning the refusal."""
    from awf.api import run as api_run

    project = _symlinked_state_project(tmp_path)
    (project / ".agentic" / "inbox").mkdir(parents=True, exist_ok=True)
    api_run._rollback_service_side_effects(
        project, TODO, "service-unit", clear_slot=True
    )
