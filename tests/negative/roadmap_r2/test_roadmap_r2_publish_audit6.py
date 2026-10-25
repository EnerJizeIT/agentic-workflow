"""R2.5 (roadmap R2) — находки аудитора, виток 9 (agent-project-auditor).

Шесть точечных дефектов протокола публикации, найденных после QA-витка:
A1 — ``commit-tree`` не гоняет ``commit-msg``/``post-commit``;
A2 — постоянный отказ ``update-ref`` (несуществующий OID) заклинивает TODO;
A3 — RUN-REPORT падает strict-UTF-8 на байт-верном пути;
A4 — транзиентный сбой чтения HEAD снимает живой intent;
A5 — owned-синк делает по одному ``git reset`` на путь.
A6 — текст про Windows в ``docs/design.md`` (проверяется глазами, не тестом).

Все репозитории — disposable в ``tmp_path``. Реальный git не трогается.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

TODO = "TODO-0001"


def _publish():
    from awf import publish

    return publish


def _git(repo: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, capture_output=True, text=True
    )


def _repo(tmp_path: Path) -> Path:
    from awf import paths

    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    (repo / ".agentic" / "logs").mkdir(parents=True)
    paths.context_dir(repo).mkdir(parents=True, exist_ok=True)
    return repo


def _hook(repo: Path, name: str, body: str) -> Path:
    hook = repo / ".git" / "hooks" / name
    hook.write_text(body, encoding="utf-8")
    hook.chmod(0o755)
    return hook


def _plan(repo: Path, files: tuple[str, ...]):
    from awf import commit_plan

    return commit_plan.CommitPlan(
        todo_id=TODO, generation=0, verified_sha="", files=files
    )


def _publish_change(repo: Path, text: str = "v2\n"):
    from awf import commit_gate

    (repo / "README.md").write_text(text, encoding="utf-8")
    return commit_gate._commit_via_isolated_index(
        repo, _plan(repo, ("README.md",)), f"awf(verify): {TODO}",
        repo / ".agentic" / "logs",
    )


def test_commit_msg_rejection_blocks_the_publication(tmp_path):
    """A1: ``commit-msg`` с exit 1 обязан отклонить публикацию.

    ``commit-tree`` (в отличие от ``git commit``) хуки сообщения не гоняет:
    политика сообщений молча отключалась. Хук идёт ДО объекта и intent —
    HEAD не двигается, коммита нет."""
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _hook(repo, "commit-msg", "#!/bin/sh\necho 'commit-msg REJECTED' >&2\nexit 1\n")

    out = _publish_change(repo)

    assert out.status != "committed", (
        "a repository that rejects messages through commit-msg must not get "
        f"a commit; got {out.status}: {out.reason}"
    )
    assert "commit-msg" in (out.reason or ""), (
        f"the refusal must name the rejecting hook; got {out.reason!r}"
    )
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head, (
        "a rejected commit-msg must leave HEAD where it was"
    )
    assert not _publish().read_receipt(repo, TODO), (
        "a rejected publication must not leave a committed receipt"
    )


def test_post_commit_hook_runs_after_the_ref_moves(tmp_path):
    """A1: ``post-commit`` выполняется после публикации (объект в HEAD)."""
    repo = _repo(tmp_path)
    _hook(
        repo,
        "post-commit",
        "#!/bin/sh\ntouch \"$(git rev-parse --show-toplevel)/POST_COMMIT_RAN\"\n",
    )

    out = _publish_change(repo)

    assert out.status == "committed", f"publish failed: {out.status}: {out.reason}"
    assert (repo / "POST_COMMIT_RAN").exists(), (
        "post-commit must run when the publication moved the ref — "
        "commit-tree + update-ref used to bypass it silently"
    )


def test_malformed_intent_oid_is_not_a_live_operation(tmp_path):
    """A2: intent с ``"deadbeef"`` не читается как живая операция.

    ``read_intent`` принимал любой непустой OID: ``update-ref`` отказывал
    на каждом resume, intent оставался навсегда — TODO заклинен."""
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    ctx = repo / ".agentic" / "context"
    intent = {
        "todo_id": TODO,
        "generation": 0,
        "expected_head": head,
        "tree_oid": "",
        "commit_oid": "deadbeef",
        "candidate_tree": "",
        "files": [],
        "delta_files": [],
        "degraded": [],
        "message": "x",
        "index_orig": {},
        "evidence_refs": [],
        "transfers": [],
        "residuals": [],
        "created_at": "",
    }
    (ctx / f"PUBLISH-INTENT-{TODO}.json").write_text(
        json.dumps(intent), encoding="utf-8"
    )

    assert _publish().read_intent(repo, TODO) is None, (
        "an OID that is not a full object id cannot describe a live intent"
    )
    assert _publish().resume_if_pending(repo, TODO) is None, (
        "the caller must run fresh instead of resuming a malformed intent"
    )


def test_missing_commit_object_supersedes_instead_of_wedging(tmp_path):
    """A2: OID правильной формы, но объекта нет — отказ не вечный.

    После отказа intent получает durable-факт ``superseded`` и снимается:
    следующий вызов не упирается в тот же отказ."""
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    ctx = repo / ".agentic" / "context"
    missing = "a" * 40
    intent = {
        "todo_id": TODO,
        "generation": 0,
        "expected_head": head,
        "tree_oid": "",
        "commit_oid": missing,
        "candidate_tree": "",
        "files": [],
        "delta_files": [],
        "degraded": [],
        "message": "x",
        "index_orig": {},
        "evidence_refs": [],
        "transfers": [],
        "residuals": [],
        "created_at": "",
    }
    (ctx / f"PUBLISH-INTENT-{TODO}.json").write_text(
        json.dumps(intent), encoding="utf-8"
    )

    first = _publish().resume_if_pending(repo, TODO)
    assert first is not None and first.status != "committed", (
        f"a missing commit object cannot be published; got {first}"
    )
    assert _publish().intent_file(repo, TODO).exists() is False, (
        "the unusable intent must be dropped, otherwise every later call "
        "refuses on it forever"
    )
    assert _publish().superseded_file(repo, TODO).exists(), (
        "dropping the intent must leave the durable superseded fact"
    )
    second = _publish().resume_if_pending(repo, TODO)
    assert second is None, (
        "after the supersede the TODO is replanned fresh, not wedged on "
        f"the same refusal; got {second}"
    )


def test_run_report_escapes_non_utf8_path(tmp_path):
    """A3: RUN-REPORT не падает strict-UTF-8 на байт-верном пути."""
    from awf.api import run as run_api

    repo = _repo(tmp_path)
    raw_name = os.fsdecode(b"bad\xff.txt")
    ctx = repo / ".agentic" / "context"
    (ctx / f"COMMIT-AUDIT-{TODO}.json").write_text(
        json.dumps(
            {
                "todo_id": TODO,
                "status": "committed",
                "excluded": [[raw_name, "foreign entry"]],
                "worker_staged": [raw_name],
                "skip_inconsistent": False,
                "skip_reason": "",
            },
            ensure_ascii=True,
        ),
        encoding="utf-8",
    )

    lines = run_api._outside_commit_lines(repo, [TODO])
    text = "\n".join(lines)
    text.encode("utf-8")
    assert "bad" in text and "\\xff" in text, (
        "the byte-faithful path must render escaped, not break the report"
    )

    report = run_api._write_report(
        repo,
        {"queue": [TODO], "completed": [TODO], "rejects": {}},
        "test close",
    )
    assert report is not None and report.is_file(), (
        "closing the run must keep writing its report for a byte-faithful path"
    )
    report.read_text(encoding="utf-8")


def test_head_read_failure_preserves_the_live_intent(tmp_path, monkeypatch):
    """A4: сбой чтения HEAD — ERROR, intent остаётся на диске."""
    repo = _repo(tmp_path)
    publish = _publish()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    ctx = repo / ".agentic" / "context"
    intent = {
        "todo_id": TODO,
        "generation": 0,
        "expected_head": head,
        "tree_oid": "",
        "commit_oid": "a" * 40,
        "candidate_tree": "",
        "files": [],
        "delta_files": [],
        "degraded": [],
        "message": "x",
        "index_orig": {},
        "evidence_refs": [],
        "transfers": [],
        "residuals": [],
        "created_at": "",
    }
    (ctx / f"PUBLISH-INTENT-{TODO}.json").write_text(
        json.dumps(intent), encoding="utf-8"
    )
    monkeypatch.setattr(publish, "_read_head", lambda _p: (False, ""))

    out = publish.resume_if_pending(repo, TODO)

    assert out is not None and out.status == "error", (
        f"an unreadable HEAD is a transient error, not «HEAD moved»; got {out}"
    )
    assert publish.intent_file(repo, TODO).exists(), (
        "the resumable anchor must survive a transient HEAD read failure"
    )
    assert not publish.superseded_file(repo, TODO).exists(), (
        "a read failure must not record the destructive superseded fact"
    )


def test_owned_index_sync_uses_one_reset_for_the_whole_set(tmp_path, monkeypatch):
    """A5: owned-пути сбрасываются одним ``git reset``, не по одному."""
    repo = _repo(tmp_path)
    publish = _publish()
    names = [f"f{i}.txt" for i in range(5)]
    for name in names:
        (repo / name).write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "files")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    index_orig = publish._index_entries(repo, names)
    intent = publish.PublishIntent(
        todo_id=TODO,
        generation=0,
        expected_head=head,
        tree_oid="",
        commit_oid=head,
        candidate_tree="",
        files=tuple(names),
        delta_files=(),
        degraded=(),
        message="x",
        index_orig=index_orig,
        evidence_refs=(),
        transfers=(),
        residuals=(),
        created_at="",
    )

    calls: list[list[str]] = []
    real_git = publish._git

    def counting_git(project_dir, args, env=None):
        calls.append(list(args))
        return real_git(project_dir, args, env=env)

    monkeypatch.setattr(publish, "_git", counting_git)
    conflicts = publish._sync_owned_index(repo, intent)

    resets = [c for c in calls if c[:2] == ["reset", "-q"]]
    assert conflicts == [], f"owned entries must sync cleanly; got {conflicts}"
    assert len(resets) == 1, (
        f"expected ONE batched reset for {len(names)} owned paths; got "
        f"{len(resets)} calls: {resets}"
    )
    assert resets[0][-len(names):] == names, (
        f"the batch must carry every owned path; got {resets[0]!r}"
    )

