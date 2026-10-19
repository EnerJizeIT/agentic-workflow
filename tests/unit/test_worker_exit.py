"""M2: shared worker-exit diagnostic (``awf/_worker_exit.py``).

The normal exit path (``agent_stage``) and the engine's kill paths
(``signal_watch``) emit ONE line through the same builder. These are the
pure-function edges: shell vs raw signal conventions, network-before-OOM
precedence, the ``Killed`` marker, and the explicit ``timeout`` class.
"""
from __future__ import annotations

from awf._worker_exit import (
    classify_worker_exit,
    exit_signal_name,
    worker_exit_line,
)


def test_signal_name_reads_both_conventions():
    assert exit_signal_name(137) == "SIGKILL"  # shell: 128 + 9
    assert exit_signal_name(-9) == "SIGKILL"  # Popen raw
    assert exit_signal_name(143) == "SIGTERM"
    assert exit_signal_name(-15) == "SIGTERM"
    assert exit_signal_name(0) == "none"
    assert exit_signal_name(1) == "none"


def test_classify_network_first_then_oom_like_then_silent():
    assert classify_worker_exit(0, "Cannot connect to API") == "network"
    # a network marker outranks the rc sign (the backoff retry owns it)
    assert classify_worker_exit(137, "Cannot connect to API") == "network"
    assert classify_worker_exit(137, "") == "oom-like"
    assert classify_worker_exit(-9, "") == "oom-like"
    assert classify_worker_exit(0, "Killed\n") == "oom-like"
    assert classify_worker_exit(0, "worker stopped talking") == "silent"


def test_signal_present_suppresses_the_line():
    assert worker_exit_line(1, 3.0, "Cannot connect", signal_present=True) == ""


def test_line_format_and_explicit_class():
    assert worker_exit_line(137, 12.6, "") == (
        "worker-exit: rc=137 signal=SIGKILL duration=13s class=oom-like"
    )
    assert worker_exit_line(0, 0.2, "", cls="timeout") == (
        "worker-exit: rc=0 signal=none duration=0s class=timeout"
    )


def test_exit_codes_above_the_signal_range_are_not_signals():
    """TODO-0226 (F1): rc>192 is an exit code, not a signal.

    ``sys.exit(-1)`` lands as 255 and an application rc of 200 is ordinary
    too — the shell convention would read them as 127/72 and invent
    SIG127/SIG72. Only 1..NSIG-1 is a real signal.
    """
    assert exit_signal_name(255) == "none"
    assert exit_signal_name(200) == "none"
    assert exit_signal_name(-9) == "SIGKILL"
    assert exit_signal_name(137) == "SIGKILL"
    assert exit_signal_name(0) == "none"
