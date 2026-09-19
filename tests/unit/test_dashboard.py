"""Tests for dashboard generation — template rendering + error visibility (DF5-7/DF5-8).

Covers:
- generate_dashboard renders without exception (DF5-7 regression)
- Dashboard writes HTML file to correct path
- Template syntax is valid (no missing braces)
- Error logging on failure (DF5-8 — errors must not be silent)
"""
from __future__ import annotations

import pytest

from awf import api
from awf.api.dashboard import generate_dashboard, generate_state_dict
from awf.pipeline_state import read_state, write_state


@pytest.fixture
def dash_project(tmp_git_repo):
    """Project with .agentic/ + state file + pipeline.yaml."""
    api.init_project(tmp_git_repo, project_name="DashTest")

    # Minimal pipeline.yaml
    pipes_dir = tmp_git_repo / ".agentic" / "pipelines"
    pipes_dir.mkdir(parents=True, exist_ok=True)
    (pipes_dir / "default.yaml").write_text(
        "stages:\n  - name: plan\n    role: supervisor\n    kind: plan\n"
        "  - name: agent-dev\n    role: developer\n    kind: execute\n"
        "  - name: verify\n    role: supervisor\n    kind: verify\n",
        encoding="utf-8",
    )

    # State file
    write_state(
        tmp_git_repo,
        stage_idx=1,
        stage_name="agent-dev",
        stage_kind="execute",
        todo_id="TODO-0001",
        pipeline_running=True,
    )
    return tmp_git_repo


class TestDashboardRenders:
    """DF5-7 regression: template must render without Jinja2 errors."""

    def test_template_syntax_valid(self, dash_project):
        """Dashboard template has no syntax errors (DF5-7: was {% endif %)."""
        result = generate_dashboard(dash_project)
        assert result is not None
        assert result.is_file()
        assert result.suffix == ".html"

    def test_html_contains_essential_sections(self, dash_project):
        """Generated HTML has key sections."""
        result = generate_dashboard(dash_project)
        html = result.read_text(encoding="utf-8")
        assert "DashTest" in html  # project name
        assert "agent-dev" in html  # stage name
        assert "TODO-0001" in html  # todo id

    def test_with_worker_activity(self, dash_project, monkeypatch):
        """Dashboard renders with worker_activity data present."""
        from awf.api import dashboard as dash_mod

        monkeypatch.setattr(
            dash_mod,
            "_read_worker_activity",
            lambda state: {"active": True, "state": "running", "pid": 12345, "cpu_seconds": 10, "worker_pid": 12345, "read_mb": 0, "write_kb": 0},
        )
        result = generate_dashboard(dash_project)
        assert result is not None
        html = result.read_text(encoding="utf-8")
        # v2 template: worker data passed to template even if section hidden by JS
        assert "worker" in html.lower()

    def test_without_worker_activity(self, dash_project):
        """Dashboard renders when worker_activity is absent (no crash)."""
        result = generate_dashboard(dash_project)
        assert result is not None

    def test_dashboard_overwrites_previous(self, dash_project):
        """Second call overwrites first (atomic write)."""
        generate_dashboard(dash_project)
        write_state(dash_project, stage_name="verify", stage_kind="verify", stage_idx=2)
        generate_dashboard(dash_project)
        result = dash_project / ".agentic" / "dashboards" / "current.html"
        html = result.read_text(encoding="utf-8")
        assert "verify" in html


class TestDashboardErrorHandling:
    """DF5-8: errors must be logged, not swallowed silently."""

    def test_missing_state_still_renders(self, tmp_git_repo):
        """No state file — dashboard still generates (uses defaults)."""
        api.init_project(tmp_git_repo, project_name="NoState")
        pipes_dir = tmp_git_repo / ".agentic" / "pipelines"
        pipes_dir.mkdir(parents=True, exist_ok=True)
        (pipes_dir / "default.yaml").write_text(
            "stages:\n  - name: plan\n    role: supervisor\n    kind: plan\n",
            encoding="utf-8",
        )
        # No write_state call
        result = generate_dashboard(tmp_git_repo)
        assert result is not None


class TestDashboardJinjaTemplate:
    """Direct template validation — catches syntax errors early."""

    def test_all_endif_have_closing_brace(self):
        """Every {% endif %} has closing } (DF5-7 was {% endif %)."""
        from awf.api.dashboard import _get_template

        template = _get_template()
        # If template loads, Jinja2 syntax is valid
        assert template is not None

    def test_template_renders_with_minimal_data(self):
        """Template renders with empty/minimal context."""
        from awf.api.dashboard import _get_template

        template = _get_template()
        html = template.render(
            project_name="Test",
            todo_id="",
            todo_summary="",
            elapsed="0s",
            status="idle",
            status_class="",
            status_text="Idle",
            status_icon="○",
            status_label="",
            stages=[],
            stages_done=0,
            stages_total=0,
            checkpoint_pending=False,
            checkpoint_form_url=None,
            events=[],
            handoffs=[],
            tasks=[],
            tasks_done=0,
            worker_activity=None,
        )
        assert len(html) > 0
        assert "<html" in html.lower() or "<!DOCTYPE" in html


class TestSalvageStatus:
    """dogfood-11: dashboard must show salvage instead of "Pipeline running".

    A worker that exits without a signal puts the orchestrator into salvage
    wait. The state file carried ``salvage_needed: true`` but the dashboard
    ignored it — users stared at a green "running" badge while the pipeline
    was stuck waiting for a supervisor decision.
    """

    def test_determine_status_salvage(self, dash_project):
        from awf.api.dashboard import _determine_status

        write_state(dash_project, salvage_needed=True, salvage_stage="agent-implementer")
        state = read_state(dash_project)
        status, _cls, text, _icon, label = _determine_status(state)
        assert status == "salvage"
        assert "agent-implementer" in text
        assert label == "Salvage"

    def test_state_dict_reports_salvage(self, dash_project):
        write_state(dash_project, salvage_needed=True, salvage_stage="agent-implementer")
        d = generate_state_dict(dash_project)
        assert d["status"] == "salvage"
        assert d["salvage_needed"] is True
        assert d["salvage_stage"] == "agent-implementer"

    def test_state_dict_reports_running_without_salvage(self, dash_project):
        d = generate_state_dict(dash_project)
        assert d["status"] == "running"
        assert d["salvage_needed"] is False
        assert d["salvage_stage"] is None

    def test_stage_start_write_clears_stale_salvage(self, dash_project):
        """Orchestrator's stage-start write must clear resolved salvage flags.

        ``write_state`` merges fields — without an explicit False/None the
        flag would stick forever after the first salvage.
        """
        write_state(dash_project, salvage_needed=True, salvage_stage="agent-implementer")
        write_state(
            dash_project,
            stage_name="agent-qa-review",
            salvage_needed=False,
            salvage_stage=None,
        )
        state = read_state(dash_project)
        assert state.get("salvage_needed") is False
        assert state.get("salvage_stage") is None


class TestTodoDiffStat:
    """Day-2 spec: diff-stat vs baseline in /api/state — one place for verify."""

    def test_diff_stat_reported(self, tmp_git_repo):
        import subprocess

        proj = tmp_git_repo
        (proj / ".agentic" / "context").mkdir(parents=True, exist_ok=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True, text=True,
        ).stdout.strip()
        (proj / ".agentic" / "context" / "BASELINE-TODO-0001.sha").write_text(
            sha + "\n", encoding="utf-8",
        )
        (proj / "README.md").write_text("changed by the stage\n", encoding="utf-8")
        write_state(
            proj, todo_id="TODO-0001", stage_name="agent-implementer", stage_kind="execute",
        )

        d = generate_state_dict(proj)

        assert "README.md" in d["todo_diff_stat"]

    def test_no_baseline_yields_empty(self, dash_project):
        d = generate_state_dict(dash_project)
        assert d["todo_diff_stat"] == ""


class TestHandoffChat:
    """Day-3 dashboard review: the chat shows ONLY the current TODO's handoffs."""

    def _write(self, proj, name, content="# H\n\nbody\n", mtime=None):
        import os

        d = proj / ".agentic" / "handoff"
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_text(content, encoding="utf-8")
        if mtime is not None:
            os.utime(f, (mtime, mtime))
        return f

    def test_old_todo_handoffs_excluded(self, tmp_git_repo):
        from awf.api.dashboard import _read_handoffs

        proj = tmp_git_repo
        self._write(proj, "agent-qa-review-TODO-0001.md")
        self._write(proj, "agent-implementer-TODO-0002.md")

        handoffs = _read_handoffs(
            proj, stage_order=["agent-implementer"], todo_id="TODO-0002",
        )

        assert [h["file"] for h in handoffs] == ["agent-implementer-TODO-0002.md"]

    def test_legacy_final_variant_included(self, tmp_git_repo):
        from awf.api.dashboard import _read_handoffs

        proj = tmp_git_repo
        self._write(proj, "agent-qa-review-TODO-0002-final.md")

        handoffs = _read_handoffs(proj, todo_id="TODO-0002")

        assert len(handoffs) == 1
        assert handoffs[0]["role"] == "agent-qa-review"

    def test_idless_files_ignored(self, tmp_git_repo):
        from awf.api.dashboard import _read_handoffs

        proj = tmp_git_repo
        self._write(proj, "agent-implementer.md")  # legacy convention

        assert _read_handoffs(proj, todo_id="TODO-0002") == []

    def test_no_todo_id_returns_empty(self, tmp_git_repo):
        from awf.api.dashboard import _read_handoffs

        proj = tmp_git_repo
        self._write(proj, "agent-x-TODO-0001.md")

        assert _read_handoffs(proj, todo_id=None) == []

    def test_dedupe_prefers_canonical_name(self, tmp_git_repo):
        from awf.api.dashboard import _read_handoffs

        proj = tmp_git_repo
        self._write(proj, "agent-qa-review-TODO-0002-final.md", mtime=2_000_000)
        self._write(proj, "agent-qa-review-TODO-0002.md", mtime=1_000_000)

        handoffs = _read_handoffs(proj, todo_id="TODO-0002")

        assert len(handoffs) == 1
        assert handoffs[0]["file"] == "agent-qa-review-TODO-0002.md"

    def test_rev_changes_with_content(self, tmp_git_repo):
        from awf.api.dashboard import _read_handoffs

        proj = tmp_git_repo
        f = self._write(proj, "agent-x-TODO-0002.md")
        rev1 = _read_handoffs(proj, todo_id="TODO-0002")[0]["rev"]

        f.write_text("# H\n\nnew body entirely\n", encoding="utf-8")
        rev2 = _read_handoffs(proj, todo_id="TODO-0002")[0]["rev"]

        assert rev1 != rev2

    def test_raw_html_escaped(self, tmp_git_repo):
        from awf.api.dashboard import _read_handoffs

        proj = tmp_git_repo
        self._write(proj, "agent-x-TODO-0002.md", content="# T\n\n<script>alert(1)</script>\n")

        html = _read_handoffs(proj, todo_id="TODO-0002")[0]["content_html"]

        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestContentEscaping:
    """Day-3: LLM-written content must not inject markup into the dashboard."""

    def test_todo_raw_html_escaped(self, dash_project):
        from awf.api.dashboard import _read_todo_content

        inbox = dash_project / ".agentic" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "TODO-0001.md").write_text(
            "# T\n\n<script>alert(1)</script>\n", encoding="utf-8",
        )

        html = _read_todo_content(dash_project, "TODO-0001")

        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_script_tag_escaped_in_embedded_state(self, dash_project):
        from awf import run_state

        proj = dash_project
        run_state.write_run(
            proj, active=True, queue=["TODO-0001"], index=1,
            current="evil</script><script>alert(1)</script>",
        )

        out = generate_dashboard(proj)
        html = out.read_text(encoding="utf-8")

        assert "</script><script>alert(1)" not in html
        assert "\\u003c/script" in html


class TestTimelineCommitShas:
    def test_verify_commit_sha_in_timeline(self, dash_project):
        import subprocess

        from awf.api.dashboard import _build_todo_timeline

        proj = dash_project
        (proj / "x.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "awf(verify): TODO-0001"], cwd=proj, check=True,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=proj,
            capture_output=True, text=True,
        ).stdout.strip()
        (proj / ".agentic" / "done" / "TODO-0001").mkdir(parents=True)

        timeline = _build_todo_timeline(proj, None)

        assert timeline[0]["id"] == "TODO-0001"
        assert timeline[0]["commit_sha"] == sha


class TestDashboardPort:
    def test_second_server_binds_same_port(self, tmp_git_repo):
        from awf.api.dashboard_server import start_dashboard_server

        port1, server1 = start_dashboard_server(tmp_git_repo)
        server1.shutdown()
        server1.server_close()

        port2, server2 = start_dashboard_server(tmp_git_repo, port=port1)
        try:
            assert port2 == port1
        finally:
            server2.shutdown()
            server2.server_close()

    def test_busy_port_raises(self, tmp_git_repo):
        from awf.api.dashboard_server import start_dashboard_server

        port1, server1 = start_dashboard_server(tmp_git_repo)
        try:
            with pytest.raises(OSError):
                start_dashboard_server(tmp_git_repo, port=port1)
        finally:
            server1.shutdown()
            server1.server_close()


class TestWorkerPidPick:
    """Day-3: prefer the real worker over transient orchestrator helpers."""

    def test_prefers_opencode_child(self):
        from awf.api.dashboard import _pick_worker_pid

        cmdlines = {"100": b"git status\x00", "200": b"opencode run --agent worker\x00"}
        picked = _pick_worker_pid(
            ["100", "200"], read_cmdline=lambda p: cmdlines.get(p, b""),
        )
        assert picked == "200"

    def test_falls_back_to_first(self):
        from awf.api.dashboard import _pick_worker_pid

        assert _pick_worker_pid(["100", "200"], read_cmdline=lambda p: b"git") == "100"


class TestInitialContextFromState:
    """Day-3: Jinja context derives from generate_state_dict (single source)."""

    def test_salvage_status_in_initial_html(self, dash_project):
        write_state(
            dash_project, salvage_needed=True, salvage_stage="agent-x",
            stage_name="agent-x", stage_kind="execute", todo_id="TODO-0001",
        )

        out = generate_dashboard(dash_project)
        html = out.read_text(encoding="utf-8")

        assert "status-badge salvage" in html
        assert 'data-frozen="true"' in html


class TestRunScopedElapsed:
    """Day-4 live fix: elapsed counts the CURRENT run, not the whole log."""

    def _log(self, proj, text: str):
        logs = proj / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "orchestrator.log").write_text(text + "\n", encoding="utf-8")

    def test_running_ignores_previous_runs(self, dash_project):
        from datetime import datetime, timezone

        old_run = "\n".join([
            "[2026-09-15T10:00:00Z] Pipeline started with 5 stages: a b",
            "[2026-09-15T10:00:01Z] Stage 1: agent-impl (agent-impl :: execute)",
            "[2026-09-15T10:30:00Z] Stage 4: verify (supervisor :: verify)",
        ])
        new_run = "\n".join([
            "[2026-09-19T05:16:07Z] Pipeline started with 5 stages: plan agent-spec-writer",
            "[2026-09-19T05:16:07Z] Stage 0: plan (supervisor :: plan)",
            "[2026-09-19T05:16:20Z] Stage 1: agent-spec-writer (agent-spec-writer :: execute)",
        ])
        self._log(dash_project, old_run + "\n" + new_run)
        write_state(
            dash_project, stage_name="agent-spec-writer", stage_kind="execute",
            stage_idx=1, todo_id="TODO-0013",
        )

        d = generate_state_dict(dash_project)

        expected = int(
            datetime(2026, 9, 19, 5, 16, 20, tzinfo=timezone.utc).timestamp()
        )
        assert d["elapsed_epoch"] == expected
        assert d["elapsed_frozen"] is False

    def test_running_during_plan_uses_run_start(self, dash_project):
        from datetime import datetime, timezone

        self._log(dash_project, "\n".join([
            "[2026-09-15T10:00:01Z] Stage 1: old (old :: execute)",
            "[2026-09-19T05:16:07Z] Pipeline started with 5 stages: plan x",
            "[2026-09-19T05:16:07Z] Stage 0: plan (supervisor :: plan)",
        ]))
        write_state(
            dash_project, stage_name="plan", stage_kind="plan", stage_idx=0,
            todo_id="TODO-0013",
        )

        d = generate_state_dict(dash_project)

        expected = int(
            datetime(2026, 9, 19, 5, 16, 7, tzinfo=timezone.utc).timestamp()
        )
        assert d["elapsed_epoch"] == expected

    def test_frozen_span_scoped_to_run(self, dash_project):
        from datetime import datetime, timezone

        self._log(dash_project, "\n".join([
            "[2026-09-15T10:00:00Z] Pipeline started with 5 stages: a b",
            "[2026-09-15T10:00:01Z] Stage 1: old (old :: execute)",
            "[2026-09-15T10:30:00Z] Stage 4: verify (supervisor :: verify)",
            "[2026-09-19T05:00:00Z] Pipeline started with 5 stages: plan x",
            "[2026-09-19T05:01:00Z] Stage 1: x (x :: execute)",
            "[2026-09-19T05:06:00Z] Stage 4: verify (supervisor :: verify)",
        ]))
        write_state(
            dash_project, stage_name="verify", stage_kind="verify", stage_idx=4,
            todo_id="TODO-0013",
        )

        d = generate_state_dict(dash_project)

        expected_epoch = int(
            datetime(2026, 9, 19, 5, 1, 0, tzinfo=timezone.utc).timestamp()
        )
        assert d["elapsed_epoch"] == expected_epoch
        assert d["elapsed_frozen"] is True
        assert d["elapsed_str"] == "5m 0s"


class TestWorkerLastLine:
    """Day-4 live fix: the log glob must match awf-agent-*.out."""

    def _log(self, proj, name: str, text: str, mtime: float = 0):
        import os

        logs = proj / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        f = logs / name
        f.write_text(text, encoding="utf-8")
        if mtime:
            os.utime(f, (mtime, mtime))
        return f

    def test_prefers_current_todo_log(self, dash_project):
        from awf.api.dashboard import _read_worker_last_line

        self._log(
            dash_project, "awf-agent-x-TODO-0002.out",
            "timestamp=2026-09-19T05:00:00Z message=loop step=5\n", mtime=2_000_000,
        )
        self._log(
            dash_project, "awf-agent-y-TODO-0001.out",
            "\x1b[0mRead src/foo.py\n", mtime=1_000_000,
        )

        assert _read_worker_last_line(dash_project, "TODO-0001") == "Read src/foo.py"

    def test_touching_file_fallback(self, dash_project):
        from awf.api.dashboard import _read_worker_last_line

        self._log(
            dash_project, "awf-agent-y-TODO-0001.out",
            'timestamp=2026-09-19T05:16:07Z level=INFO message="touching file" file="/x/y/foo.py"\n',
        )

        assert _read_worker_last_line(dash_project, "TODO-0001") == "touching foo.py"

    def test_no_logs_returns_empty(self, dash_project):
        from awf.api.dashboard import _read_worker_last_line

        assert _read_worker_last_line(dash_project, "TODO-0001") == ""


class TestElapsedFormat:
    """W4.1: one unified dd hh mm format everywhere."""

    def test_formats(self):
        from awf.api.dashboard import _format_elapsed_from_seconds as f

        assert f(9) == "9s"
        assert f(90) == "1m 30s"
        assert f(3600 + 60) == "1h 1m"
        assert f(86400 + 3600 + 60) == "1d 1h 1m"


class TestStageSpans:
    """W4.2: run-scoped start/end epochs for chat entries."""

    def _log(self, proj, text):
        logs = proj / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "orchestrator.log").write_text(text + "\n", encoding="utf-8")

    def test_scoped_to_last_run(self, dash_project):
        from awf.api.dashboard import _stage_spans

        self._log(dash_project, "\n".join([
            "[2026-09-15T10:00:00Z] Pipeline started with 5 stages: a b",
            "[2026-09-15T10:00:01Z] Stage 1: old (old :: execute)",
            "[2026-09-19T05:16:07Z] Pipeline started with 5 stages: plan x",
            "[2026-09-19T05:16:07Z] Stage 0: plan (supervisor :: plan)",
            "[2026-09-19T05:17:00Z] Stage 1: x (x :: execute)",
        ]))

        spans = _stage_spans(dash_project)

        assert "old" not in spans
        assert spans["x"]["start"] > spans["plan"]["start"]
        assert spans["x"]["end"] is None  # still running


class TestChatEntries:
    """W4.2/W4.3: active entry on top, completed newest-first with times."""

    def _setup(self, dash_project, stage_name="agent-implementer", stage_kind="execute"):
        proj = dash_project
        logs = proj / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "orchestrator.log").write_text("\n".join([
            "[2026-09-19T05:16:07Z] Pipeline started with 5 stages: plan a b",
            "[2026-09-19T05:16:07Z] Stage 0: plan (supervisor :: plan)",
            "[2026-09-19T05:17:00Z] Stage 1: agent-spec-writer (agent-spec-writer :: execute)",
            "[2026-09-19T05:22:00Z] Stage 2: agent-implementer (agent-implementer :: execute)",
        ]) + "\n", encoding="utf-8")
        (proj / ".agentic" / "handoff").mkdir(parents=True, exist_ok=True)
        (proj / ".agentic" / "handoff" / "agent-spec-writer-TODO-0001.md").write_text(
            "# H\n\ndone\n", encoding="utf-8",
        )
        (logs / "awf-agent-implementer-TODO-0001.out").write_text(
            "\x1b[0mRead src/foo.py\n", encoding="utf-8",
        )
        write_state(
            proj, stage_name=stage_name, stage_kind=stage_kind, stage_idx=2,
            todo_id="TODO-0001",
        )

    def test_active_entry_on_top_with_timer_and_line(self, dash_project, monkeypatch):
        from datetime import datetime, timezone

        self._setup(dash_project)
        monkeypatch.setattr(
            "awf.api.dashboard._read_worker_activity",
            lambda state: {"active": True, "worker_pid": 1, "state": "running", "cpu_seconds": 1},
        )
        d = generate_state_dict(dash_project)

        active = d["handoffs"][0]
        assert active["active"] is True
        assert active["role"] == "agent-implementer"
        assert active["started_epoch"] == int(
            datetime(2026, 9, 19, 5, 22, 0, tzinfo=timezone.utc).timestamp()
        )
        assert active["line"] == "Read src/foo.py"
        assert active["awaiting"] is False

    def test_completed_entry_has_times_and_duration(self, dash_project):
        from datetime import datetime, timezone

        self._setup(dash_project)
        d = generate_state_dict(dash_project)

        done = d["handoffs"][1]
        assert done["role"] == "agent-spec-writer"
        assert done["active"] is False
        assert done["started_epoch"] == int(
            datetime(2026, 9, 19, 5, 17, 0, tzinfo=timezone.utc).timestamp()
        )
        assert done["duration"] == "5m 0s"
        assert done["started_at"] and done["ended_at"]

    def test_verify_entry_is_flagged(self, dash_project):
        self._setup(dash_project, stage_name="verify", stage_kind="verify")
        d = generate_state_dict(dash_project)

        active = d["handoffs"][0]
        assert active["is_verify"] is True
        assert active["awaiting"] is True


class TestTodoSummary:
    """W4.5: one human paragraph, full text stays under <details>."""

    def test_extracts_first_paragraph(self, dash_project):
        from awf.api.dashboard import _read_todo_summary

        inbox = dash_project / ".agentic" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "TODO-0001.md").write_text(
            "---\ntags: x\n---\n"
            "# TODO-0001: B7\n\n"
            "## Контекст\n\n"
            "Сделать экспорт отчёта в CSV по кнопке.\n"
            "Он должен работать офлайн.\n\n"
            "## Детали\n\nвторой абзац\n",
            encoding="utf-8",
        )

        summary = _read_todo_summary(dash_project, "TODO-0001")

        assert summary == "Сделать экспорт отчёта в CSV по кнопке. Он должен работать офлайн."

    def test_caps_long_text(self, dash_project):
        from awf.api.dashboard import _read_todo_summary

        inbox = dash_project / ".agentic" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "TODO-0001.md").write_text("# T\n\n" + "длинно " * 200, encoding="utf-8")

        summary = _read_todo_summary(dash_project, "TODO-0001")

        assert len(summary) <= 400
        assert summary.endswith("…")

    def test_no_todo(self, dash_project):
        from awf.api.dashboard import _read_todo_summary

        assert _read_todo_summary(dash_project, "TODO-9999") == ""

    def test_in_state_dict(self, dash_project):
        inbox = dash_project / ".agentic" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "TODO-0001.md").write_text("# T\n\nКороткая суть задачи.\n", encoding="utf-8")
        write_state(dash_project, todo_id="TODO-0001", stage_name="x", stage_kind="execute")

        d = generate_state_dict(dash_project)

        assert d["todo_summary"] == "Короткая суть задачи."


class TestCheckpointEvents:
    """W4.8: auto-approved checkpoints must not look like they need a human."""

    def test_wording(self):
        from awf.api.dashboard import _parse_log_events

        log = "\n".join([
            "[2026-09-19T05:16:07Z] BD-36: checkpoint decision=approve",
            "[2026-09-19T05:16:08Z] BD-36: checkpoint opened waiting for user input",
            "[2026-09-19T05:16:09Z] BD-36: checkpoint still waiting for user",
        ])
        events = _parse_log_events(log)
        msgs = [e["msg"] for e in events]

        assert msgs.count("⏸ Checkpoint auto-approved") == 1
        assert msgs.count("⏸ Checkpoint opened — needs user") == 1
        assert len(msgs) == 2  # "still waiting" polling line is skipped
