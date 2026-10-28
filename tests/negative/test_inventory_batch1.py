"""Инвентарь 0239, батч 1: metrics-кэши, run.py, managed_mkdir, A3.

Каждый кейс — симлинк управляемой поверхности наружу проекта: эффект не
должен случиться ВНЕ проекта, канарейка остаётся целой. Проекты —
disposable в ``tmp_path``; канарейки лежат рядом, вне проекта.

(a) ``awf/metrics.py`` — оба кэша пишутся managed-примитивом;
(b) ``awf/api/run.py`` — два отчёта, ``.ready`` и удаления идут через
    no-follow барьер;
(c) ``managed_fs.managed_mkdir`` — компонент-симлинк отказывает до
    создания каталога; два переведённых сайта (approve inbox,
    ``_unblock_todo`` context) не выходят наружу даже при свопе после
    preflight;
(d) A3 — note не пишет «BLOCKED cleared», если перенос не состоялся.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

T = "TODO-0001"
MODELS_CATALOG = {
    "openai": {
        "models": {
            "gpt": {"name": "GPT", "cost": {"input": 1, "output": 2}},
        }
    }
}


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for name in ("inbox", "outbox", "context", "state", "logs", "done"):
        (project / ".agentic" / name).mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text(
        "project: demo\nmodels: {}\n", encoding="utf-8"
    )
    return project


def _outside(tmp_path: Path, name: str = "outside") -> Path:
    outside = tmp_path / name
    outside.mkdir()
    (outside / "canary.txt").write_text("keep me", encoding="utf-8")
    return outside


def _swap_for_symlink(path: Path, target: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    path.symlink_to(target, target_is_directory=True)


# ── (a) metrics: оба кэша ───────────────────────────────────────────────


def test_metrics_models_cache_refuses_symlinked_state(tmp_path):
    from awf import metrics

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    _swap_for_symlink(project / ".agentic" / "state", outside)

    warnings: list[str] = []
    saved = metrics.save_models_cache(
        MODELS_CATALOG, ["openai/gpt"], project, None, warnings
    )

    assert saved is None
    assert any("не записан" in w for w in warnings)
    assert not (outside / "metrics_models_cache.json").exists()
    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"


def test_metrics_models_cache_writes_normal_state(tmp_path):
    from awf import metrics

    project = _project(tmp_path)
    warnings: list[str] = []
    saved = metrics.save_models_cache(
        MODELS_CATALOG, ["openai/gpt"], project, None, warnings
    )

    assert saved
    assert warnings == []
    data = json.loads(
        (project / ".agentic" / "state" / "metrics_models_cache.json").read_text(
            encoding="utf-8"
        )
    )
    assert data["catalog"]["openai"]["models"]["gpt"]["cost"]["input"] == 1


def test_metrics_subscriptions_cache_refuses_symlinked_state(tmp_path, monkeypatch):
    from awf import metrics

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    _swap_for_symlink(project / ".agentic" / "state", outside)
    payload = {
        "as_of": "2026-10-21",
        "plans": [{"name": "pro", "price_usd_month": 20}],
    }
    monkeypatch.setattr(
        metrics, "refresh_subscription_table", lambda url, warnings: (payload, None)
    )

    warnings: list[str] = []
    table = metrics.resolve_subscriptions_table(
        project,
        refresh=True,
        url="http://example.test/subscriptions.json",
        warnings=warnings,
    )

    assert table["source"] == "url"
    assert any("не записан" in w for w in warnings)
    assert not (outside / "metrics_subscriptions.json").exists()
    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"


def test_metrics_models_cache_in_project_path_refuses_symlinked_parent(tmp_path):
    """Настроенный путь ВНУТРИ проекта — под барьером проекта (H1).

    ``cache/models.json`` не содержит компонента ``.agentic``, но лист
    лежит в проекте: симлинк ``cache`` наружу не должен отправить запись
    за границу проекта.
    """
    from awf import metrics

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    _swap_for_symlink(project / "cache", outside)

    warnings: list[str] = []
    saved = metrics.save_models_cache(
        MODELS_CATALOG, ["openai/gpt"], project, "cache/models.json", warnings
    )

    assert saved is None
    assert not (outside / "models.json").exists()
    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"


def test_metrics_models_cache_outside_project_path_written_with_warning(tmp_path):
    """Путь вне проекта — внешняя поверхность: запись + предупреждение (H1)."""
    from awf import metrics

    project = _project(tmp_path)
    target = tmp_path / "external-cache" / "nested" / "models.json"

    warnings: list[str] = []
    saved = metrics.save_models_cache(
        MODELS_CATALOG, ["openai/gpt"], project, str(target), warnings
    )

    assert saved
    assert target.is_file()
    assert any("вне проекта" in w for w in warnings)


# ── (b) api/run.py: отчёты, сигнал, удаления ────────────────────────────


def test_run_report_write_refuses_symlinked_outbox(tmp_path):
    from awf.api import run
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    _swap_for_symlink(project / ".agentic" / "outbox", outside)

    with pytest.raises(AwfApiError):
        run._write_report(project, {"active": True, "queue": [T]}, "test stop")

    assert not list(outside.glob("RUN-REPORT-*.md"))
    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"


def test_service_run_report_write_refuses_symlinked_outbox(tmp_path):
    from awf.api import run
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    _swap_for_symlink(project / ".agentic" / "outbox", outside)

    with pytest.raises(AwfApiError):
        run._write_service_report(project, {"queue": [T], "completed": []}, "done")

    assert not list(outside.glob("SERVICE-RUN-REPORT-*.md"))
    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"


def test_ready_touch_refuses_symlinked_inbox(tmp_path):
    from awf.api import run
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    _swap_for_symlink(project / ".agentic" / "inbox", outside)

    with pytest.raises(AwfApiError):
        run._publish_ready_signal(
            project / ".agentic" / "inbox" / f"{T}.ready",
            operation="publish ready signal",
        )

    assert not (outside / f"{T}.ready").exists()
    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"


def test_ready_touch_refuses_symlinked_leaf(tmp_path):
    """An already existing marker is left alone — unless it is a symlink.

    "Already published" is the regular file the writer put there; a link to
    a foreign file must not count as a ready signal, so the leaf rule of the
    inventory refuses it even though ``exists()`` is true.
    """
    from awf.api import run
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    physical = outside / "signal.txt"
    physical.write_text("", encoding="utf-8")
    leaf = project / ".agentic" / "inbox" / f"{T}.ready"
    leaf.symlink_to(physical)

    with pytest.raises(AwfApiError):
        run._publish_ready_signal(leaf, operation="publish ready signal")

    assert physical.read_text(encoding="utf-8") == ""
    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"


def test_ready_publish_leaves_existing_regular_marker_untouched(tmp_path):
    """Существующий обычный маркер — сигнал по существованию, не свежести (H3)."""
    from awf.api import run

    project = _project(tmp_path)
    marker = project / ".agentic" / "inbox" / f"{T}.ready"
    marker.write_text("sentinel", encoding="utf-8")
    stamp = 1_000_000_000
    os.utime(marker, (stamp, stamp))
    before = marker.stat()

    run._publish_ready_signal(marker, operation="publish ready signal")

    after = marker.stat()
    assert marker.read_text(encoding="utf-8") == "sentinel"
    assert after.st_mtime_ns == before.st_mtime_ns


def test_run_report_unlink_refuses_symlinked_outbox(tmp_path):
    from awf.api import run

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    _swap_for_symlink(project / ".agentic" / "outbox", outside)
    physical = outside / "RUN-REPORT-20260101-000000.md"
    physical.write_text("# stale\n", encoding="utf-8")
    target = project / ".agentic" / "outbox" / physical.name

    run._drop_stale_report(project, target)

    assert physical.exists(), "a symlinked outbox sent the delete outside the project"
    log = (project / ".agentic" / "logs" / "orchestrator.log").read_text(
        encoding="utf-8"
    )
    assert "stale report kept:" in log, "a skipped cleanup must not be silent"


def test_ready_unlink_refuses_symlinked_inbox(tmp_path):
    from awf.api import run

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    _swap_for_symlink(project / ".agentic" / "inbox", outside)
    physical = outside / f"{T}.ready"
    physical.write_text("", encoding="utf-8")
    target = project / ".agentic" / "inbox" / physical.name

    run._unlink_managed_quietly(
        project, target, operation="roll back ready signal"
    )

    assert physical.exists(), "a symlinked inbox sent the delete outside the project"


# ── (c) managed_mkdir и два переведённых сайта ──────────────────────────


def test_managed_mkdir_refuses_symlinked_component(tmp_path):
    from awf.infra import managed_fs

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    _swap_for_symlink(project / ".agentic" / "context", outside)

    with pytest.raises(managed_fs.ManagedPathError):
        managed_fs.managed_mkdir(project / ".agentic" / "context" / "nested" / "dir")

    assert not (outside / "nested").exists()
    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"


def test_managed_write_under_agentic_refuses_symlinked_component(tmp_path):
    """The shared mkdir+write primitive of the context/state writers
    (``BASELINE-*`` links, ``CLAIM-*``, ``last-kill``) refuses a swapped
    component before the directory or the file appears outside."""
    from awf.infra import managed_fs

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    _swap_for_symlink(project / ".agentic" / "context", outside)

    target = project / ".agentic" / "context" / "PRIMITIVE.md"
    with pytest.raises(managed_fs.ManagedPathError):
        managed_fs.managed_write_under_agentic(target, "payload")

    assert not (outside / "PRIMITIVE.md").exists()
    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"


def test_managed_mkdir_refuses_existing_leaf_not_a_directory(tmp_path):
    """Существующий лист-не-каталог — отказ, файл не тронут (H4)."""
    from awf.infra import managed_fs

    project = _project(tmp_path)
    leaf = project / ".agentic" / "context" / "not-a-dir"
    leaf.write_text("keep", encoding="utf-8")

    with pytest.raises(managed_fs.ManagedPathError):
        managed_fs.managed_mkdir(leaf)

    assert leaf.is_file()
    assert leaf.read_text(encoding="utf-8") == "keep"


def test_managed_mkdir_creates_missing_directories(tmp_path):
    from awf.infra import managed_fs

    project = _project(tmp_path)
    target = project / ".agentic" / "context" / "fresh" / "deeper"

    managed_fs.managed_mkdir(target)

    assert target.is_dir()


def test_approve_refuses_symlinked_inbox(tmp_path):
    from awf.api import pipeline
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    _swap_for_symlink(project / ".agentic" / "inbox", outside)

    with pytest.raises(AwfApiError):
        pipeline.approve_commit(project, T)

    assert not (outside / f"APPROVE-{T}.ready").exists()
    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"


def test_unblock_todo_refuses_context_swapped_after_preflight(tmp_path, monkeypatch):
    from awf import pipeline_engine
    from awf.infra import managed_fs

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    (project / ".agentic" / "outbox" / f"BLOCKED-{T}.ready").write_text(
        "", encoding="utf-8"
    )
    _swap_for_symlink(project / ".agentic" / "context", outside)
    monkeypatch.setattr(
        managed_fs, "assert_no_symlink_escape", lambda *a, **k: None
    )

    cleared = pipeline_engine._unblock_todo(
        project, T, project / ".agentic" / "logs"
    )

    assert cleared is False
    assert not (outside / f"BLOCKED-{T}.ready").exists()
    assert (project / ".agentic" / "outbox" / f"BLOCKED-{T}.ready").exists()


# ── (d) A3: note не врёт при отказе unblock ─────────────────────────────


def test_note_does_not_claim_blocked_cleared_on_refusal(tmp_path, monkeypatch):
    from awf import pipeline_engine
    from awf.api import pipeline

    project = _project(tmp_path)
    (project / ".agentic" / "inbox" / f"{T}.md").write_text("# task\n", encoding="utf-8")
    (project / ".agentic" / "inbox" / f"APPROVE-{T}.ready").write_text(
        "", encoding="utf-8"
    )
    (project / ".agentic" / "outbox" / f"BLOCKED-{T}.ready").write_text(
        "", encoding="utf-8"
    )
    monkeypatch.setattr(
        pipeline_engine, "_unblock_todo", lambda *a, **k: False
    )

    target, note = pipeline._resolve_pending_closure(project, T)

    assert target == T
    assert "BLOCKED cleared" not in note


def test_note_does_not_claim_blocked_cleared_on_partial_move(tmp_path):
    """REVIEW-фикс F1: the closure is the ``.ready`` marker, not "something
    moved". The ``.md`` moves, ``.ready`` cannot (destination is a dir)."""
    from awf.api import pipeline

    project = _project(tmp_path)
    outbox = project / ".agentic" / "outbox"
    (project / ".agentic" / "inbox" / f"{T}.md").write_text("# task\n", encoding="utf-8")
    (project / ".agentic" / "inbox" / f"APPROVE-{T}.ready").write_text("", encoding="utf-8")
    (outbox / f"BLOCKED-{T}.ready").write_text("", encoding="utf-8")
    (outbox / f"BLOCKED-{T}.md").write_text("why blocked", encoding="utf-8")
    (project / ".agentic" / "context" / f"BLOCKED-{T}.ready").mkdir()
    target, note = pipeline._resolve_pending_closure(project, T)
    assert target == T
    assert (outbox / f"BLOCKED-{T}.ready").exists()
    assert "BLOCKED cleared" not in note and "not cleared" in note


# ── (e) M4-добор: один кодек `git log` на четыре площадки ───────────────


def _repo_with_raw_subject(tmp_path: Path) -> Path:
    """A real repo whose HEAD subject carries a raw 0xff byte.

    ``commit-tree`` re-encodes an invalid message; ``fast-import`` writes
    the bytes as given — the exact object the audit named.
    """
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    msg = b"awf(verify): TODO-0001 \xe2\x80\x94 \xff\n"
    stream = (
        b"blob\nmark :1\ndata 2\nx\n"
        b"commit refs/heads/master\nmark :2\n"
        b"committer Test <test@example.com> 1700000000 +0000\n"
        b"data %d\n" % len(msg)
        + msg
        + b"M 100644 :1 a.txt\n\n"
    )
    subprocess.run(
        ["git", "fast-import", "--quiet"], cwd=repo, input=stream, check=True
    )
    return repo


def test_git_log_subject_with_raw_byte_survives_every_reader(tmp_path):
    from awf import agent_stage, git_utils, metrics
    from awf.api import dashboard, wait_event

    repo = _repo_with_raw_subject(tmp_path)

    line = git_utils.git_stdout(
        repo, "log", "-1", "--pretty=%h %s", errors="surrogateescape"
    )
    assert "\udcff" in line, "the raw byte must round-trip, not crash the read"
    assert agent_stage._latest_commit_line(repo).endswith("\udcff")

    warnings: list[str] = []
    assert metrics.collect_commit_info(repo, warnings)["TODO-0001"], warnings
    assert wait_event._last_completed_todo(repo) == ("TODO-0001", "commit")
    assert dashboard._verify_commit_shas(repo)["TODO-0001"]


def test_rollback_diff_stat_refuses_instead_of_raising_decode_error(tmp_path):
    """M4 #2 (architect-review): the rollback summary is decoded strictly on
    purpose, so a raw non-UTF-8 name must be a clean refusal (``AwfApiError``)
    — not a ``UnicodeDecodeError`` escaping the API as a traceback."""
    import subprocess

    from awf.api import pipeline
    from awf.api._errors import AwfApiError

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "core.quotePath", "false"], cwd=repo, check=True
    )
    name = os.fsdecode(b"bad-\xff.txt")
    (repo / name).write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c", "user.name=t",
            "-c", "user.email=t@example.test",
            "commit", "-q", "-m", "x",
        ],
        cwd=repo,
        check=True,
    )
    (repo / name).write_text("two\n", encoding="utf-8")

    with pytest.raises(AwfApiError) as excinfo:
        pipeline._rollback_diff_stat(repo, "HEAD")

    assert "not valid UTF-8" in str(excinfo.value)
    assert (repo / name).read_text(encoding="utf-8") == "two\n"
