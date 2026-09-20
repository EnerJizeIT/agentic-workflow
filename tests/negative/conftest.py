"""Shared fixtures for the negative suite.

AUD12-08: ``tmp_git_repo`` was a verbatim copy of the unit-suite fixture —
it now lives in tests/conftest.py and reaches this suite by fixture
cascade. The negative suite needs real repositories (archive/restore,
baselines, work fingerprints) — same contract.
"""
