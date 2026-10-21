"""Session-kill tripwire as a pytest plugin (TODO-0240 Z1).

``tests/conftest.py`` guards the project suite, but ``verify_pack`` runs a QA
finding file from ``.agentic/context/`` — pytest does not load
``tests/conftest.py`` for that path, so a natural repro such as calling
``os.killpg`` with target ``1`` inside ``pytest.raises`` would signal every
process of the user session (the 2026-09-20 incident: a non-positive target
is a broadcast to the whole uid). The ``qa_findings`` runner loads this
module explicitly with ``-p awf._safety_tripwire`` so the guard is in place
before collection.

The rule is the same one the suite carries: never call through with a
pid/pgid <= 1 — raise loudly instead. The marker prefix stays ``SAFETY:
os.…`` so a test can pin it.
"""
import os


def _forbid_session_kill(real, name):
    def guarded(target, sig, *args, **kwargs):
        if isinstance(target, int) and target <= 1:
            raise RuntimeError(
                f"SAFETY: {name}({target!r}, {sig}) targets pid/pgid <= 1 — "
                "this would signal the entire user session. Refusing to "
                "call through (2026-09-20 session-kill incident)."
            )
        return real(target, sig, *args, **kwargs)

    guarded._awf_tripwire = True
    return guarded


def _install() -> None:
    if not getattr(os.kill, "_awf_tripwire", False):
        os.kill = _forbid_session_kill(os.kill, "os.kill")
    if hasattr(os, "killpg") and not getattr(os.killpg, "_awf_tripwire", False):
        os.killpg = _forbid_session_kill(os.killpg, "os.killpg")


_install()
