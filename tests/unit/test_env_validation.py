"""QA 2026-08-03: env-var int parsing must not crash on invalid input.

Before fix: ``int(os.environ.get("AWF_APPROVE_TIMEOUT_SECONDS", ...))``
at module-import time crashed the whole ``awf.commit_gate`` module if the
env var contained a typo (e.g. ``1800s``). Same pattern in
``awf.supervisor`` at runtime. Both now fall back to defaults.
"""
from __future__ import annotations

import importlib

from awf import commit_gate, supervisor


class TestCommitGateEnvValidation:
    """AWF_APPROVE_TIMEOUT_SECONDS parsing — safe fallback on typo."""

    def test_invalid_value_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("AWF_APPROVE_TIMEOUT_SECONDS", "1800s")  # typo
        assert commit_gate._safe_int_env("AWF_APPROVE_TIMEOUT_SECONDS", 1800) == 1800

    def test_empty_value_falls_back(self, monkeypatch) -> None:
        monkeypatch.setenv("AWF_APPROVE_TIMEOUT_SECONDS", "")
        assert commit_gate._safe_int_env("AWF_APPROVE_TIMEOUT_SECONDS", 1800) == 1800

    def test_negative_clamped_to_one(self, monkeypatch) -> None:
        """Negative timeout makes no sense — clamp to 1 (min meaningful)."""
        monkeypatch.setenv("AWF_APPROVE_TIMEOUT_SECONDS", "-5")
        assert commit_gate._safe_int_env("AWF_APPROVE_TIMEOUT_SECONDS", 1800) == 1

    def test_valid_value_used(self, monkeypatch) -> None:
        monkeypatch.setenv("AWF_APPROVE_TIMEOUT_SECONDS", "999")
        assert commit_gate._safe_int_env("AWF_APPROVE_TIMEOUT_SECONDS", 1800) == 999

    def test_module_import_does_not_crash_on_typo(self, monkeypatch) -> None:
        """Regression: importing awf.commit_gate with invalid env must not raise.

        Before fix: ``APPROVE_TIMEOUT_SECONDS = int(os.environ.get(...))``
        ran at module scope — invalid value crashed import for the entire
        commit_gate module (and any caller that imported it).
        """
        monkeypatch.setenv("AWF_APPROVE_TIMEOUT_SECONDS", "not-a-number")
        # Re-import the module from scratch — if import crashes, test fails.
        importlib.reload(commit_gate)
        assert commit_gate._get_approve_timeout() == 1800


class TestSupervisorEnvValidation:
    """AWF_SUPERVISOR_TIMEOUT parsing — safe fallback on typo."""

    def test_invalid_value_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("AWF_SUPERVISOR_TIMEOUT", "abc")
        assert supervisor._safe_supervisor_timeout() == 3600

    def test_empty_value_falls_back(self, monkeypatch) -> None:
        monkeypatch.setenv("AWF_SUPERVISOR_TIMEOUT", "")
        assert supervisor._safe_supervisor_timeout() == 3600

    def test_negative_clamped_to_one(self, monkeypatch) -> None:
        monkeypatch.setenv("AWF_SUPERVISOR_TIMEOUT", "-1")
        assert supervisor._safe_supervisor_timeout() == 1

    def test_valid_value_used(self, monkeypatch) -> None:
        monkeypatch.setenv("AWF_SUPERVISOR_TIMEOUT", "7200")
        assert supervisor._safe_supervisor_timeout() == 7200
