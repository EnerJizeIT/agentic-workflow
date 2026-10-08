"""REPORTS26 B2: write entry points require a real awf project.

Source: awf-bug-20261007-dispatch-todo-molcha-zapisal-yunit-v-home-agentic.md.
The first ``awf_dispatch_todo`` wrote the TODO and BASELINE into
``/home/pklochkov/.agentic/`` — a stale directory from old experiments,
without ``config.yaml`` and outside any git repo. The guard only checked
that the ``.agentic/`` directory EXISTS and let it through:
``baseline_sha="(not a git repo)"``, no error, the unit silently landed
in the wrong place (the repeat call "worked" because the directory was
already poisoned).

Four invariants:
(a) a stale directory (.agentic/ without config.yaml) + ``dispatch_todo``
    → ``AwfApiError`` mentioning config.yaml; NOTHING appears in the
    stale directory (no TODO, no baseline, no .ready);
(b) the same refusal for ``create_baseline`` (no BASELINE-* residue);
(c) a normal project: dispatch → ``resolved_project_dir`` == the
    absolute project path, files are there, ``baseline_sha`` is a git
    SHA; ``BaselineResult`` carries the same field;
(d) ``project_dir != cwd``: with an explicit project_dir the files go
    strictly into it — even when the cwd has its own .agentic/.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api
from awf.api.pipeline import create_baseline


def _snapshot(root: Path) -> set[str]:
    """Relative paths of everything under root — before/after identity."""
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def _stale_dir(tmp_path: Path) -> Path:
    """A directory that looks like an old awf project: .agentic/ with
    subdirectories and a leftover TODO, no config.yaml, not a git repo."""
    stale = tmp_path / "stale"
    (stale / ".agentic" / "inbox").mkdir(parents=True)
    (stale / ".agentic" / "state").mkdir(parents=True)
    (stale / ".agentic" / "inbox" / "TODO-0099.md").write_text("old junk\n")
    return stale


class TestDispatchRefusesStaleDir:
    """(a) dispatch into a stale directory: refusal + zero side effects."""

    def test_dispatch_refuses_stale_dir(self, tmp_path):
        stale = _stale_dir(tmp_path)
        before = _snapshot(stale)

        with pytest.raises(api.AwfApiError) as ei:
            api.dispatch_todo(stale, "# Task\n\ndo work\n")

        assert "config.yaml" in str(ei.value)
        # the refused path, absolute — no guessing which dir was meant
        assert str(stale.resolve()) in str(ei.value)
        assert _snapshot(stale) == before, (
            "refused dispatch left files in the stale directory: "
            f"{sorted(_snapshot(stale) - before)}"
        )


class TestBaselineRefusesStaleDir:
    """(b) the same refusal for create_baseline (no BASELINE-* residue)."""

    def test_baseline_refuses_stale_dir(self, tmp_path):
        stale = _stale_dir(tmp_path)
        before = _snapshot(stale)

        with pytest.raises(api.AwfApiError) as ei:
            create_baseline(stale, "TODO-0001")

        assert "config.yaml" in str(ei.value)
        assert str(stale.resolve()) in str(ei.value)
        assert _snapshot(stale) == before, (
            "refused baseline left files in the stale directory: "
            f"{sorted(_snapshot(stale) - before)}"
        )


class TestResolvedProjectDir:
    """(c) the answer names where the unit actually landed."""

    def test_dispatch_result_names_the_project(self, tmp_git_repo: Path):
        api.init_project(tmp_git_repo, project_name="B2c")
        result = api.dispatch_todo(
            tmp_git_repo, "# Task\n\ndo work\n", todo_id="TODO-0001"
        )
        assert result.resolved_project_dir == str(tmp_git_repo.resolve())
        assert len(result.baseline_sha) == 40, (
            f"baseline_sha must be a git SHA, got {result.baseline_sha!r}"
        )
        inbox = tmp_git_repo / ".agentic" / "inbox"
        assert (inbox / "TODO-0001.md").is_file()
        assert (inbox / "TODO-0001.ready").is_file()
        # as_dict carries the field for the MCP answer
        assert result.as_dict()["resolved_project_dir"] == str(tmp_git_repo.resolve())

    def test_baseline_result_names_the_project(self, tmp_git_repo: Path):
        api.init_project(tmp_git_repo, project_name="B2d")
        result = create_baseline(tmp_git_repo, "TODO-0001")
        assert result.resolved_project_dir == str(tmp_git_repo.resolve())
        assert result.as_dict()["resolved_project_dir"] == str(tmp_git_repo.resolve())


class TestExplicitProjectDirWinsOverCwd:
    """(d) explicit project_dir + a different cwd (with its own .agentic/):
    the files go strictly into project_dir."""

    def test_files_land_in_project_dir_not_cwd(
        self, tmp_git_repo: Path, tmp_path: Path, monkeypatch
    ):
        api.init_project(tmp_git_repo, project_name="B2cwd")
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / ".agentic" / "inbox").mkdir(parents=True)
        monkeypatch.chdir(elsewhere)

        result = api.dispatch_todo(
            tmp_git_repo, "# Task\n\nbody\n", todo_id="TODO-0001"
        )

        inbox = tmp_git_repo / ".agentic" / "inbox"
        assert (inbox / "TODO-0001.md").is_file()
        assert (inbox / "TODO-0001.ready").is_file()
        assert (
            result.resolved_project_dir == str(tmp_git_repo.resolve())
        ), "the answer must name the project the unit landed in"
        # nothing leaked into the cwd's .agentic/
        assert not (elsewhere / ".agentic" / "inbox" / "TODO-0001.md").exists()
        assert not list(elsewhere.rglob("TODO-0001*"))
        assert not list(elsewhere.rglob("BASELINE-TODO-0001*"))
