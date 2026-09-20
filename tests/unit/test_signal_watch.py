"""U6a/U6c tests for signal_watch: model-endpoint preflight + no-output watchdog.

Plus unit tests for the awf._net helpers (classifier, endpoint resolution,
log tail) and one bounded real-network smoke test (unreachable URL).
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from awf import _net
from awf.signal_watch import run_subprocess_until_signal


class _Clock:
    """Fake monotonic clock advanced by the fake sleep (below)."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def monotonic(self) -> float:
        return self.t


def _install_fake_time(monkeypatch, clock: _Clock) -> list[float]:
    """Monkeypatch time.monotonic + time.sleep; sleeps advance the clock.

    Returns the list of sleep durations (for backoff assertions).
    """
    sleeps: list[float] = []

    def fake_sleep(dt: float) -> None:
        sleeps.append(dt)
        clock.t += dt

    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", fake_sleep)
    return sleeps


class _InstantPopen:
    """Popen fake that exits immediately; records that a spawn happened."""

    def __init__(self, cmd, cwd=None, **kwargs):
        type(self).spawned = True
        self.cmd = cmd
        self.returncode = 0

    spawned: bool = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass

    def terminate(self):
        pass


class _HungPopen:
    """Popen fake that never exits (watchdog target). Never a real pid."""

    def __init__(self, cmd, cwd=None, **kwargs):
        type(self).spawned = True
        type(self).killed = False
        self.cmd = cmd
        self.returncode = None
        self.pid = 999999999  # getpgid() must fail for this — never killpg

    spawned: bool = False
    killed: bool = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        type(self).killed = True

    def terminate(self):
        type(self).killed = True


def _write_opencode_json(path: Path, providers: dict) -> Path:
    path.write_text(json.dumps({"provider": providers}), encoding="utf-8")
    return path


# ─── Part A: preflight endpoint ────────────────────────────────────────────


class TestPreflight:

    MODEL_CMD = ["fake-opencode", "run", "--model", "prov/m", "--", "prompt"]
    OC_PROV = {"prov": {"options": {"baseURL": "http://127.0.0.1:1/v1"}}}

    def test_dead_endpoint_not_spawned_until_up(
        self, tmp_path, monkeypatch
    ) -> None:
        """Unreachable N times → no spawn, backoff waits; up → spawn."""
        oc = _write_opencode_json(tmp_path / "opencode.json", self.OC_PROV)
        clock = _Clock()
        sleeps = _install_fake_time(monkeypatch, clock)
        _InstantPopen.spawned = False
        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _InstantPopen)

        calls = {"n": 0}

        def reachable(url, timeout=_net.PREFLIGHT_HTTP_TIMEOUT):
            calls["n"] += 1
            return calls["n"] >= 5  # dead for 4 checks, up on the 5th

        monkeypatch.setattr(_net, "endpoint_reachable", reachable)

        result = run_subprocess_until_signal(
            list(self.MODEL_CMD), cwd=tmp_path,
            preflight_timeout=65, opencode_config=oc,
        )

        assert result.returncode == 0
        assert _InstantPopen.spawned is True
        assert calls["n"] == 5
        # backoff 5 → 10 → 20 → 30 (cap: without it the 4th would be 60)
        assert sleeps == [5, 10, 20, 30]

    def test_dead_endpoint_budget_exhausted_timeout(
        self, tmp_path, monkeypatch
    ) -> None:
        """Always unreachable → TimeoutError, worker NEVER spawned."""
        oc = _write_opencode_json(tmp_path / "opencode.json", self.OC_PROV)
        _install_fake_time(monkeypatch, _Clock())
        _InstantPopen.spawned = False
        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _InstantPopen)

        calls = {"n": 0}
        monkeypatch.setattr(
            _net, "endpoint_reachable",
            lambda url, timeout=_net.PREFLIGHT_HTTP_TIMEOUT: calls.__setitem__("n", calls["n"] + 1) or False,
        )

        with pytest.raises(TimeoutError, match="unreachable"):
            run_subprocess_until_signal(
                list(self.MODEL_CMD), cwd=tmp_path,
                preflight_timeout=10, opencode_config=oc,
            )

        assert _InstantPopen.spawned is False
        assert calls["n"] == 3  # check, backoff, check, backoff, check, out of budget

    def test_no_model_flag_skips_preflight(self, tmp_path, monkeypatch) -> None:
        """No --model in cmd → no endpoint probe at all."""
        oc = _write_opencode_json(tmp_path / "opencode.json", self.OC_PROV)
        _install_fake_time(monkeypatch, _Clock())
        _InstantPopen.spawned = False
        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _InstantPopen)

        def boom(*a, **kw):
            raise AssertionError("endpoint_reachable must not be called")

        monkeypatch.setattr(_net, "endpoint_reachable", boom)

        result = run_subprocess_until_signal(
            ["fake-opencode", "run", "--", "prompt"], cwd=tmp_path,
            preflight_timeout=10, opencode_config=oc,
        )

        assert result.returncode == 0
        assert _InstantPopen.spawned is True

    def test_cloud_provider_no_baseurl_skips_preflight(
        self, tmp_path, monkeypatch
    ) -> None:
        """Provider without options.baseURL (cloud) → preflight skipped."""
        oc = _write_opencode_json(
            tmp_path / "opencode.json", {"prov": {"models": {"m": {}}}},
        )
        _install_fake_time(monkeypatch, _Clock())
        _InstantPopen.spawned = False
        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _InstantPopen)

        def boom(*a, **kw):
            raise AssertionError("endpoint_reachable must not be called")

        monkeypatch.setattr(_net, "endpoint_reachable", boom)

        result = run_subprocess_until_signal(
            list(self.MODEL_CMD), cwd=tmp_path,
            preflight_timeout=10, opencode_config=oc,
        )

        assert result.returncode == 0
        assert _InstantPopen.spawned is True

    def test_preflight_timeout_zero_disables(self, tmp_path, monkeypatch) -> None:
        """preflight_timeout=0 → no probe even with --model."""
        oc = _write_opencode_json(tmp_path / "opencode.json", self.OC_PROV)
        _install_fake_time(monkeypatch, _Clock())
        _InstantPopen.spawned = False
        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _InstantPopen)

        def boom(*a, **kw):
            raise AssertionError("endpoint_reachable must not be called")

        monkeypatch.setattr(_net, "endpoint_reachable", boom)

        result = run_subprocess_until_signal(
            list(self.MODEL_CMD), cwd=tmp_path,
            preflight_timeout=0, opencode_config=oc,
        )

        assert result.returncode == 0
        assert _InstantPopen.spawned is True


class TestModelEndpointUrl:

    def test_resolves_base_url(self, tmp_path):
        oc = _write_opencode_json(
            tmp_path / "opencode.json",
            {"prov": {"options": {"baseURL": "http://127.0.0.1:1/v1"}}},
        )
        assert (
            _net.model_endpoint_url("prov/m", oc) == "http://127.0.0.1:1/v1"
        )

    def test_unknown_provider_none(self, tmp_path):
        oc = _write_opencode_json(
            tmp_path / "opencode.json",
            {"prov": {"options": {"baseURL": "http://x/v1"}}},
        )
        assert _net.model_endpoint_url("other/m", oc) is None

    def test_provider_without_baseurl_none(self, tmp_path):
        oc = _write_opencode_json(tmp_path / "opencode.json", {"prov": {}})
        assert _net.model_endpoint_url("prov/m", oc) is None

    def test_bare_model_none(self, tmp_path):
        oc = _write_opencode_json(
            tmp_path / "opencode.json",
            {"prov": {"options": {"baseURL": "http://x/v1"}}},
        )
        assert _net.model_endpoint_url("just-model", oc) is None

    def test_missing_file_none(self, tmp_path):
        assert _net.model_endpoint_url("prov/m", tmp_path / "absent.json") is None

    def test_corrupt_file_none(self, tmp_path):
        oc = tmp_path / "opencode.json"
        oc.write_text("{not json", encoding="utf-8")
        assert _net.model_endpoint_url("prov/m", oc) is None


class TestParseModelFromCmd:

    def test_finds_model_flag(self):
        assert (
            _net.parse_model_from_cmd(
                ["opencode", "run", "--model", "prov/m", "--", "p"]
            )
            == "prov/m"
        )

    def test_no_model_flag(self):
        assert _net.parse_model_from_cmd(["opencode", "run", "--", "p"]) is None

    def test_model_flag_without_value(self):
        assert _net.parse_model_from_cmd(["opencode", "run", "--model"]) is None


# ─── Part C: watchdog ──────────────────────────────────────────────────────


class TestWatchdog:

    def test_stale_log_kills_worker_and_raises(self, tmp_path, monkeypatch):
        """Hung worker + stale log mtime → process tree killed, TimeoutError."""
        _HungPopen.spawned = False
        _HungPopen.killed = False
        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _HungPopen)

        with pytest.raises(TimeoutError, match="no output"):
            run_subprocess_until_signal(
                ["hung-agent-worker"], cwd=tmp_path,
                logs_dir=tmp_path,
                no_output_timeout=0.01,  # real mtime, real poll interval (3s)
            )

        assert _HungPopen.spawned is True
        assert _HungPopen.killed is True
        # the watchdog log line landed in the awf log
        log_file = tmp_path / "orchestrator.log"
        assert log_file.is_file()
        assert "U6c" in log_file.read_text(encoding="utf-8")

    def test_fresh_log_does_not_fire(self, tmp_path, monkeypatch):
        """Fresh log mtime → watchdog stays quiet, worker exits naturally."""
        exits = {"n": 0}

        class _DelayedExitPopen:
            spawned = False
            killed = False

            def __init__(self, cmd, cwd=None, **kwargs):
                type(self).spawned = True
                self.cmd = cmd
                self.pid = 999999999

            def poll(self):
                exits["n"] += 1
                return 0 if exits["n"] >= 3 else None

            def wait(self, timeout=None):
                return 0

            def kill(self):
                type(self).killed = True

            def terminate(self):
                type(self).killed = True

        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _DelayedExitPopen)
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        result = run_subprocess_until_signal(
            ["fresh-agent-worker"], cwd=tmp_path,
            logs_dir=tmp_path,
            no_output_timeout=900,
        )

        assert result.returncode == 0
        assert _DelayedExitPopen.killed is False

    def test_zero_disables_watchdog(self, tmp_path, monkeypatch):
        """no_output_timeout=0 → watchdog off even for a silent worker."""
        exits = {"n": 0}

        class _DelayedExitPopen:
            spawned = False
            killed = False

            def __init__(self, cmd, cwd=None, **kwargs):
                type(self).spawned = True
                self.cmd = cmd
                self.pid = 999999999

            def poll(self):
                exits["n"] += 1
                return 0 if exits["n"] >= 2 else None

            def wait(self, timeout=None):
                return 0

            def kill(self):
                type(self).killed = True

            def terminate(self):
                type(self).killed = True

        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _DelayedExitPopen)
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        result = run_subprocess_until_signal(
            ["quiet-agent-worker"], cwd=tmp_path,
            logs_dir=tmp_path,
            no_output_timeout=0,
        )

        assert result.returncode == 0
        assert _DelayedExitPopen.killed is False


    def test_watchdog_skipped_after_signal_seen(self, tmp_path, monkeypatch):
        """Fired signal + then silent: the watchdog must NOT kill — the
        worker is logically done; the hard timeout owns the flush case.
        (A watchdog kill here would raise TimeoutError('no output').)"""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        log_file = tmp_path / "hung-agent-worker.out"

        class _SignalThenHangPopen:
            spawned = False
            killed = False
            _backdated = False

            def __init__(self, cmd, cwd=None, **kwargs):
                type(self).spawned = True
                self.cmd = cmd
                self.pid = 999999999
                # appears AFTER the pre-spawn snapshot → counts as new
                (outbox / "DONE-TODO-0001.ready").write_text("")

            def poll(self):
                if not type(self)._backdated:
                    type(self)._backdated = True
                    import os as _os

                    ts = time.time() - 3600
                    _os.utime(log_file, (ts, ts))  # make the log look stale
                return None  # never exits

            def wait(self, timeout=None):
                return None

            def kill(self):
                type(self).killed = True

            def terminate(self):
                type(self).killed = True

        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _SignalThenHangPopen)
        _install_fake_time(monkeypatch, _Clock())

        # Fake clock + hard_timeout=30 bounds the loop; the watchdog (0.01s
        # on a 1h-stale log) would fire on the first iteration if it were
        # not skipped after the signal.
        result = run_subprocess_until_signal(
            ["hung-agent-worker"], cwd=tmp_path,
            watch_paths=[outbox / "DONE-TODO-0001.ready"],
            logs_dir=tmp_path,
            no_output_timeout=0.01,
            hard_timeout=30,
        )

        assert result.returncode == 0  # signal consumed → stage done


class TestLogHolder:

    def test_populated_when_log_dir_given(self, tmp_path, monkeypatch):
        _InstantPopen.spawned = False
        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _InstantPopen)
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        holder: dict[str, str] = {}
        run_subprocess_until_signal(
            ["agent-impl-worker"], cwd=tmp_path,
            logs_dir=tmp_path, log_holder=holder,
            no_output_timeout=0,
        )

        assert holder.get("log_path") == str(tmp_path / "agent-impl-worker.out")
        assert Path(holder["log_path"]).is_file()
        # U6b: run-start offset = size of the marker preamble the run wrote
        # first (append-mode log — classification must start after it).
        assert holder["log_start_offset"] == Path(holder["log_path"]).stat().st_size
        assert holder["log_start_offset"] > 0

    def test_untouched_without_log_dir(self, tmp_path, monkeypatch):
        _InstantPopen.spawned = False
        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _InstantPopen)
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        holder: dict[str, str] = {}
        run_subprocess_until_signal(
            ["agent-impl-worker"], cwd=tmp_path,
            log_holder=holder, no_output_timeout=0,
        )

        assert holder == {}


# ─── classifier + log tail ─────────────────────────────────────────────────


class TestNetworkClassifier:

    @pytest.mark.parametrize("text", [
        "error: Cannot connect to API. Retrying in 5s...",
        "connect ECONNREFUSED 127.0.0.1:8000",
        "TypeError: fetch failed",
        "socket hang up: Connection reset by peer",
        "connect ETIMEDOUT 10.0.0.5:443",
    ])
    def test_known_markers(self, text):
        assert _net.is_network_failure(text) is True

    @pytest.mark.parametrize("text", [
        "",
        "boom: some internal error",
        "AssertionError: something else broke",
        "Killed (out of memory)",
    ])
    def test_non_network(self, text):
        assert _net.is_network_failure(text) is False

    def test_marker_inside_long_tail(self):
        tail = "x" * 100000 + "fetch failed"
        assert _net.is_network_failure(tail) is True


class TestReadLogTail:

    def test_returns_whole_file_when_small(self, tmp_path):
        f = tmp_path / "w.out"
        f.write_text("short log", encoding="utf-8")
        assert _net.read_log_tail(f) == "short log"

    def test_returns_last_bytes_when_large(self, tmp_path):
        f = tmp_path / "w.out"
        f.write_bytes(b"head\n" + b"a" * 70000 + b"\ntail-line")
        tail = _net.read_log_tail(f, max_bytes=65536)
        assert tail.endswith("tail-line")
        assert not tail.startswith("head")
        assert len(tail.encode("utf-8")) <= 65536 + len("tail-line")

    def test_missing_file_empty(self, tmp_path):
        assert _net.read_log_tail(tmp_path / "absent.out") == ""

    def test_start_offset_skips_earlier_runs(self, tmp_path):
        """Append-mode log: a previous run's network marker must be cut off
        by start_offset — otherwise it misclassifies the next run's death."""
        f = tmp_path / "w.out"
        stale = b"error: Cannot connect to API.\n"
        f.write_bytes(stale + b"current run: plain crash output\n")

        tail = _net.read_log_tail(f, start_offset=len(stale))
        assert tail == "current run: plain crash output\n"
        assert _net.is_network_failure(tail) is False
        # without the offset the stale marker is visible (the bug)
        assert _net.is_network_failure(_net.read_log_tail(f)) is True

    def test_start_offset_beyond_size_empty(self, tmp_path):
        f = tmp_path / "w.out"
        f.write_bytes(b"short")
        assert _net.read_log_tail(f, start_offset=1000) == ""


# ─── real-network smoke (bounded) ──────────────────────────────────────────


class TestEndpointReachableSmoke:

    def test_unreachable_url_false_bounded(self):
        """Real helper call against a closed localhost port → False, fast."""
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # port released → nothing is listening on it

        t0 = time.monotonic()
        assert _net.endpoint_reachable(f"http://127.0.0.1:{port}/v1", timeout=3) is False
        assert time.monotonic() - t0 < 10

    def test_http_error_counts_alive(self):
        """A 500 response still means the endpoint answered → alive."""

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(500)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert _net.endpoint_reachable(f"http://127.0.0.1:{port}/v1", timeout=3) is True
        finally:
            server.shutdown()
            server.server_close()
