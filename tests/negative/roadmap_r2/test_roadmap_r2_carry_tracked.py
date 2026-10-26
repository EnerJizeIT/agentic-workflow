"""RB-2 (TODO-0259): carry_over_from переносит и tracked-дельту.

Наблюдение 0254→0257: REJECT-файл хранил только untracked, а tracked-правки
отклонённого юнита после повторной выдачи попадали в baseline как
предсуществующая грязная работа. Кандидат и коммит их исключали — пришлось
вручную снимать BASELINE-*.worktree/status и делать base-коммит.

Два регресс-теста:
1. tracked-изменение отклонено → retry с carry_over_from → кандидат и
   коммит содержат изменение;
2. без carry_over_from тот же tracked-путь остаётся исключённым.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from awf import api, candidate_verify, commit_gate, commit_plan, reject_files

SRC = "TODO-0001"
RETRY = "TODO-0002"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")
    api.init_project(
        repo,
        project_name="RB2",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    (repo / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
    (repo / "module.py").write_text("v1\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_module.py").write_text(
        "def test_module():\n    assert True\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _attempt(repo: Path) -> None:
    """The rejected attempt: a tracked edit, a tracked test edit and a new
    untracked file."""
    (repo / "module.py").write_text("attempt\n", encoding="utf-8")
    (repo / "tests" / "test_module.py").write_text(
        "def test_module():\n    assert 'attempt'\n", encoding="utf-8"
    )
    (repo / "newfile.py").write_text("new\n", encoding="utf-8")


def _reject_attempt(repo: Path) -> str:
    origin = api.dispatch_todo(repo, f"# {SRC}\norigin\n", todo_id=SRC)
    _attempt(repo)
    result = api.reject_commit(repo, SRC, "rejected for the test")
    assert set(result.reject_files) == {
        "module.py",
        "tests/test_module.py",
        "newfile.py",
    }, "the reject snapshot must record tracked AND new untracked paths"
    assert set(reject_files.read_reject_files(repo, SRC) or []) == {
        "module.py",
        "tests/test_module.py",
        "newfile.py",
    }
    return origin.baseline_sha


def test_carry_over_from_adopts_rejected_tracked_delta(tmp_path):
    repo = _repo(tmp_path)
    origin_sha = _reject_attempt(repo)

    api.dispatch_todo(
        repo, f"# {RETRY}\nretry\n", todo_id=RETRY, carry_over_from=SRC
    )
    baseline = commit_plan.read_baseline_sha(repo, RETRY)
    assert baseline, "the retry must have a baseline"
    assert baseline == origin_sha, (
        "the retry baseline must stay the rejected unit's baseline — no "
        "base-commit and no moved HEAD"
    )

    plan = commit_plan._plan_split(repo, baseline, RETRY)
    carried = {"module.py", "tests/test_module.py", "newfile.py"}
    assert carried <= set(plan.files), (
        "the retry plan must own the rejected attempt's tracked delta; "
        f"files={plan.files!r}, excluded={plan.excluded!r}"
    )

    built = candidate_verify.build_evidence(
        repo, RETRY, baseline, commands=["true"], worktree_dir=tmp_path / "wt"
    )
    assert built.refusal == "", built.refusal
    assert built.materialization is not None
    assert carried <= set(built.materialization.plan.files)
    assert built.materialization.degraded == (), (
        "carried tracked paths are full-file owned, not degraded — approve "
        "must not need confirm_degraded; "
        f"degraded={built.materialization.degraded!r}"
    )
    assert (built.materialization.worktree / "module.py").read_text(
        encoding="utf-8"
    ) == "attempt\n", "the candidate must carry the attempt's tracked content"

    logs = repo / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    commit_plan_obj = commit_plan.build_commit_plan(repo, RETRY, baseline)
    assert carried <= set(commit_plan_obj.files)
    outcome = commit_gate._commit_via_isolated_index(
        repo, commit_plan_obj, f"awf(verify): {RETRY}", logs
    )
    assert outcome.status == "committed", (outcome.status, outcome.reason)
    assert _git(repo, "show", "HEAD:module.py").stdout == "attempt\n", (
        "the retry commit must contain the rejected attempt's tracked delta"
    )
    assert _git(repo, "show", "HEAD:newfile.py").stdout == "new\n"


def test_plain_retry_keeps_tracked_delta_excluded(tmp_path):
    repo = _repo(tmp_path)
    _reject_attempt(repo)

    api.dispatch_todo(repo, f"# {RETRY}\nretry\n", todo_id=RETRY)
    baseline = commit_plan.read_baseline_sha(repo, RETRY)
    plan = commit_plan._plan_split(repo, baseline, RETRY)

    assert "module.py" not in set(plan.files), (
        "without carry_over_from a pre-dispatch tracked change stays foreign; "
        f"files={plan.files!r}"
    )
    assert "tests/test_module.py" not in set(plan.files)
    assert "newfile.py" not in set(plan.files)


def test_a_second_reject_keeps_the_carried_tracked_delta(tmp_path):
    """A → B → C: B's baseline sits on A's dirty tree, so A's paths are dirty
    in B's own baseline. B must still RECORD them — otherwise C (the retry of
    B) treats them as pre-existing and silently drops them from its commit.
    """
    repo = _repo(tmp_path)
    _reject_attempt(repo)

    second = "TODO-0003"
    api.dispatch_todo(
        repo, f"# {RETRY}\nretry\n", todo_id=RETRY, carry_over_from=SRC
    )
    api.reject_commit(repo, RETRY, "rejected again")

    recorded = set(reject_files.read_reject_files(repo, RETRY) or [])
    assert {"module.py", "tests/test_module.py"} <= recorded, (
        "the retry's own reject must keep the adopted tracked paths; "
        f"recorded={sorted(recorded)!r}"
    )

    api.dispatch_todo(
        repo, f"# {second}\nretry again\n", todo_id=second, carry_over_from=RETRY
    )
    baseline = commit_plan.read_baseline_sha(repo, second)
    plan = commit_plan._plan_split(repo, baseline, second)

    assert {"module.py", "tests/test_module.py", "newfile.py"} <= set(plan.files), (
        "a three-unit retry chain must not lose the tracked delta; "
        f"files={plan.files!r}, excluded={plan.excluded!r}"
    )


def test_carry_over_adopts_a_non_ascii_tracked_path(tmp_path):
    """QA (TODO-0259): the tracked half must be byte-faithful.

    ``_tracked_attempt_paths`` bridges the model's raw bytes and the baseline
    status text (``raw_identity``/``display_path``). A wrong bridge drops a
    non-ASCII path from the REJECT file silently and the retry loses the
    attempt's edit — the leak this unit closes, only for non-ASCII names.
    """
    repo = _repo(tmp_path)
    name = "файл.py"
    (repo / name).write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unicode base")

    api.dispatch_todo(repo, f"# {SRC}\norigin\n", todo_id=SRC)
    (repo / name).write_text("attempt\n", encoding="utf-8")
    api.reject_commit(repo, SRC, "rejected for the test")

    recorded = set(reject_files.read_reject_files(repo, SRC) or [])
    assert name in recorded, (
        f"a non-ASCII tracked edit must be recorded byte-faithfully; "
        f"recorded={sorted(recorded)!r}"
    )

    api.dispatch_todo(repo, f"# {RETRY}\nretry\n", todo_id=RETRY, carry_over_from=SRC)
    baseline = commit_plan.read_baseline_sha(repo, RETRY)
    plan = commit_plan._plan_split(repo, baseline, RETRY)
    assert name in set(plan.files), (
        f"the retry must own the non-ASCII tracked edit; files={plan.files!r}"
    )

    logs = repo / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    plan_obj = commit_plan.build_commit_plan(repo, RETRY, baseline)
    outcome = commit_gate._commit_via_isolated_index(
        repo, plan_obj, f"awf(verify): {RETRY}", logs
    )
    assert outcome.status == "committed", (outcome.status, outcome.reason)
    assert _git(repo, "show", f"HEAD:{name}").stdout == "attempt\n"


def test_carry_over_adopts_a_tracked_deletion(tmp_path):
    """QA (TODO-0259): a deletion is a tracked delta too.

    The attempt removes a tracked file; the REJECT file must list it and the
    retry's commit must REMOVE it (a missing worktree path is not a missing
    plan entry — the isolated index has to drop the blob).
    """
    repo = _repo(tmp_path)
    api.dispatch_todo(repo, f"# {SRC}\norigin\n", todo_id=SRC)
    (repo / "module.py").unlink()
    api.reject_commit(repo, SRC, "rejected for the test")

    assert "module.py" in set(reject_files.read_reject_files(repo, SRC) or []), (
        "a tracked deletion must be recorded as the attempt's delta"
    )

    api.dispatch_todo(repo, f"# {RETRY}\nretry\n", todo_id=RETRY, carry_over_from=SRC)
    baseline = commit_plan.read_baseline_sha(repo, RETRY)
    plan = commit_plan._plan_split(repo, baseline, RETRY)
    assert "module.py" in set(plan.files), (
        f"the retry must own the deletion; files={plan.files!r}"
    )

    logs = repo / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    plan_obj = commit_plan.build_commit_plan(repo, RETRY, baseline)
    outcome = commit_gate._commit_via_isolated_index(
        repo, plan_obj, f"awf(verify): {RETRY}", logs
    )
    assert outcome.status == "committed", (outcome.status, outcome.reason)
    gone = subprocess.run(
        ["git", "cat-file", "-e", "HEAD:module.py"],
        cwd=repo,
        capture_output=True,
    )
    assert gone.returncode != 0, "the retry commit must not resurrect module.py"


def test_carry_over_adopts_both_sides_of_a_staged_rename(tmp_path):
    """QA (TODO-0259): a rename is ONE pair — both sides must carry.

    Recording only the destination would leave the source as a foreign
    deletion (or resurrect the old path); recording neither would drop the
    attempt's rename. The retry's commit must contain the new path and NOT
    the old one.
    """
    repo = _repo(tmp_path)
    api.dispatch_todo(repo, f"# {SRC}\norigin\n", todo_id=SRC)
    _git(repo, "mv", "module.py", "renamed.py")
    (repo / "renamed.py").write_text("attempt\n", encoding="utf-8")
    api.reject_commit(repo, SRC, "rejected for the test")

    recorded = set(reject_files.read_reject_files(repo, SRC) or [])
    assert {"module.py", "renamed.py"} <= recorded, (
        f"a rename pair must carry both sides; recorded={sorted(recorded)!r}"
    )

    api.dispatch_todo(repo, f"# {RETRY}\nretry\n", todo_id=RETRY, carry_over_from=SRC)
    baseline = commit_plan.read_baseline_sha(repo, RETRY)
    plan = commit_plan._plan_split(repo, baseline, RETRY)
    assert {"module.py", "renamed.py"} <= set(plan.files), (
        f"the retry must own the pair; files={plan.files!r}"
    )

    logs = repo / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    plan_obj = commit_plan.build_commit_plan(repo, RETRY, baseline)
    outcome = commit_gate._commit_via_isolated_index(
        repo, plan_obj, f"awf(verify): {RETRY}", logs
    )
    assert outcome.status == "committed", (outcome.status, outcome.reason)
    assert _git(repo, "show", "HEAD:renamed.py").stdout == "attempt\n"
    old = subprocess.run(
        ["git", "cat-file", "-e", "HEAD:module.py"], cwd=repo, capture_output=True
    )
    assert old.returncode != 0, "the old path must not survive the rename commit"
