"""R1.1 (roadmap R1-SAFETY-AND-STATE): файловые границы managed-каталогов.

Репро аудита S9 перенесены в автотесты: на исходной версии (база d78a28b)
каждый из них КРАСНЫЙ — reset следует симлинку runtime-каталога наружу
(S9-01), fallback-лог дописывает внешний файл через симлинк и меняет его
права (S9-03), remove_orphans принимает traversal в id и удаляет внешний
.md (S9-04). Тесты доказывают безопасное поведение после правки.

Границы (приёмка R1):
- symlink .agentic / runtime-каталога / fallback → отказ ДО изменений,
  canary bytes/mode нетронуты;
- валидный TODO + ``../../../bad`` в одном запросе → не удалено ничего;
- symlink leaf у лога и hardlinked существующий лог → ни записи, ни
  chmod внешнего inode;
- reset не трогает lock/lease/done; I/O-ошибка после preflight журналится
  как partial_failure с применённым subset.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from awf.api import lifecycle as api
from awf.api._errors import AwfApiError


def _agentic(project: Path) -> Path:
    a = project / ".agentic"
    a.mkdir(parents=True, exist_ok=True)
    return a


def _canary(path: Path, mode: int = 0o644) -> None:
    path.write_text("synthetic disposable file\n", encoding="utf-8")
    path.chmod(mode)


def _assert_canary_untouched(path: Path, mode: int = 0o644) -> None:
    assert path.is_file(), "canary target was deleted"
    assert path.read_text(encoding="utf-8") == "synthetic disposable file\n"
    assert (path.stat().st_mode & 0o777) == mode


# ── S9-01: reset не выходит за проект через симлинк каталога ─────────────


class TestResetSymlinkRefusal:
    def test_refuses_symlinked_outbox(self, tmp_path):
        project = tmp_path / "project"
        agentic = _agentic(project)
        (agentic / "inbox").mkdir()
        (agentic / "inbox" / "todo.md").write_text("# t")
        outside = tmp_path / "outside"
        outside.mkdir()
        canary = outside / "canary.txt"
        _canary(canary)
        (agentic / "outbox").symlink_to(outside, target_is_directory=True)

        with pytest.raises(AwfApiError):
            api.reset_runtime(project, tasks_only=True)

        _assert_canary_untouched(canary)
        assert (agentic / "outbox").is_symlink()
        assert (agentic / "inbox" / "todo.md").exists()

    def test_refuses_symlinked_agentic(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside"
        (outside / "inbox").mkdir(parents=True)
        canary = outside / "inbox" / "canary.txt"
        _canary(canary)
        (project / ".agentic").symlink_to(outside, target_is_directory=True)

        with pytest.raises(AwfApiError):
            api.reset_runtime(project)

        _assert_canary_untouched(canary)

    def test_refuses_symlinked_logs(self, tmp_path):
        project = tmp_path / "project"
        agentic = _agentic(project)
        (agentic / "inbox").mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        canary = outside / "worker.log"
        _canary(canary)
        (agentic / "logs").symlink_to(outside, target_is_directory=True)

        with pytest.raises(AwfApiError):
            api.reset_runtime(project)

        _assert_canary_untouched(canary)

    def test_refuses_symlinked_fallback_leaf(self, tmp_path):
        project = tmp_path / "project"
        agentic = _agentic(project)
        (agentic / "inbox").mkdir()
        (agentic / "logs").mkdir()
        external = tmp_path / "external.log"
        _canary(external)
        (agentic / "logs" / "worker-output.out").symlink_to(external)

        with pytest.raises(AwfApiError):
            api.reset_runtime(project)

        _assert_canary_untouched(external)

    def test_error_in_last_dir_keeps_first(self, tmp_path):
        """Preflight всего плана: симлинк в последнем каталоге не удаляет первый."""
        project = tmp_path / "project"
        agentic = _agentic(project)
        for d in ["inbox", "outbox", "context"]:
            (agentic / d).mkdir()
            (agentic / d / "junk.txt").write_text("junk")
        outside = tmp_path / "outside"
        outside.mkdir()
        canary = outside / "keep.txt"
        _canary(canary)
        (agentic / "logs").symlink_to(outside, target_is_directory=True)

        with pytest.raises(AwfApiError):
            api.reset_runtime(project)

        # Nothing was cleaned — the first directory is intact.
        assert (agentic / "inbox" / "junk.txt").exists()
        assert (agentic / "outbox" / "junk.txt").exists()
        _assert_canary_untouched(canary)


# ── S9-04: remove_orphans валидирует весь список до первого unlink ──────


class TestRemoveOrphansValidation:
    def _project_with_inbox(self, tmp_path: Path) -> Path:
        project = tmp_path / "project"
        agentic = _agentic(project)
        (agentic / "inbox").mkdir()
        return project

    def test_traversal_id_removes_nothing(self, tmp_path):
        project = self._project_with_inbox(tmp_path)
        disposable = tmp_path / "disposable.md"
        _canary(disposable)

        with pytest.raises(AwfApiError):
            api.remove_orphans(project, ["../../../disposable"])

        assert disposable.is_file()
        assert disposable.read_text(encoding="utf-8") == "synthetic disposable file\n"

    def test_valid_then_invalid_removes_nothing(self, tmp_path):
        project = self._project_with_inbox(tmp_path)
        inbox = project / ".agentic" / "inbox"
        (inbox / "TODO-0001.md").write_text("# orphan")
        (inbox / "TODO-0001.ready").touch()
        disposable = tmp_path / "disposable.md"
        _canary(disposable)

        with pytest.raises(AwfApiError):
            api.remove_orphans(project, ["TODO-0001", "../../../disposable"])

        # The valid id listed FIRST was not removed either.
        assert (inbox / "TODO-0001.md").exists()
        assert (inbox / "TODO-0001.ready").exists()
        assert disposable.is_file()

    def test_refuses_symlinked_inbox(self, tmp_path):
        project = tmp_path / "project"
        agentic = _agentic(project)
        outside = tmp_path / "outside"
        outside.mkdir()
        victim = outside / "TODO-0002.md"
        _canary(victim)
        (agentic / "inbox").symlink_to(outside, target_is_directory=True)

        with pytest.raises(AwfApiError):
            api.remove_orphans(project, ["TODO-0002"])

        assert victim.is_file()

    def test_unknown_valid_id_silently_skipped(self, tmp_path):
        project = self._project_with_inbox(tmp_path)
        result = api.remove_orphans(project, ["TODO-9999"])
        assert result.orphan_ids == []


# ── S9-03: fallback и лог открываются без следования ссылке ─────────────


_WORKER_CMD = [sys.executable, "-c", "print('worker')"]


class TestWorkerLogBoundaries:
    def test_log_created_0600(self, tmp_path):
        from awf.signal_watch import run_subprocess_until_signal

        logs = tmp_path / "logs"
        logs.mkdir()
        run_subprocess_until_signal(
            cmd=_WORKER_CMD, cwd=tmp_path, logs_dir=logs,
            preflight_timeout=0, no_output_timeout=0,
        )
        log = logs / "worker-output.out"
        assert log.is_file()
        assert (log.stat().st_mode & 0o777) == 0o600

    def test_fallback_symlink_refused_no_write(self, tmp_path):
        from awf.signal_watch import run_subprocess_until_signal

        logs = tmp_path / "logs"
        logs.mkdir()
        external = tmp_path / "external.txt"
        _canary(external)
        (logs / "worker-output.out").symlink_to(external)

        with pytest.raises(Exception):
            run_subprocess_until_signal(
                cmd=_WORKER_CMD, cwd=tmp_path, logs_dir=logs,
                preflight_timeout=0, no_output_timeout=0,
            )

        _assert_canary_untouched(external)

    def test_named_log_symlink_refused_no_write(self, tmp_path):
        from awf.signal_watch import run_subprocess_until_signal

        logs = tmp_path / "logs"
        logs.mkdir()
        external = tmp_path / "external.txt"
        _canary(external)
        (logs / "awf-agent-implementer-TODO-0001.out").symlink_to(external)

        with pytest.raises(Exception):
            run_subprocess_until_signal(
                cmd=_WORKER_CMD, cwd=tmp_path, logs_dir=logs,
                log_name="awf-agent-implementer-TODO-0001.out",
                preflight_timeout=0, no_output_timeout=0,
            )

        _assert_canary_untouched(external)

    def test_hardlinked_existing_log_not_touched(self, tmp_path):
        from awf.signal_watch import run_subprocess_until_signal

        logs = tmp_path / "logs"
        logs.mkdir()
        external = tmp_path / "external.txt"
        _canary(external, mode=0o644)
        os.link(external, logs / "worker-output.out")
        assert (logs / "worker-output.out").stat().st_nlink == 2

        holder: dict = {}
        run_subprocess_until_signal(
            cmd=_WORKER_CMD, cwd=tmp_path, logs_dir=logs,
            log_holder=holder, preflight_timeout=0, no_output_timeout=0,
        )

        # The shared inode is neither appended to nor chmod-ed.
        _assert_canary_untouched(external, mode=0o644)
        # A fresh safe file was used instead.
        fresh = list(logs.glob(".worker-output-legacy-*.out"))
        assert fresh, "no fresh safe log created for the hardlinked legacy log"
        assert Path(holder["log_path"]).name.startswith(".worker-output-legacy-")

    def test_logs_dir_symlink_refused(self, tmp_path):
        from awf.signal_watch import run_subprocess_until_signal

        outside = tmp_path / "outside"
        outside.mkdir()
        logs = tmp_path / "logs"
        logs.symlink_to(outside, target_is_directory=True)

        with pytest.raises(Exception):
            run_subprocess_until_signal(
                cmd=_WORKER_CMD, cwd=tmp_path, logs_dir=logs,
                preflight_timeout=0, no_output_timeout=0,
            )

        assert list(outside.iterdir()) == []

    def test_orchestrator_log_agentic_symlink_refused(self, tmp_path):
        """R1.1/S9-03 (REVIEW fix): the orchestrator.log writer runs through
        the managed no-follow helper too. A symlinked ``.agentic`` is
        refused — orchestrator.log must not land outside the project."""
        from awf._log import log as _log

        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside"
        (outside / "logs").mkdir(parents=True)
        (project / ".agentic").symlink_to(outside, target_is_directory=True)

        _log(project / ".agentic" / "logs", "must not escape")

        assert list((outside / "logs").iterdir()) == []

    def test_non_posix_refused(self, tmp_path, monkeypatch):
        # Lazy import: on the baseline tree the module does not exist yet,
        # so this is a new-feature check, not one of the S9 repros.
        try:
            from awf.infra import managed_fs
        except ImportError:
            pytest.fail("awf.infra.managed_fs is missing")
        monkeypatch.setattr(managed_fs, "is_posix", lambda: False)
        with pytest.raises(Exception):
            managed_fs.open_append_log(tmp_path / "x.out")


# ── reset: lock/lease/done неприкосновенны, partial_failure журналится ──


class TestResetInvariants:
    def test_preserves_lock_lease_and_done(self, tmp_path):
        project = tmp_path / "project"
        agentic = _agentic(project)
        (agentic / "inbox").mkdir()
        (agentic / "logs").mkdir()
        (agentic / "state").mkdir()
        (agentic / "done").mkdir()
        lease = agentic / "logs" / "awf-launch.lease"
        lease.write_text("424242")
        lock = agentic / "state" / ".lock"
        lock.write_bytes(b"")
        archived = agentic / "done" / "TODO-0001.md"
        archived.write_text("# archived")

        api.reset_runtime(project)

        assert lease.exists(), "reset must not touch the launch lease"
        assert lock.exists(), "reset must not touch the state lock"
        assert archived.exists(), "reset must not touch the done archive"

    def test_partial_failure_journaled(self, tmp_path, monkeypatch):
        project = tmp_path / "project"
        agentic = _agentic(project)
        for d in ["inbox", "outbox", "context", "logs"]:
            (agentic / d).mkdir()
            (agentic / d / "junk.txt").write_text("junk")
        # A subdirectory forces shutil.rmtree, which we make fail.
        (agentic / "context" / "sub").mkdir()
        (agentic / "context" / "sub" / "junk.txt").write_text("junk")

        def boom(*a, **kw):
            raise OSError("synthetic mid-reset failure")

        monkeypatch.setattr(api.shutil, "rmtree", boom)

        result = api.reset_runtime(project)

        assert result.partial_failure is True
        assert result.cleaned_dirs == ["inbox", "outbox"]
        assert "context" in result.notes
        # The earlier directories really were cleaned (subset applied).
        assert not (agentic / "inbox" / "junk.txt").exists()
        assert not (agentic / "outbox" / "junk.txt").exists()
        # The failed directory is left for the repeat.
        assert (agentic / "context" / "sub" / "junk.txt").exists()


# ── прочие log/report-writer'ы: symlink .agentic → отказ до создания ─────


class TestOtherManagedWriters:
    def test_managed_write_text_refuses_symlinked_agentic(self, tmp_path):
        """The safe atomic writer (verify TEST-RESULTS, verify_pack GATES,
        dashboard) refuses a symlinked ``.agentic`` and writes nothing out.
        """
        try:
            from awf.infra import managed_fs
        except ImportError:
            pytest.fail("awf.infra.managed_fs is missing")

        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside"
        (outside / "outbox").mkdir(parents=True)
        (project / ".agentic").symlink_to(outside, target_is_directory=True)

        with pytest.raises(managed_fs.ManagedPathError):
            managed_fs.managed_write_text(
                project / ".agentic" / "outbox" / "report.md",
                "payload\n",
                root=project,
            )

        assert list((outside / "outbox").iterdir()) == []

    def test_lock_writer_refuses_symlinked_agentic(self, tmp_path):
        """The state lock writer refuses a symlinked ``.agentic`` instead of
        creating ``state/.lock`` outside the project (F-A class)."""
        from awf._lock import locked

        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside"
        (outside / "state").mkdir(parents=True)
        (project / ".agentic").symlink_to(outside, target_is_directory=True)

        with pytest.raises(Exception):
            with locked(project):
                pass

        assert list((outside / "state").iterdir()) == []
