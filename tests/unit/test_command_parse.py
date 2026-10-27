"""AUD-27-3: one shared command parser for every suite consumer.

The gate, the verify pack's flake probe and ``scripts/suite-canonical.sh``
used to run three parsers with three different coverage sets. These tests
pin the shared module: runner prefixes (``uv/poetry/pdm/hatch run``),
``sudo``/``then``/``do``/PATH wrappers, ``python -m pytest`` and the
script's runner lookup.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from awf import command_parse

# --- AUD-27-3: one shared command parser for the three consumers -----------


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest tests/",
        "uv run pytest tests/",
        "uv run --no-project pytest tests/",
        "uv run --python 3.12 pytest tests/",
        "uv run -- pytest tests/",
        "poetry run pytest tests/",
        "pdm run pytest tests/",
        "hatch run pytest tests/",
        "uv run python -m pytest tests/",
        "uv run python3 -m pytest tests/",
        "sudo uv run pytest tests/",
        "/usr/local/bin/uv run pytest tests/",
        "env FOO=1 uv run pytest tests/",
        "PYTHONPATH=. poetry run pytest tests/",
        "if true; then uv run pytest tests/; fi",
        "for f in a; do poetry run pytest tests/; done",
    ],
)
def test_shared_parser_unwraps_runner_prefixes(cmd):
    """AUD-27-1: uv/poetry/pdm/hatch, sudo, then/do and PATH wrappers."""
    kinds = [
        program.kind
        for program in (
            command_parse.parse_words(command_parse.split_words(segment))
            for segment in command_parse.split_segments(cmd)
        )
        if program is not None
    ]
    assert "pytest" in kinds or "python-pytest" in kinds


@pytest.mark.parametrize(
    "python",
    ["python3", "python3.13t", "pypy3", "pypy3.10"],
)
def test_python_variants_resolve_the_module_runner(python):
    """AUD-28-2: the interpreter is not only ``pythonN.N``."""
    program = command_parse.parse_words([python, "-m", "pytest", "tests/"])
    assert program is not None and program.kind == "python-pytest"


@pytest.mark.parametrize(
    "cmd,kind,word",
    [
        ("uv run --no-project ./scripts/run-all.sh", "other",
         "./scripts/run-all.sh"),
        ("uv run --frozen /venv/bin/pytest tests/", "pytest",
         "/venv/bin/pytest"),
        ("uv run --no-project npm test", "other", "npm"),
        ("uv run --extra dev pytest tests/", "pytest", "pytest"),
    ],
)
def test_unknown_runner_flag_keeps_the_trailing_program(cmd, kind, word):
    """A prefix of only ``option value`` runs nothing, so the last word is
    the program (``uv run --no-project ./scripts/run-all.sh``). A known
    runner family (``npm``) is a program in the lookahead too."""
    program = command_parse.parse_words(command_parse.split_words(cmd))
    assert program is not None
    assert (program.kind, program.word) == (kind, word)


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("uv run pytest tests/", "pytest"),
        ("poetry run python -m pytest", "pytest"),
        ("uv run python3 -m pytest", "pytest"),
        ("sudo /usr/bin/pytest tests/", "pytest"),
        ("bash -c 'pytest tests/'", "pytest"),
        ("npm test", "npm"),
        ("go test ./...", "go"),
        ("cargo test", "cargo"),
        ("make test", "make"),
        ("uv run scripts/run-all.sh", "unknown"),
        ("sudo ./scripts/run-all.sh", "unknown"),
        ("", "unknown"),
    ],
)
def test_detect_runner_names_the_family(cmd, expected):
    """The script's artifact counts need the runner name from this parser."""
    assert command_parse.detect_runner(cmd) == expected


def test_cli_runner_flag_uses_the_shared_parser(awf_bin, awf_env):
    r = subprocess.run(
        [awf_bin, "suite-command", "--runner", "uv run pytest tests/"],
        capture_output=True,
        text=True,
        env=dict(awf_env),
        check=False,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == "pytest"


def test_artifact_runner_field_does_not_name_the_wrapper(awf_bin, awf_env):
    """AUD-28-8: ``uv run scripts/run-all.sh`` has no countable runner.

    The wrapping prefix is not the runner: the artifact's ``runner`` field
    must say ``unknown`` (counts stay null with a hint), not ``uv`` — a
    false count format. Same answer through the CLI the script calls.
    """
    r = subprocess.run(
        [awf_bin, "suite-command", "--runner", "uv run scripts/run-all.sh"],
        capture_output=True,
        text=True,
        env=dict(awf_env),
        check=False,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == "unknown"
    assert command_parse.detect_runner("uv run scripts/run-all.sh") == "unknown"


def test_script_uses_the_shared_runner_parser():
    """AUD-27-3: the script is not a third parser any more."""
    script = (Path(__file__).parents[2] / "scripts" / "suite-canonical.sh").read_text(
        encoding="utf-8"
    )
    assert "suite-command --runner" in script
    assert "def detect_runner" not in script
    assert "def _unwrap_one" not in script


# --- CTX3: a single ``&`` is a separator for the flake probe ---------------


def test_single_ampersand_is_per_caller():
    """The gate keeps its blind spot; the verify pack must see both runs."""
    assert command_parse.split_segments("echo x & pytest tests/") == [
        "echo x & pytest tests/"
    ]
    assert command_parse.split_segments(
        "echo x & pytest tests/", split_single_amp=True
    ) == ["echo x ", " pytest tests/"]
    assert command_parse.split_segments("a && pytest tests/") == [
        "a ",
        " pytest tests/",
    ]


# --- CTX4: a failed ``suite-command --runner`` leaves the reason -----------


def _git_init(proj: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=proj, check=True)
    (proj / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=proj, check=True)


def test_script_artifact_names_cli_unavailable_runner_source(tmp_path):
    """CTX4: the artifact says WHY the runner is unknown.

    ``awf suite-command --runner`` failing used to leave only the warning on
    stderr; the artifact now carries ``runner_source: cli-unavailable`` and
    a matching ``counts_hint``, so a later reader does not guess.
    """
    source = Path(__file__).parents[2] / "scripts" / "suite-canonical.sh"
    proj = tmp_path / "proj"
    (proj / ".agentic").mkdir(parents=True)
    (proj / "scripts").mkdir()
    (proj / "bin").mkdir()
    (proj / "scripts" / "suite-canonical.sh").write_text(
        source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    fake = proj / "bin" / "awf"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        "  tree-sha) echo deadbeef ;;\n"
        "  suite-command)\n"
        '    case "${2:-}" in\n'
        "      --runner|--runner=*) exit 3 ;;\n"
        "    esac\n"
        "    echo true ;;\n"
        "  *) exit 9 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    _git_init(proj)
    (proj / ".agentic" / "config.yaml").write_text(
        "verification:\n  test_cmd: 'true'\n", encoding="utf-8"
    )
    (proj / ".git" / "info" / "exclude").write_text(
        ".agentic/context/\n", encoding="utf-8"
    )
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env["AWF_PYTHON"] = sys.executable

    r = subprocess.run(
        ["bash", str(proj / "scripts" / "suite-canonical.sh"), "--project", str(proj)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    assert "cannot name the runner" in r.stderr
    artifacts = list((proj / ".agentic" / "context").glob("SUITE-*.json"))
    assert len(artifacts) == 1
    data = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert data["runner"] == "unknown"
    assert data["runner_source"] == "cli-unavailable"
    assert data["counts"] is None
    assert "runner unavailable" in data["counts_hint"]


def test_cli_runner_flag_accepts_a_command_starting_with_a_dash(awf_bin, awf_env):
    """AUD-28-12: ``--runner=--version`` is a value, not a new option.

    The script passes the canonical command attached (``--runner="$cmd"``),
    so a command whose first word is a long option no longer trips argparse
    (rc=2) and fakes ``runner_source: cli-unavailable``.
    """
    r = subprocess.run(
        [awf_bin, "suite-command", "--runner=--version"],
        capture_output=True,
        text=True,
        env=dict(awf_env),
        check=False,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "--version"


def test_script_passes_the_runner_value_attached():
    """AUD-28-12: the script uses ``--runner="$cmd"`` for the same reason."""
    script = (Path(__file__).parents[2] / "scripts" / "suite-canonical.sh").read_text(
        encoding="utf-8"
    )
    assert '--runner="$cmd"' in script


def test_cli_runner_flag_does_not_need_a_readable_project(tmp_path, awf_bin, awf_env):
    """The script asks for a runner in ANY project — a broken config included.

    ``suite-canonical.sh`` runs in the worker's project and calls
    ``awf suite-command --runner="$cmd"`` for the artifact. A project whose
    ``verification.test_cmd`` is broken makes the canonical-command question
    exit 5, but the runner question must still answer: the script uses it
    for ``runner``/``counts``, not for the command. The CLI returns before
    resolving the project, so rc stays 0.
    """
    project = tmp_path / "project"
    (project / ".agentic").mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text(
        "verification: [broken\n", encoding="utf-8"
    )

    r = subprocess.run(
        [
            awf_bin,
            "suite-command",
            "--project-dir",
            str(project),
            "--runner",
            "timeout 5 pytest tests/",
        ],
        capture_output=True,
        text=True,
        env=dict(awf_env),
        check=False,
    )

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "pytest"
