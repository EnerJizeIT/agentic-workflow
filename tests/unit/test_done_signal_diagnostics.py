"""A12: "work in the tree, no signal" — the stage-log diagnostics surface.

A silent exit with work in the tree logs every accepted signal prefix and the
outbox files actually written for the unit, without content and without
ending the stage on an unreadable file name.
"""
from __future__ import annotations

import time
from pathlib import Path

from awf import commit_plan, pipeline_engine, verify
from awf.pipeline import Stage


def _git_project(tmp_path: Path) -> tuple[Path, str]:
    from conftest import _git_init

    from awf.git_utils import current_sha

    project = tmp_path / "repo"
    project.mkdir()
    _git_init(project)
    for name in ("outbox", "context", "logs", "inbox"):
        (project / ".agentic" / name).mkdir(parents=True)
    return project, current_sha(project)


def _write_bytes_name(directory: Path, raw: bytes) -> None:
    """Create a file whose name is not valid UTF-8 (os.open, not Path)."""
    import os

    fd = os.open(directory / os.fsdecode(raw), os.O_WRONLY | os.O_CREAT, 0o644)
    os.close(fd)


def _silent_work_messages(tmp_path, monkeypatch, write_outbox, *, real_log=False):
    """Run one agent stage that leaves work in the tree but no signal.

    ``write_outbox`` plays the worker: it decides which outbox files land
    next to the work. Returns every stage-log message the engine emitted.
    """
    todo = "TODO-0001"
    project, sha = _git_project(tmp_path)
    outbox = project / ".agentic" / "outbox"
    messages: list[str] = []

    def mock_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
        (project_dir / "README.md").write_text("worker was here\n", encoding="utf-8")
        write_outbox(outbox)

    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
    monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "")
    monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **kw: None)
    if not real_log:
        # CTX1 (QA9): with ``real_log`` the stage must survive the actual
        # UTF-8 write — a fake logger cannot prove that.
        monkeypatch.setattr(
            pipeline_engine, "_log", lambda logs_dir, msg: messages.append(msg)
        )
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: sha)
    monkeypatch.setattr(
        pipeline_engine, "_maybe_commit",
        lambda *a, **kw: commit_plan.CommitOutcome(
            commit_plan.OUTCOME_SKIPPED, "policy 'next' does not commit"
        ),
    )
    monkeypatch.setattr(verify, "attempt_auto_done", lambda *a, **kw: False)
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)

    stages = [
        Stage(name="plan", role="supervisor", kind="plan"),
        Stage(name="worker", role="worker", kind="execute"),
        Stage(name="verify", role="supervisor", kind="verify"),
    ]
    pipeline_engine.execute_agent_stage(
        stage=stages[1], current_todo=todo, project_dir=project,
        config={}, logs_dir=tmp_path, stages=stages, stage_idx=1,
        retry_counts=[0, 0, 0], auto=False, agent_hard_timeout=None,
    )
    return messages


def test_silent_work_log_names_signal_and_outbox_files(tmp_path, monkeypatch):
    def write(outbox):
        (outbox / "DONE-TODO-0001.md").write_text("# done\n", encoding="utf-8")
        (outbox / "DONE-TODO-0001.json").write_text('{"notes": "x"}', encoding="utf-8")

    messages = _silent_work_messages(tmp_path, monkeypatch, write)

    diagnostic = [m for m in messages if "expected signal" in m]
    assert diagnostic, f"no diagnostic line among {messages!r}"
    line = diagnostic[0]
    assert "DONE-TODO-0001.ready" in line
    assert "DONE-TODO-0001.md" in line and "DONE-TODO-0001.json" in line
    assert "# done" not in line, "the diagnostic lists names, not contents"
    # A12/BD-31: the retry budget runs out and the same event reaches the
    # salvage line — one composition, so both surfaces name the signal.
    salvage = [m for m in messages if "salvage path" in m]
    assert salvage, f"no salvage line among {messages!r}"
    assert "DONE-TODO-0001.ready" in salvage[0]
    assert "DONE-TODO-0001.md" in salvage[0]


def test_silent_work_log_says_none_when_worker_left_nothing_in_outbox(
    tmp_path, monkeypatch
):
    """Work in the tree with an empty outbox still names the missing signal."""
    messages = _silent_work_messages(tmp_path, monkeypatch, lambda outbox: None)

    diagnostic = [m for m in messages if "expected signal" in m]
    assert diagnostic, f"no diagnostic line among {messages!r}"
    assert "DONE-TODO-0001.ready" in diagnostic[0]
    assert "outbox files for this unit: (none)" in diagnostic[0]


def test_silent_work_log_names_every_accepted_signal_prefix(tmp_path, monkeypatch):
    """CTX7: лог называет все четыре префикса, которые принимает движок."""
    def write(outbox):
        (outbox / "REVIEW-APPROVED-TODO-0001.md").write_text("# ok\n", encoding="utf-8")

    messages = _silent_work_messages(tmp_path, monkeypatch, write)

    diagnostic = [m for m in messages if "expected signal" in m][0]
    for prefix in ("DONE", "BLOCKED", "REVIEW-APPROVED", "REVIEW-REJECTED"):
        assert f"{prefix}-TODO-0001.ready" in diagnostic


def test_silent_work_log_marks_a_truncated_outbox_list(tmp_path, monkeypatch):
    """CTX10: больше десяти файлов — в логе видно, сколько не показано."""
    def write(outbox):
        for i in range(13):
            (outbox / f"PROGRESS-TODO-0001-{i:02d}.log").write_text(
                "x", encoding="utf-8"
            )

    messages = _silent_work_messages(tmp_path, monkeypatch, write)

    diagnostic = [m for m in messages if "expected signal" in m][0]
    assert "… +3" in diagnostic


# ─── outbox names: only this unit, only known report prefixes ──────────


def test_unit_file_names_names_only_and_capped(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    for i in range(15):
        (outbox / f"PROGRESS-TODO-0001-{i:02d}.log").write_text("x", encoding="utf-8")
    (outbox / "REVIEW-TODO-0002.md").write_text("foreign\n", encoding="utf-8")

    names = pipeline_engine._unit_file_names(outbox, "TODO-0001")

    assert len(names) == 15
    assert all("TODO-0001" in name for name in names)
    assert pipeline_engine._unit_file_names(tmp_path / "missing", "TODO-0001") == []
    rendered = pipeline_engine._format_unit_files(outbox, "TODO-0001")
    assert rendered.count(", ") == 9, "ten names on the line"
    assert rendered.endswith("… +5")


def test_unit_file_names_ignores_foreign_numbers(tmp_path):
    """A run-report timestamp embedding the number is not this unit's file."""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "RUN-REPORT-20261031-031255.md").write_text("x", encoding="utf-8")
    (outbox / "DONE-0001.md").write_text("# done\n", encoding="utf-8")
    (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")

    names = pipeline_engine._unit_file_names(outbox, "TODO-0001")

    assert names == ["DONE-0001.md", "DONE-TODO-0001.ready"]


def test_unit_file_names_ignores_sibling_ids(tmp_path):
    """QA1: полный id тоже матчится по границе, не подстрокой."""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    for name in ("DONE-TODO-00011.md", "TODO-00010.json", "SESSION-TODO-000100.md"):
        (outbox / name).write_text("x", encoding="utf-8")
    (outbox / "DONE-TODO-0001.md").write_text("# done\n", encoding="utf-8")

    assert pipeline_engine._unit_file_names(outbox, "TODO-0001") == [
        "DONE-TODO-0001.md"
    ]


def test_unit_file_names_ignores_the_legacy_id_in_a_foreign_prefix(tmp_path):
    """CTX5: `RUN-0001.md` — не файл юнита, короткий id матчится только
    для известных префиксов отчётов."""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "RUN-0001.md").write_text("x", encoding="utf-8")
    (outbox / "x-0001-y.txt").write_text("x", encoding="utf-8")
    (outbox / "BLOCKED-0001.md").write_text("x", encoding="utf-8")

    assert pipeline_engine._unit_file_names(outbox, "TODO-0001") == [
        "BLOCKED-0001.md"
    ]


# ─── unreadable names never end the stage (CTX1) ────────────────────────


def test_unit_file_names_sanitizes_non_utf8_name(tmp_path):
    """CTX1: байтовое имя не роняет диагностику — оно заменяется на U+FFFD."""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    _write_bytes_name(outbox, b"DONE-TODO-0001-\xff.md")

    names = pipeline_engine._unit_file_names(outbox, "TODO-0001")

    assert len(names) == 1
    assert "\ufffd" in names[0]
    names[0].encode("utf-8")  # would raise UnicodeEncodeError before the fix


def test_stage_log_accepts_the_sanitized_outbox_names(tmp_path):
    """CTX1: строка диагностики целиком пишется в реальный UTF-8 лог."""
    from awf import _log as stage_log

    outbox = tmp_path / "outbox"
    outbox.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    _write_bytes_name(outbox, b"DONE-TODO-0001-\xff.md")

    line = f"expected signal … outbox files: {pipeline_engine._format_unit_files(outbox, 'TODO-0001')}"
    stage_log.log(logs, line)

    assert "\ufffd" in (logs / "orchestrator.log").read_text(encoding="utf-8")


def test_silent_work_log_writes_the_non_utf8_outbox_name(tmp_path, monkeypatch):
    """CTX1 (стадийный путь): байтовое имя в outbox не обрывает стадию."""
    def write(outbox):
        (outbox / "DONE-TODO-0001.md").write_text("# done\n", encoding="utf-8")
        _write_bytes_name(outbox, b"DONE-TODO-0001-\xff.md")

    messages = _silent_work_messages(tmp_path, monkeypatch, write)

    diagnostic = [m for m in messages if "expected signal" in m][0]
    assert "\ufffd" in diagnostic
    diagnostic.encode("utf-8")


def test_stage_survives_the_non_utf8_name_with_the_real_logger(tmp_path, monkeypatch):
    """QA9: доказательство CTX1 на настоящем логгере, а не на заглушке."""
    def write(outbox):
        (outbox / "DONE-TODO-0001.md").write_text("# done\n", encoding="utf-8")
        _write_bytes_name(outbox, b"DONE-TODO-0001-\xff.md")

    messages = _silent_work_messages(tmp_path, monkeypatch, write, real_log=True)

    assert messages == [], "the real logger writes the file, nothing is intercepted"
    log_text = (tmp_path / "orchestrator.log").read_text(encoding="utf-8")
    assert "expected signal is one of" in log_text
    assert "\ufffd" in log_text


# ─── a rejected `.ready` is explained, not called missing ───────────────


def test_silent_work_log_explains_a_rejected_ready(tmp_path, monkeypatch):
    """QA3: `.ready` рядом с пустым `.md` — сигнал отвергнут, а не пропущен."""
    def write(outbox):
        (outbox / "DONE-TODO-0001.md").write_text("   \n", encoding="utf-8")
        (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")

    messages = _silent_work_messages(tmp_path, monkeypatch, write)

    diagnostic = [m for m in messages if "expected signal" in m][0]
    assert (
        "DONE-TODO-0001.ready is present but its companion "
        "DONE-TODO-0001.md is empty" in diagnostic
    )
    assert "an empty report is not a signal" in diagnostic


def test_silent_work_log_has_no_rejection_note_without_a_ready(tmp_path, monkeypatch):
    """QA3 (обратная сторона): нет `.ready` — нет и объяснения про пустой `.md`."""
    def write(outbox):
        (outbox / "DONE-TODO-0001.md").write_text("# done\n", encoding="utf-8")

    messages = _silent_work_messages(tmp_path, monkeypatch, write)

    diagnostic = [m for m in messages if "expected signal" in m][0]
    assert "is present but its companion" not in diagnostic


def test_silent_work_log_calls_an_unreadable_companion_unreadable(
    tmp_path, monkeypatch
):
    """`read_signal_for_todo` считает битый `.md` пустым — лог обязан сказать
    «unreadable», иначе оператор ищет пустой отчёт там, где он битый."""
    def write(outbox):
        (outbox / "DONE-TODO-0001.md").write_bytes(b"\xff\xfe binary garbage")
        (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")

    messages = _silent_work_messages(tmp_path, monkeypatch, write)

    diagnostic = [m for m in messages if "expected signal" in m][0]
    assert "DONE-TODO-0001.md is unreadable" in diagnostic
    assert "an unreadable report is not a signal" in diagnostic


def test_a_ready_without_a_report_is_a_signal_not_a_rejection(tmp_path):
    """QA5: `.ready` без компаньона — валидный сигнал, объяснять нечего."""
    from awf.signals import read_signal_for_todo

    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")

    assert read_signal_for_todo(outbox, "TODO-0001", "DONE") == "DONE-TODO-0001"
    assert pipeline_engine._rejected_ready_note(outbox, "TODO-0001", ["DONE"]) == ""


def test_silent_work_log_explains_the_md_ready_typo(tmp_path, monkeypatch):
    """QA8: агентская опечатка `.md.ready` (BD-21) тоже попадает в лог."""
    def write(outbox):
        (outbox / "DONE-TODO-0001.md").write_text("\n", encoding="utf-8")
        (outbox / "DONE-TODO-0001.md.ready").write_text("", encoding="utf-8")

    messages = _silent_work_messages(tmp_path, monkeypatch, write)

    diagnostic = [m for m in messages if "expected signal" in m][0]
    assert "DONE-TODO-0001.md.ready is present" in diagnostic
    assert "DONE-TODO-0001.md is empty" in diagnostic


# ─── QA-границы: ровно лимит, короткий id, пустой список префиксов ──────


class TestQaBoundaries:
    """Границы, на которых строка диагностики врёт оператору."""

    def test_exactly_the_limit_has_no_truncation_marker(self, tmp_path):
        """Ровно 10 файлов — это не усечение; `… +0` читался бы как потеря."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        for i in range(10):
            (outbox / f"PROGRESS-TODO-0001-{i:02d}.log").write_text(
                "x", encoding="utf-8"
            )

        rendered = pipeline_engine._format_unit_files(outbox, "TODO-0001")

        assert "… +" not in rendered
        assert rendered.count(", ") == 9

    def test_legacy_short_ready_with_empty_report_is_explained(self, tmp_path):
        """Короткое имя `DONE-0001.ready` — принятая форма, отказ по нему
        тоже обязан быть назван, иначе оператор ищет файл у канонического id."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "DONE-0001.md").write_text(" \n", encoding="utf-8")
        (outbox / "DONE-0001.ready").write_text("", encoding="utf-8")

        note = pipeline_engine._rejected_ready_note(outbox, "TODO-0001", ["DONE"])

        assert "DONE-0001.ready is present" in note
        assert "DONE-0001.md is empty" in note

    def test_no_prefixes_does_not_promise_a_done_signal(self):
        """Стадия без воркер-сигнала: не обещать `DONE-…ready`, которого
        движок не прочитает (`expected_signal_prefixes` вернул пустой список)."""
        note = pipeline_engine._expected_signals_note([], "TODO-0001")

        assert ".ready" not in note
        assert "no worker signal is expected" in note

    def test_a_ready_with_a_healthy_report_claims_no_rejection(self, tmp_path):
        """Компаньон в порядке — значит отказ случился по другой причине.

        Ветка страхует от вранья, если у читателя сигналов появится ещё один
        повод отказа: нот не имеет права называть пустым живой отчёт.
        """
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "DONE-TODO-0001.md").write_text("# done\n", encoding="utf-8")
        (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")

        assert pipeline_engine._rejected_ready_note(
            outbox, "TODO-0001", ["DONE"]
        ) == ""


def test_unit_file_names_requires_a_prefix_boundary(tmp_path):
    """QA-4: ``DONEFOO-...`` — не префикс ``DONE``, чужое имя.

    Префикс обязан кончаться на ``-``/``.``: иначе диагностика выдаёт
    посторонний файл за артефакт юнита.
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    for name in ("DONEFOO-TODO-0001.md", "PROGRESSIVE-TODO-0001.md"):
        (outbox / name).write_text("x", encoding="utf-8")
    (outbox / "DONE-TODO-0001.md").write_text("# done\n", encoding="utf-8")
    (outbox / "TEST-RESULTS-TODO-0001.log").write_text("ok\n", encoding="utf-8")

    assert pipeline_engine._unit_file_names(outbox, "TODO-0001") == [
        "DONE-TODO-0001.md",
        "TEST-RESULTS-TODO-0001.log",
    ]
