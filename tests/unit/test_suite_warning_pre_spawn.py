"""OPS IMP2 (R2, AC5–AC7, AC21, INV4): the "big unit" warning before the spawn.

T4 — ``declares_full_suite`` goes through the WHOLE ``detect_full_suite``
function (not a list of spellings); T5 — an explicit caller timeout
suppresses the line at any stage/project keys. The warning never changes
rc and never writes BLOCKED; a broken contract builds none — that error
stays with the verify pack.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import pipeline_engine, suite_guard
from awf.pipeline import Stage


def _with_testpaths(tmp_path: Path) -> Path:
    proj = tmp_path / "with-testpaths"
    proj.mkdir()
    (proj / "pytest.ini").write_text(
        "[pytest]\ntestpaths = tests\n", encoding="utf-8"
    )
    return proj


FULL_COMMANDS = [
    "pytest tests/ -q",
    "pytest .",
    "pytest ./tests",
    "pytest $PWD/tests",
    "bash scripts/run-all.sh",
    "uv run pytest tests/",
    "poetry run pytest tests/",
    "bash -c 'pytest tests/'",
    "pytest -q",
    "pytest -k foo",
]

NARROW_COMMANDS = [
    "pytest tests/unit/test_x.py",
    "pytest tests/ -k foo",
    "ruff check .",
]


@pytest.mark.parametrize("command", FULL_COMMANDS)
def test_a_full_run_is_declared(command: str, tmp_path: Path) -> None:
    """AC5/AC6: the full-suite spellings (testpaths declared)."""
    proj = _with_testpaths(tmp_path)
    assert suite_guard.declares_full_suite([command], proj) == command


@pytest.mark.parametrize("command", NARROW_COMMANDS)
def test_a_narrow_run_is_not_declared(command: str, tmp_path: Path) -> None:
    """AC6: an explicit file, a ``-k`` on a target, a non-pytest tool."""
    proj = _with_testpaths(tmp_path)
    assert suite_guard.declares_full_suite([command], proj) is None


def test_bare_pytest_needs_testpaths(tmp_path: Path) -> None:
    """AC6: without ``testpaths`` a bare pytest is not "full" for this gate."""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert suite_guard.declares_full_suite(["pytest -q"], plain) is None
    assert (
        suite_guard.declares_full_suite(["pytest -q"], _with_testpaths(tmp_path))
        == "pytest -q"
    )


def test_the_first_full_command_wins(tmp_path: Path) -> None:
    proj = _with_testpaths(tmp_path)
    assert (
        suite_guard.declares_full_suite(
            ["ruff check .", "pytest tests/ -q", "pytest ."], proj
        )
        == "pytest tests/ -q"
    )


def test_missing_or_odd_verify_is_none(tmp_path: Path) -> None:
    proj = _with_testpaths(tmp_path)
    assert suite_guard.declares_full_suite(None, proj) is None
    assert suite_guard.declares_full_suite("pytest tests/", proj) is None
    assert suite_guard.declares_full_suite([], proj) is None


# ── integration: the line lands before the spawn ─────────────────────────


def _todo(contract_lines: list[str]) -> str:
    return "\n".join(["# unit", "---", *contract_lines, "---", ""])


class _Harness:
    """``execute_agent_stage`` with a scripted worker, like IMP1's tests."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.project = tmp_path / "proj"
        for d in ("inbox", "outbox", "context", "logs"):
            (self.project / ".agentic" / d).mkdir(parents=True)
        self.logs: list[str] = []
        self.runs = 0
        self.captured: dict[str, object] = {}
        self._install(monkeypatch)

    def _install(self, monkeypatch) -> None:
        outbox = self.project / ".agentic" / "outbox"

        def mock_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
            self.runs += 1
            self.captured.update(kw)
            (outbox / f"DONE-{todo_id}.md").write_text("# Done\n")
            (outbox / f"DONE-{todo_id}.ready").write_text("")

        monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
        monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
        monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: "abc123")
        monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_log", lambda _dir, msg: self.logs.append(msg))
        monkeypatch.setattr(pipeline_engine, "_write_state", lambda *a, **kw: None)
        from awf import signals as sig_module

        monkeypatch.setattr(sig_module, "clean_stage_signals", lambda *a, **kw: None)
        from awf import verify

        monkeypatch.setattr(verify, "attempt_auto_done", lambda *a, **kw: False)
        monkeypatch.setattr(verify, "work_fingerprint", lambda *a, **kw: "")

    def write_todo(self, text: str) -> None:
        (self.project / ".agentic" / "inbox" / "TODO-0001.md").write_text(
            text, encoding="utf-8"
        )
        (self.project / ".agentic" / "inbox" / "TODO-0001.ready").write_text("")

    def run(
        self,
        tmp_path: Path,
        *,
        caller: int | None = None,
        stage_timeout: int | None = None,
        config: dict | None = None,
    ):
        stages = [
            Stage(
                name="worker", role="worker", kind="execute",
                timeout=stage_timeout,
            ),
            Stage(name="verify", role="supervisor", kind="verify"),
        ]
        return pipeline_engine.execute_agent_stage(
            stage=stages[0],
            current_todo="TODO-0001",
            project_dir=self.project,
            config=config or {},
            logs_dir=tmp_path,
            stages=stages,
            stage_idx=0,
            retry_counts=[0, 0],
            auto=False,
            agent_hard_timeout=caller,
            pipeline_name=None,
        )

    def warns(self) -> list[str]:
        return [line for line in self.logs if line.startswith("STAGE-WARN")]

    def blocked(self) -> bool:
        outbox = self.project / ".agentic" / "outbox"
        return any(outbox.glob("BLOCKED-*"))


def test_warning_lands_once_with_cap_and_stage_starts(
    tmp_path: Path, monkeypatch
) -> None:
    """AC5/AC7: one line, the command and 3600, rc and BLOCKED unchanged."""
    h = _Harness(tmp_path, monkeypatch)
    h.write_todo(_todo(['verify: ["pytest tests/ -q"]']))

    result = h.run(tmp_path)

    warns = h.warns()
    assert len(warns) == 1
    assert "full suite in verify: `pytest tests/ -q`" in warns[0]
    assert "cap 3600s (source: default)" in warns[0]
    assert "raise timeout or split the unit" in warns[0]
    assert h.runs == 1, "the stage must start"
    assert result[2] == 0
    assert not h.blocked()


def test_project_cap_is_named(tmp_path: Path, monkeypatch) -> None:
    """AC5: a project key changes the cap in the line, not the decision."""
    h = _Harness(tmp_path, monkeypatch)
    h.write_todo(_todo(['verify: ["pytest tests/ -q"]']))

    h.run(tmp_path, config={"automation": {"stage_timeout_seconds": 7200}})

    assert len(h.warns()) == 1
    assert "cap 7200s (source: project)" in h.warns()[0]
    assert h.captured["hard_timeout"] == 7200
    assert h.runs == 1


def test_stage_key_alone_does_not_suppress(tmp_path: Path, monkeypatch) -> None:
    """R2: stage and project keys never suppress — only the caller does."""
    h = _Harness(tmp_path, monkeypatch)
    h.write_todo(_todo(['verify: ["pytest tests/ -q"]']))

    h.run(tmp_path, stage_timeout=600)

    assert len(h.warns()) == 1
    assert "cap 600s (source: stage)" in h.warns()[0]
    assert h.runs == 1


def test_explicit_caller_suppresses_even_with_both_keys(
    tmp_path: Path, monkeypatch
) -> None:
    """AC21: an explicit caller timeout (7200) wins over stage and project."""
    h = _Harness(tmp_path, monkeypatch)
    h.write_todo(_todo(['verify: ["pytest tests/ -q"]']))

    h.run(
        tmp_path,
        caller=7200,
        stage_timeout=600,
        config={"automation": {"stage_timeout_seconds": 7200}},
    )

    assert h.warns() == []
    assert "stage timeout: 7200s (source: caller)" in h.logs
    assert h.captured["hard_timeout"] == 7200


@pytest.mark.parametrize(
    "contract",
    [
        _todo(['verify: ["pytest tests/unit/test_x.py"]']),
        _todo(['verify: ["pytest tests/ -k foo"]']),
        _todo(['verify: ["ruff check ."]']),
        _todo([]),
    ],
    ids=["file", "keyword", "tool", "no-verify"],
)
def test_no_full_run_no_warning(
    contract: str, tmp_path: Path, monkeypatch
) -> None:
    """AC7: no ``verify:`` or no full run → no line; the stage still runs."""
    h = _Harness(tmp_path, monkeypatch)
    h.write_todo(contract)

    h.run(tmp_path)

    assert h.warns() == []
    assert h.runs == 1


def test_a_broken_contract_builds_no_warning(
    tmp_path: Path, monkeypatch
) -> None:
    """AC7: the parse error stays with the verify pack; the stage starts."""
    h = _Harness(tmp_path, monkeypatch)
    h.write_todo(_todo(['verify: "pytest tests/"']))

    h.run(tmp_path)

    assert h.warns() == []
    assert h.runs == 1
