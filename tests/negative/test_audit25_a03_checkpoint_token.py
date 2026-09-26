"""A-03 (аудит 2026-09-25, слой 5): решение checkpoint — только с токеном формы.

Дефект (воспроизведён в аудите): POST-обработчик чекпоинта
(``awf/plan_checkpoint.py``) принимает ``decision=approve|edit|reject``
без токена формы и без ограничения пути — локальный клиент, узнавший
порт (он публикуется в состоянии/журнале), может первым отправить
решение (first-wins) и определить ход пайплайна.

Инварианты:
1. Форма при открытии получает криптостойкий одноразовый токен;
   решение возможно только с ним.
2. Проверка токена и пути — под тем же lock, которым фиксируется
   решение; токен аннулируется после первого принятого POST
   (first-wins сохраняется, но только среди запросов с валидным токеном).
3. Запросы без токена, с чужим токеном и на другой путь не меняют
   holder/файл решения; Origin — дополнительная проверка.
4. Повторный POST с тем же токеном не меняет первое решение.

Уровень — реальный one-shot-сервер на свободном порту, через
``run_plan_checkpoint``: форма рендерится в изолированный tempdir,
токен извлекается из её HTML, POST — прямой HTTP-запрос (то, что
делает атакующий: он знает порт, токена не имеет).
"""
from __future__ import annotations

import json
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from awf import plan_checkpoint

TODO = "TODO-0001"
ORIG = "Original task"

_FORM_TOKEN_RE = re.compile(r'formToken = "([^"]+)"')


def _make_project(tmp_path: Path) -> Path:
    inbox = tmp_path / ".agentic" / "inbox"
    phases = tmp_path / ".agentic" / "phases"
    logs = tmp_path / ".agentic" / "logs"
    inbox.mkdir(parents=True)
    phases.mkdir(parents=True)
    logs.mkdir(parents=True)
    (inbox / f"{TODO}.md").write_text(ORIG, encoding="utf-8")
    (phases / "plan.md").write_text("- [ ] Step 1", encoding="utf-8")
    return tmp_path


def _post(port: int, path: str, data: bytes) -> tuple[int, bytes]:
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _extract_form_token() -> str:
    """A-03: одноразовый токен, который рендерится в форму."""
    html_files = sorted(
        Path(tempfile.gettempdir()).glob(f"awf-checkpoint-{TODO}-*.html")
    )
    assert html_files, "форма-HTML не найдена в tempdir"
    html = html_files[-1].read_text(encoding="utf-8")
    m = _FORM_TOKEN_RE.search(html)
    assert m, "formToken не найден в HTML формы"
    return m.group(1)


def _decision_file(project: Path) -> Path:
    return project / ".agentic" / "context" / f"CHECKPOINT-{TODO}.json"


def _run_with_post(project: Path, monkeypatch, post, timeout: int = 5) -> str:
    """run_plan_checkpoint, где фоновый поток делает POST после рендера.

    ``post(port, get_token)`` — вызывается после 0.2s: сервер уже на порту,
    форма-HTML уже на диске. ``get_token()`` — ленивая извлечение токена:
    сценарий, где токен не нужен (атакующий без токена), не вызывает её.
    """
    monkeypatch.setattr(
        plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None
    )
    real_start = plan_checkpoint._start_checkpoint_server

    def capturing_start(port, decision_holder, edited_holder, **kw):
        server = real_start(port, decision_holder, edited_holder, **kw)

        def _fire():
            time.sleep(0.2)
            post(server.server_address[1], _extract_form_token)

        threading.Thread(target=_fire, daemon=True).start()
        return server

    monkeypatch.setattr(plan_checkpoint, "_start_checkpoint_server", capturing_start)
    return plan_checkpoint.run_plan_checkpoint(
        TODO, project, config=None,
        logs_dir=project / ".agentic" / "logs", timeout=timeout,
    )


# ── prove_red: атакующий без токена / с чужим токеном ───────────────────────


def test_post_without_token_is_rejected(tmp_path, monkeypatch):
    """A-03.3: POST без токена — отказ, holder и файл решения не меняются.

    Baseline (дефект): решение принимается (approve), файл решения
    создаётся — красный.
    """
    isolated = tmp_path / "ckpt-tmp"
    isolated.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(isolated))
    project = _make_project(tmp_path)
    seen: dict = {}

    def post(port: int, get_token) -> None:
        # Атакующий знает только порт — токена у него нет (и извлекать
        # нечего: из формулы атаки токен не нужен).
        seen["status"], _ = _post(port, "/checkpoint", b"decision=approve")

    result = _run_with_post(project, monkeypatch, post, timeout=2)

    assert result == "timeout", (
        f"решение без токена принято (A-03): {result!r}"
    )
    assert seen.get("status") == 403, (
        f"ожидался отказ, получен статус {seen.get('status')!r}"
    )
    assert not _decision_file(project).is_file(), (
        "файл решения создан отклонённым POST"
    )
    assert (project / ".agentic" / "inbox" / f"{TODO}.md").read_text(
        encoding="utf-8"
    ) == ORIG, "TODO изменён отклонённым POST"


def test_post_with_foreign_token_is_rejected(tmp_path, monkeypatch):
    """A-03.1/3: POST с чужим (сфабрикованным) токеном — отказ.

    Baseline (дефект): решение принимается (reject) — ход пайплайна
    определяет атакующий — красный.
    """
    isolated = tmp_path / "ckpt-tmp"
    isolated.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(isolated))
    project = _make_project(tmp_path)
    seen: dict = {}

    def post(port: int, get_token) -> None:
        # Токен чужой формы/сфабрикованный — не тот, что в этой форме.
        payload = b"decision=reject&token=attacker-forged-token"
        seen["status"], _ = _post(port, "/checkpoint", payload)

    result = _run_with_post(project, monkeypatch, post, timeout=2)

    assert result == "timeout", (
        f"решение с чужим токеном принято (A-03): {result!r}"
    )
    assert seen.get("status") == 403, (
        f"ожидался отказ, получен статус {seen.get('status')!r}"
    )
    assert not _decision_file(project).is_file(), (
        "файл решения создан отклонённым POST"
    )


# ── Регрессы: легитимный путь не сломан, first-wins держится ────────────────


class TestCheckpointTokenRegressions:
    """A-03 регрессы: валидный токен проходит, потраченный — не проходит,
    другой путь отклоняется, файл решения не утёк в журнал."""

    def test_valid_token_accepted(self, tmp_path, monkeypatch):
        """A-03.1: POST с токеном формы — решение принимается (200)."""
        isolated = tmp_path / "ckpt-tmp"
        isolated.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(isolated))
        project = _make_project(tmp_path)
        seen: dict = {}

        def post(port: int, get_token) -> None:
            token = get_token()
            seen["token"] = token
            seen["status"], _ = _post(
                port, "/checkpoint", b"decision=approve&token=" + token.encode()
            )

        result = _run_with_post(project, monkeypatch, post)

        assert result == "approve"
        assert seen.get("status") == 200, (
            f"POST с валидным токеном должен пройти, получен "
            f"{seen.get('status')!r}"
        )
        consumed = _decision_file(project).with_name(
            _decision_file(project).name + ".consumed"
        )
        assert consumed.is_file(), (
            "принятое решение должно сохраниться на диске (B1)"
        )
        # Токен не должен утекать в журнал.
        log_text = (project / ".agentic" / "logs" / "orchestrator.log").read_text(
            encoding="utf-8"
        )
        assert seen["token"] not in log_text, "токен формы утёк в журнал"

    def test_repeated_post_with_same_token_keeps_first_decision(
        self, tmp_path, monkeypatch
    ):
        """A-03.4: повторный POST с тем же (потраченным) токеном не меняет
        первое решение; сам повтор — отказ (токен одноразовый)."""
        isolated = tmp_path / "ckpt-tmp"
        isolated.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(isolated))
        project = _make_project(tmp_path)
        seen: dict = {}

        def post(port: int, get_token) -> None:
            token = get_token()
            seen["first"], _ = _post(
                port, "/checkpoint", b"decision=approve&token=" + token.encode()
            )
            seen["second"], _ = _post(
                port, "/checkpoint", b"decision=reject&token=" + token.encode()
            )

        result = _run_with_post(project, monkeypatch, post)

        assert result == "approve", (
            f"повторный POST изменил первое решение (A-03.4): {result!r}"
        )
        assert seen["first"] == 200
        assert seen["second"] == 403, (
            f"потраченный токен должен быть отклонён, "
            f"получен {seen['second']!r}"
        )
        consumed = _decision_file(project).with_name(
            _decision_file(project).name + ".consumed"
        )
        data = json.loads(consumed.read_text(encoding="utf-8"))
        assert data["decision"] == "approve"

    def test_nonascii_token_rejected_no_state_change(self, tmp_path, monkeypatch):
        """A-03.3 edge: не-ASCII токен (в т.ч. битые UTF-8 байты).

        Текущая реализация не доходит до чистого 403:
        ``secrets.compare_digest`` бросает TypeError на не-ASCII str
        (awf/plan_checkpoint.py:653), клиент получает сброс соединения.
        Инвариант при этом держится: решение не принимается, файл решения
        не создаётся (файл задачи decision=approve не трогает по
        конструкции — см. комментарий к ассертам). Фикс (чистый 403 вместо
        сброса) — следующий проход через REVIEW; этот тест фиксирует
        инвариант, чтобы чинить не «по-своему» и не начать принимать такой
        POST.
        """
        isolated = tmp_path / "ckpt-tmp"
        isolated.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(isolated))
        project = _make_project(tmp_path)
        seen: dict = {}

        def post(port: int, get_token) -> None:
            try:
                seen["status"], _ = _post(
                    port,
                    "/checkpoint",
                    b"decision=approve&token=%D0%90",
                )
            except OSError:
                # URLError / RemoteDisconnected / ConnectionResetError —
                # сброс соединения до ответа (текущее поведение; см. докстринг).
                seen["status"] = "connection-reset"

        result = _run_with_post(project, monkeypatch, post, timeout=2)

        assert seen.get("status") != 200, (
            f"POST с не-ASCII токеном принят (A-03.3): {seen.get('status')!r}"
        )
        assert result == "timeout", (
            f"решение с не-ASCII токеном принято (A-03.3): {result!r}"
        )
        assert not _decision_file(project).is_file(), (
            "файл решения создан POST с не-ASCII токеном"
        )
        # Файл задачи не проверяем: decision=approve его не трогает по
        # конструкции (пишет только edit); «пробитие» на approve поймает
        # assert result == "timeout" выше.

    def test_post_to_other_path_rejected(self, tmp_path, monkeypatch):
        """A-03.3: валидный токен, но другой путь — отказ, решение не меняется."""
        isolated = tmp_path / "ckpt-tmp"
        isolated.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(isolated))
        project = _make_project(tmp_path)
        seen: dict = {}

        def post(port: int, get_token) -> None:
            token = get_token()
            seen["status"], _ = _post(
                port, "/", b"decision=approve&token=" + token.encode()
            )

        result = _run_with_post(project, monkeypatch, post, timeout=2)

        assert result == "timeout", (
            f"решение по другому пути принято (A-03.3): {result!r}"
        )
        assert seen.get("status") == 404, (
            f"ожидался 404, получен {seen.get('status')!r}"
        )
        assert not _decision_file(project).is_file(), (
            "файл решения создан отклонённым POST"
        )
