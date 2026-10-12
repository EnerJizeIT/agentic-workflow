"""REPORTS33 (TODO-0210): дашборд в одном окне + вкладки забегов.

Источник — запрос владельца 12.10: (1) «на каждом забеге открывается
новый дашборд — стоит в том же окне»; (2) «вкладки для разных забегов,
чтобы переключаться к статистике и истории по завершённым».

Инварианты:
1. Дедуп открытия (одно окно). _open_dashboard_sync пишет маркер
   .agentic/context/dashboard_opened (ts + url) при реальном открытии
   браузера. Повторное открытие (явный тул И авто-открытие запуска),
   пока маркер свеж (<= 600 c) и url маркера всё ещё обслуживается,
   НЕ зовёт webbrowser — возвращает already_open=True + url. Истёкший
   или удалённый маркер, а также url другого сервера — открывают снова.
   Маркер живёт в runtime-каталоге context/ и не попадает в коммиты.
2. /api/runs: активный забег (position/current) + завершённые
   (RUN-REPORT-*.md из outbox: дата, причина, прогресс очереди),
   новые — первыми. Только чтение.
3. /api/run-report/<имя>: содержимое отчёта (name + content + html).
   Чужие имена и ../ traversal отвергаются — путь не уходит за
   пределы .agentic/outbox.
4. Рендер страницы дашборда несёт бар вкладок (id="runbar") и панель
   отчёта (id="pane-run-report") с JS-клиентами обоих эндпоинтов;
   live-вид и автообновление (poll /api/state) не меняются.
"""
from __future__ import annotations

import asyncio
import json
import socket
import time
from pathlib import Path

import pytest
from agent_workflow_ui.tools import awf

from awf import api, run_state

REPORT_1 = "RUN-REPORT-20260101-120000.md"
REPORT_2 = "RUN-REPORT-20260102-083000.md"

BODY_1 = (
    "# Run report (20260101-120000)\n"
    "\n"
    "**Reason:** test reason one\n"
    "**Queue:** TODO-0001, TODO-0002 — 2/2 done\n"
    "**Completed:** TODO-0001, TODO-0002\n"
)
BODY_2 = (
    "# Run report (20260102-083000)\n"
    "\n"
    "**Reason:** test reason two\n"
    "**Queue:** TODO-0003 — 1/1 done\n"
    "**Completed:** TODO-0003\n"
)


@pytest.fixture
def project(tmp_git_repo) -> Path:
    repo = tmp_git_repo
    api.init_project(repo, project_name="DashTabs")
    pipes = repo / ".agentic" / "pipelines"
    pipes.mkdir(parents=True, exist_ok=True)
    (pipes / "default.yaml").write_text(
        'name: "default"\n'
        'stages:\n'
        '  - name: "plan"\n    role: "supervisor"\n'
        '  - name: "worker"\n    role: "worker"\n'
        '    on_blocked: "escalate"\n    max_retries: 3\n'
        '  - name: "verify"\n    role: "supervisor"\n'
        '    on_approved: "commit_and_next"\n    on_rejected: "replan"\n',
        encoding="utf-8",
    )
    roles = repo / ".agentic" / "roles"
    roles.mkdir(parents=True, exist_ok=True)
    (roles / "worker.md").write_text("# Worker\nExecute the task.\n", encoding="utf-8")
    return repo


@pytest.fixture
def live_port(project: Path):
    """A real listener on loopback — _port_alive is a plain TCP connect."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    yield port
    sock.close()


@pytest.fixture
def port_file(project: Path, live_port: int) -> int:
    f = project / ".agentic" / "state" / "dashboard_port"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(str(live_port), encoding="utf-8")
    return live_port


def _open_marker(project: Path) -> Path:
    return project / ".agentic" / "context" / "dashboard_opened"


def _write_open_marker(project: Path, url: str, ts: float | None = None) -> None:
    f = _open_marker(project)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"ts": ts if ts is not None else time.time(), "url": url}),
                 encoding="utf-8")


# ─── (a)/(b) open dedup: one window per project ────────────────────────────


class TestOpenDedup:
    def test_first_open_opens_and_writes_marker(self, project, port_file, monkeypatch):
        """Первое открытие: браузер вызывается, маркер пишется (ts + url)."""
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        url = f"http://127.0.0.1:{port_file}"

        result = awf._open_dashboard_sync(project, wait=False)

        assert result["opened"] is True
        assert result["url"] == url
        assert opened == [url]
        marker = json.loads(_open_marker(project).read_text(encoding="utf-8"))
        assert marker["url"] == url
        assert abs(time.time() - marker["ts"]) < 60, "marker ts implausible"

    def test_second_open_within_window_is_already_open(self, project, port_file, monkeypatch):
        """(a) второе открытие в пределах окна: уже открыто, браузер
        не вызывается второй раз."""
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        url = f"http://127.0.0.1:{port_file}"

        first = awf._open_dashboard_sync(project, wait=False)
        second = awf._open_dashboard_sync(project, wait=False)

        assert first["opened"] is True
        assert second["already_open"] is True
        assert second["opened"] is False
        assert second["url"] == url
        assert opened == [url], "the browser was called twice — a second window"

    def test_open_after_marker_expiry_reopens(self, project, port_file, monkeypatch):
        """(b) маркер истёк (> 600 c) — окно открывается снова."""
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        _write_open_marker(project, f"http://127.0.0.1:{port_file}", ts=time.time() - 601)

        result = awf._open_dashboard_sync(project, wait=False)

        assert result["opened"] is True
        assert "already_open" not in result or result.get("already_open") is not True
        assert len(opened) == 1

    def test_open_after_marker_cleared_reopens(self, project, port_file, monkeypatch):
        """(b) маркер удалён — открытие как в первый раз."""
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        _write_open_marker(project, f"http://127.0.0.1:{port_file}")
        _open_marker(project).unlink()

        result = awf._open_dashboard_sync(project, wait=False)

        assert result["opened"] is True
        assert opened == [f"http://127.0.0.1:{port_file}"]

    def test_fresh_marker_other_url_reopens(self, project, port_file, monkeypatch):
        """Свежий маркер на ЧУЖОМ url (сервер ушёл на новый порт) —
        старое окно мёртвое, новое обязательно."""
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        _write_open_marker(project, "http://127.0.0.1:1")

        result = awf._open_dashboard_sync(project, wait=False)

        assert result["opened"] is True
        assert opened == [f"http://127.0.0.1:{port_file}"]

    def test_corrupt_marker_opens(self, project, port_file, monkeypatch):
        """Мусорный маркер — не сигнал «уже открыто» (та же политика,
        что у boot-маркера: битый файл не закрывает дашборд)."""
        f = _open_marker(project)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("not-json", encoding="utf-8")
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

        result = awf._open_dashboard_sync(project, wait=False)

        assert result["opened"] is True
        assert opened == [f"http://127.0.0.1:{port_file}"]

    def test_tool_reports_already_open(self, project, port_file, monkeypatch):
        """МCP-тул: повтор в пределах окна — status ok + already_open,
        не ошибка (дашборд доступен по url)."""
        monkeypatch.setattr("webbrowser.open", lambda url: True)
        url = f"http://127.0.0.1:{port_file}"

        first = asyncio.run(awf.awf_open_pipeline_dashboard(project_dir=str(project)))
        second = asyncio.run(awf.awf_open_pipeline_dashboard(project_dir=str(project)))

        assert first["status"] == "ok" and first["opened"] is True
        assert second["status"] == "ok"
        assert second["opened"] is False
        assert second["already_open"] is True
        assert second["url"] == url


# ─── (c)/(d) server endpoints: /api/runs + /api/run-report/<name> ──────────


def _get(port: int, path: str) -> tuple[int, bytes]:
    """Raw-сокет GET: путь уходит в сервер ровно как передан (traversal
    без нормализации клиентом)."""
    req = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(req)
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    out = b"".join(chunks)
    head, _, body = out.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n", 1)[0].split()[1])
    return status, body


@pytest.fixture
def outbox_reports(project: Path) -> Path:
    outbox = project / ".agentic" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / REPORT_1).write_text(BODY_1, encoding="utf-8")
    (outbox / REPORT_2).write_text(BODY_2, encoding="utf-8")
    (outbox / "DONE-TODO-0001.md").write_text("not a run report\n", encoding="utf-8")
    return outbox


@pytest.fixture
def server(project: Path):
    from awf.api.dashboard_server import start_dashboard_server, stop_dashboard_server

    port, srv = start_dashboard_server(project, port=0)
    yield port
    stop_dashboard_server(srv)


def _seed_active_run(project: Path) -> None:
    run_state.write_run(
        project,
        active=True,
        index=0,
        current="TODO-0001",
        queue=[
            {"todo_id": "TODO-0001", "pipeline": ""},
            {"todo_id": "TODO-0002", "pipeline": ""},
        ],
    )


class TestRunsEndpoints:
    def test_api_runs_active_and_history(self, project, outbox_reports, server):
        """(c) /api/runs: активный забег + завершённые (новые первыми,
        с датой и сводкой)."""
        _seed_active_run(project)
        status, body = _get(server, "/api/runs")
        assert status == 200
        data = json.loads(body.decode("utf-8"))

        assert data["active"] is not None
        assert data["active"]["current"] == "TODO-0001"
        assert data["active"]["position"]

        hist = data["history"]
        assert [h["name"] for h in hist] == [REPORT_2, REPORT_1]
        assert hist[0]["date"].startswith("2026-01-02")
        assert hist[0]["reason"] == "test reason two"
        assert "2/2" in hist[1]["queue"] or "1/1" in hist[0]["queue"]

    def test_api_runs_without_run_state(self, project, outbox_reports, server):
        """(c) без активного забега active=None, история на месте."""
        status, body = _get(server, "/api/runs")
        assert status == 200
        data = json.loads(body.decode("utf-8"))
        assert data["active"] is None
        assert [h["name"] for h in data["history"]] == [REPORT_2, REPORT_1]

    def test_api_run_report_returns_content(self, project, outbox_reports, server):
        """(d) /api/run-report/<имя> отдаёт содержимое отчёта."""
        status, body = _get(server, f"/api/run-report/{REPORT_1}")
        assert status == 200
        data = json.loads(body.decode("utf-8"))
        assert data["name"] == REPORT_1
        assert data["content"] == BODY_1

    def test_api_run_report_traversal_refused(self, project, outbox_reports, server):
        """(d) ../ traversal не уходит за пределы outbox (raw-запрос:
        клиент не нормализует путь)."""
        (project / ".agentic" / "state").mkdir(parents=True, exist_ok=True)
        secret = "TOP-SECRET-OUTBOX-ESCAPE"
        (project / ".agentic" / "state" / "secret.md").write_text(secret, encoding="utf-8")

        for path in ("/api/run-report/../../state/secret.md",
                     "/api/run-report/%2e%2e%2fstate%2fsecret.md"):
            status, body = _get(server, path)
            assert status == 404, f"traversal served: {path}"
            assert secret not in body.decode("utf-8", errors="replace")

    def test_api_run_report_foreign_name_refused(self, project, outbox_reports, server):
        """(d) чужой файл из outbox (не RUN-REPORT) — не отдаётся."""
        status, body = _get(server, "/api/run-report/DONE-TODO-0001.md")
        assert status == 404
        assert b"not a run report" not in body

    def test_api_run_report_missing_refused(self, project, outbox_reports, server):
        status, _ = _get(server, "/api/run-report/RUN-REPORT-19990101-000000.md")
        assert status == 404


# ─── (e) rendered page carries the run tab bar ─────────────────────────────


class TestTemplateRunBar:
    def test_rendered_page_has_run_bar_and_pane(self, project):
        """(e) рендер страницы: бар вкладок + панель отчёта + JS-клиенты
        обоих эндпоинтов (live-вид не теряет /api/state poll)."""
        from awf.api.dashboard import generate_dashboard

        path = generate_dashboard(project)
        assert path is not None, "dashboard generation failed"
        html = path.read_text(encoding="utf-8")

        assert 'id="runbar"' in html
        assert 'id="pane-run-report"' in html
        assert "/api/runs" in html
        assert "/api/run-report/" in html
        assert "fetch('/api/state'" in html, "live view lost its /api/state poll"


# ─── QA findings (agent-qa-review) ──────────────────────────────────────────


class TestQAFindings:
    def test_qa_f1_content_tabbar_restored_on_return_to_live(self, project):
        """QA F1 (требование 4: вкладки не ломают live-вид): вид отчёта
        скрывает контент-таббар (.content > .tabs); возврат в live-вид
        обязан его вернуть. Сейчас бар скрывается один раз (selectRunTab,
        report-ветка) и нигде не показывается обратно: после «отчёт →
        ● Активный» кнопки Agent Chat / Итерация / События пропадают,
        панель застревает на той, что была активна, до перезагрузки
        страницы. Фикс = показать бар обратно в live-ветке (или в
        switchTab)."""
        import re as _re

        from awf.api.dashboard import generate_dashboard

        html = generate_dashboard(project).read_text(encoding="utf-8")
        selector = "document.querySelector('.content > .tabs')"
        n = html.count(selector)
        m = _re.search(r"if \(id === 'live'\) \{(.*?)\n  \}", html, flags=_re.S)
        live_body = m.group(1) if m else ""
        restored_in_live = (
            "style.display = ''" in live_body and ".tabs" in live_body
        )
        assert n >= 2 or restored_in_live, (
            "QA F1: контент-таббар (.content > .tabs) скрыт видом отчёта, "
            "но при возврате в live-вид не восстанавливается — переключение "
            "панелей (Agent Chat / Итерация / События) не работает до "
            "перезагрузки страницы"
        )
