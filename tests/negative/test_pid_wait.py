"""A38 (TODO-0351): поведение помощника ожидания exec.

Вынесено вместе с помощником из-за порога храповика: сама регрессия
живёт в `test_run_next_failure_transitions.py`, здесь — строгость
помощника (окно fork→exec и таймаут).
"""
from __future__ import annotations

import pytest

from tests.negative._pid_wait import wait_looks_like_awf_start


def test_wait_helper_survives_pre_exec_cmdline(monkeypatch):
    """A38 (TODO-0351): окно fork→exec — не идентичность процесса.

    До правки одиночное чтение ``/proc/<pid>/cmdline`` сразу после
    ``Popen`` возвращало argv РОДИТЕЛЯ (pytest), и тест краснел только в CI.
    Помощник обязан пережить столько «чужих» чтений, сколько длится exec, и
    вернуться ровно на первом совпавшем argv.
    """
    from awf.api import _liveness

    pre_exec = (
        "python3\x00-m\x00pytest\x00"
        "tests/negative/test_run_next_failure_transitions.py\x00"
    )
    after_exec = (
        "python3\x00-c\x00pass\x00-m\x00awf\x00start\x00"
        "--project-dir\x00/elsewhere\x00"
    )
    scripted = iter([pre_exec, pre_exec, after_exec])
    seen_pids: list[int] = []

    def fake_read_cmdline(pid: int) -> str:
        seen_pids.append(pid)
        return next(scripted)

    monkeypatch.setattr(_liveness, "read_cmdline", fake_read_cmdline)

    wait_looks_like_awf_start(424242, timeout=2.0, step=0.01)

    assert seen_pids == [424242, 424242, 424242], (
        "помощник принял argv до exec за настоящий: чтений "
        f"{len(seen_pids)} вместо 3"
    )


@pytest.mark.parametrize(
    "stuck_cmdline",
    [
        "python3\x00-m\x00pytest\x00tests\x00",
        None,
    ],
    ids=["foreign-argv", "unreadable"],
)
def test_wait_helper_is_strict_on_timeout(monkeypatch, stuck_cmdline):
    """A38: таймаут — красный тест с последним argv, не тихий пропуск.

    TODO-0351 п.2: «если через таймаут cmdline не наш — тест красный с
    понятным сообщением». Проверяем оба исхода таймаута: живой процесс с
    чужим argv и нечитаемый/умерший (``read_cmdline`` возвращает None).
    """
    from awf.api import _liveness

    monkeypatch.setattr(_liveness, "read_cmdline", lambda pid: stuck_cmdline)

    with pytest.raises(AssertionError) as excinfo:
        wait_looks_like_awf_start(424242, timeout=0.1, step=0.01)

    message = str(excinfo.value)
    assert "424242" in message, f"в сообщении нет pid: {message}"
    assert "-m awf start" in message, (
        f"в сообщении нет ожидаемой формы argv: {message}"
    )
    assert repr(stuck_cmdline) in message, (
        f"в сообщении нет последнего прочитанного argv: {message}"
    )
