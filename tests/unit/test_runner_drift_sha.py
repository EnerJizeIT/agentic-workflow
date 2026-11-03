"""A40: the runner gate compares commits, not printed commit strings.

``git rev-parse --short`` picks its length per repository, so one commit
printed as ``b2fa1840`` in the pinned runner and ``b2fa184`` in this awf —
and the string comparison refused every launch as "pinned runner drift".
The gate now resolves both recorded forms (full OID, short id, tag) in the
repo that recorded them and compares the full OIDs.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf import versions
from awf.api import pipeline as api_pipeline


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(repo),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )
    return result.stdout.strip()


def _repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    (repo / "awf").mkdir(parents=True)
    (repo / "awf" / "__init__.py").write_text("", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    return repo


def _commit(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _short(repo: Path) -> str:
    """The per-repo short form, exactly what ``repo_commit`` records."""
    return _git(repo, "rev-parse", "--short", "HEAD")


def _tag(repo: Path, name: str) -> None:
    _git(repo, "tag", name)


def _project(tmp_path: Path, runner: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".agentic").mkdir(parents=True, exist_ok=True)
    (project / ".agentic" / "config.yaml").write_text(
        f"project:\n  name: x\nautomation:\n  runner_dir: {runner}\n",
        encoding="utf-8",
    )
    return project


def _recorded(monkeypatch, runner_value: str, process_value: str, repo: Path) -> None:
    """Pin the recorded strings and the repo the process fact is read from."""
    monkeypatch.setattr(versions, "repo_commit", lambda path: runner_value)
    monkeypatch.setattr(versions, "process_commit", lambda: process_value)
    monkeypatch.setattr(versions, "process_repo", lambda: repo)


@pytest.mark.parametrize("short_len", [7, 8])
def test_full_short_and_tag_forms_of_one_commit_are_not_drift(
    tmp_path, monkeypatch, short_len
):
    """The same commit recorded full on one side and short on the other."""
    runner = _repo(tmp_path, "runner")
    oid = _commit(runner)
    short = oid[:short_len]
    assert short != oid

    for pinned, running in ((short, oid), (oid, short), ("v1", oid)):
        if pinned == "v1":
            _tag(runner, "v1")
        _recorded(monkeypatch, pinned, running, runner)
        project = _project(tmp_path, runner)
        assert api_pipeline._runner_drift_refusal(project) is None, (pinned, running)


def test_short_lengths_differing_are_not_drift(tmp_path, monkeypatch):
    """The incident itself: ``b2fa1840`` in the runner vs ``b2fa184`` in awf."""
    runner = _repo(tmp_path, "runner")
    oid = _commit(runner)
    _recorded(monkeypatch, oid[:8], oid[:7], runner)

    assert api_pipeline._runner_drift_refusal(_project(tmp_path, runner)) is None


def test_different_commits_are_refused_with_both_sha(tmp_path, monkeypatch, capsys):
    runner = _repo(tmp_path, "runner")
    other = _repo(tmp_path, "other")
    (other / "awf" / "extra.txt").write_text("x\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "two")
    pinned_oid, running_oid = _commit(runner), _commit(other)
    _recorded(monkeypatch, pinned_oid[:7], running_oid[:8], other)

    refusal = api_pipeline._runner_drift_refusal(_project(tmp_path, runner))

    assert refusal is not None and refusal.exit_code == 1
    message = refusal.message
    assert versions.short_oid(pinned_oid) in message
    assert versions.short_oid(running_oid) in message
    assert f'git -C "{runner}" reset --hard {running_oid}' in message
    assert "pinned runner drift" in capsys.readouterr().err


def test_unknown_commit_is_named_not_called_drift(tmp_path, monkeypatch, capsys):
    runner = _repo(tmp_path, "runner")
    _recorded(monkeypatch, "deadbeef00", _commit(runner)[:7], runner)

    refusal = api_pipeline._runner_drift_refusal(_project(tmp_path, runner))

    assert refusal is not None and refusal.exit_code == 1
    assert "deadbeef00" in refusal.message
    assert f'git -C "{runner}"' in refusal.message
    # A40: an id that does not resolve is not evidence of drift — the word
    # must not reach the operator, or they will chase a sync that cannot help.
    assert "drift" not in refusal.message
    assert "pinned runner commit unknown" in capsys.readouterr().err


# ── The same verdict on the two advisory surfaces ───────────────────────


def _equal_versions(monkeypatch) -> None:
    """The version pair matches, so only the commit line can speak."""
    monkeypatch.setattr(versions, "process_version", lambda: "2.1.0")
    monkeypatch.setattr(versions, "plugin_version", lambda: "2.1.0")
    monkeypatch.setattr(versions, "plugin_requirement", lambda: "")


def _distinct(repo: Path) -> Path:
    """Another commit: identical trees on the same second share one OID."""
    (repo / "awf" / "extra.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")
    return repo


def _versioned(repo: Path) -> Path:
    (repo / "awf" / "__init__.py").write_text('__version__ = "2.1.0"\n')
    return repo


def test_handshake_does_not_name_one_commit_as_two(tmp_path, monkeypatch):
    """A40 on the launch warning: ``repo_commit`` still prints per-repo short
    forms, so the handshake must not read 7 vs 8 characters as two commits."""
    runner = _versioned(_repo(tmp_path, "runner"))
    oid = _commit(runner)
    _equal_versions(monkeypatch)
    # Full OID on the process side vs the runner's own short form: the old
    # string comparison warned here even though both name one commit.
    monkeypatch.setattr(versions, "process_commit", lambda: oid)
    monkeypatch.setattr(versions, "process_repo", lambda: runner)

    facts = versions.handshake(tmp_path, {"automation": {"runner_dir": str(runner)}})

    assert "differs" not in facts["warning"], facts["warning"]


def test_handshake_names_a_real_difference(tmp_path, monkeypatch):
    """The advisory side of the gate: two commits still read as a difference,
    named by their short forms (not silently swallowed by the resolver)."""
    runner = _versioned(_repo(tmp_path, "runner"))
    process = _versioned(_distinct(_repo(tmp_path, "process")))
    _equal_versions(monkeypatch)
    monkeypatch.setattr(versions, "process_commit", lambda: _commit(process)[:8])
    monkeypatch.setattr(versions, "process_repo", lambda: process)

    facts = versions.handshake(tmp_path, {"automation": {"runner_dir": str(runner)}})

    assert "runner commit" in facts["warning"]
    # The advisory line quotes the recorded forms; only the verdict resolves.
    assert f"runner commit {_short(runner)}" in facts["warning"]
    assert f"process commit {_commit(process)[:8]}" in facts["warning"]


def test_doctor_verdict_compares_one_form(tmp_path, monkeypatch):
    """A40 on the ``doctor`` row: same commit at 8 vs 7 characters is one
    commit; two repos are two commits."""
    from awf.doctor import _different_code

    runner = _repo(tmp_path, "runner")
    other = _distinct(_repo(tmp_path, "other"))
    monkeypatch.setattr(versions, "process_repo", lambda: runner)

    assert _different_code(runner, _commit(runner)[:8], _commit(runner)[:7]) is False
    assert _different_code(runner, _commit(runner)[:8], _commit(other)[:8]) is True


def test_unresolvable_process_commit_is_unknown_not_drift(
    tmp_path, monkeypatch, capsys
):
    """Both sides are checked: a pinned id that resolves against a process id
    that does not cannot be called drift — the sync target would be a guess."""
    runner = _repo(tmp_path, "runner")
    _recorded(monkeypatch, _commit(runner)[:8], "0000000", runner)

    refusal = api_pipeline._runner_drift_refusal(_project(tmp_path, runner))

    assert refusal is not None
    assert "drift" not in refusal.message
    assert "0000000" in refusal.message
    assert "pinned runner commit unknown" in capsys.readouterr().err
