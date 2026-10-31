"""A12: the DONE.json schema side of the contract.

``_load_done_json`` names the offending key, the expectation and the actual
JSON type; ``parse_done_json`` keeps returning ``dict | None``;
``collect_done_facts`` logs that reason and never raises.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf.unit_contract import (
    _load_done_json,
    collect_done_facts,
    parse_done_json,
)


def _reason(text: str) -> str | None:
    """The schema reason from the production seam (data, reason)."""
    return _load_done_json(text)[1]


def _outbox_and_logs(tmp_path: Path) -> tuple[Path, Path]:
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    return outbox, logs


# ─── (1) schema reason: key + expectation + actual type ────────────────


class TestSchemaReason:
    def test_gates_object_names_key_expectation_and_type(self):
        reason = _reason(
            '{"gates": {"verify": {}, "checklist": {}}}'
        )
        assert reason == "'gates' must be a list of strings, got object"

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("{nope", "not valid JSON"),
            ("[1, 2]", "top-level value must be a JSON object, got array"),
            (
                '{"files_changed": "x"}',
                "'files_changed' must be a list of strings, got string",
            ),
            (
                '{"gates": [1]}',
                "'gates' must be a list of strings, got an entry of type number",
            ),
            (
                '{"tests_run": {"cmd": "x"}}',
                "'tests_run' must be a list of objects {cmd, result}, got object",
            ),
            (
                '{"tests_run": [{"cmd": "x"}]}',
                "'tests_run[result]' must be a string, got null",
            ),
            ("{\"notes\": 5}", "'notes' must be a string, got number"),
            (
                '{"stage": "impl/1"}',
                "'stage' must be an object, got string",
            ),
            (
                '{"stage": {"stage_id": 7}}',
                "'stage.stage_id' must be a non-empty string, got number",
            ),
            (
                '{"stage": {"attempt": "1"}}',
                "'stage.attempt' must be an integer, got string",
            ),
            (
                '{"stage": {"attempt": true}}',
                "'stage.attempt' must be an integer, got boolean",
            ),
        ],
    )
    def test_each_key_is_named(self, text, expected):
        assert expected in (_reason(text) or "")

    def test_valid_done_json_has_no_reason(self):
        text = (
            '{"files_changed": ["awf/x.py"], '
            '"tests_run": [{"cmd": "pytest", "result": "1 passed"}], '
            '"gates": ["tests"], "notes": "ok", '
            '"stage": {"stage_id": "impl", "attempt": 1, "role": "worker"}}'
        )
        assert _reason(text) is None
        assert parse_done_json(text) is not None

    def test_public_parser_keeps_its_contract(self):
        assert parse_done_json("{}") == {}
        assert parse_done_json('{"gates": ["tests"]}') == {"gates": ["tests"]}
        assert parse_done_json('{"gates": {"verify": {}}}') is None
        assert parse_done_json("{nope") is None


@pytest.mark.parametrize("field", ["stage_id", "role", "model"])
def test_blank_stage_field_reason_names_emptiness(field):
    """QA2: пустая строка — это не «got string», а «got empty string»."""
    reason = _reason(f'{{"stage": {{"{field}": "   "}}}}')
    assert reason == (
        f"'stage.{field}' must be a non-empty string, got empty string"
    )


# ─── (2) collect_done_facts logs the reason, never raises ───────────────


def test_collect_done_facts_logs_reason_and_degrades(tmp_path):
    outbox, logs = _outbox_and_logs(tmp_path)
    (outbox / "DONE-TODO-0001.json").write_text(
        '{"gates": {"verify": {"rc": 0}}}', encoding="utf-8"
    )

    fact, lines, declared = collect_done_facts(outbox, "TODO-0001", logs)

    assert lines == [] and declared is None
    assert "DONE-json=invalid" in fact
    log_text = (logs / "orchestrator.log").read_text(encoding="utf-8")
    assert "U3: DONE-TODO-0001.json — 'gates' must be a list of strings, got object" in log_text
    assert "handoff continues without the machine-facts section" in log_text


def test_collect_done_facts_names_broken_json_without_raising(tmp_path):
    """Broken JSON is a different branch from a schema violation (TODO A12)."""
    outbox, logs = _outbox_and_logs(tmp_path)
    (outbox / "DONE-TODO-0001.json").write_text("{nope", encoding="utf-8")

    fact, lines, declared = collect_done_facts(outbox, "TODO-0001", logs)

    assert lines == [] and declared is None
    assert "DONE-json=invalid" in fact
    log_text = (logs / "orchestrator.log").read_text(encoding="utf-8")
    assert "U3: DONE-TODO-0001.json — not valid JSON" in log_text


@pytest.mark.parametrize(
    "payload, expected",
    [
        (
            '{"files_changed": "awf/x.py"}',
            "'files_changed' must be a list of strings, got string",
        ),
        (
            '{"tests_run": [{"cmd": "pytest"}]}',
            "'tests_run[result]' must be a string, got null",
        ),
        ('{"notes": ["x"]}', "'notes' must be a string, got array"),
        (
            '{"stage": {"role": 1}}',
            "'stage.role' must be a non-empty string, got number",
        ),
        ("[1, 2]", "top-level value must be a JSON object, got array"),
    ],
)
def test_collect_done_facts_names_every_schema_key(tmp_path, payload, expected):
    """QA4: причина доходит до лога по любому ключу, не только по `gates`."""
    outbox, logs = _outbox_and_logs(tmp_path)
    (outbox / "DONE-TODO-0001.json").write_text(payload, encoding="utf-8")

    fact, lines, declared = collect_done_facts(outbox, "TODO-0001", logs)

    assert lines == [] and declared is None
    assert "DONE-json=invalid" in fact
    log_text = (logs / "orchestrator.log").read_text(encoding="utf-8")
    assert f"U3: DONE-TODO-0001.json — {expected}" in log_text


def test_done_json_absent_and_invalid_are_different_facts(tmp_path):
    """CTX4: «файла нет» и «файл мимо схемы» — разные действия следующей роли."""
    outbox, logs = _outbox_and_logs(tmp_path)

    fact, _, _ = collect_done_facts(outbox, "TODO-0001", logs)
    assert "DONE-json=absent" in fact and "invalid" not in fact

    (outbox / "DONE-TODO-0001.json").write_text(
        '{"gates": {"verify": {}}}', encoding="utf-8"
    )
    fact, _, _ = collect_done_facts(outbox, "TODO-0001", logs)
    assert "DONE-json=invalid" in fact


def test_collect_done_facts_calls_an_empty_json_invalid(tmp_path):
    """QA6: пустой файл — «написан и непригоден», а не «не написан»."""
    outbox, logs = _outbox_and_logs(tmp_path)
    (outbox / "DONE-TODO-0001.json").write_text("  \n", encoding="utf-8")

    fact, lines, declared = collect_done_facts(outbox, "TODO-0001", logs)

    assert lines == [] and declared is None
    assert "DONE-json=invalid" in fact
    log_text = (logs / "orchestrator.log").read_text(encoding="utf-8")
    assert "U3: DONE-TODO-0001.json is empty" in log_text


def test_empty_but_valid_json_is_present(tmp_path):
    """CTX2: `{}` — файл есть, рендерить нечего; факт `present`, не `absent`."""
    outbox, logs = _outbox_and_logs(tmp_path)
    (outbox / "DONE-TODO-0001.json").write_text("{}", encoding="utf-8")

    fact, lines, declared = collect_done_facts(outbox, "TODO-0001", logs)

    assert lines == [] and declared is None
    assert "DONE-json=present" in fact
    assert not (logs / "orchestrator.log").exists(), "nothing to warn about"


def test_tests_run_strings_are_named_as_entries(tmp_path):
    """Самая частая живая ошибка: `tests_run` — список строк вместо объектов.

    Инструкция следующей роли прямо предупреждает «items are OBJECTS
    {cmd, result}, not strings», значит и отказ обязан называть эту ветку,
    а не только «got object» на весь список.
    """
    outbox, logs = _outbox_and_logs(tmp_path)
    (outbox / "DONE-TODO-0001.json").write_text(
        '{"tests_run": ["pytest -q", "ruff check ."]}', encoding="utf-8"
    )

    fact, lines, declared = collect_done_facts(outbox, "TODO-0001", logs)

    assert lines == [] and declared is None
    assert "DONE-json=invalid" in fact
    log_text = (logs / "orchestrator.log").read_text(encoding="utf-8")
    assert (
        "'tests_run' entries must be objects {cmd, result}, got string"
        in log_text
    )


def test_deeply_nested_json_is_invalid_not_a_crash():
    """QA-3: ``json.loads`` бросает RecursionError, не ValueError.

    Патологическая вложенность не должна ронять стадию: контракт — причина
    в лог, файл пропущен, исключения нет.
    """
    text = "[" * 100000 + "]" * 100000

    assert _reason(text) is not None
    assert _reason(text).startswith("not valid JSON")
    assert parse_done_json(text) is None
