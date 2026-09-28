"""ORCH M4.3: формальная запись результата стадии — stage-блок DONE.json
и секция «Stage facts» в handoff.

Каждая стадия объявляет факты результата (роль/модель/выход/факт), движок
проверяет их и передаёт следующей роли. DONE.json расширяется обратно
совместимо: необязательный блок ``stage`` (минимум ``{stage_id, attempt}``),
старая форма без блока работает как раньше. Движок заполняет идентичность
сам — handoff несёт stage_id/attempt/role всегда, даже без DONE.json;
расходится объявленный блок и факты движка — пометка в handoff, не ошибка.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from awf import agent_stage
from awf.agent_stage import collect_handoff
from awf.pipeline import Stage
from awf.unit_contract import parse_done_json


def _rsf(*args, **kw):
    """render_stage_facts — new in ORCH M4.3, imported lazily so the
    module stays collectable (and the prove_red test fails with a real
    error, not a collection error) on the pre-fix baseline."""
    from awf.unit_contract import render_stage_facts

    return render_stage_facts(*args, **kw)

T = "TODO-0001"
ROLE = "agent-implementer"
SID = "impl"


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".agentic" / "outbox").mkdir(parents=True)
    (proj / ".agentic" / "context").mkdir(parents=True)
    (proj / ".agentic" / "logs").mkdir(parents=True)
    return proj


def _stub_git(monkeypatch, changed_files: list[str] | None = None) -> None:
    files = changed_files or []

    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[:2] == ["git", "diff"]:
            if "--name-only" in cmd:
                return SimpleNamespace(stdout="\n".join(files) + "\n")
            return SimpleNamespace(stdout="")
        return None

    monkeypatch.setattr("awf.signal_watch.subprocess.run", fake_run)


def _handoff(proj: Path, **kw):
    defaults = dict(attempt=1, stage_name=SID, stage_id=SID)
    defaults.update(kw)
    return collect_handoff(
        ROLE, T, proj, proj / ".agentic" / "logs", **defaults
    )


# ─── handoff: идентичность без DONE.json (prove_red) ─────────────────────

def test_handoff_carries_stage_identity_without_done_json(tmp_path, monkeypatch):
    """Движок заполняет идентичность сам: без DONE.json handoff всё равно
    несёт stage_id/attempt/role (и model, если известен из конфига)."""
    proj = _project(tmp_path)
    _stub_git(monkeypatch)

    out = _handoff(proj, attempt=2, model="vllm/llm")
    body = out.read_text(encoding="utf-8")

    assert "## Stage facts" in body
    assert f"- stage_id: `{SID}`" in body
    assert "- attempt: 2" in body
    assert f"- role: `{ROLE}`" in body
    assert "- model: `vllm/llm`" in body
    assert "check vs DONE.json" not in body  # блока нет — сверки нет


def test_stage_facts_always_present_without_model(tmp_path, monkeypatch):
    """Модель не читается из конфига — строка опускается, остальное на месте."""
    proj = _project(tmp_path)
    _stub_git(monkeypatch)

    body = _handoff(proj).read_text(encoding="utf-8")

    assert "## Stage facts" in body
    assert f"- stage_id: `{SID}`" in body
    assert "- attempt: 1" in body
    assert f"- role: `{ROLE}`" in body
    assert "- model:" not in body


# ─── parse_done_json: stage-блок, обратная совместимость ─────────────────

class TestParseDoneJsonStage:
    def test_legacy_done_json_without_stage_still_valid(self):
        data = parse_done_json(
            '{"files_changed": ["awf/x.py"], "notes": "old form"}'
        )
        assert data == {"files_changed": ["awf/x.py"], "notes": "old form"}

    def test_stage_minimal_block_valid(self):
        data = parse_done_json('{"stage": {"stage_id": "impl", "attempt": 1}}')
        assert data is not None
        assert data["stage"] == {"stage_id": "impl", "attempt": 1}

    def test_stage_full_block_valid(self):
        data = parse_done_json(
            json.dumps(
                {
                    "stage": {
                        "stage_id": "impl",
                        "attempt": 2,
                        "role": ROLE,
                        "model": "vllm/llm",
                    },
                    "notes": "new form",
                }
            )
        )
        assert data["stage"]["model"] == "vllm/llm"
        assert data["notes"] == "new form"

    def test_stage_unknown_keys_allowed(self):
        data = parse_done_json('{"stage": {"stage_id": "impl", "extra": 1}}')
        assert data is not None
        assert data["stage"]["extra"] == 1

    def test_stage_empty_block_is_valid(self):
        assert parse_done_json('{"stage": {}}') == {"stage": {}}

    def test_stage_not_a_dict_is_broken(self):
        assert parse_done_json('{"stage": "impl/1"}') is None
        assert parse_done_json('{"stage": [1, 2]}') is None

    def test_stage_id_not_a_string_is_broken(self):
        assert parse_done_json('{"stage": {"stage_id": 7}}') is None

    def test_stage_id_blank_string_is_broken(self):
        assert parse_done_json('{"stage": {"stage_id": " "}}') is None

    def test_attempt_not_an_int_is_broken(self):
        assert parse_done_json('{"stage": {"attempt": "1"}}') is None
        assert parse_done_json('{"stage": {"attempt": 1.5}}') is None

    def test_attempt_bool_is_broken(self):
        assert parse_done_json('{"stage": {"attempt": true}}') is None

    def test_role_and_model_not_strings_are_broken(self):
        assert parse_done_json('{"stage": {"role": ["x"]}}') is None
        assert parse_done_json('{"stage": {"model": 5}}') is None

    def test_broken_stage_skips_whole_file(self):
        """Нарушение схемы блока = файл пропускается как раньше (не роняет
        handoff) — вместе с остальными валидными ключами."""
        data = parse_done_json('{"notes": "ok", "stage": {"attempt": "x"}}')
        assert data is None


# ─── render_stage_facts: секция + сверка ─────────────────────────────────

class TestRenderStageFacts:
    def test_minimal_identity_lines(self):
        lines = _rsf(SID, 1, ROLE)
        assert lines == [
            f"- stage_id: `{SID}`",
            "- attempt: 1",
            f"- role: `{ROLE}`",
        ]

    def test_model_line_only_when_known(self):
        lines = _rsf(SID, 1, ROLE, model="vllm/llm")
        assert "- model: `vllm/llm`" in lines
        assert not any(
            line.startswith("- model:") for line in _rsf(SID, 1, ROLE)
        )

    def test_declared_match_is_matched_note(self):
        declared = {"stage_id": SID, "attempt": 1, "role": ROLE}
        lines = _rsf(SID, 1, ROLE, declared=declared)
        assert "- check vs DONE.json: matched" in lines

    def test_declared_mismatch_names_each_field(self):
        declared = {"stage_id": "plan", "attempt": 1, "role": "other"}
        lines = _rsf(SID, 2, ROLE, declared=declared)
        check = [line for line in lines if line.startswith("- check vs")]
        assert len(check) == 1
        assert "mismatched" in check[0]
        assert "stage_id" in check[0]
        assert "attempt" in check[0]
        assert "role" in check[0]
        assert "plan" in check[0]  # объявленное значение видно

    def test_partial_declared_checks_only_present_fields(self):
        declared = {"attempt": 1}
        lines = _rsf(SID, 1, ROLE, declared=declared)
        assert "- check vs DONE.json: matched" in lines

    def test_no_declared_no_check_line(self):
        lines = _rsf(SID, 1, ROLE)
        assert not any("check vs" in line for line in lines)

    def test_empty_declared_block_no_check_line(self):
        lines = _rsf(SID, 1, ROLE, declared={})
        assert not any("check vs" in line for line in lines)


# ─── handoff: сверка с DONE.json ─────────────────────────────────────────

class TestStageFactsCrossCheckInHandoff:
    def test_done_json_stage_matched(self, tmp_path, monkeypatch):
        proj = _project(tmp_path)
        _stub_git(monkeypatch)
        (proj / ".agentic" / "outbox" / f"DONE-{T}.json").write_text(
            json.dumps(
                {
                    "stage": {"stage_id": SID, "attempt": 2, "role": ROLE},
                    "notes": "done",
                }
            ),
            encoding="utf-8",
        )

        body = _handoff(proj, attempt=2).read_text(encoding="utf-8")

        assert "## Stage facts" in body
        assert "- check vs DONE.json: matched" in body
        assert "## Machine facts (DONE.json)" in body

    def test_done_json_stage_mismatched_is_note_not_error(self, tmp_path, monkeypatch):
        """Старый DONE.json с другого ретрая (attempt=1) против движка (2):
        пометка mismatched, handoff собран, секции на месте."""
        proj = _project(tmp_path)
        _stub_git(monkeypatch)
        (proj / ".agentic" / "outbox" / f"DONE-{T}.json").write_text(
            json.dumps(
                {"stage": {"stage_id": "plan", "attempt": 1}, "notes": "stale"}
            ),
            encoding="utf-8",
        )

        body = _handoff(proj, attempt=2).read_text(encoding="utf-8")

        check = [line for line in body.splitlines() if line.startswith("- check vs")]
        assert len(check) == 1
        assert "mismatched" in check[0]
        assert "plan" in check[0]
        assert "stale" in body  # валидные ключи файла всё равно отрендерились

    def test_legacy_done_json_without_stage_block(self, tmp_path, monkeypatch):
        """Старая форма DONE.json: Stage facts от движка, сверки нет."""
        proj = _project(tmp_path)
        _stub_git(monkeypatch)
        (proj / ".agentic" / "outbox" / f"DONE-{T}.json").write_text(
            json.dumps({"notes": "old form"}), encoding="utf-8"
        )

        body = _handoff(proj).read_text(encoding="utf-8")

        assert "## Stage facts" in body
        assert f"- stage_id: `{SID}`" in body
        assert "check vs" not in body
        assert "## Machine facts (DONE.json)" in body

    def test_broken_stage_block_degrades_like_broken_file(self, tmp_path, monkeypatch):
        """Нарушение схемы в stage — файл пропускается как раньше (без
        Machine facts), но Stage facts от движка на месте."""
        proj = _project(tmp_path)
        _stub_git(monkeypatch)
        (proj / ".agentic" / "outbox" / f"DONE-{T}.json").write_text(
            json.dumps({"stage": "garbage"}), encoding="utf-8"
        )

        body = _handoff(proj).read_text(encoding="utf-8")

        assert "## Machine facts (DONE.json)" not in body
        assert "## Stage facts" in body
        assert f"- stage_id: `{SID}`" in body
        assert "check vs" not in body
        log = (proj / ".agentic" / "logs" / "orchestrator.log").read_text(
            encoding="utf-8"
        )
        assert f"DONE-{T}.json" in log


# ─── промпт: worker получает stage_id/attempt ────────────────────────────

def _capture_run(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_run(cmd, cwd, watch_paths=None, **kwargs):
        captured["cmd"] = cmd

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True,
    )
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)
    return captured


def test_prompt_carries_stage_identity_line(tmp_path, monkeypatch):
    """Одна строка контракта: worker не угадывает stage_id/attempt, а
    копирует их в DONE.json."""
    captured = _capture_run(monkeypatch, tmp_path)
    proj = _project(tmp_path)
    roles = proj / ".agentic" / "roles"
    roles.mkdir(parents=True)
    (roles / f"{ROLE}.md").write_text("# role\n", encoding="utf-8")
    stage = Stage(name=SID, id=SID, role=ROLE, kind="execute")

    agent_stage.run_agent_stage(
        stage, T, proj, {}, proj / ".agentic" / "logs", attempt=3,
    )

    prompt = captured["cmd"][-1]
    assert f'stage_id="{SID}"' in prompt
    assert "attempt=3" in prompt
    assert f'"stage_id": "{SID}"' in prompt
    assert '"attempt": 3' in prompt
