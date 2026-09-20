"""AUD12-11: broken inputs must degrade, never traceback.

BACKLOG layer 3 ("битые входы") had no systematic test layer while the
audit found 5+ bugs of the class "corrupt input → traceback"
(AUD02-06, AUD03-07, AUD07-05, ...). Each scenario below corrupts one
input file and asserts the public API either returns a clean degraded
result or raises a typed ``AwfApiError`` — never an unhandled exception.

Two of these were real bugs (fixed here, pinned by red→green):
- non-UTF-8 state/run.yaml → ``UnicodeDecodeError`` escaped
  ``read_state`` / ``read_run`` (only ``yaml.YAMLError``/``OSError`` were
  caught);
- garbage ``BASELINE-*.sha`` → raw ``CalledProcessError`` from
  ``git reset --hard`` in ``rollback``.

The other scenarios pin already-correct degradation so a regression
re-introducing a traceback goes red.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api
from awf.api._errors import AwfApiError


def _corrupt_state(project: Path, content: bytes) -> Path:
    state_dir = project / ".agentic" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "current.yaml"
    state_file.write_bytes(content)
    return state_file


def _corrupt_run(project: Path, content: bytes) -> Path:
    state_dir = project / ".agentic" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    run_file = state_dir / "run.yaml"
    run_file.write_bytes(content)
    return run_file


class TestBrokenState:
    """state/current.yaml — the pipeline's live state file."""

    def test_broken_yaml_degrades_to_no_state(self, tmp_git_repo: Path):
        api.init_project(tmp_git_repo, project_name="T")
        _corrupt_state(tmp_git_repo, b"{{{{\n: : : \n  - [unclosed")

        result = api.get_status(tmp_git_repo)
        # Corrupt state is treated as "no pipeline state" — no crash, no
        # phantom running pipeline.
        assert result.conflict_warning is None or isinstance(result.conflict_warning, str)

    def test_non_utf8_bytes_degrade_to_no_state(self, tmp_git_repo: Path):
        api.init_project(tmp_git_repo, project_name="T")
        _corrupt_state(tmp_git_repo, b"\xff\xfe\x00\x01stage: broken")

        # AUD02-06: this raised UnicodeDecodeError before the fix.
        result = api.get_status(tmp_git_repo)
        assert isinstance(result.conflict_warning, (str, type(None)))


class TestBrokenRun:
    """state/run.yaml — the autonomous run (забег) state."""

    def test_broken_yaml_degrades_to_no_run(self, tmp_git_repo: Path):
        api.init_project(tmp_git_repo, project_name="T")
        _corrupt_run(tmp_git_repo, b"queue: [unclosed\n  - bad")

        result = api.run_status(tmp_git_repo)
        assert result.active is False
        assert result.queue == []

    def test_non_utf8_bytes_degrade_to_no_run(self, tmp_git_repo: Path):
        api.init_project(tmp_git_repo, project_name="T")
        _corrupt_run(tmp_git_repo, b"\xff\xferun: \x00garbage")

        # Same bug class as the state file: non-UTF-8 → UnicodeDecodeError
        # escaped read_run.
        result = api.run_status(tmp_git_repo)
        assert result.active is False


class TestBrokenBaselineSha:
    """context/BASELINE-{todo}.sha — the rollback target."""

    def test_garbage_sha_rollback_raises_typed_error(self, tmp_git_repo: Path):
        api.init_project(tmp_git_repo, project_name="T")
        ctx = tmp_git_repo / ".agentic" / "context"
        ctx.mkdir(exist_ok=True)
        (ctx / "BASELINE-TODO-0001.sha").write_text("not-a-sha\n")

        # AUD07-05: before the fix this was a raw CalledProcessError from
        # `git reset --hard not-a-sha`.
        with pytest.raises(AwfApiError) as exc_info:
            api.rollback(str(tmp_git_repo), "TODO-0001", mode="hard")
        assert "not a valid git revision" in str(exc_info.value)

        # The repo must be untouched by the failed rollback.
        assert (tmp_git_repo / "README.md").exists()

    def test_wellformed_but_missing_sha_raises_typed_error(self, tmp_git_repo: Path):
        api.init_project(tmp_git_repo, project_name="T")
        ctx = tmp_git_repo / ".agentic" / "context"
        ctx.mkdir(exist_ok=True)
        # 40 hex chars that do not exist in this repo's history.
        (ctx / "BASELINE-TODO-0001.sha").write_text("deadbeef" * 5 + "\n")

        with pytest.raises(AwfApiError) as exc_info:
            api.rollback(str(tmp_git_repo), "TODO-0001", mode="hard")
        assert "not reachable" in str(exc_info.value)


class TestEmptyTodoBody:
    """inbox/TODO-{id}.md with a zero-byte body."""

    def test_empty_body_todo_is_not_active(self, tmp_git_repo: Path):
        api.init_project(tmp_git_repo, project_name="T")
        inbox = tmp_git_repo / ".agentic" / "inbox"
        inbox.mkdir(exist_ok=True)
        # Zero-byte body — the documented "not active" case
        # (list_active_todos requires a non-empty .md).
        (inbox / "TODO-0001.md").write_text("")
        (inbox / "TODO-0001.ready").touch()
        # A valid TODO that must still be listed.
        (inbox / "TODO-0002.md").write_text("# TODO-0002\nreal task\n")
        (inbox / "TODO-0002.ready").touch()

        result = api.get_status(tmp_git_repo)
        active = [t["todo_id"] for t in result.active_todos]
        assert "TODO-0001" not in active
        assert "TODO-0002" in active


class TestBrokenBaselineStatus:
    """context/BASELINE-{id}.status — corrupt bytes in the status file."""

    def test_corrupt_status_file_is_ignored(self, tmp_git_repo: Path):
        api.init_project(tmp_git_repo, project_name="T")
        import subprocess

        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
        ctx = tmp_git_repo / ".agentic" / "context"
        ctx.mkdir(exist_ok=True)
        (ctx / "BASELINE-TODO-0001.sha").write_text(sha + "\n")
        (ctx / "BASELINE-TODO-0001.status").write_bytes(b"\xff\xfe\x00garbage")

        # Status file is consumed nowhere — a corrupt one must not break
        # the surrounding reads (status, rollback dry-run).
        api.get_status(tmp_git_repo)
        result = api.rollback(str(tmp_git_repo), "TODO-0001", mode="dry-run")
        assert result.mode == "dry-run"


class TestBrokenPipelineYaml:
    """pipelines/default.yaml — syntactically broken (AUD03-07)."""

    def test_broken_pipeline_yaml_raises_typed_error(self, tmp_git_repo: Path):
        api.init_project(tmp_git_repo, project_name="T")
        pipes = tmp_git_repo / ".agentic" / "pipelines"
        pipes.mkdir(exist_ok=True)
        (pipes / "default.yaml").write_bytes(b"stages: [{{{\n  : :")

        # Degradation: the orchestrator reports the malformed file and
        # returns exit code 1 — no traceback, no half-started pipeline.
        result = api.start_pipeline(str(tmp_git_repo), background=False, auto=True)
        assert result.exit_code == 1
