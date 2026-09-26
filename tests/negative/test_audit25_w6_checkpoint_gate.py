"""Волна 6.4 (аудит 2026-09-25, FU-06): вернуть рабочий гейт plan checkpoint.

Механика FU-06 (воспроизведена и понята): проводка «гейт после известного
TODO» (AUD16-02: 17/17 auto-approve, форма не открывалась) исправлена и
работает — полный цикл на живом background-child подтверждён (см. DONE).
Оставшаяся дыра — тихий skip гейта: одноразовый флаг запуска
no_checkpoints (RUN3 #6) едет транспорт-переменной AWF_NO_CHECKPOINTS,
а окружение child-процесса собирается как ``{**os.environ, ...}``
(``awf/api/_background.py``). Родитель, несущий флаг (сессия внутри
no_checkpoints-забега — живое состояние этого проекта: run.yaml
``no_checkpoints: true``, у воркеров флаг в env), передаёт его ВСЕМУ
поздним background-запускам — гейт молча пропускать форму, а строка
ложа «RUN3-6: checkpoint skipped ... start no_checkpoints=true»
винит запуск, который флаг не передавал. Контракт RUN3 #6: флаг живёт
только в процессе пайплайна и «the next launch is unaffected».

Красные на baseline:
- test_inherited_flag_does_not_ride_into_child — флаг наследуется.

Цикл целиком (зелёные до и после — доказательство инварианта 2/3,
функции верхнего уровня: run_pipeline + настоящая форма + настоящий
HTTP-POST с токеном, снятым с рендеренной формы):
- test_full_cycle_approve_pipeline_continues
- test_full_cycle_reject_pipeline_stops_at_gate
- test_full_cycle_edit_rewrites_todo_and_continues
- test_form_timeout_aborts_with_hint_and_clean_state — недоставка
  решения: внятный отказ + подсказка, состояние почищено, без вечного
  ожидания (таймаут-маппинг гейта timeout→rc=1 закреплён отдельно:
  tests/integration/test_plan_checkpoint.py::TestCheckpointGateDispatch::
  test_timeout_aborts_pipeline_dogfood3).

Инвариант 4 (флаг выключен, поведение не меняется):
- test_flag_off_gate_skips_without_form
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from awf import api, plan_checkpoint
from awf.pipeline_state import read_state

TODO = "TODO-0001"
_FORM_TOKEN_RE = re.compile(r'formToken = "([^"]+)"')


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_project(tmp_path: Path) -> Path:
    """Temp git project: init + pipeline plan/worker/verify + active TODO."""
    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=proj, check=True)
    api.init_project(proj, project_name="W6CkptGate")
    (proj / ".agentic" / "pipelines" / "default.yaml").write_text(
        "name: default\n"
        "stages:\n"
        "  - name: plan\n    role: supervisor\n"
        "  - name: worker\n    role: worker\n"
        "  - name: verify\n    role: supervisor\n",
        encoding="utf-8",
    )
    (proj / ".agentic" / "roles" / "worker.md").write_text(
        "# worker\n", encoding="utf-8"
    )
    (proj / ".agentic" / "inbox" / f"{TODO}.md").write_text(
        "# task\ninitial text\n", encoding="utf-8"
    )
    (proj / ".agentic" / "inbox" / f"{TODO}.ready").touch()
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=proj, check=True)
    return proj


def _clean_gate_env(monkeypatch) -> None:
    """Чистый контекст гейта: окружение сессии может нести bypass-флаги
    (воркер no_checkpoints-забега несёт AWF_NO_CHECKPOINTS=1) — тесты
    цикла стартуют без них, иначе гейт молча пропустится."""
    monkeypatch.delenv("AWF_NO_CHECKPOINTS", raising=False)
    monkeypatch.delenv("AWF_PLAN_CHECKPOINT", raising=False)
    monkeypatch.delenv("AWF_BACKGROUND_CHILD", raising=False)


def _fake_opencode_stages(monkeypatch) -> None:
    """LLM-сабпроцессы — единственный внешний рубеж (тот же паттерн, что
    tests/stubs/opencode в e2e): plan/verify отдают пустой сигнал, worker
    пишет DONE. Гейт, форма, токен, HTTP, state, сигналы — настоящие."""
    import awf.pipeline_engine as pe

    monkeypatch.setattr(pe, "_run_supervisor_stage", lambda *a, **kw: "")

    def _agent_stage(*args, **kw):
        stage, current_todo, project_dir, config, logs_dir = args[:5]
        outbox = project_dir / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / f"DONE-{current_todo}.md").write_text("done\n", encoding="utf-8")
        (outbox / f"DONE-{current_todo}.ready").touch()

    monkeypatch.setattr(pe, "_run_agent_stage", _agent_stage)


def _wait_form(project: Path, timeout: float = 20.0) -> tuple[str, int]:
    """Token + port из state/формы: ждём, пока чекпоинт откроет форму."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = read_state(project) or {}
        url = st.get("checkpoint_form_url")
        if st.get("checkpoint_pending") and url:
            form = Path(str(url)[len("file://"):])
            if form.is_file():
                m = _FORM_TOKEN_RE.search(form.read_text(encoding="utf-8"))
                port = st.get("checkpoint_port")
                if m and port:
                    return m.group(1), int(port)
        time.sleep(0.1)
    raise TimeoutError(f"checkpoint form did not open within {timeout}s")


def _post(port: int, payload: bytes) -> int:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/checkpoint", data=payload, timeout=5
        ) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def _cycle(tmp_path, monkeypatch, payload: bytes) -> tuple[Path, int, str]:
    """Полный цикл: run_pipeline в потоке, форма отвечает из этого.

    Возвращает (project, rc, orchestrator_log).
    """
    proj = _make_project(tmp_path)
    _clean_gate_env(monkeypatch)
    _fake_opencode_stages(monkeypatch)

    holder: dict = {}

    def runner():
        from awf.orchestrator import run_pipeline

        args = SimpleNamespace(
            project_dir=str(proj), pipeline=None, from_stage=None,
            auto=False, timeout=300, todo_id=TODO, no_checkpoints=False,
        )
        holder["rc"] = run_pipeline(args)

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    token, port = _wait_form(proj)
    assert _post(port, payload + b"&token=" + token.encode()) == 200
    t.join(timeout=60)
    assert not t.is_alive(), "pipeline did not finish after the decision"
    log = (proj / ".agentic" / "logs" / "orchestrator.log").read_text(
        encoding="utf-8"
    )
    return proj, holder["rc"], log


def _plumbing_project(tmp_path: Path) -> Path:
    """Минимальный проект для spawn-тестов (git не нужен)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".agentic").mkdir()
    (proj / ".agentic" / "config.yaml").write_text(
        "project:\n  name: leak\n", encoding="utf-8"
    )
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir()
    (inbox / f"{TODO}.md").write_text("# Task\n", encoding="utf-8")
    (inbox / f"{TODO}.ready").touch()
    return proj


def _capture_popen(monkeypatch) -> dict:
    """Fake Popen на спавн-границе: ловит argv/env child-процесса
    (паттерн TestLaunchNoCheckpointsPlumbing)."""
    captured: dict = {}

    class _CapturingPopen:
        pid = 2**31  # вне диапазона OS — probe_alive не подвиснет

        def __init__(self, args, **kwargs):
            captured["argv"] = list(args)
            captured["env"] = dict(kwargs.get("env") or {})

    from awf.api import _background

    monkeypatch.setattr(_background.subprocess, "Popen", _CapturingPopen)
    monkeypatch.setattr(
        "awf.api.pipeline._verify_child_alive", lambda pid, log_file=None: True
    )
    return captured


# ── FU-06: одноразовый флаг не переезжает между запусками ───────────────────


def test_inherited_flag_does_not_ride_into_child(tmp_path, monkeypatch):
    """Красный на baseline. Родитель несёт AWF_NO_CHECKPOINTS=1 (сессия
    внутри no_checkpoints-забега), запуск флаг НЕ передаёт — child должен
    стартовать чистым: гейт следующего запуска не отключается молча."""
    proj = _plumbing_project(tmp_path)
    captured = _capture_popen(monkeypatch)
    monkeypatch.setenv("AWF_NO_CHECKPOINTS", "1")
    monkeypatch.delenv("AWF_PLAN_CHECKPOINT", raising=False)

    result = api.start_pipeline(proj, background=True)
    assert result.run_mode == "background"
    assert "AWF_NO_CHECKPOINTS" not in captured["env"], (
        "FU-06: одноразовый флаг запуска утекает из окружения родителя в "
        "child — гейт пропустит форму без запроса на это, а лог обвинит "
        "запуск ('start no_checkpoints=true')"
    )


def test_requested_flag_set_despite_polluted_parent(tmp_path, monkeypatch):
    """Положительный транспорт не сломан: параметр ставит флаг в child
    даже при заляпанном родителе."""
    proj = _plumbing_project(tmp_path)
    captured = _capture_popen(monkeypatch)
    monkeypatch.setenv("AWF_NO_CHECKPOINTS", "1")
    monkeypatch.delenv("AWF_PLAN_CHECKPOINT", raising=False)

    result = api.start_pipeline(proj, background=True, no_checkpoints=True)
    assert result.run_mode == "background"
    assert captured["env"].get("AWF_NO_CHECKPOINTS") == "1"


def test_clean_parent_launch_stays_clean(tmp_path, monkeypatch):
    """Регресс: чистый родитель + без параметра → флага в child нет."""
    proj = _plumbing_project(tmp_path)
    captured = _capture_popen(monkeypatch)
    monkeypatch.delenv("AWF_NO_CHECKPOINTS", raising=False)
    monkeypatch.delenv("AWF_PLAN_CHECKPOINT", raising=False)

    result = api.start_pipeline(proj, background=True)
    assert result.run_mode == "background"
    assert "AWF_NO_CHECKPOINTS" not in captured["env"]


# ── полный цикл через run_pipeline (инвариант 2) ────────────────────────────


def test_full_cycle_approve_pipeline_continues(tmp_path, monkeypatch):
    """Форма открыта → POST approve с токеном → решение применено,
    пайплайн следует дальше (worker-стадия началась)."""
    proj, _rc, log = _cycle(tmp_path, monkeypatch, b"decision=approve")
    assert "checkpoint decision=approve" in log
    assert "Stage 1: worker" in log, "pipeline did not continue after approve"

    st = read_state(proj) or {}
    assert st.get("checkpoint_pending") is False
    # решение consum'd: повторный вход в гейт не применит его дважды
    ctx = proj / ".agentic" / "context"
    assert not (ctx / f"CHECKPOINT-{TODO}.json").is_file()
    assert (ctx / f"CHECKPOINT-{TODO}.json.consumed").is_file()
    # TODO не тронут approve
    todo = (proj / ".agentic" / "inbox" / f"{TODO}.md").read_text(encoding="utf-8")
    assert todo == "# task\ninitial text\n"


def test_full_cycle_reject_pipeline_stops_at_gate(tmp_path, monkeypatch):
    """POST reject → пайплайн останавливается на гейте: worker не
    запускается, rc=1, TODO не тронут."""
    proj, rc, log = _cycle(tmp_path, monkeypatch, b"decision=reject")
    assert rc == 1
    assert "checkpoint rejected" in log
    assert "Stage 1: worker" not in log, "worker started despite reject"
    todo = (proj / ".agentic" / "inbox" / f"{TODO}.md").read_text(encoding="utf-8")
    assert todo == "# task\ninitial text\n"


def test_full_cycle_edit_rewrites_todo_and_continues(tmp_path, monkeypatch):
    """POST edit с edited_content → TODO переписан, пайплайн идёт дальше."""
    new_text = "REWRITTEN by the form"
    payload = (
        b"decision=edit&edited_content=" + urllib.parse.quote(new_text).encode()
    )
    proj, _rc, log = _cycle(tmp_path, monkeypatch, payload)
    assert "checkpoint decision=edit" in log
    assert "rewritten via edit" in log
    assert "Stage 1: worker" in log, "pipeline did not continue after edit"
    todo = (proj / ".agentic" / "inbox" / f"{TODO}.md").read_text(encoding="utf-8")
    assert todo == new_text


def test_form_timeout_aborts_with_hint_and_clean_state(tmp_path, monkeypatch):
    """Недоставка решения: внятный отказ + подсказка, состояние почищено,
    временная форма убрана — без вечного ожидания (ожидание ограничено
    timeout; маппинг гейта timeout→rc=1 — test_timeout_aborts_pipeline_
    dogfood3 в integration)."""
    proj = _make_project(tmp_path)
    _clean_gate_env(monkeypatch)

    result = plan_checkpoint.run_plan_checkpoint(
        TODO, proj, config=None,
        logs_dir=proj / ".agentic" / "logs", timeout=2,
    )
    assert result == "timeout"

    st = read_state(proj) or {}
    assert st.get("checkpoint_pending") is False
    assert st.get("checkpoint_form_url") is None
    assert st.get("checkpoint_port") is None

    log = (proj / ".agentic" / "logs" / "orchestrator.log").read_text(
        encoding="utf-8"
    )
    assert "pipeline will abort" in log
    assert "awf continue" in log
    # временная форма не осталась
    assert not list((proj / ".agentic").glob("awf-checkpoint-*"))


# ── инвариант 4: выключенный флаг не меняет поведения ───────────────────────


def test_flag_off_gate_skips_without_form(tmp_path, monkeypatch):
    """automation.plan_checkpoint: false — гейт no-op, форма не
    открывается, пайплайн проходит план-стадию (rc=0)."""
    from awf.pipeline_engine import _run_plan_checkpoint_gate

    proj = _plumbing_project(tmp_path)
    _clean_gate_env(monkeypatch)

    def _fail_if_called(*_a, **_kw):
        raise AssertionError("form must not open when the config flag is off")

    monkeypatch.setattr("awf.plan_checkpoint.run_plan_checkpoint", _fail_if_called)

    rc = _run_plan_checkpoint_gate(
        current_todo=TODO,
        project_dir=proj,
        config={"automation": {"plan_checkpoint": False}},
        auto=False,
        logs_dir=proj / ".agentic" / "logs",
    )
    assert rc == 0
