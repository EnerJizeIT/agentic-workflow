"""A-07 (аудит 2026-09-25, слой 5): сервер форм не зависает на теле POST.

Дефект (воспроизведён в аудите): ``do_POST`` в
``agent_workflow_ui/http_endpoint.py`` при превышении лимита 1 MiB
сначала вычитывает заявленное тело целиком (drain), удерживая поток и
память; частичная отправка с большим ``Content-Length`` удерживает поток
навсегда (таймаута чтения нет); ``ThreadingHTTPServer`` без
ограничения одновременных обработчиков.

Инварианты:
1. Неверный или отрицательный ``Content-Length`` → немедленный 400
   (без чтения тела), статус формы не меняется.
2. Тело сверх лимита (1 MiB) → 413 + ``Connection: close`` без
   вычитывания тела; submit не фиксируется.
3. Неполное тело не удерживает поток бесконечно: чтение ограничено по
   времени (``BODY_READ_TIMEOUT``), по истечении — 408, submit не
   фиксируется.
4. Допустимое тело обрабатывается как раньше; число активных
   обработчиков ограничено (``MAX_CONCURRENT_HANDLERS``), перелив —
   немедленный 503, запрос не обрабатывается.

Уровень — реальный сервер на свободном порту (как в продакшене);
запросы — через raw-сокет, потому что urllib не даёт контроля над
``Content-Length`` (завышенное значение) и над частичной отправкой
тела.

Примечание про prove-red: в worktree базовой ревизии editable-install
плагина отключён (bootstrap герметичности), поэтому пакет импортируется
через sys.path-шим к src-копии дерева — шим идёт до импорта.
"""
from __future__ import annotations

import re
import socket
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "agent_workflow_ui" / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent_workflow_ui.config import load  # noqa: E402
from agent_workflow_ui.http_endpoint import start_http_server  # noqa: E402
from agent_workflow_ui.state import FormRecord, FormRegistry  # noqa: E402

FORM_ID = "FORM-a07"


# ── raw-клиент ───────────────────────────────────────────────────────────────


def _response_complete(out: bytes) -> bool:
    """Ответ целиком: заголовки + тело до заявленного Content-Length."""
    head, sep, rest = out.partition(b"\r\n\r\n")
    if not sep:
        return False
    m = re.search(rb"(?im)^content-length:\s*(\d+)", head)
    return m is not None and len(rest) >= int(m.group(1))


def _raw_post(
    port: int, body: bytes, length_header: str | None, wait: float
) -> tuple[int | None, bytes, bool]:
    """POST на /submit/<form_id>; ``length_header=None`` — честный
    Content-Length. Возвращает ``(status, ответ, closed)``. ``status`` —
    None, если сервер не прислал полный ответ в пределах ``wait``
    (зависание — до-фикс поведение). ``closed`` — сервер закрыл
    соединение (EOF после ответа; семантика «отказ без drain»)."""
    if length_header is None:
        length_header = str(len(body))
    request = (
        f"POST /submit/{FORM_ID} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Content-Length: {length_header}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + body
    status: int | None = None
    out = b""
    closed = False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=wait) as s:
            s.sendall(request)
            s.settimeout(wait)
            try:
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        closed = True
                        break
                    out += chunk
                    if _response_complete(out):
                        break
            except TimeoutError:
                pass
            else:
                # Ответ получен — проверяем закрытие соединения сервером.
                s.settimeout(2.0)
                try:
                    if not s.recv(1):
                        closed = True
                except TimeoutError:
                    pass
    except OSError:
        pass
    m = re.match(rb"^HTTP/\d\.\d (\d{3})", out)
    if m:
        status = int(m.group(1))
    return status, out, closed


@pytest.fixture
def form_server(tmp_path, monkeypatch):
    """Реальный сервер форм на свободном порту с одной pending-формой."""
    monkeypatch.chdir(tmp_path)
    config = load()
    config.inputs_dir.mkdir(parents=True, exist_ok=True)
    registry = FormRegistry()
    registry.add(FormRecord(
        form_id=FORM_ID,
        template="test",
        opened_at=datetime.now(timezone.utc),
    ))
    server, port = start_http_server(config, registry)
    yield config, registry, port
    server.shutdown()


# ── prove_red: мусорный / отрицательный Content-Length ──────────────────────


@pytest.mark.parametrize("bad_length", ["abc", "-1"])
def test_bad_content_length_rejected_quickly(form_server, bad_length):
    """A-07.1: ``Content-Length: abc`` / ``-1`` → немедленный 400, статус
    формы не изменился, submit не записан, тело не читается.

    На базовой ревизии эти случаи уже отвечали 400 (AUD09-07) — тест
    закрепит инвариант 1 как регресс-гард: отказ должен остаться
    немедленным и без чтения тела при любом будущем пересобирании
    валидации заголовка.
    """
    config, registry, port = form_server
    t0 = time.monotonic()
    status, _, _ = _raw_post(port, b"choice=option1", length_header=bad_length, wait=5.0)
    elapsed = time.monotonic() - t0

    assert status == 400, (
        f"Content-Length: {bad_length!r} — ожидался 400, "
        f"получен статус {status!r} (None = ответ не пришёл)"
    )
    assert elapsed < 3.0, (
        f"отказ на неверном Content-Length должен быть немедленным, "
        f"ушло {elapsed:.1f}s"
    )
    assert registry.get(FORM_ID).status == "pending", (
        "статус формы изменился запросом с неверным Content-Length"
    )
    assert not (config.inputs_dir / f"{FORM_ID}.yaml").exists()


# ── prove_red: тело сверх лимита ────────────────────────────────────────────


def test_oversized_body_rejected_without_drain(form_server):
    """A-07.2: заявлено 10 MiB (≫ лимита 1 MiB), реально отправлен
    1 KiB → 413 за ограниченное время, соединение закрыто (тело не
    вычитывается), submit не записан.

    Baseline (дефект): drain-цикл вычитывает всё заявленное тело —
    поток висит, пока клиент не сдастся (status None) — красный.
    """
    config, registry, port = form_server
    t0 = time.monotonic()
    status, _, closed = _raw_post(
        port, b"x" * 1024, length_header=str(10 * 1024 * 1024), wait=5.0,
    )
    elapsed = time.monotonic() - t0

    assert status == 413, (
        f"тело сверх лимита — ожидался 413, получен статус {status!r} "
        f"(None = ответ не пришёл)"
    )
    assert closed, "соединение после 413 должно быть закрыто (тело не drain)"
    assert elapsed < 3.0, (
        f"413 должен прийти до чтения тела, ушло {elapsed:.1f}s"
    )
    assert registry.get(FORM_ID).status == "pending", (
        "статус формы изменился запросом с телом сверх лимита"
    )
    assert not (config.inputs_dir / f"{FORM_ID}.yaml").exists()


# ── Регрессы ─────────────────────────────────────────────────────────────────


def test_valid_body_still_accepted(form_server):
    """A-07.4: легитимный submit (urlencoded, честный Content-Length)
    проходит как раньше: 200 + YAML на месте + статус submitted."""
    config, registry, port = form_server
    data = b"choice=option1&comment=hi"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/submit/{FORM_ID}", data=data, method="POST",
    )
    resp = urllib.request.urlopen(req)
    assert resp.status == 200

    yaml_file = config.inputs_dir / f"{FORM_ID}.yaml"
    assert yaml_file.exists(), f"YAML не записан: {yaml_file}"
    assert "option1" in yaml_file.read_text()
    assert registry.get(FORM_ID).status == "submitted"


def test_incomplete_body_read_times_out(form_server, monkeypatch):
    """A-07.3: заявлено < лимита, реально отправлена малая часть —
    чтение тела ограничено по времени (monkeypatch на 1 c), по
    истечении — 408 + close, submit не записан.

    Baseline (дефект): таймаута чтения нет — поток ждёт тело до
    срабатывания лимита клиента (status None) — красный.
    """
    import agent_workflow_ui.http_endpoint as ep

    monkeypatch.setattr(ep, "BODY_READ_TIMEOUT", 1.0)
    config, registry, port = form_server
    payload = b"choice=option1"
    t0 = time.monotonic()
    status, _, closed = _raw_post(
        port, payload[:6], length_header=str(len(payload) + 4000), wait=6.0,
    )
    elapsed = time.monotonic() - t0

    assert status == 408, (
        f"неполное тело — ожидался отказ по таймауту (408), "
        f"получен статус {status!r} (None = ответ не пришёл)"
    )
    assert closed, "соединение после таймаута чтения должно быть закрыто"
    assert elapsed < 5.0, (
        f"таймаут чтения не ограничен, ушло {elapsed:.1f}s"
    )
    assert registry.get(FORM_ID).status == "pending", (
        "статус формы изменился неполным телом"
    )
    assert not (config.inputs_dir / f"{FORM_ID}.yaml").exists()


def test_handler_count_is_bounded(form_server, monkeypatch):
    """A-07.4: число активных обработчиков ограничено — перелив получает
    немедленный 503, запрос не обрабатывается, submit не записан.

    Baseline (дефект): ограничения нет (каждому подключению — поток) —
    503 не возвращается — красный.
    """
    import agent_workflow_ui.http_endpoint as ep

    monkeypatch.setattr(ep._BoundedThreadingHTTPServer, "max_concurrent", 2)
    config, registry, port = form_server
    held: list[socket.socket] = []
    try:
        # Два «зависших» клиента: соединение открыто, данных нет —
        # обработчики активны и держатся на чтении request line.
        held = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
        time.sleep(0.5)  # потоки обработчиков стартовали, счётчик обновлён
        status, _, _ = _raw_post(
            port, b"choice=option1", length_header=None, wait=5.0,
        )
    finally:
        for s in held:
            s.close()

    assert status == 503, (
        f"перелив активных обработчиков — ожидался 503, получен {status!r}"
    )
    assert registry.get(FORM_ID).status == "pending", (
        "статус формы изменился запросом при переливе"
    )
    assert not (config.inputs_dir / f"{FORM_ID}.yaml").exists()
