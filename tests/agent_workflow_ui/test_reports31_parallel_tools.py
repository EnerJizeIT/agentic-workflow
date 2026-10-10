"""TODO-0193: parallel awf tool calls must not take the MCP server down.

Incident (awf-bug-20261010-mcp-connection-closed-parallelnye-awf-vyzovy-
dispatch-todo.md, jql, ран B): two awf tools fired in ONE message
(awf_dispatch_todo + awf_todo_update) → «MCP error -32000: Connection
closed», the server process gone, every awf tool dropped until an opencode
restart. The dispatch files landed on disk; the parallel update's append
was lost.

Root mechanism (established with a repro harness, see DONE-TODO-0193.md):

- The server never crashed. On a parallel pair it processes BOTH requests
  and writes BOTH responses to stdout (byte-verified: 2× «Response sent»,
  2× timed pipe writes, both frames received by a raw client). The fault
  log of the incident server shows a clean atexit STOP, no CRASH — the
  process exited because the CLIENT closed the connection.
- What breaks is the pairing: awf_todo_update is much lighter than
  awf_dispatch_todo (no git baseline, fewer writes), so on the baseline
  (handlers run as real parallel threads via asyncio.to_thread) the update
  response arrives BEFORE the dispatch response. A client that consumes
  responses in request order (the incident client's behavior) then hangs
  waiting for the response it already received, gives up on its own
  timeout, and closes the connection — «-32000: Connection closed». The
  server, seeing stdin EOF, exits cleanly.
- The same parallelism also races on shared project state: two
  api.update_todo calls interleave their read-modify-write and one append
  is silently lost (measured: ~40% of pairs on the baseline).

What this file pins (per the TODO invariants):

1. a REAL stdio round-trip: two tools/call requests in flight at the same
   time — a correct (out-of-order-tolerant) client gets both answers, the
   server survives, and EVERY line the server puts on stdout is a
   JSON-RPC frame (invariant 4: the protocol stream is the only thing
   allowed there);
2. the incident client shape (strict request-order consumption): red on
   the baseline — the out-of-order response breaks the pairing and the
   client times out (the «Connection closed» of the incident); green with
   the fix, because mutating tools are serialized and answers come back
   in request order (invariant 1);
3. mutating tools are serialized — parallel awf_todo_update appends never
   lose content (invariant 3; red on the baseline: without the @_mutating
   lock the read-modify-write in api.update_todo interleaves and drops an
   append);
4. a tool error is an answer, not a dead server (invariant 2);
5. a stuck mutation queue is a clean error, not a hang (invariant 3's
   deadlock defense).

The stdio tests launch a real server subprocess (the runtime path of
``__main__.main`` minus the three user-config skill installers, which run
once at startup, copy bundled files into ~/.config/opencode, and are
outside the incident window). The client speaks raw JSON-RPC over pipes —
the minimal stdio client the TODO asks for, and the only one that can
assert on the raw bytes of the protocol stream.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from awf import api

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "agent_workflow_ui" / "src"

#: the thin launcher: __main__.main's runtime path (fault log, config,
#: project dir, jinja env, HTTP thread, create_server().run(stdio)) minus
#: ensure_skill_installed / ensure_supervisor_skill_installed /
#: ensure_agents_md — startup-only user-config copies, no tool-call path.
_SERVER_MAIN = """
import logging
import sys
from pathlib import Path

import agent_workflow_ui as awui
from agent_workflow_ui.config import detect_project_dir, ensure_directories, load
from agent_workflow_ui.fault_log import setup_fault_log
from agent_workflow_ui.http_endpoint import start_http_server
from agent_workflow_ui.render.engine import create_env
from agent_workflow_ui.server import create_server
from agent_workflow_ui.state import (
    get_registry, set_config, set_http_port, set_jinja_env, set_project_dir,
)

setup_fault_log()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
config = load()
ensure_directories(config)
set_config(config)
set_project_dir(detect_project_dir())
env = create_env([
    config.templates_dir,
    str(Path(awui.__file__).parent / "render" / "default_templates"),
])
set_jinja_env(env)
registry = get_registry()
server, port = start_http_server(config, registry)
set_http_port(port)
mcp = create_server()
mcp.run()
server.shutdown()
"""


# ─── fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def project(tmp_git_repo) -> Path:
    """Git repo + .agentic + one not-started TODO for update targets."""
    api.init_project(tmp_git_repo, project_name="Reports31")
    api.dispatch_todo(tmp_git_repo, "# TODO-0001 — parallel target\n\nScratch.\n")
    return tmp_git_repo


class _RawClient:
    """Minimal stdio MCP client: raw JSON-RPC lines over a Popen pipe.

    A dedicated reader thread owns the stdout stream and pushes every raw
    line to a queue — select() on a buffered text stream is a race (data
    can sit in the stream's internal buffer while the fd reports "not
    ready"), and a single reader keeps the raw ``lines`` log intact.
    """

    def __init__(self, proc: subprocess.Popen, project: Path):
        self.proc = proc
        self.project = project
        self._next_id = 0
        self.lines: list[str] = []  # every raw line read from stdout
        self._queue: queue.Queue[str | Exception] = queue.Queue()
        self._pending: dict[int, dict] = {}  # out-of-order responses
        self._reader = threading.Thread(
            target=self._read_loop, daemon=True, name="reports31-stdout-reader"
        )
        self._reader.start()

    def _read_loop(self) -> None:
        try:
            for raw in self.proc.stdout:
                line = raw.rstrip("\n")
                self.lines.append(line)
                self._queue.put(line)
        except Exception as e:  # stream died mid-read
            self._queue.put(e)
        finally:
            self._queue.put(None)  # EOF marker

    def send_request(self, method: str, params: dict | None = None) -> int:
        self._next_id += 1
        msg = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)
        return self._next_id

    def send_notification(self, method: str) -> None:
        self._write({"jsonrpc": "2.0", "method": method})

    def _write(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def read_line(self, timeout: float = 30.0) -> str:
        """One raw line from the server's stdout (blocking, with timeout)."""
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    "server process exited: rc="
                    f"{self.proc.returncode}\nstderr tail:\n"
                    f"{self.proc.stderr.read()[-2000:] if self.proc.stderr else ''}"
                ) from None
            raise TimeoutError("no line from server within timeout") from None
        if item is None:
            raise RuntimeError("server stdout EOF (connection closed by the server)")
        if isinstance(item, Exception):
            raise RuntimeError(f"server stdout read failed: {item}") from item
        return item

    def _frame(self, req_id: int, timeout: float) -> dict:
        """Next line as a JSON-RPC frame; out-of-order ids are buffered."""
        deadline = time.monotonic() + timeout
        while True:
            if req_id in self._pending:
                return self._pending.pop(req_id)
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no response for request {req_id}")
            line = self.read_line(timeout=left)
            assert line.startswith("{"), (
                f"NON-PROTOCOL line on the server stdout (stream corrupted):"
                f" {line[:200]!r}"
            )
            msg = json.loads(line)
            assert msg.get("jsonrpc") == "2.0", f"not a JSON-RPC frame: {line[:200]!r}"
            if msg.get("id") is not None and msg["id"] != req_id:
                self._pending[msg["id"]] = msg
                continue
            assert "result" in msg or "error" in msg, f"no result/error: {line[:200]!r}"
            return msg

    def wait_for_response(self, req_id: int, timeout: float = 60.0) -> dict:
        """A correct client: answers may arrive in any order (JSON-RPC
        allows it) — buffer the out-of-order ones, return the wanted one."""
        return self._frame(req_id, timeout)

    def wait_in_order(self, expected_id: int, timeout: float = 60.0) -> dict:
        """The incident client shape: the NEXT line must be the response
        to the given request. An out-of-order arrival is a failure — that
        pairing break is what hung the incident client and closed the
        connection."""
        deadline = time.monotonic() + timeout
        line = self.read_line(timeout=deadline - time.monotonic())
        assert line.startswith("{"), (
            f"NON-PROTOCOL line on the server stdout (stream corrupted):"
            f" {line[:200]!r}"
        )
        msg = json.loads(line)
        assert msg.get("jsonrpc") == "2.0", f"not a JSON-RPC frame: {line[:200]!r}"
        assert msg.get("id") == expected_id, (
            f"OUT-OF-ORDER response: expected id={expected_id} (first "
            f"request), got id={msg.get('id')} — an in-order client can "
            f"no longer pair the answers (the 2026-10-10 incident)"
        )
        assert "result" in msg or "error" in msg, f"no result/error: {line[:200]!r}"
        return msg

    def assert_all_lines_are_frames(self) -> None:
        for line in self.lines:
            msg = json.loads(line)  # raises on any non-JSON garbage
            assert msg.get("jsonrpc") == "2.0", f"non-JSON-RPC line: {line[:200]!r}"

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
            self.proc.wait(timeout=5)


@pytest.fixture
def stdio_server(project, tmp_path):
    """Real server subprocess on stdio + raw client. Yields the client."""
    server_main = tmp_path / "server_main.py"
    server_main.write_text(_SERVER_MAIN, encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            # hermetic: everything under the test's tmp tree
            "AWF_MCP_LOG": str(tmp_path / "mcp-server.log"),
            "AWF_INPUTS_DIR": str(tmp_path / "inputs"),
            "AWF_TEMPLATES_DIR": str(tmp_path / "templates"),
            "AWF_DASHBOARDS_DIR": str(tmp_path / "dashboards"),
            "AWF_TEMP_DIR": str(tmp_path / "runtime"),
            "AWF_HTTP_PORT": "0",
            "AWF_PLAN_CHECKPOINT": "false",
            "AWF_DISABLE_FORM_PERSIST": "1",
            # import the working tree, never a stale installed copy
            "PYTHONPATH": str(SRC_DIR) + os.pathsep + str(REPO_ROOT),
        }
    )
    stderr = tmp_path / "server.stderr"
    with stderr.open("w") as err:
        proc = subprocess.Popen(
            [sys.executable, str(server_main)],
            cwd=str(project),  # detect_project_dir() finds the .agentic/
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err,
            text=True,
            env=env,
        )
    client = _RawClient(proc, project)
    init_id = client.send_request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "reports31-raw", "version": "0"},
        },
    )
    client.wait_for_response(init_id, timeout=30)
    client.send_notification("notifications/initialized")
    try:
        yield client
    finally:
        client.close()


def _call_result(msg: dict) -> dict:
    """The tool's structured result dict out of a tools/call response."""
    assert "error" not in msg, f"JSON-RPC error: {msg['error']}"
    result = msg["result"]
    text = result["content"][0]["text"]
    return json.loads(text)


# ─── invariant 3: mutating tools are serialized (red on baseline) ───────


class TestMutationSerialization:
    """Parallel mutating calls queue under one lock — no lost writes."""

    def test_parallel_updates_do_not_lose_content(self, project):
        """Two parallel awf_todo_update appends per round: nothing lost.

        api.update_todo is a read-modify-write (read md → backup → write
        md). Without the @_mutating lock, the two asyncio.to_thread
        workers interleave: both read the same old content, the second
        write drops the first append. The switch interval is tightened so
        a thread switch lands inside the read→write window — the race the
        2026-10-10 incident hit in production, on a deterministic window.
        """
        from agent_workflow_ui.tools.awf import awf_todo_update

        md = project / ".agentic" / "inbox" / "TODO-0001.md"
        lost: list[str] = []

        async def round_(i: int) -> None:
            res = await asyncio.gather(
                awf_todo_update(
                    todo_id="TODO-0001",
                    project_dir=str(project),
                    append=f"marker-{i}-A\n",
                ),
                awf_todo_update(
                    todo_id="TODO-0001",
                    project_dir=str(project),
                    append=f"marker-{i}-B\n",
                ),
            )
            assert all(r["status"] == "ok" for r in res)
            text = md.read_text(encoding="utf-8")
            for suffix in ("A", "B"):
                if f"marker-{i}-{suffix}" not in text:
                    lost.append(f"marker-{i}-{suffix}")

        async def _all_rounds() -> None:
            for i in range(30):
                await round_(i)

        old = sys.getswitchinterval()
        sys.setswitchinterval(1e-4)  # force switches inside the RMW window
        try:
            asyncio.run(_all_rounds())
        finally:
            sys.setswitchinterval(old)

        assert not lost, f"parallel updates lost content: {lost[:10]}"

    def test_mutation_lock_timeout_is_clean_error(self, project, monkeypatch):
        """A queue that cannot drain answers with an error, not a hang."""
        import agent_workflow_ui.tools.awf as awf_tools

        monkeypatch.setattr(awf_tools, "MUTATION_LOCK_TIMEOUT", 0.05)

        async def scenario() -> dict:
            lock = awf_tools._get_mutation_lock()
            await lock.acquire()  # hold the lock from this test's task
            try:
                return await asyncio.wait_for(
                    awf_tools.awf_todo_update(
                        todo_id="TODO-0001",
                        project_dir=str(project),
                        append="should not land\n",
                    ),
                    timeout=5,
                )
            finally:
                lock.release()

        result = asyncio.run(scenario())
        assert result["status"] == "error"
        assert "still running" in result["error"]
        # the refused call must not have written anything
        text = (project / ".agentic" / "inbox" / "TODO-0001.md").read_text(
            encoding="utf-8"
        )
        assert "should not land" not in text


# ─── invariants 1/2/4: real stdio server, two calls in flight ───────────


class TestStdioServerRobustness:
    """The server subprocess survives parallel calls; stdout stays clean."""

    def _send_pair(self, client) -> tuple[int, int]:
        dispatch_id = client.send_request(
            "tools/call",
            {
                "name": "awf_dispatch_todo",
                "arguments": {
                    "project_dir": str(client.project),
                    "content": "# TODO-0002 — parallel dispatch (0193)\n\nScratch.\n",
                },
            },
        )
        # back-to-back: both requests are in flight before any answer
        update_id = client.send_request(
            "tools/call",
            {
                "name": "awf_todo_update",
                "arguments": {
                    "todo_id": "TODO-0001",
                    "project_dir": str(client.project),
                    "append": "## Решения владельца\n\nrepro 0193 parallel update\n",
                },
            },
        )
        return dispatch_id, update_id

    def test_parallel_calls_both_answer_and_server_survives(self, stdio_server):
        """A correct client (out-of-order-tolerant) gets both answers,
        the server is still serving after, and every stdout line is a
        protocol frame (invariants 1/4)."""
        client = stdio_server
        dispatch_id, update_id = self._send_pair(client)

        dispatch_res = _call_result(client.wait_for_response(dispatch_id))
        update_res = _call_result(client.wait_for_response(update_id))
        assert dispatch_res["status"] == "ok", dispatch_res
        assert update_res["status"] == "ok", update_res

        # the server must still be able to answer after the parallel pair
        alive_id = client.send_request(
            "tools/call",
            {"name": "awf_status", "arguments": {"project_dir": str(client.project)}},
        )
        alive = _call_result(client.wait_for_response(alive_id))
        assert alive["status"] == "ok", alive

        client.assert_all_lines_are_frames()

        # both side effects on disk (the incident lost the update)
        inbox = client.project / ".agentic" / "inbox"
        assert (inbox / "TODO-0002.md").is_file()
        assert "repro 0193 parallel update" in (
            inbox / "TODO-0001.md"
        ).read_text(encoding="utf-8")

    def test_in_order_client_survives_parallel_pair(self, stdio_server):
        """The incident's client shape: responses are consumed strictly in
        request order (the next line MUST answer the first request).

        Red on the baseline: the handlers run as parallel threads and the
        lighter awf_todo_update finishes before awf_dispatch_todo, so the
        update response arrives first — the in-order client can no longer
        pair the answers, hangs on its own timeout and would close the
        connection (the «-32000: Connection closed» of 2026-10-10; the
        server itself stayed healthy and exited cleanly on the client's
        EOF).

        Green with the fix: mutating tools are serialized in request
        order, so the dispatch response comes back first and the pairing
        holds.
        """
        client = stdio_server
        dispatch_id, update_id = self._send_pair(client)

        first = _call_result(client.wait_in_order(dispatch_id))
        second = _call_result(client.wait_in_order(update_id))
        assert first["status"] == "ok", first
        assert second["status"] == "ok", second

        # still serving after the pair
        alive_id = client.send_request(
            "tools/call",
            {"name": "awf_status", "arguments": {"project_dir": str(client.project)}},
        )
        assert _call_result(client.wait_in_order(alive_id))["status"] == "ok"

        client.assert_all_lines_are_frames()

    def test_tool_error_keeps_server_alive(self, stdio_server):
        """A failing tool call is an answer (status: error), not a dead
        server: the very next call must succeed (invariant 2)."""
        client = stdio_server
        bad_id = client.send_request(
            "tools/call",
            {
                "name": "awf_status",
                "arguments": {"project_dir": "/definitely/not/a/project"},
            },
        )
        bad = _call_result(client.wait_for_response(bad_id))
        assert bad["status"] == "error", bad

        good_id = client.send_request(
            "tools/call",
            {"name": "awf_status", "arguments": {"project_dir": str(client.project)}},
        )
        good = _call_result(client.wait_for_response(good_id))
        assert good["status"] == "ok", good
        client.assert_all_lines_are_frames()
