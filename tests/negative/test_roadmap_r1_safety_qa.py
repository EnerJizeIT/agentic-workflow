"""R1.1 (roadmap R1-SAFETY-AND-STATE) — QA-проба границ (agent-qa-review).

Независимые тесты поверх tests/negative/test_roadmap_r1_safety.py. Зелёные
фиксируют верное поведение без покрытия, включая закрытую в проходе F1
дыру S9-03: log-writer следовал симлинку промежуточного ``.agentic``
наружу. Один КРАСНЫЙ
(test_background_start_agentic_symlink_no_external_write) доказывает
оставшуюся открытой точку того же класса: ``awf-start.out`` в
``awf/api/_background.py`` пишется обычным ``os.open`` без ManagedFS,
поэтому фоновый запуск уходит за границу проекта через симлинк
``.agentic``.

Требование TODO B: «для destructive/log-writer: no-follow по
ПРОМЕЖУТОЧНЫМ компонентам И leaf» — для КАЖДОГО log-writer. REVIEW просил
проверить «любые другие открытия логов»; ``_background.py`` не переведён.
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


_WORKER_CMD = [sys.executable, "-c", "print('worker')"]


# ── зелёные: верное поведение без покрытия ──────────────────────────────


class TestR1SafetyQAGreen:
    def test_reset_refuses_symlinked_state_dir(self, tmp_path):
        """Preflight ловит и симлинк самого ``state`` — до удалений."""
        project = tmp_path / "project"
        agentic = _agentic(project)
        (agentic / "inbox").mkdir()
        (agentic / "inbox" / "todo.md").write_text("# t")
        outside = tmp_path / "outside"
        outside.mkdir()
        canary = outside / "keep.txt"
        _canary(canary)
        (agentic / "state").symlink_to(outside, target_is_directory=True)

        with pytest.raises(AwfApiError):
            api.reset_runtime(project)

        _assert_canary_untouched(canary)
        assert (agentic / "inbox" / "todo.md").exists()

    def test_remove_orphans_symlinked_leaf_removes_link_not_target(self, tmp_path):
        """Симлинк-лист в inbox: unlink снимает ссылку, чужой файл целен."""
        project = tmp_path / "project"
        agentic = _agentic(project)
        (agentic / "inbox").mkdir()
        external = tmp_path / "external.md"
        _canary(external)
        (agentic / "inbox" / "TODO-0001.md").symlink_to(external)

        result = api.remove_orphans(project, ["TODO-0001"])

        assert result.orphan_ids == ["TODO-0001"]
        _assert_canary_untouched(external)
        assert not (agentic / "inbox" / "TODO-0001.md").is_symlink()

    def test_existing_log_rechmodded_0600(self, tmp_path):
        """Существующий лог 0644 переводится в 0600 (ratchet прежней версии)."""
        from awf.signal_watch import run_subprocess_until_signal

        logs = tmp_path / "logs"
        logs.mkdir()
        existing = logs / "worker-output.out"
        existing.write_text("from a previous run\n")
        existing.chmod(0o644)

        run_subprocess_until_signal(
            cmd=_WORKER_CMD, cwd=tmp_path, logs_dir=logs,
            preflight_timeout=0, no_output_timeout=0,
        )

        assert (existing.stat().st_mode & 0o777) == 0o600

    def test_hardlinked_fresh_log_is_0600(self, tmp_path):
        """Свежий sibling для hardlinked legacy-лога рождается 0600."""
        from awf.signal_watch import run_subprocess_until_signal

        logs = tmp_path / "logs"
        logs.mkdir()
        external = tmp_path / "external.txt"
        _canary(external, mode=0o644)
        os.link(external, logs / "worker-output.out")

        run_subprocess_until_signal(
            cmd=_WORKER_CMD, cwd=tmp_path, logs_dir=logs,
            preflight_timeout=0, no_output_timeout=0,
        )

        _assert_canary_untouched(external, mode=0o644)
        fresh = list(logs.glob(".worker-output-legacy-*.out"))
        assert fresh
        assert (fresh[0].stat().st_mode & 0o777) == 0o600


# ── ЗЕЛЁНЫЙ (закрыто в F1): симлинк промежуточного ``.agentic`` ─────────


class TestR1SafetyQARed:
    def test_agentic_symlink_refused_no_write(self, tmp_path):
        """worker-log отказывает ДО записи при симлинке ``.agentic``.

        Было КРАСНЫМ на проходе QA; закрыто F1 (``signal_watch`` больше
        не обнуляет anchor): отказ до записи, снаружи пусто.
        """
        from awf.signal_watch import run_subprocess_until_signal

        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside"
        (outside / "logs").mkdir(parents=True)
        (project / ".agentic").symlink_to(outside, target_is_directory=True)
        logs = project / ".agentic" / "logs"

        with pytest.raises(Exception):
            run_subprocess_until_signal(
                cmd=_WORKER_CMD, cwd=project, logs_dir=logs,
                preflight_timeout=0, no_output_timeout=0,
            )

        assert list((outside / "logs").iterdir()) == []


# ── КРАСНЫЙ: не переведённая точка открытия лога (awf-start.out) ─────────


class TestR1SafetyQABackgroundLog:
    def test_background_start_agentic_symlink_no_external_write(
        self, tmp_path, monkeypatch
    ):
        """RED: ``awf-start.out`` пишется через симлинк ``.agentic`` наружу.

        ``awf/api/_background.py:141`` открывает лог фонового запуска
        обычным ``os.open`` (``logs_dir.mkdir`` на :102 — тоже). Требование
        раздела B — no-follow по промежуточным компонентам и leaf для
        КАЖДОГО log-writer; REVIEW просил проверить «любые другие открытия
        логов». Достижимо через ``awf api`` / MCP ``awf_start``
        (``pipeline.py:1216``). Отказа ИЛИ записи внутрь проекта нет:
        файл создаётся во внешнем каталоге.
        """
        from awf.api import _background as bg

        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside"
        (outside / "logs").mkdir(parents=True)
        (project / ".agentic").symlink_to(outside, target_is_directory=True)

        class _DummyPopen:
            def __init__(self, *a, **kw):
                self.pid = 4242

        monkeypatch.setattr(bg.subprocess, "Popen", _DummyPopen)

        try:
            bg.start_in_background(
                project, pipeline=None, from_stage=None, auto=False, timeout=60,
            )
        except AwfApiError:
            pass  # a refusal is the required behavior

        leaked = sorted(p.name for p in (outside / "logs").iterdir())
        assert leaked == [], (
            f"background log escaped through the .agentic symlink: {leaked}"
        )


# ── КРАСНЫЕ (agent-qa-review iter-4): оставшиеся дыры того же класса ──────


class TestR1SafetyQAFindings:
    def test_verify_test_results_log_agentic_symlink_no_external_write(
        self, tmp_path
    ):
        """RED (F-A, P0, статический симлинк, БЕЗ гонки): TEST-RESULTS-лог
        ``awf/verify.py::run_verify_commands`` пишется ``atomic_write_text``
        по пути ``<project>/.agentic/outbox/TEST-RESULTS-<id>.log`` без
        ManagedFS. Симлинк ``.agentic`` наружу → лог уходит за проект.
        Тот же класс, что S9-03; точка открытия лога, не переведённая в
        разделе B («любые другие открытия логов» из REVIEW); достижима
        штатной стадией verify / ``awf verify``.
        """
        from awf.verify import run_verify_commands

        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside"
        (outside / "outbox").mkdir(parents=True)
        (project / ".agentic").symlink_to(outside, target_is_directory=True)

        run_verify_commands(
            {"verification": {"test_cmd": "true"}},
            project_dir=project, todo_id="TODO-0001",
        )

        leaked = sorted(p.name for p in (outside / "outbox").iterdir())
        assert leaked == [], (
            f"TEST-RESULTS log escaped through the .agentic symlink: {leaked}"
        )

    def test_intermediate_component_swap_refused(self, tmp_path, monkeypatch):
        """RED (F-B): ``managed_fs.open_append_log`` не делает no-follow
        по ПРОМЕЖУТОЧНЫМ компонентам в момент open — только lstat-preflight
        плюс один ``os.open(path.parent, O_NOFOLLOW)``, где O_NOFOLLOW
        защищает лишь leaf самого parent (``logs``), но НЕ ``.agentic``.
        Подмена ``.agentic`` на симлинк между preflight и open уводит файл
        наружу (TOCTOU), вопреки разделу B TODO.

        Симуляция честная: обёртка вызывает НАСТОЯЩУЮ проверку (путь пока
        чист), затем подменяет ``.agentic`` — ровно гонка validate→open.
        Исправление (по-компонентный обход через dir_fd/O_NOFOLLOW) на
        такой подмене обязано отказать (ELOOP на ``.agentic``).
        """
        import shutil

        from awf.infra import managed_fs

        project = tmp_path / "project"
        (project / ".agentic" / "logs").mkdir(parents=True)
        outside = tmp_path / "outside"
        (outside / "logs").mkdir(parents=True)

        real_check = managed_fs.assert_no_symlink_escape
        state = {"swapped": False}

        def racing_check(path, *, root):
            real_check(path, root=root)  # passes: path is clean right now
            if not state["swapped"]:
                state["swapped"] = True
                shutil.rmtree(project / ".agentic")
                (project / ".agentic").symlink_to(
                    outside, target_is_directory=True
                )

        monkeypatch.setattr(
            managed_fs, "assert_no_symlink_escape", racing_check
        )

        with pytest.raises(Exception):
            managed_fs.open_append_log(
                project / ".agentic" / "logs" / "worker-output.out",
                root=project,
            )

        assert list((outside / "logs").iterdir()) == [], (
            "intermediate .agentic swap between validate and open escaped"
        )

    def test_leaf_swap_between_validate_and_open_refused(
        self, tmp_path, monkeypatch
    ):
        """Acceptance row 3: the LEAF swapped to a symlink between validate
        and open is refused by ``O_NOFOLLOW`` on the leaf — the external
        inode is neither written nor chmod-ed.

        The F-B test above covers an INTERMEDIATE (``.agentic``) swap; this
        pins the leaf guard itself so a refactor that drops ``O_NOFOLLOW``
        from the leaf open regresses red (the follow would fchmod the
        external file to 0600).
        """
        from awf.infra import managed_fs

        project = tmp_path / "project"
        (project / ".agentic" / "logs").mkdir(parents=True)
        external = tmp_path / "external.txt"
        _canary(external, mode=0o644)
        leaf = project / ".agentic" / "logs" / "worker-output.out"

        real_check = managed_fs.assert_no_symlink_escape
        state = {"swapped": False}

        def racing_check(path, *, root):
            real_check(path, root=root)  # clean right now
            if not state["swapped"]:
                state["swapped"] = True
                if leaf.exists() or leaf.is_symlink():
                    leaf.unlink()
                leaf.symlink_to(external)

        monkeypatch.setattr(
            managed_fs, "assert_no_symlink_escape", racing_check
        )

        with pytest.raises(Exception):
            fd, _ = managed_fs.open_append_log(leaf, root=project)
            os.close(fd)

        _assert_canary_untouched(external, mode=0o644)


# ── КРАСНЫЙ (F2, dobor 0219): plain mkdir после lstat-preflight ───────────
#
# Аудит 0215 F2: «``open_append_log`` lacks create → callers use plain
# mkdir». Добор 0219 просил закрыть F2 здесь. Хелпер по-прежнему без
# ``create`` (``_open_dir_chain(root, path)`` — ``create=False``), а оба
# вызывающих создают каталог логов обычным ``Path.mkdir(parents=True)``
# ПОСЛЕ lstat-preflight: ``awf/_log.py:165`` и ``awf/api/_background.py:117``.
# Это тот же класс TOCTOU, что закрытый F-B (подмена ``.agentic`` между
# проверкой и записью) — но для СОЗДАНИЯ КАТАЛОГА, не открытия файла.


class TestR1F2DirectoryCreateResidual:
    def test_log_mkdir_intermediate_swap_no_external_dir(
        self, tmp_path, monkeypatch
    ):
        """RED (F2): ``_log.log`` создаёт каталог логов обычным mkdir после
        lstat-preflight. Подмена ``.agentic`` на симлинк наружу между
        проверкой и ``mkdir`` уводит создание каталога за границу проекта
        (F-B-класс), вопреки требованию «no-follow по промежуточным» и
        пункту F2 добора 0219. Исправление — создание каталога через
        ``mkdirat`` на уже проверенном fd (``_open_dir_chain(create=True)``),
        то есть ``create`` в ``open_append_log``; тогда ``ELOOP`` на
        ``.agentic`` отказывает до создания. Достижимо штатным логированием.
        """
        import shutil

        from awf import _log
        from awf.infra import managed_fs

        project = tmp_path / "project"
        (project / ".agentic" / "logs").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()

        real_check = managed_fs.assert_no_symlink_escape
        state = {"n": 0}

        def racing_check(path, *, root):
            real_check(path, root=root)  # passes: the tree is clean right now
            state["n"] += 1
            if state["n"] == 1:
                # swap AFTER the preflight, BEFORE the caller's mkdir
                shutil.rmtree(project / ".agentic")
                (project / ".agentic").symlink_to(
                    outside, target_is_directory=True
                )

        monkeypatch.setattr(
            managed_fs, "assert_no_symlink_escape", racing_check
        )

        _log.log(project / ".agentic" / "logs", "orchestrator line")

        leaked = sorted(p.name for p in outside.iterdir())
        assert leaked == [], (
            "logs directory created OUTSIDE the project via a swapped "
            f".agentic symlink: {leaked}"
        )


# ── QA-0219-finding: dispatch inbox — plain mkdir после preflight (F2 class) ─
#
# Тот же класс, что закрытый F-0219-1 (_log.py/_background.py): создание
# каталога обычным ``Path.mkdir(parents=True)`` ПОСЛЕ lstat-preflight.
# Здесь точка — ``awf/api/dispatch.py``: ``inbox.mkdir(parents=True,
# exist_ok=True)`` сразу за ``assert_no_symlink_escape(inbox, ...)``: подмена
# ``.agentic`` на симлинк наружу между проверкой и mkdir создаёт ``inbox``
# ВНЕ проекта (пустой каталог), хотя последующая запись файла отказывает.
# Каталог здесь избыточен: ``open_managed(create=True)`` уже создаёт цепочку
# через ``mkdirat`` на проверенном fd.


class TestR1F2DispatchInboxMkdirResidual:
    def test_dispatch_inbox_mkdir_intermediate_swap_no_external_dir(
        self, tmp_git_repo, monkeypatch
    ):
        """RED: ``dispatch_todo`` создаёт ``.agentic/inbox`` обычным mkdir
        после preflight — подмена ``.agentic`` на симлинк наружу между
        проверкой и mkdir оставляет каталог за границей проекта."""
        import shutil

        from awf import api
        from awf.infra import managed_fs

        proj = tmp_git_repo
        api.init_project(proj, project_name="DispatchInboxMkdir")
        agentic = proj / ".agentic"
        shutil.rmtree(agentic / "inbox", ignore_errors=True)

        outside = tmp_git_repo.parent / "outside"
        outside.mkdir()

        real_check = managed_fs.assert_no_symlink_escape
        state = {"swapped": False}

        def racing_check(path, *, root):
            real_check(path, root=root)
            if not state["swapped"] and Path(path).name == "inbox":
                state["swapped"] = True
                shutil.rmtree(agentic)
                agentic.symlink_to(outside, target_is_directory=True)

        monkeypatch.setattr(managed_fs, "assert_no_symlink_escape", racing_check)

        with pytest.raises(Exception):
            api.dispatch_todo(
                proj, content="# TODO-0001 — x\n", todo_id="TODO-0001"
            )

        leaked = sorted(p.name for p in outside.iterdir())
        assert leaked == [], (
            "dispatch created a directory OUTSIDE the project via a swapped "
            f".agentic symlink: {leaked}"
        )
