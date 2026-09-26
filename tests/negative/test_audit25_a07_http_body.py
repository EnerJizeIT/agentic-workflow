"""A-07 (аудит 2026-09-25, слой 5): checkpoint-сервер не зависает на теле POST.

Дефект (воспроизведён в аудите): ``do_POST`` в ``awf/plan_checkpoint.py``
преобразует ``Content-Length`` в int без обработки мусора/отрицательного
числа и читает тело ``rfile.read(length)`` без лимита размера и без
таймаута. Неверный заголовок — исключение в потоке (сброс соединения,
traceback); большой или медленно отправляемый запрос — поток и память
удерживаются до тех пор, пока клиент не сдастся. ``ThreadingHTTPServer``
без ограничения одновременных обработчиков.

Инварианты:
1. Неверный или отрицательный ``Content-Length`` → немедленный 400,
   решение не фиксируется.
2. Тело сверх лимита → 413 без вычитывания тела, соединение закрывается,
   решение не фиксируется.
3. Неполное тело не удерживает поток бесконечно: чтение ограничено по
   времени, по истечении — отказ, решение не фиксируется.
4. Допустимое тело обрабатывается как раньше; число активных обработчиков
   ограничено (перелив — немедленный 503, запрос не обрабатывается).

Вклад A-03 (F1): не-ASCII токен — чистый 403 без TypeError в
``secrets.compare_digest`` (сброс соединения + traceback в stderr).

Уровень — реальный one-shot-сервер на свободном порту (port=0, как в
продакшене); запросы — через raw-сокет, потому что urllib не даёт
контроля над ``Content-Length`` (мусор, отрицательное, завышенное) и над
частичной отправкой тела.
"""
from __future__ import annotations

import re
import socket
import time

import pytest

from awf import plan_checkpoint

FORM_TOKEN = "audit25-a07-form-token"


# ── raw-клиент ───────────────────────────────────────────────────────────────


def _response_complete(out: bytes) -> bool:
    """Ответ целиком: заголовки + тело до заявленного Content-Length."""
    head, sep, rest = out.partition(b"\r\n\r\n")
    if not sep:
        return False
    m = re.search(rb"(?im)^content-length:\s*(\d+)", head)
    return m is not None and len(rest) >= int(m.group(1))


def _raw_http(port: int, request: bytes, wait: float) -> tuple[int | None, bytes, bool]:
    """Сырой HTTP-запрос к checkpoint-серверу.

    Возвращает ``(status, ответ, closed)``. ``status`` — None, если сервер
    не прислал полный ответ в пределах ``wait`` (зависание или смерть
    соединения до ответа — до-фикс поведение). ``closed`` — сервер закрыл
    соединение (EOF после ответа; семантика «отказ без drain»).
    """
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


def _raw_post(
    port: int, body: bytes, length_header: str | None, wait: float
) -> tuple[int | None, bytes, bool]:
    """POST на /checkpoint; ``length_header=None`` — честный Content-Length."""
    if length_header is None:
        length_header = str(len(body))
    request = (
        f"POST /checkpoint HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Content-Length: {length_header}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + body
    return _raw_http(port, request, wait)


def _start_server() -> tuple[object, dict, dict]:
    decision: dict = {}
    edited: dict = {}
    server = plan_checkpoint._start_checkpoint_server(
        port=0, decision_holder=decision, edited_holder=edited, token=FORM_TOKEN,
    )
    return server, decision, edited


# ── prove_red: мусорный / отрицательный Content-Length ──────────────────────


@pytest.mark.parametrize("bad_length", ["abc", "-1"])
def test_bad_content_length_is_rejected_quickly(bad_length):
    """A-07.1: ``Content-Length: abc`` / ``-1`` → немедленный 400, решение
    не изменилось, ограниченно по времени.

    Baseline (дефект): "abc" — ValueError в потоке, соединение рвётся без
    ответа (status None); "-1" — ``rfile.read(-1)`` ждёт конца потока
    (status None) — красный.
    """
    server, decision, _ = _start_server()
    port = server.server_address[1]
    try:
        t0 = time.monotonic()
        status, _, _ = _raw_post(
            port,
            b"decision=approve&token=" + FORM_TOKEN.encode(),
            length_header=bad_length,
            wait=5.0,
        )
        elapsed = time.monotonic() - t0
    finally:
        server.shutdown()
        server.server_close()

    assert status == 400, (
        f"Content-Length: {bad_length!r} — ожидался 400, "
        f"получен статус {status!r} (None = ответ не пришёл)"
    )
    assert elapsed < 3.0, (
        f"отказ на неверном Content-Length должен быть немедленным, "
        f"ушло {elapsed:.1f}s"
    )
    assert decision == {}, "решение зафиксировано запросом с неверным Content-Length"


# ── prove_red: тело сверх лимита ────────────────────────────────────────────


def test_oversized_body_is_rejected_without_drain():
    """A-07.2: заявлено 10 MiB (≫ лимита 64 KiB), реально отправлен 1 KiB →
    413 за ограниченное время, решение не изменилось, соединение закрыто
    (тело не вычитывается).

    Baseline (дефект): ``rfile.read(10 MiB)`` ждёт тело, которое не придёт
    (status None) — красный.
    """
    server, decision, _ = _start_server()
    port = server.server_address[1]
    try:
        t0 = time.monotonic()
        status, _, closed = _raw_post(
            port,
            b"x" * 1024,
            length_header=str(10 * 1024 * 1024),
            wait=5.0,
        )
        elapsed = time.monotonic() - t0
    finally:
        server.shutdown()
        server.server_close()

    assert status == 413, (
        f"тело сверх лимита — ожидался 413, получен статус {status!r} "
        f"(None = ответ не пришёл)"
    )
    assert closed, "соединение после 413 должно быть закрыто (тело не drain)"
    assert elapsed < 3.0, (
        f"413 должен прийти до чтения тела, ушло {elapsed:.1f}s"
    )
    assert decision == {}, "решение зафиксировано запросом с телом сверх лимита"


# ── Регрессы ─────────────────────────────────────────────────────────────────


def test_incomplete_body_read_times_out(tmp_path, monkeypatch):
    """A-07.3: заявлено < лимита, реально отправлена малая часть — чтение
    тела ограничено по времени, по истечении — отказ, решение не изменилось.

    Baseline (дефект): таймаута чтения нет — поток ждёт тело до срабатывания
    лимита клиента (status None) — красный.
    """
    monkeypatch.setattr(plan_checkpoint, "CHECKPOINT_BODY_READ_TIMEOUT", 1.0)
    server, decision, _ = _start_server()
    port = server.server_address[1]
    payload = b"decision=approve&token=" + FORM_TOKEN.encode()
    try:
        t0 = time.monotonic()
        status, _, closed = _raw_post(
            port, payload[:64], length_header=str(len(payload) + 4000), wait=6.0,
        )
        elapsed = time.monotonic() - t0
    finally:
        server.shutdown()
        server.server_close()

    assert status == 408, (
        f"неполное тело — ожидался отказ по таймауту (408), "
        f"получен статус {status!r} (None = ответ не пришёл)"
    )
    assert closed, "соединение после таймаута чтения должно быть закрыто"
    assert elapsed < 5.0, (
        f"таймаут чтения не ограничен, ушло {elapsed:.1f}s"
    )
    assert decision == {}, "решение зафиксировано неполным телом"


def test_valid_body_still_accepted():
    """A-07.4: нормальный POST формы (токен, реальное тело) проходит как
    раньше — валидация тела не сломала легитимный путь."""
    server, decision, _ = _start_server()
    port = server.server_address[1]
    try:
        status, _, _ = _raw_post(
            port,
            b"decision=approve&token=" + FORM_TOKEN.encode(),
            length_header=None,
            wait=5.0,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert status == 200, f"валидный POST должен пройти, получен {status!r}"
    assert decision.get("decision") == "approve"


def test_handler_count_is_bounded(tmp_path, monkeypatch):
    """A-07.4: число активных обработчиков ограничено — перелив получает
    немедленный 503, запрос не обрабатывается, решение не изменилось.

    Baseline (дефект): ограничения нет (каждому подключению — поток) —
    503 не возвращается — красный.
    """
    monkeypatch.setattr(
        plan_checkpoint._BoundedThreadingHTTPServer, "max_concurrent", 2
    )
    server, decision, _ = _start_server()
    port = server.server_address[1]
    held: list[socket.socket] = []
    try:
        # Два «зависших» клиента: соединение открыто, данных нет —
        # обработчики активны и держатся на чтении request line.
        held = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
        time.sleep(0.5)  # потоки обработчиков стартовали, счётчик обновлён
        status, _, _ = _raw_post(
            port,
            b"decision=approve&token=" + FORM_TOKEN.encode(),
            length_header=None,
            wait=5.0,
        )
    finally:
        for s in held:
            s.close()
        server.shutdown()
        server.server_close()

    assert status == 503, (
        f"перелив активных обработчиков — ожидался 503, получен {status!r}"
    )
    assert decision == {}, "решение зафиксировано запросом при переливе"


def test_nonascii_token_rejected_clean_403(tmp_path, capsys):
    """A-03 F1: не-ASCII токен — чистый 403, решение не изменилось,
    traceback в stderr нет.

    Baseline (дефект): ``secrets.compare_digest`` бросает TypeError на
    не-ASCII str — соединение рвётся без ответа (status None), traceback
    в stderr — красный.
    """
    server, decision, _ = _start_server()
    port = server.server_address[1]
    try:
        status, _, _ = _raw_post(
            port, b"decision=approve&token=%D0%90", length_header=None, wait=5.0,
        )
    finally:
        server.shutdown()
        server.server_close()

    time.sleep(0.2)  # handler thread: handle_error (traceback) до закрытия
    err = capsys.readouterr().err

    assert status == 403, (
        f"не-ASCII токен — ожидался чистый 403, получен статус {status!r} "
        f"(None = сброс соединения без ответа)"
    )
    assert "Traceback" not in err, (
        f"TypeError в сравнении токена ушёл в stderr:\n{err[-800:]}"
    )
    assert decision == {}, "решение зафиксировано POST с не-ASCII токеном"
