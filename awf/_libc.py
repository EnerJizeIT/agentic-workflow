"""TODO-0242 (A.5): the ONE libc handle for prctl callers.

Two modules used to load libc each in its own way: ``_env.py`` opened
``CDLL("libc.so.6")`` at import (musl has no such SONAME) and
``_engine_liveness.py`` opened ``CDLL(None)`` per call. The portable handle
is the process-global namespace — ``CDLL(None)`` — and one cached loader
keeps both callers on it.

The cache is loaded in the PARENT process (``_env`` does it at import;
``reset_cache`` re-primes in the caller): ``CDLL`` after ``fork`` in a
multi-threaded orchestrator is unsafe, so the preexec function must only
call ``prctl``. ``reset_cache`` exists for tests that monkeypatch
``ctypes.CDLL``.
"""
from __future__ import annotations

import ctypes

_cdll = None
_loaded = False


def get_libc():
    """The cached process-global libc handle (``CDLL(None)``), or None.

    A load failure is not fatal: callers degrade to their documented
    fallbacks (a log line, a no-op signal).
    """
    global _cdll, _loaded
    if _loaded:
        return _cdll
    _loaded = True
    try:
        _cdll = ctypes.CDLL(None, use_errno=True)
    except OSError:
        _cdll = None
    return _cdll


def reset_cache() -> None:
    """Forget and immediately re-prime the handle (tests patch ``ctypes.CDLL``).

    The re-prime happens in the CALLER — the parent process. It is the
    whole point of the cache: ``_pdeathsig_preexec`` runs after ``fork`` in
    a multi-threaded orchestrator, so ``get_libc`` there must never reach
    ``CDLL`` (dlopen after ``fork`` is unsafe, AUD14-07). A plain clear
    would leave the next post-fork caller to load the library itself.
    """
    global _cdll, _loaded
    _cdll = None
    _loaded = False
    get_libc()


__all__ = ["get_libc", "reset_cache"]
