"""CARRY IMP2 (R13/K13): the raw full-suite gate.

T4 — a full suite run without a matching canonical artifact leaves a
``SUITE-WARN:`` and the ``suite_gate=raw-full-suite`` handoff fact; the
stage and the commit are not blocked. A matching artifact (same tree sha,
same command) silences it; somebody else's command or tree does not.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import _git_init

from awf import git_utils, suite_command, suite_guard
from awf.agent_stage import collect_handoff

PROJECT_KEY = "verification:\n  test_cmd: npm test\n"


def _repo(tmp_path: Path, config: str | None = PROJECT_KEY) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    # AUD12-08: the git boilerplate has one home — conftest._git_init.
    _git_init(proj)
    (proj / ".agentic").mkdir()
    if config is not None:
        (proj / ".agentic" / "config.yaml").write_text(config, encoding="utf-8")
    exclude = proj / ".git" / "info" / "exclude"
    exclude.write_text(".agentic/context/\n", encoding="utf-8")
    return proj


def _artifact(proj: Path, command: str, sha: str | None = None) -> Path:
    sha = sha or git_utils.tree_fingerprint(proj)
    context = proj / ".agentic" / "context"
    context.mkdir(parents=True, exist_ok=True)
    path = context / f"SUITE-{sha}-abcd1234.json"
    path.write_text(json.dumps({"sha": sha, "command": command}), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "text",
    [
        "pytest tests/ -q",
        "python3 -m pytest tests --maxfail=1",
        "cd proj && pytest -q tests/",
        "bash scripts/run-all.sh",
        "./scripts/run-all.sh",
        # QA-0277 F1: the worker log is opencode's --print-logs echo.
        '\x1b[0m$ \x1b[0mcd "/p" && /venv/bin/python -m pytest tests/ -q -n auto',
        "$ cd /p && python3 -m pytest tests/ -q 2>&1 | tail -5",
        "grep -c x awf/_proc.py && python3 -m pytest tests/ -q",
        "timeout 600 python3 -m pytest tests/",
        "bash -c 'pytest tests/'",
        'bash -c "cd /p && pytest tests/"',
        "pytest tests -m slow",
    ],
)
def test_full_suite_lines_are_detected(text):
    assert suite_guard.detect_full_suite(text)


@pytest.mark.parametrize(
    "text",
    [
        "pytest tests/unit/test_x.py -q",
        "pytest tests -k smoke",
        "pytest tests/ --deselect x -k 'not slow'",
        "npm test",
        "Run `bash scripts/run-all.sh` before the final commit",
        "AGENTS.md: ворота — bash scripts/run-all.sh",
        "",
        # QA-0277 F2: reading the script is not running it.
        "cat scripts/run-all.sh",
        "$ sed -n '1,80p' scripts/run-all.sh",
        # QA-0277 F3: a quoted payload and metadata are not command echoes.
        'pgrep -af "suite-canonical|pytest tests/" | grep -v opencode',
        'for cmd in "python -m pytest tests/ -q"; do echo "$cmd"; done',
        'timestamp=2026-10-09T16:51:54.800Z level=INFO message=evaluated '
        'permission=bash pattern="cat scripts/run-all.sh"',
        "pytest -m tests",
        "bash -n scripts/run-all.sh",
    ],
)
def test_narrow_runs_are_not_full_suite(text):
    assert not suite_guard.detect_full_suite(text)


def test_raw_full_suite_warns_without_artifact(tmp_path):
    proj = _repo(tmp_path)
    warn = suite_guard.check(proj, "pytest tests/ -q\n")
    assert warn.startswith("SUITE-WARN:")


REAL_LOG = (
    "timestamp=2026-10-27T04:00:49.890Z level=INFO run=x message=loop\n"
    '\x1b[0m$ \x1b[0mcd "/home/u/proj" && /venv/bin/python -m pytest '
    "tests/ -q -n auto --timeout=300 -p no:cacheprovider 2>&1 | tail -4\n"
)


def test_real_print_logs_line_warns_and_artifact_silences(tmp_path):
    """QA-0277 F1/AC33: the actual log format must move the gate."""
    proj = _repo(tmp_path)
    assert suite_guard.check(proj, REAL_LOG).startswith("SUITE-WARN:")
    _artifact(proj, "npm test")
    assert suite_guard.check(proj, REAL_LOG) == ""


def test_reading_the_script_is_not_a_full_run(tmp_path):
    """QA-0277 F2: `cat`/`sed` of run-all.sh must not warn."""
    proj = _repo(tmp_path)
    assert suite_guard.check(proj, "$ cat scripts/run-all.sh\n") == ""
    assert suite_guard.check(
        proj, "$ sed -n '1,80p' scripts/run-all.sh\n",
    ) == ""


def test_matching_artifact_silences(tmp_path):
    proj = _repo(tmp_path)
    _artifact(proj, "npm test")
    assert suite_guard.check(proj, "pytest tests/ -q\n") == ""


def test_foreign_command_does_not_silence(tmp_path):
    proj = _repo(tmp_path)
    # The shell's AWF_SUITE_CMD override is not the engine's canonical string.
    _artifact(proj, "pytest tests -k smoke")
    assert suite_guard.check(proj, "pytest tests/ -q\n").startswith("SUITE-WARN:")


def test_foreign_tree_does_not_silence(tmp_path):
    proj = _repo(tmp_path)
    _artifact(proj, "npm test", sha="0" * 40)
    assert suite_guard.check(proj, "pytest tests/ -q\n").startswith("SUITE-WARN:")


def test_legacy_fallback_artifact_silences(tmp_path):
    proj = _repo(tmp_path, config=None)
    canonical = suite_command.resolve(proj)
    assert canonical is not None and canonical.source == "legacy"
    _artifact(proj, canonical.command)
    assert suite_guard.check(proj, "pytest tests/ -q\n") == ""


def test_narrow_override_does_not_silence_legacy(tmp_path, monkeypatch):
    proj = _repo(tmp_path, config=None)
    monkeypatch.setenv("AWF_SUITE_CMD", "pytest tests -k smoke")
    _artifact(proj, "pytest tests -k smoke")
    assert suite_guard.check(proj, "pytest tests/ -q\n").startswith("SUITE-WARN:")


def test_no_full_run_no_check(tmp_path):
    proj = _repo(tmp_path)
    assert suite_guard.check(proj, "pytest tests/unit/test_x.py -q\n") == ""
    assert suite_guard.check(proj, "") == ""


def test_handoff_carries_engine_fact(tmp_path):
    proj = _repo(tmp_path)
    logs = proj / ".agentic" / "logs"
    logs.mkdir()

    out = collect_handoff(
        "agent-implementer", "TODO-0001", proj, logs,
        engine_facts=[suite_guard.RAW_SUITE_FACT],
    )
    body = out.read_text(encoding="utf-8")
    assert "## Engine facts" in body
    assert "suite_gate=raw-full-suite" in body


def test_handoff_without_engine_facts_is_unchanged(tmp_path):
    proj = _repo(tmp_path)
    logs = proj / ".agentic" / "logs"
    logs.mkdir()

    out = collect_handoff("agent-implementer", "TODO-0001", proj, logs)
    assert "## Engine facts" not in out.read_text(encoding="utf-8")


def test_stage_wires_the_warning_into_the_handoff(tmp_path, monkeypatch):
    """K13: after the worker exits the gate reads its log from
    ``log_start_offset`` and hands the fact to collect_handoff; the stage
    itself still succeeds and the worker's DONE.json is never rewritten.

    Two attempts share one fake worker: the first runs the raw full suite
    in its own log, the second only *inherits* that line from the previous
    attempt (it sits before the offset) and runs a narrow target.
    """
    from awf import agent_stage
    from awf.pipeline import Stage

    proj = _repo(tmp_path)
    (proj / ".agentic" / "inbox").mkdir()
    (proj / ".agentic" / "outbox").mkdir()
    (proj / ".agentic" / "logs").mkdir()
    roles = proj / ".agentic" / "roles"
    roles.mkdir()
    (roles / "developer.md").write_text("# Developer role\n", encoding="utf-8")
    done_json = proj / ".agentic" / "outbox" / "DONE-TODO-0001.json"
    done_json.write_text('{"notes": "worker fact"}\n', encoding="utf-8")
    done_before = done_json.read_bytes()

    # The fake worker's log and the offset where THIS attempt starts.
    state = {"log": "", "offset": "0"}
    captured: dict = {}
    warn_log: list[str] = []

    def fake_run(cmd, cwd, watch_paths=None, watch_new_glob=None,
                 logs_dir=None, hard_timeout=None, env=None, log_holder=None,
                 **kwargs):
        log = Path(logs_dir) / "worker.out"
        log.write_text(state["log"], encoding="utf-8")
        log_holder["log_path"] = str(log)
        log_holder["log_start_offset"] = state["offset"]
        outbox = Path(cwd) / ".agentic" / "outbox"
        (outbox / "DONE-TODO-0001.md").write_text("# Done\n", encoding="utf-8")
        (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")

        class _R:
            returncode = 0

        return _R()

    def run_once():
        captured.clear()
        warn_log.clear()
        agent_stage.run_agent_stage(
            Stage(name="agent-dev", role="developer", kind="execute"),
            "TODO-0001", proj, {}, proj / ".agentic" / "logs",
        )

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True,
    )
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "_log", lambda logs_dir, msg: warn_log.append(str(msg)))
    monkeypatch.setattr(
        agent_stage, "collect_handoff",
        lambda *a, **kw: captured.update(kw),
    )

    # Attempt 1: the raw full suite is this attempt's own command.
    # QA-0277 F1: the real --print-logs command echo, ANSI included.
    state["log"] = '\x1b[0m$ \x1b[0mcd "/p" && python3 -m pytest tests/ -q\n'
    run_once()
    assert captured.get("engine_facts") == [suite_guard.RAW_SUITE_FACT]
    assert any(m.startswith("SUITE-WARN:") for m in warn_log)

    # Attempt 2: the same line is history (before the offset); the current
    # command is narrow — no warning, no fact.
    prior = '\x1b[0m$ \x1b[0mcd "/p" && python3 -m pytest tests/ -q\n'
    state["log"] = prior + '$ pytest tests/unit/test_x.py -q\n'
    state["offset"] = str(len(prior.encode("utf-8")))
    run_once()
    assert captured.get("engine_facts") == []
    assert not any(m.startswith("SUITE-WARN:") for m in warn_log), warn_log

    assert done_json.read_bytes() == done_before, "the gate rewrote DONE.json"


# --- AUD1–AUD3 (owner review 0277) -----------------------------------------


def _pyproject(proj: Path, body: str) -> None:
    (proj / "pyproject.toml").write_text(body, encoding="utf-8")


def test_bare_pytest_is_full_only_with_testpaths(tmp_path):
    """AUD1: no positional target is full when pytest config names testpaths."""
    assert not suite_guard.detect_full_suite("pytest -q")
    assert suite_guard.detect_full_suite("pytest -q", testpaths=True)

    proj = _repo(tmp_path)
    assert suite_guard.check(proj, "$ pytest -q\n") == ""
    _pyproject(proj, '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    assert suite_guard.check(proj, "$ pytest -q\n").startswith("SUITE-WARN:")


def test_selectors_do_not_cancel_testpaths_fullness(tmp_path):
    """AUD1 decision: -k/-m narrow the run, not pytest's default targets."""
    proj = _repo(tmp_path)
    _pyproject(proj, '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    assert suite_guard.check(proj, "$ pytest -q -k smoke\n").startswith(
        "SUITE-WARN:"
    )
    assert suite_guard.check(proj, "$ pytest -m slow\n").startswith("SUITE-WARN:")


def test_empty_or_missing_testpaths_keeps_bare_pytest_narrow(tmp_path):
    assert not suite_command.testpaths_configured(tmp_path)
    _pyproject(tmp_path, '[tool.pytest.ini_options]\ntestpaths = []\n')
    assert not suite_command.testpaths_configured(tmp_path)
    (tmp_path / "pytest.ini").write_text("[pytest]\ntestpaths = tests unit\n")
    assert suite_command.testpaths_configured(tmp_path)


def test_explicit_file_target_stays_narrow_with_testpaths():
    assert not suite_guard.detect_full_suite(
        "pytest tests/unit/test_x.py -q", testpaths=True
    )
    assert not suite_guard.detect_full_suite("pytest tests -k smoke", testpaths=True)


@pytest.mark.parametrize(
    "text",
    [
        "python3 -m compileall tests/",
        "python3 -m ruff check tests/",
        "python3 -m py_compile tests/test_x.py",
        "python3 tests/run.py",
    ],
)
def test_python_module_must_be_pytest(text):
    """AUD2: only ``-m pytest``/``-m py.test`` runs the suite."""
    assert not suite_guard.detect_full_suite(text)


def test_python_module_pytest_still_counts():
    assert suite_guard.detect_full_suite("python3 -m pytest tests/ -q")
    assert suite_guard.detect_full_suite("python3 -mpytest tests/ -q")


def test_markdown_quote_is_not_a_command(tmp_path):
    """AUD3: ``> pytest tests/`` is quoted text, not a redirect-plus-run."""
    proj = _repo(tmp_path)
    assert suite_guard.check(proj, "> pytest tests/\n") == ""


def test_heredoc_body_is_not_a_command(tmp_path):
    """AUD3: a heredoc body is data even when it looks like a command."""
    proj = _repo(tmp_path)
    text = "$ cat <<'EOF'\npytest tests/\nEOF\n"
    assert suite_guard.check(proj, text) == ""
    after = text + "pytest tests/ -q\n"
    assert suite_guard.check(proj, after).startswith("SUITE-WARN:")


# --- architect-review (0277): placement of the AUD2/AUD3 rules ---------------


def test_python_script_keeps_its_own_dash_m():
    """AUD2 boundary: ``-m`` after the script path belongs to the script."""
    assert not suite_guard.detect_full_suite("python3 tests/run.py -m pytest tests/")
    assert suite_guard.detect_full_suite("python3 -X importtime -m pytest tests/ -q")
    assert suite_guard.detect_full_suite("python3 -W ignore -m pytest tests/ -q")


def test_quoted_double_arrow_does_not_swallow_the_next_commands():
    """AUD3 boundary: ``<<`` inside quotes is text, not a heredoc opener."""
    text = 'git commit -m "docs <<EOF"\npytest tests/ -q\n'
    assert suite_guard.detect_full_suite(text)


# --- qa-review (0277): printed output must not open a heredoc ----------------


def test_printed_heredoc_listing_does_not_swallow_commands():
    """F4: ``<<'PY'`` in a printed script listing (no prompt) is text.

    Real log evidence: a `sed`/`cat` dump of ``scripts/suite-canonical.sh``
    contains ``cached_exit=$(... <<'PY'`` with a line-number prefix. Without
    the prompt rule that listing opened a heredoc and the real command echoes
    after it were eaten as body (never checked)."""
    listing = (
        "   140\t  cached_exit=$(\"$pybin\" - \"$artifact\" \"$cmd\" <<'PY'\n"
    )
    text = listing + '$ cd /p && python3 -m pytest tests/ -q\n'
    assert suite_guard.detect_full_suite(text)


def test_prompted_heredoc_closes_and_later_commands_are_seen(tmp_path):
    """F4: a real prompted heredoc still hides its body, then resumes."""
    proj = _repo(tmp_path)
    text = (
        "\x1b[0m$ \x1b[0mpython - <<'PY'\n"
        "print('pytest tests/')\n"
        "PY\n"
        "\x1b[0m$ \x1b[0mpytest tests/ -q\n"
    )
    assert suite_guard.check(proj, text).startswith("SUITE-WARN:")


# --- audit 0277 виток 2: AUD10 (substitution), AUD11 (redirects) -------------


@pytest.mark.parametrize(
    "text",
    [
        "pytest -q 2>&1",
        "pytest -q > out.txt",
        "pytest -q 2> err.log",
        "pytest -q >> out.txt",
        "pytest -q < /dev/null",
        "pytest --timeout 600",
    ],
)
def test_redirect_or_option_value_is_not_a_target(text):
    """AUD11: a redirect/value is not a target — bare pytest stays full."""
    assert suite_guard.detect_full_suite(text, testpaths=True)


@pytest.mark.parametrize(
    "text",
    [
        "pytest -q 2>&1",
        "pytest -q > out.txt",
        "pytest --timeout 600",
    ],
)
def test_redirect_or_option_value_stays_narrow_without_testpaths(text):
    assert not suite_guard.detect_full_suite(text)


def test_explicit_target_survives_a_redirect():
    assert suite_guard.detect_full_suite("pytest tests/ -q 2>&1")
    assert suite_guard.detect_full_suite("pytest tests/ > out.txt")


def test_command_substitution_is_an_unknown_set():
    """AUD10: the ``$(...)`` set is unknown — never claimed "full"."""
    assert not suite_guard.detect_full_suite(
        "pytest $(find tests -name '*.py')", testpaths=True
    )
    assert not suite_guard.detect_full_suite("pytest `find tests -name '*.py'`")


def test_bare_pytest_with_testpaths_warns_through_check(tmp_path):
    """AUD11 end-to-end: redirects do not hide the testpaths rule."""
    proj = _repo(tmp_path)
    _pyproject(proj, '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    assert suite_guard.check(proj, "$ pytest -q 2>&1\n").startswith("SUITE-WARN:")
    assert suite_guard.check(proj, "$ pytest -q > out.txt\n").startswith("SUITE-WARN:")
    assert suite_guard.check(proj, "$ pytest --timeout 600\n").startswith("SUITE-WARN:")
    assert suite_guard.check(proj, "$ pytest $(find tests -name '*.py')\n") == ""


# --- architect-review (0277): redirects are split off, not swallowed ---------


@pytest.mark.parametrize(
    "text",
    [
        "pytest tests>out.txt",
        "pytest tests/ -q>out.txt",
        "pytest tests 2>out.txt",
        "pytest tests>>out.txt",
        "2>/dev/null pytest tests",
        "&> /dev/null pytest tests",
    ],
)
def test_glued_or_leading_redirect_keeps_the_target(text):
    """A redirect attached to a target word must not swallow the word."""
    assert suite_guard.detect_full_suite(text)


@pytest.mark.parametrize(
    "text", ["pytest -q >out.txt", "pytest -q 2>err.log", "pytest &>/dev/null"]
)
def test_glued_redirect_without_a_target_stays_narrow(text):
    assert not suite_guard.detect_full_suite(text)


def test_inline_script_ends_the_interpreter_options():
    """``-c`` consumes the script: a later ``-m pytest`` is the script's arg."""
    assert not suite_guard.detect_full_suite(
        "python3 -c 'print(1)' -m pytest tests"
    )


# --- qa-review (0277): a substitution outside the target set ----------------

@pytest.mark.parametrize(
    "text",
    [
        "pytest tests/ -q -n $(nproc)",
        "pytest tests/ -q -p $(echo no:cacheprovider)",
        "pytest tests/ --maxfail=$(cat n)",
        "pytest tests/ > $(mktemp)",
        "python3 -m pytest tests/ -q -n $(nproc)",
    ],
)
def test_substitution_outside_the_target_keeps_the_target(text):
    """F5: a substitution in an option value / redirect target is not the
    target set, so it must not erase the explicit ``tests/``."""
    assert suite_guard.detect_full_suite(text)


def test_substitution_in_a_value_warns_through_check(tmp_path):
    """F5 end-to-end: ``-n $(nproc)`` does not hide the raw full run."""
    proj = _repo(tmp_path)
    assert suite_guard.check(
        proj, "$ pytest tests/ -q -n $(nproc)\n"
    ).startswith("SUITE-WARN:")


def test_substitution_in_the_target_set_stays_unknown():
    """AUD10 kept: the substitution target itself is an unknown set."""
    assert not suite_guard.detect_full_suite("pytest $(find tests -name '*.py')")


# --- audit 0277 виток 3: CTX1 (unknown option value is not a target) --------


@pytest.mark.parametrize(
    "text",
    [
        "pytest --cov awf",
        "pytest --junitxml report.xml",
        "pytest --durations 10",
        "pytest -q --cov awf --junitxml report.xml",
    ],
)
def test_unknown_option_value_is_not_a_target(text):
    """CTX1: a spaced option value is data — the ``testpaths`` rule decides."""
    assert suite_guard.detect_full_suite(text, testpaths=True)
    assert not suite_guard.detect_full_suite(text)


def test_unknown_option_value_with_explicit_target_is_full():
    """CTX1: ``--cov awf tests/`` still names the whole suite."""
    assert suite_guard.detect_full_suite("pytest --cov awf tests/", testpaths=True)
    assert suite_guard.detect_full_suite("pytest --cov awf tests/")


def test_unknown_option_value_does_not_hide_a_file_target():
    """CTX1: a model target stays a target, an option value does not."""
    assert not suite_guard.detect_full_suite(
        "pytest --cov awf tests/unit/test_x.py", testpaths=True
    )


def test_unknown_option_value_end_to_end(tmp_path):
    """CTX1: the gate sees a bare run with unknown spaced options."""
    proj = _repo(tmp_path)
    _pyproject(proj, '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    assert suite_guard.check(proj, "$ pytest --cov awf\n").startswith("SUITE-WARN:")
    assert suite_guard.check(
        proj, "$ pytest --cov awf tests/\n"
    ).startswith("SUITE-WARN:")


# --- qa-review (0277), виток 4: wrapper option values before the runner ------
#
# ``timeout -k 5 300 pytest tests/`` and ``env -u FOO pytest tests/`` execute
# the whole suite. The prefix stripper consumed a wrapper option but not the
# value that follows it, so the value became the runner name and the raw run
# stayed invisible — exactly the miss R13 puts above a false alarm.


@pytest.mark.parametrize(
    "text",
    [
        "timeout -k 5 300 pytest tests/",
        "timeout --kill-after 5 300 pytest tests/",
        "timeout -s KILL 300 python3 -m pytest tests/ -q",
        "timeout -k 5 --preserve-status 300 pytest tests -q",
        "env -u FOO pytest tests/",
        "env --unset FOO pytest tests/",
        "env -C /tmp pytest tests/",
        "env --chdir /tmp pytest tests/",
        "exec -a suite pytest tests/",
        "bash -c 'timeout -k 5 300 pytest tests/'",
    ],
)
def test_wrapper_option_value_does_not_hide_the_runner(text):
    """The value of a wrapper option is data, not the runner (R13)."""
    assert suite_guard.detect_full_suite(text)


@pytest.mark.parametrize(
    "text",
    [
        "timeout -k 5 300 pytest tests/unit/test_x.py",
        "env -u FOO pytest tests -k smoke",
        "timeout 300 npm test",
    ],
)
def test_wrapper_option_value_keeps_a_narrow_run_narrow(text):
    """Unwrapping must not turn a narrow run into a full one."""
    assert not suite_guard.detect_full_suite(text)


def test_wrapped_raw_run_warns_end_to_end(tmp_path):
    """The end-to-end path sees the wrapper too, not only the classifier."""
    proj = _repo(tmp_path)
    assert suite_guard.check(
        proj, "$ timeout -k 5 300 pytest tests/\n"
    ).startswith("SUITE-WARN:")
    assert suite_guard.check(
        proj, "$ env -u FOO python3 -m pytest tests/ -q\n"
    ).startswith("SUITE-WARN:")


# --- qa-review (0277), виток 4: a combined shell cluster with ``c`` ----------
#
# ``bash -lc 'pytest tests/'`` and ``sh -ec '...'`` run their payload exactly
# like ``bash -c``. The old check only recognised the standalone ``-c`` word,
# so a login shell wrapper hid the raw run (R13: the miss is the costly side).


@pytest.mark.parametrize(
    "text",
    [
        "bash -lc 'pytest tests/'",
        "bash -ec 'cd /p && pytest tests/ -q'",
        "sh -xc 'pytest tests/'",
        "dash -lc 'python3 -m pytest tests/'",
        "zsh -ic 'pytest tests/'",
    ],
)
def test_combined_shell_cluster_with_c_runs_the_payload(text):
    assert suite_guard.detect_full_suite(text)


@pytest.mark.parametrize(
    "text",
    [
        # ``-l``/``-e`` still run the script they are given.
        "bash -l scripts/run-all.sh",
        "bash -e scripts/run-all.sh",
    ],
)
def test_combined_shell_cluster_still_runs_a_script_path(text):
    assert suite_guard.detect_full_suite(text)


@pytest.mark.parametrize(
    "text",
    [
        "bash -n scripts/run-all.sh",
        "bash -lc 'pytest tests/unit/test_x.py'",
        "bash -lc 'npm test'",
    ],
)
def test_combined_shell_cluster_without_a_full_payload_stays_narrow(text):
    """``-n`` runs nothing; a narrow payload is not the whole suite."""
    assert not suite_guard.detect_full_suite(text)


# --- audit 0277 виток 4: CTX3 (shell spellings of the suite target) ---------
#
# ``pytest .``, ``./tests``, ``./tests/``, ``tests/.`` and ``$PWD/tests`` all
# collect the same tree as ``pytest tests/``. The old exact-string check saw
# only ``tests``/``tests/``, so a shell-spelled raw run stayed invisible.


@pytest.mark.parametrize(
    "text",
    [
        "pytest .",
        "pytest ./tests",
        "pytest ./tests/",
        "pytest tests/.",
        "pytest $PWD/tests",
        "pytest ${PWD}/tests",
        "pytest -q ./tests/ -n auto",
        "cd /p && pytest ./tests -q",
    ],
)
def test_shell_spelled_suite_target_is_full(text):
    """CTX3: the same directory under shell sugar is the whole suite."""
    assert suite_guard.detect_full_suite(text)
    assert suite_guard.detect_full_suite(text, testpaths=True)


@pytest.mark.parametrize(
    "text",
    [
        "pytest ./tests/unit/test_x.py",
        "pytest ./tests/unit",
        "pytest . -k smoke",
        "pytest ./tests -k smoke",
    ],
)
def test_shell_spelled_narrow_target_stays_narrow(text):
    """CTX3: normalization must not widen a narrow target."""
    assert not suite_guard.detect_full_suite(text, testpaths=True)


def test_shell_spelled_target_warns_end_to_end(tmp_path):
    """CTX3: the gate's own path, not only the classifier."""
    proj = _repo(tmp_path)
    assert suite_guard.check(proj, "$ pytest ./tests/\n").startswith("SUITE-WARN:")


# --- audit 0277 виток 4: CTX4 (reserved words before the runner) ------------
#
# ``if [ -f x ]; then pytest tests/`` splits into a "then pytest tests/"
# segment. The reserved word was read as the runner name, so the raw run
# stayed invisible. ``sudo`` is the same shape as ``env``/``timeout``.


@pytest.mark.parametrize(
    "text",
    [
        "if [ -f pyproject.toml ]; then pytest tests/ -q; fi",
        'for f in "a"; do pytest tests/ -q; done',
        "if true; then python3 -m pytest tests/; else echo skip; fi",
        "sudo pytest tests/",
        "sudo -u root pytest tests/ -q",
        "sudo --user root python3 -m pytest tests/",
    ],
)
def test_reserved_word_and_sudo_before_the_runner(text):
    """CTX4: a reserved word or ``sudo`` does not hide the runner."""
    assert suite_guard.detect_full_suite(text)


@pytest.mark.parametrize(
    "text",
    [
        "if [ -f pyproject.toml ]; then pytest tests/unit/test_x.py; fi",
        "for f in a; do pytest tests -k smoke; done",
        "sudo -u root npm test",
    ],
)
def test_reserved_word_keeps_a_narrow_run_narrow(text):
    """CTX4: unwrapping must not turn a narrow run into a full one."""
    assert not suite_guard.detect_full_suite(text)


# --- audit 0277 виток 4: CTX2 (the gate must not stop the stage) ------------


def test_unreadable_untracked_file_does_not_break_the_fingerprint(tmp_path):
    """CTX2: mode 000 on an untracked file is a fact, not a crash."""
    proj = _repo(tmp_path)
    bomb = proj / "unreadable.txt"
    bomb.write_text("secret\n", encoding="utf-8")
    bomb.chmod(0)
    try:
        first = git_utils.tree_fingerprint(proj)
        second = git_utils.tree_fingerprint(proj)
    finally:
        bomb.chmod(0o644)
    assert first == second, "unreadable untracked file made the fingerprint unstable"


def test_unreadable_untracked_file_does_not_stop_the_gate(tmp_path):
    """CTX2: ``check`` still answers instead of raising."""
    proj = _repo(tmp_path)
    bomb = proj / "unreadable.txt"
    bomb.write_text("secret\n", encoding="utf-8")
    bomb.chmod(0)
    try:
        assert suite_guard.check(proj, "$ pytest tests/\n").startswith("SUITE-WARN:")
    finally:
        bomb.chmod(0o644)


def test_a_crashing_gate_leaves_the_stage_alive(tmp_path, monkeypatch):
    """CTX2: a crash inside ``suite_guard.check`` is logged, not raised.

    The stage is an observer of the worker log; its own failure must not
    turn a finished unit into a pipeline failure (same rule as verify-pack).
    """
    from awf import agent_stage
    from awf.pipeline import Stage

    proj = _repo(tmp_path)
    for name in ("inbox", "outbox", "logs", "roles"):
        (proj / ".agentic" / name).mkdir()
    (proj / ".agentic" / "roles" / "developer.md").write_text(
        "# Developer role\n", encoding="utf-8"
    )

    captured: dict = {}
    log_lines: list[str] = []

    def fake_run(cmd, cwd, watch_paths=None, watch_new_glob=None,
                 logs_dir=None, hard_timeout=None, env=None, log_holder=None,
                 **kwargs):
        log = Path(logs_dir) / "worker.out"
        log.write_text("$ pytest tests/\n", encoding="utf-8")
        log_holder["log_path"] = str(log)
        log_holder["log_start_offset"] = "0"
        outbox = Path(cwd) / ".agentic" / "outbox"
        (outbox / "DONE-TODO-0001.md").write_text("# Done\n", encoding="utf-8")
        (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")

        class _R:
            returncode = 0

        return _R()

    def boom(*args, **kwargs):
        raise PermissionError("unreadable untracked file")

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True,
    )
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "_log", lambda d, m: log_lines.append(str(m)))
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: captured.update(kw))
    monkeypatch.setattr(agent_stage.suite_guard, "check", boom)

    agent_stage.run_agent_stage(
        Stage(name="agent-dev", role="developer", kind="execute"),
        "TODO-0001", proj, {}, proj / ".agentic" / "logs",
    )

    assert captured.get("engine_facts") == []
    assert any("suite gate crashed" in line for line in log_lines), log_lines
