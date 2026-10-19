"""R2.5 (roadmap R2) — аудит витка 10, седьмой блок.

Три находки аудитора (REVIEW-фикс 9):

* **A-1** — CAS-старое значение для unborn HEAD зашито 40 нулями (sha1):
  в sha256-репозитории ``update-ref`` отвергает его как «not a valid old
  SHA1», отказ распознаётся как ретраибельная гонка и первый коммит
  заклинен навсегда. Ждём: атрибуты формата репозитория дают zero-OID
  нужной длины — первый коммит проходит.
* **A-2** — ``archive_after_receipt`` сначала перемещает юнит, затем пишет
  ``ARCHIVE-{todo}.json``. Отказ записи факта (симлинк ``.agentic/context``)
  давал «TODO NOT archived» при уже перемещённом юните и повторялся вечно.
  Ждём: перенос успешен, факт best-effort, ``publication_facts`` выводит
  архив из дерева.
* **A-3** — у возобновлённого юнита RUN-REPORT печатал OID дважды
  («Outside the commit» + «Publication»). Ждём один дом OID.

Все репозитории — disposable в ``tmp_path``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf import commit_gate, commit_plan

TODO = "TODO-0001"


def _publish():
    from awf import publish

    return publish


def _git(repo: Path, *args: str, check: bool = True):
    r = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def _finish_repo(repo: Path) -> Path:
    from awf import paths

    (repo / ".agentic" / "logs").mkdir(parents=True, exist_ok=True)
    paths.context_dir(repo).mkdir(parents=True, exist_ok=True)
    return repo


def _repo(tmp_path: Path, *, object_format: str = "") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    init = ["init", "-q"]
    if object_format:
        init += [f"--object-format={object_format}"]
    first = _git(repo, *init, check=False)
    if first.returncode != 0 or not (repo / ".git").exists():
        pytest.skip(f"git init failed ({first.returncode}): {first.stderr.strip()}")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _seed_first_commit(repo: Path) -> Path:
    (repo / "README.md").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return _finish_repo(repo)


def _plan(files: tuple[str, ...]) -> commit_plan.CommitPlan:
    return commit_plan.CommitPlan(
        todo_id=TODO, generation=0, verified_sha="", files=files
    )


def _commit(repo: Path):
    return commit_gate._commit_via_isolated_index(
        repo, _plan(("README.md",)), f"awf(verify): {TODO}",
        repo / ".agentic" / "logs",
    )


def test_first_commit_in_sha256_repo_is_not_wedged(tmp_path):
    """A-1: zero-OID CAS must match the repository's object format."""
    publish = _publish()
    repo = _repo(tmp_path, object_format="sha256")
    # Deliberately UNBORN HEAD: the CAS old-value is the zero OID, and its
    # width is what this test is about.
    _finish_repo(repo)
    (repo / "README.md").write_text("v1\n", encoding="utf-8")

    out = _commit(repo)
    assert out.status == "committed", (
        "the first commit in a sha256 repository must publish; got "
        f"{out.status}: {out.reason}"
    )
    assert _git(repo, "rev-list", "--count", "HEAD").stdout.strip() == "1"
    assert publish.read_receipt(repo, TODO) is not None
    assert not publish.intent_file(repo, TODO).exists()


def test_archive_fact_write_failure_does_not_lie_about_the_move(tmp_path):
    """A-2: a refused ARCHIVE fact must not report the unit as not archived."""
    publish = _publish()
    from awf import paths

    repo = _seed_first_commit(_repo(tmp_path))
    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    out = _commit(repo)
    assert out.status == "committed", out.reason

    ctx = paths.context_dir(repo)
    outside = tmp_path / "outside-context"
    outside.mkdir()
    for item in ctx.iterdir():
        item.rename(outside / item.name)
    ctx.rmdir()
    ctx.symlink_to(outside, target_is_directory=True)

    (paths.inbox(repo)).mkdir(parents=True, exist_ok=True)
    (paths.inbox(repo) / f"{TODO}.md").write_text("# TODO\n", encoding="utf-8")

    archived = publish.archive_after_receipt(repo, TODO)
    dest = paths.done_dir(repo) / TODO
    assert archived == dest, (
        "the move succeeded — the archive step must return it even when the "
        f"fact write is refused; got {archived!r}"
    )
    assert dest.is_dir(), "the unit must actually be in done/"
    facts = publish.publication_facts(repo, TODO)
    assert facts["archived"], (
        "publication_facts must derive the archive from the tree when the "
        "ARCHIVE fact could not be written"
    )


def test_resumed_publish_report_names_the_commit_once(tmp_path):
    """A-3: the resumed unit's OID has ONE home in RUN-REPORT."""
    publish = _publish()
    from awf.api import run as run_api

    repo = _seed_first_commit(_repo(tmp_path))
    (repo / "README.md").write_text("v2\n", encoding="utf-8")

    with pytest.raises(publish.PublishCrash):
        commit_gate._commit_via_isolated_index(
            repo, _plan(("README.md",)), f"awf(verify): {TODO}",
            repo / ".agentic" / "logs", crash_at="index_sync",
        )
    out = commit_gate.maybe_commit(
        "verify", TODO, "commit_and_report", repo,
        repo / ".agentic" / "logs", auto=False,
    )
    assert out.status == "committed", out.reason
    assert out.sha

    state = {
        "current": TODO,
        "queue": [{"todo_id": TODO}],
        "completed": [TODO],
        "rejects": {},
    }
    text = run_api._write_report(repo, state, "test reason").read_text(
        encoding="utf-8"
    )
    assert text.count(out.sha) == 1, (
        "the commit OID must have ONE home in RUN-REPORT; a resumed publish "
        f"rendered it {text.count(out.sha)} times:\n{text}"
    )
    assert "## Outside the commit" in text
    assert "## Publication" in text
    assert f"- {TODO}: receipt committed" in text
