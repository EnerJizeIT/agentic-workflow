"""A-20 (аудит 2026-09-25, слой 6): MCP-инструменты не блокируют цикл событий.

Общий адаптер ``_exec`` отправляет синхронные api-вызовы в
``asyncio.to_thread``. Инструмент, который вызывает api прямо из
``async def``, держит весь event loop (и все остальные MCP-вызовы на
нём) до завершения своего вызова.

Методика теста: медленная часть dispatch (baseline) подменяется
артифакльной задержкой (time.sleep в подменённой функции — реального
сетевых/IO-тайминга нет), поверх конкурентно запускаются медленный
``awf_dispatch_todo`` и лёгкий ``awf_status``. Каждый таск пишет время
своего завершения (loop.time) изнутри себя — сбор результатов в
тест-корутине блокировку loop замаскировал бы.
"""
from __future__ import annotations

import asyncio
import time

from agent_workflow_ui.tools import awf

from awf import api


def test_light_call_not_blocked_by_slow_dispatch(tmp_git_repo, monkeypatch):
    """Лёгкий вызов на том же loop завершается ДО медленного dispatch."""
    api.init_project(tmp_git_repo, project_name="A20")

    # Медленная часть dispatch: baseline с артифакльной задержкой 1.5 с.
    # Патчим имя в модуле dispatch — именно туда его импортировала
    # dispatch_todo (from .pipeline import create_baseline).
    from awf.api import dispatch as dispatch_mod

    real_create_baseline = dispatch_mod.create_baseline
    baseline_calls = []

    def slow_baseline(*args, **kwargs):
        baseline_calls.append(1)
        time.sleep(1.5)
        return real_create_baseline(*args, **kwargs)

    monkeypatch.setattr(dispatch_mod, "create_baseline", slow_baseline)

    times: dict[str, float] = {}

    async def scenario():
        loop = asyncio.get_running_loop()

        async def light_task():
            result = await awf.awf_status(project_dir=str(tmp_git_repo))
            times["light"] = loop.time()
            return result

        async def dispatch_task():
            result = await awf.awf_dispatch_todo(
                "# A20 async task", project_dir=str(tmp_git_repo)
            )
            times["dispatch"] = loop.time()
            return result

        light = asyncio.ensure_future(light_task())
        dispatch = asyncio.ensure_future(dispatch_task())
        light_result = await light
        dispatch_result = await dispatch
        return light_result, dispatch_result

    light_result, dispatch_result = asyncio.run(scenario())

    # Оба инструмента отработали (контракт ответов сохранён).
    assert light_result["status"] == "ok"
    assert dispatch_result["status"] == "ok"
    assert dispatch_result["todo_id"] == "TODO-0001"
    assert baseline_calls == [1]

    # Лёгкий вызов завершён раньше медленного, с запасом >= 0.5 с.
    # Базовый (дефектный) код: dispatch блокирует loop на 1.5 с — лёгкий
    # таск не может завершиться, пока dispatch не вернётся, разница ~0.
    assert times["light"] < times["dispatch"] - 0.5, (
        f"light tool was blocked by the slow dispatch: "
        f"light finished at {times['light']:.2f}s, dispatch at "
        f"{times['dispatch']:.2f}s — the event loop was serialized"
    )


def test_dispatch_ok_contract_unchanged(tmp_git_repo):
    """Регрессия: результат dispatch (success path) не изменился."""
    api.init_project(tmp_git_repo, project_name="A20c")
    result = asyncio.run(
        awf.awf_dispatch_todo("# A20 contract", project_dir=str(tmp_git_repo))
    )
    assert result["status"] == "ok"
    assert result["todo_id"] == "TODO-0001"
    assert result["baseline_sha"]
    assert (tmp_git_repo / ".agentic" / "inbox" / "TODO-0001.md").is_file()
    assert (tmp_git_repo / ".agentic" / "inbox" / "TODO-0001.ready").is_file()
    assert result["next_action"].startswith("TODO-0001 dispatched.")


def test_dispatch_error_contract_unchanged(tmp_git_repo, tmp_path):
    """Регрессия: ошибки dispatch обрабатываются как раньше."""
    # Пустой контент — AwfApiError из api превращается в {status: error}.
    api.init_project(tmp_git_repo, project_name="A20e")
    result = asyncio.run(awf.awf_dispatch_todo("", project_dir=str(tmp_git_repo)))
    assert result["status"] == "error"
    assert "content is required" in result["error"]

    # Нет .agentic — тоже error dict, не исключение.
    bare = tmp_path / "bare"
    bare.mkdir()
    result = asyncio.run(awf.awf_dispatch_todo("# t", project_dir=str(bare)))
    assert result["status"] == "error"
    assert result["error"]
