"""A70 (TODO-0401): the unit volume ceiling at dispatch.

A TODO declaring more than ``MAX_UNIT_FILES`` paths is refused BEFORE
anything is written: no TODO in the inbox, no dispatch journal, no baseline,
no ``.ready`` — a half-dispatch of an oversized unit must not exist. The
auto-id counter is read-only for a refusal, so the next dispatch still gets
the same number. Paths carried over from a rejected attempt are this unit's
work (they enter its commit), so they count toward the same ceiling.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api, paths
from awf._path_link import encode_path_link
from awf.api.dispatch import MAX_UNIT_FILES, _unit_volume

ORIGIN = "TODO-0009"


def _files(count: int) -> list[str]:
    return [f"src/mod{i:02d}.py" for i in range(1, count + 1)]


def _todo(files: list[str], *, todo_id: str = "TODO-0001") -> str:
    listed = ", ".join(f'"{path}"' for path in files)
    return f"---\nfiles: [{listed}]\n---\n# {todo_id} — unit\n\nbody\n"


@pytest.fixture
def project(tmp_git_repo: Path) -> Path:
    api.init_project(
        tmp_git_repo,
        project_name="VolumeGuard",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    return tmp_git_repo


@pytest.fixture
def carry_project(project: Path) -> Path:
    """A project with a rejected origin whose carry-over list is reusable."""
    (paths.inbox(project) / f"{ORIGIN}.md").write_text(
        f"# {ORIGIN} — rejected\n", encoding="utf-8"
    )
    (paths.context_dir(project) / f"REJECT-{ORIGIN}.files").write_bytes(
        encode_path_link(["old_a.py", "old_b.py"]).encode(
            "utf-8", "surrogateescape"
        )
    )
    return project


class TestDeclaredVolume:
    def test_twelve_files_dispatch(self, project: Path) -> None:
        assert MAX_UNIT_FILES == 12, "the discipline number is fixed by A70"
        result = api.dispatch_todo(
            project, _todo(_files(MAX_UNIT_FILES)), no_baseline_tests=True
        )
        assert result.todo_id == "TODO-0001"
        assert (paths.inbox(project) / "TODO-0001.md").is_file()

    def test_thirteen_files_refused_with_numbers(self, project: Path) -> None:
        with pytest.raises(api.AwfApiError) as exc:
            api.dispatch_todo(
                project,
                _todo(_files(MAX_UNIT_FILES + 1)),
                no_baseline_tests=True,
            )
        message = str(exc.value)
        assert "dispatch refused: TODO-0001 declares 13 files" in message
        assert f"limit {MAX_UNIT_FILES}" in message
        assert "Разбей на юниты по модулям" in message
        assert "358 минут" in message

    def test_refusal_leaves_no_artifacts(self, project: Path) -> None:
        context = paths.context_dir(project)

        with pytest.raises(api.AwfApiError):
            api.dispatch_todo(
                project,
                _todo(_files(MAX_UNIT_FILES + 1)),
                no_baseline_tests=True,
            )

        assert not list(paths.inbox(project).glob("TODO-*.md")), "no TODO created"
        assert not list(context.glob("BASELINE-TODO-0001*")), "no baseline"
        assert not list(context.glob("DISPATCH-TODO-0001*")), "no journal"

        logs = paths.logs_dir(project)
        if logs.is_dir():
            for log in logs.iterdir():
                text = log.read_text(encoding="utf-8", errors="replace")
                assert "TODO-0001" not in text, f"operation log written: {log.name}"

    def test_counter_is_not_moved(self, project: Path) -> None:
        with pytest.raises(api.AwfApiError):
            api.dispatch_todo(
                project,
                _todo(_files(MAX_UNIT_FILES + 1)),
                no_baseline_tests=True,
            )
        result = api.dispatch_todo(
            project, _todo(_files(1)), no_baseline_tests=True
        )
        assert result.todo_id == "TODO-0001"

    def test_duplicate_path_counted_once(self, project: Path) -> None:
        """A repeated path is the same work, not a second path (AR-6)."""
        files = _files(MAX_UNIT_FILES)
        result = api.dispatch_todo(
            project, _todo([*files, files[0]]), no_baseline_tests=True
        )
        assert result.todo_id == "TODO-0001"


class TestCarriedVolume:
    def test_carried_paths_count_toward_the_limit(
        self, carry_project: Path
    ) -> None:
        with pytest.raises(api.AwfApiError) as exc:
            api.dispatch_todo(
                carry_project,
                _todo(_files(MAX_UNIT_FILES), todo_id="TODO-0002"),
                todo_id="TODO-0002",
                carry_over_from=ORIGIN,
                no_baseline_tests=True,
            )
        message = str(exc.value)
        assert "declares 12 files" in message
        assert "bring the unit to 14" in message
        assert f"limit {MAX_UNIT_FILES}" in message
        assert f"Дополнительно: 2 перенесено из {ORIGIN}" in message
        assert not (paths.inbox(carry_project) / "TODO-0002.md").exists()
        assert not list(
            paths.context_dir(carry_project).glob("BASELINE-TODO-0002*")
        )

    def test_thirteen_carried_paths_without_files_refused(
        self, carry_project: Path
    ) -> None:
        """The ceiling holds for a unit that declares nothing (AR-6)."""
        (paths.context_dir(carry_project) / f"REJECT-{ORIGIN}.files").write_bytes(
            encode_path_link(_files(MAX_UNIT_FILES + 1)).encode(
                "utf-8", "surrogateescape"
            )
        )
        with pytest.raises(api.AwfApiError) as exc:
            api.dispatch_todo(
                carry_project,
                "# TODO-0002 — unit\n\nbody\n",
                todo_id="TODO-0002",
                carry_over_from=ORIGIN,
                no_baseline_tests=True,
            )
        message = str(exc.value)
        assert "declares 0 files" in message
        assert f"bring the unit to {MAX_UNIT_FILES + 1}" in message
        assert not (paths.inbox(carry_project) / "TODO-0002.md").exists()

    def test_carried_path_already_declared_is_not_double_counted(
        self, carry_project: Path
    ) -> None:
        files = _files(MAX_UNIT_FILES)
        (paths.context_dir(carry_project) / f"REJECT-{ORIGIN}.files").write_bytes(
            encode_path_link([files[0]]).encode("utf-8", "surrogateescape")
        )
        result = api.dispatch_todo(
            carry_project,
            _todo(files, todo_id="TODO-0002"),
            todo_id="TODO-0002",
            carry_over_from=ORIGIN,
            no_baseline_tests=True,
        )
        assert result.todo_id == "TODO-0002"
        assert result.carry_over_files == [files[0]]


class TestOtherAdoptionChannels:
    """AR-1: include_untracked and claim_from enter the unit commit too."""

    def test_gate_reads_the_adopted_set(self) -> None:
        """AR-2: one source of truth — a channel that joins ``adopted`` is
        counted even when the message breakdown does not know it yet."""
        total, declared, sources = _unit_volume(
            {"files": ["a.py", "b.py"]},
            {"a.py", "b.py", "future.py"},
            carry_over_files=[],
            include_files=[],
            claimed_files=[],
        )
        assert (total, declared) == (3, 2)
        assert "1 из других принятых путей" in sources

    def test_included_untracked_path_counts(self, project: Path) -> None:
        (project / "extra.py").write_text("new\n", encoding="utf-8")
        with pytest.raises(api.AwfApiError) as exc:
            api.dispatch_todo(
                project,
                _todo(_files(MAX_UNIT_FILES)),
                include_untracked=["extra.py"],
                no_baseline_tests=True,
            )
        message = str(exc.value)
        assert "declares 12 files" in message
        assert "bring the unit to 13" in message
        assert "Дополнительно: 1 включено из untracked" in message
        assert not list(paths.inbox(project).glob("TODO-*.md"))

    def test_claimed_path_counts(self, project: Path) -> None:
        api.create_baseline(project, ORIGIN)
        (project / "claimed.py").write_text("predecessor work\n", encoding="utf-8")
        plan = api.plan_claim_changes(project, ORIGIN, paths=["claimed.py"])
        api.apply_claim_changes(project, plan, confirmed_digest=plan.digest)

        with pytest.raises(api.AwfApiError) as exc:
            api.dispatch_todo(
                project,
                _todo(_files(MAX_UNIT_FILES), todo_id="TODO-0002"),
                todo_id="TODO-0002",
                claim_from=ORIGIN,
                no_baseline_tests=True,
            )
        message = str(exc.value)
        assert "declares 12 files" in message
        assert "bring the unit to 13" in message
        assert f"Дополнительно: 1 принято из {ORIGIN}" in message
        assert not (paths.inbox(project) / "TODO-0002.md").exists()
