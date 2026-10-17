"""QA-верификация к TODO-0218 (R1.4), роль agent-qa-review (стадия 4).

Пробел покрытия требования 2 (ExecutionId + handshake ребёнка): тест QA6
проверяет, что ``start_pipeline`` публикует метку ДО вызова
``start_in_background``, но подменяет сам ``start_in_background`` фейком.
Значит строка в ``_background``, которая кладёт метку в окружение ребёнка
(``AWF_EXECUTION_ID``), не исполняется ни одним тестом: удали её — набор
останется зелёным, а handshake молча выродится в legacy «метки нет =
работай», и crash-orphan ребёнок прежнего запуска снова поднимет второй
пайплайн поверх живого проекта.

Тест зовёт НАСТОЯЩИЙ ``start_in_background`` с заглушкой
``subprocess.Popen`` и проверяет фактический ``env``, ушедший ребёнку, —
обе стороны: метка есть → она в env; метки нет → ключа в env нет.
"""
from __future__ import annotations

import awf.api._background as bg
from awf import api
from awf.api import _lease


def _project(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="QaExecEnv")
    return tmp_git_repo


class _FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid


class TestBackgroundChildGetsExecutionId:
    def test_child_env_carries_current_execution_id(
        self, tmp_git_repo, monkeypatch
    ):
        proj = _project(tmp_git_repo)
        marker = _lease.rotate_execution_id(proj)
        assert _lease.read_execution_id(proj) == marker

        captured: dict = {}

        def fake_popen(argv, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc(4242)

        monkeypatch.setattr(bg.subprocess, "Popen", fake_popen)

        pid, _log, _pid = bg.start_in_background(
            proj, pipeline=None, from_stage=None, auto=False, timeout=10
        )

        env = captured["env"]
        assert env is not None, "Popen вызван без env — ребёнок без ExecutionId"
        assert env.get(_lease.EXECUTION_ENV) == marker, (
            "ребёнку не передан текущий ExecutionId — handshake требования 2 "
            f"недостижим (env={env.get(_lease.EXECUTION_ENV)!r}, marker={marker!r})"
        )
        assert pid == 4242

    def test_child_env_omits_key_when_marker_absent(
        self, tmp_git_repo, monkeypatch
    ):
        proj = _project(tmp_git_repo)
        monkeypatch.delenv(_lease.EXECUTION_ENV, raising=False)
        assert _lease.read_execution_id(proj) is None

        captured: dict = {}

        def fake_popen(argv, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc(4243)

        monkeypatch.setattr(bg.subprocess, "Popen", fake_popen)

        bg.start_in_background(
            proj, pipeline=None, from_stage=None, auto=False, timeout=10
        )

        assert _lease.EXECUTION_ENV not in captured["env"], (
            "метки нет — ключ не должен появляться в env ребёнка (иначе "
            "handshake сравнил бы пустое значение с несуществующей меткой)"
        )
