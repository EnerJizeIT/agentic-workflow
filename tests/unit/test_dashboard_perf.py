"""FU-17b2: dashboard server perf (AUD15-02) + TEST-RESULTS archiving (AUD15-07).

- 10 concurrent /api/state polls collapse into ONE generate_state_dict
  (single-flight + TTL coalescing)
- TEST-RESULTS-{task}.log archives with the task; get_report reads the tail
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

# task id for the archive/tail fixtures (kept in one place)
_TASK_ID = "TODO-0001"


class TestStateCoalescing:
    """AUD15-02: 10 concurrent polls → ONE generate_state_dict."""

    def _start_server(self, tmp_git_repo):
        from awf.api.dashboard_server import start_dashboard_server

        port, server = start_dashboard_server(tmp_git_repo)
        return port, server

    def test_concurrent_polls_collapse_to_one_computation(self, tmp_git_repo, monkeypatch):
        import awf.api.dashboard as dash_mod

        counter = {"n": 0}
        lock = threading.Lock()

        def slow_compute(project_dir):
            with lock:
                counter["n"] += 1
            time.sleep(0.4)  # force all 10 requests to overlap
            return {"status": "running", "n": counter["n"]}

        monkeypatch.setattr(dash_mod, "generate_state_dict", slow_compute)

        port, server = self._start_server(tmp_git_repo)
        try:
            results = [None] * 10
            errors = []

            def fetch(i):
                # 20 ms stagger: all 10 still overlap (compute sleeps 400 ms)
                time.sleep(0.02 * i)
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/state", timeout=30
                    ) as r:
                        results[i] = json.loads(r.read())
                except Exception as e:  # noqa: BLE001 — report the failure
                    errors.append(repr(e))

            threads = [threading.Thread(target=fetch, args=(i,)) for i in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)

            assert not errors, f"/api/state failed: {errors}"
            assert all(r == results[0] for r in results), "polls must share one result"
            assert counter["n"] == 1, (
                f"10 concurrent polls ran {counter['n']} computations — "
                f"expected single-flight coalescing"
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_rapid_repeats_hit_ttl_cache(self, tmp_git_repo, monkeypatch):
        import awf.api.dashboard as dash_mod

        counter = {"n": 0}

        def compute(project_dir):
            counter["n"] += 1
            return {"status": "running"}

        monkeypatch.setattr(dash_mod, "generate_state_dict", compute)

        port, server = self._start_server(tmp_git_repo)
        try:
            for _ in range(3):  # three back-to-back polls, well within the TTL
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/state", timeout=10
                ) as r:
                    json.loads(r.read())
            assert counter["n"] == 1, "rapid repeats must be served from the TTL cache"
        finally:
            server.shutdown()
            server.server_close()


class TestTestResultsArchive:
    """AUD15-07: the test-results log archives with its task; tail-only read."""

    def test_archive_moves_test_results(self, tmp_git_repo):
        from awf.todos import archive_todo

        inbox = tmp_git_repo / ".agentic" / "inbox"
        outbox = tmp_git_repo / ".agentic" / "outbox"
        inbox.mkdir(parents=True, exist_ok=True)
        outbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"{_TASK_ID}.md").write_text("# done task\n", encoding="utf-8")
        (outbox / f"TEST-RESULTS-{_TASK_ID}.log").write_text(
            "1 passed in 1.0s\n", encoding="utf-8",
        )

        dest = archive_todo(tmp_git_repo, _TASK_ID)

        assert dest is not None
        assert (dest / f"TEST-RESULTS-{_TASK_ID}.log").is_file()
        assert not (outbox / f"TEST-RESULTS-{_TASK_ID}.log").exists()

    def test_report_shows_tail_of_archived_log(self, tmp_git_repo):
        from awf.api.lifecycle import get_report
        from awf.todos import archive_todo

        inbox = tmp_git_repo / ".agentic" / "inbox"
        outbox = tmp_git_repo / ".agentic" / "outbox"
        inbox.mkdir(parents=True, exist_ok=True)
        outbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"{_TASK_ID}.md").write_text("# done task\n", encoding="utf-8")
        # 2 MB of padding: the report used to read ALL of this for 5 lines
        (outbox / f"TEST-RESULTS-{_TASK_ID}.log").write_text(
            "padding\n" * 100_000
            + "347 passed in 12.3s\n"
            + "457 passed in 23.4s\n"
            + "567 passed in 34.5s\n"
            + "677 passed in 45.6s\n"
            + "787 passed in 56.7s\n",
            encoding="utf-8",
        )
        archive_todo(tmp_git_repo, _TASK_ID)

        report = get_report(tmp_git_repo)

        tail = report.latest_test_log_tail or ""
        assert tail.strip().splitlines()[-1] == "787 passed in 56.7s"
        assert len(tail.splitlines()) <= 5
        assert "padding" not in tail
