"""QA-тесты маркера background-ребёнка (фикс TODO-0118).

``_is_background_child`` освобождает спавненный пайплайн-процесс от
гардов его собственного запуска (start/continue). Маркер двойной
(env ``AWF_BACKGROUND_CHILD`` И собственный argv ``-m awf start``):
env утекает во все потомки (воркеры, их шеллы, их тесты), а голый argv
может быть человеком в терминале. Тесты фиксируют договор
двойного маркера:

1. нет env → False (обычный вызывающий, любой argv);
2. env + свой argv → True (спавненный ребёнок);
3. env + чужой argv (потомок воркера) → False — утечка env сама по
   себе не освобождает;
4. env + нечитаемый /proc → True (деградация на не-Linux,
   задокументирована в докстринге функции);
5. env + реальный argv тестового процесса (не наш) → False —
   случай потомка воркера без моков.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf.api import _liveness
from awf.api import pipeline as pipeline_mod

_SELF_CMDLINE = "/proc/self/cmdline"


def _fake_self_cmdline(monkeypatch, argv: bytes | None, *, error: bool = False):
    """Подмена чтения ``/proc/self/cmdline`` (только этого пути)."""
    real_read = Path.read_bytes

    def fake_read(self):
        if str(self) == _SELF_CMDLINE:
            if error:
                raise OSError("simulated: no /proc")
            return argv
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", fake_read)


def test_no_env_marker_returns_false(monkeypatch):
    """Без env — обычный вызывающий, argv не имеет значения."""
    monkeypatch.delenv("AWF_BACKGROUND_CHILD", raising=False)
    _fake_self_cmdline(monkeypatch, b"python3\x00-m\x00awf\x00start\x00")
    assert pipeline_mod._is_background_child() is False


def test_env_marker_with_our_argv_returns_true(monkeypatch):
    """Env + свой argv (``-m awf start``) — спавненный ребёнок."""
    monkeypatch.setenv("AWF_BACKGROUND_CHILD", "1")
    _fake_self_cmdline(
        monkeypatch, b"python3\x00-m\x00awf\x00start\x00--project-dir\x00/tmp\x00"
    )
    assert pipeline_mod._is_background_child() is True


def test_env_marker_with_foreign_argv_returns_false(monkeypatch):
    """Env + чужой argv (потомок воркера) — случай утечки.

    Env-переменную наследуют все потомки; регрессия argv-проверки
    (обрат к env-only) заставила бы pytest воркера считаться
    background-ребёнком, и lease/liveness-гарды молча перестали бы
    работать для него.
    """
    monkeypatch.setenv("AWF_BACKGROUND_CHILD", "1")
    _fake_self_cmdline(monkeypatch, b"python3\x00-m\x00pytest\x00tests\x00")
    assert pipeline_mod._is_background_child() is False


def test_env_marker_without_proc_degrades_to_true(monkeypatch):
    """Env + нечитаемый /proc (не-Linux) — деградация к env-маркеру,
    как задокументировано в докстринге функции."""
    monkeypatch.setenv("AWF_BACKGROUND_CHILD", "1")
    _fake_self_cmdline(monkeypatch, None, error=True)
    assert pipeline_mod._is_background_child() is True


def test_env_marker_with_real_test_argv_returns_false(monkeypatch):
    """Случай потомка воркера без моков: argv тестового процесса не
    ``-m awf start`` — с env он обязан НЕ считаться ребёнком."""
    real_cmdline = Path(_SELF_CMDLINE).read_bytes().decode(
        "utf-8", errors="replace"
    )
    if _liveness.cmdline_is_ours(real_cmdline):
        pytest.skip("argv тестраннера похож на `-m awf start` — предпосылка не выполнена")
    monkeypatch.setenv("AWF_BACKGROUND_CHILD", "1")
    assert pipeline_mod._is_background_child() is False
