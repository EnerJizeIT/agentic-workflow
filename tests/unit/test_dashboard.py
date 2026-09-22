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
        """Dashboard renders when worker_activity is absent (no crash).

        AUD12-12: used to assert only "not None" — a template regression
        (dropped section) stayed green. Now the basic sections are pinned.
        """
        result = generate_dashboard(dash_project)
        assert result is not None
        html = result.read_text(encoding="utf-8")
        assert "<title>awf" in html
        assert 'id="status-badge"' in html
        assert 'id="stage-list"' in html
        assert 'id="pane-chat"' in html

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

    def test_template_parses(self):
        """Dashboard template compiles (DF5-7 was a broken {% endif %}).

        AUD12-12: renamed from ``test_all_endif_have_closing_brace`` — the
        name promised a brace check the test never did; it loads the
        template (Jinja2 parse), so it gets an honest name.
        """
        from awf.api.dashboard import _get_template

        template = _get_template()
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
        # AUD10-06: salvage_needed left /api/state (no consumer) — the
        # status itself carries the signal now.
        write_state(dash_project, salvage_needed=True, salvage_stage="agent-implementer")
        d = generate_state_dict(dash_project)
        assert d["status"] == "salvage"
        assert "salvage_needed" not in d
        assert d["salvage_stage"] == "agent-implementer"

    def test_state_dict_reports_running_without_salvage(self, dash_project):
        d = generate_state_dict(dash_project)
        assert d["status"] == "running"
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

    def test_non_agent_role_log_found(self, dash_project):
        """AUD16-06: a role without 'agent-' in the name (worker/auditor)
        produces awf-{role}-{todo}.out — the glob must match it."""
        from awf.api.dashboard import _read_worker_last_line

        self._log(
            dash_project, "awf-worker-TODO-0001.out",
            "Building feature x\n", mtime=2_000_000,
        )
        self._log(
            dash_project, "awf-agent-y-TODO-0001.out",
            "stale line\n", mtime=1_000_000,
        )

        assert _read_worker_last_line(dash_project, "TODO-0001") == "Building feature x"


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


class TestTotalElapsed:
    """Day-5: total across iterations next to the current-iteration timer."""

    def _log(self, proj, text):
        logs = proj / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "orchestrator.log").write_text(text + "\n", encoding="utf-8")

    def test_two_closed_plus_open(self, dash_project):
        from datetime import datetime, timezone

        from awf.api.dashboard import _total_elapsed

        self._log(dash_project, "\n".join([
            "[2026-09-18T10:00:00Z] Pipeline started with 5 stages: a",
            "[2026-09-18T10:10:00Z] Pipeline complete",
            "[2026-09-18T11:00:00Z] Pipeline started with 5 stages: a",
            "[2026-09-18T11:05:00Z] Pipeline complete",
            "[2026-09-19T12:00:00Z] Pipeline started with 5 stages: a",
        ]))

        closed, running, start = _total_elapsed(dash_project)

        assert closed == 600 + 300
        assert running is True
        assert start == int(datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc).timestamp())

    def test_all_closed(self, dash_project):
        from awf.api.dashboard import _total_elapsed

        self._log(dash_project, "\n".join([
            "[2026-09-18T10:00:00Z] Pipeline started with 5 stages: a",
            "[2026-09-18T10:10:00Z] Pipeline complete",
        ]))

        closed, running, start = _total_elapsed(dash_project)

        assert closed == 600
        assert running is False
        assert start == 0

    def test_state_dict_carries_total(self, dash_project):
        self._log(dash_project, "\n".join([
            "[2026-09-18T10:00:00Z] Pipeline started with 5 stages: a",
            "[2026-09-18T10:10:00Z] Pipeline complete",
        ]))
        write_state(dash_project, stage_name="x", stage_kind="execute", todo_id="TODO-0001")

        d = generate_state_dict(dash_project)

        assert d["total_elapsed_sec"] == 600
        assert d["total_running"] is False


class TestSummarySkipsHtmlComment:
    def test_html_comment_skipped(self, dash_project):
        from awf.api.dashboard import _read_todo_summary

        inbox = dash_project / ".agentic" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "TODO-0001.md").write_text(
            "<!-- role_hint: agent-spec-writer -->\n"
            "# TODO-0001 — заголовок\n\n"
            "## EXECUTE NOW\n\n"
            "Пайплайн: делаем X и закрываем Y.\n",
            encoding="utf-8",
        )

        assert _read_todo_summary(dash_project, "TODO-0001") == "Пайплайн: делаем X и закрываем Y."


class TestCorruptContentDoesNotBreakPoll:
    """AUD10-02: no file read by the poller may 500 /api/state."""

    def test_corrupt_handoff_does_not_break_state(self, dash_project):
        handoff_dir = dash_project / ".agentic" / "handoff"
        handoff_dir.mkdir(parents=True, exist_ok=True)
        (handoff_dir / "agent-x-TODO-0001.md").write_bytes(b"# H\n\n\xff\xfe junk\n")

        d = generate_state_dict(dash_project)  # must not raise

        by_role = {h["role"]: h for h in d["handoffs"]}
        assert "agent-x" in by_role  # degraded content, not a crash
        # QA-0030: content must DEGRADE (U+FFFD), not silently vanish —
        # a bare except with no errors="replace" loses the whole handoff.
        assert "\ufffd" in by_role["agent-x"]["content_html"]

    def test_corrupt_todo_does_not_break_state(self, dash_project):
        inbox = dash_project / ".agentic" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "TODO-0001.md").write_bytes(b"# T\n\n\xff\xff\n")

        d = generate_state_dict(dash_project)  # must not raise

        assert isinstance(d["todo_content_html"], str)

    def test_worker_log_removed_mid_glob(self, dash_project, monkeypatch):
        """A file vanishing between glob() and stat() must not raise."""
        from pathlib import Path as _Path

        import awf.api.dashboard as dash_mod

        logs = dash_project / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        f = logs / "awf-agent-x-TODO-0001.out"
        f.write_text("hello line\n", encoding="utf-8")

        real_stat = _Path.stat

        def flaky_stat(self, *a, **kw):
            if self.name == "awf-agent-x-TODO-0001.out":
                raise OSError("vanished")
            return real_stat(self, *a, **kw)

        monkeypatch.setattr(_Path, "stat", flaky_stat)

        result = dash_mod._read_worker_last_line(dash_project, "TODO-0001")
        assert isinstance(result, str)


class TestActiveEntryLiveLine:
    """AUD10-03: the worker's last line must reach the chat re-render key."""

    def _setup(self, proj):
        logs = proj / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "orchestrator.log").write_text("\n".join([
            "[2026-09-19T05:16:07Z] Pipeline started with 3 stages: plan a b",
            "[2026-09-19T05:16:07Z] Stage 0: plan (supervisor :: plan)",
            "[2026-09-19T05:22:00Z] Stage 1: agent-implementer (agent-implementer :: execute)",
        ]) + "\n", encoding="utf-8")
        write_state(
            proj, stage_name="agent-implementer", stage_kind="execute",
            stage_idx=1, todo_id="TODO-0001",
        )

    def test_active_rev_changes_with_line(self, dash_project, monkeypatch):
        self._setup(dash_project)
        monkeypatch.setattr(
            "awf.api.dashboard._read_worker_activity",
            lambda state: {"active": True, "worker_pid": 1, "state": "running", "cpu_seconds": 1},
        )
        logf = dash_project / ".agentic" / "logs" / "awf-agent-implementer-TODO-0001.out"
        logf.write_text("\x1b[0mRead src/foo.py\n", encoding="utf-8")
        a1 = generate_state_dict(dash_project)["handoffs"][0]
        logf.write_text("\x1b[0mRead src/bar.py\n", encoding="utf-8")
        a2 = generate_state_dict(dash_project)["handoffs"][0]

        assert a1["active"] is True and a2["active"] is True
        assert a1["line"] == "Read src/foo.py"
        assert a2["line"] == "Read src/bar.py"
        # same stage, same start — only the line changed, and the rev must
        # change with it, or the client never re-renders the line.
        assert a1["rev"] != a2["rev"]


class TestRoleSpansAndOrder:
    """AUD10-04: role-keyed handoff entries get stage spans (name≠role)
    and the chat order is reverse-chronological, not pipeline-order."""

    def _log(self, proj, text):
        logs = proj / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "orchestrator.log").write_text(text + "\n", encoding="utf-8")

    def _handoff(self, proj, name, content="# H\n\ndone\n"):
        d = proj / ".agentic" / "handoff"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(content, encoding="utf-8")

    def test_supervisor_entry_gets_times(self, dash_project):
        """plan/verify both run role 'supervisor' — the merged span wins."""
        from datetime import datetime, timezone

        self._log(dash_project, "\n".join([
            "[2026-09-19T05:16:07Z] Pipeline started with 3 stages: plan agent-dev verify",
            "[2026-09-19T05:16:07Z] Stage 0: plan (supervisor :: plan)",
            "[2026-09-19T05:17:00Z] Stage 1: agent-dev (developer :: execute)",
            "[2026-09-19T05:22:00Z] Stage 2: verify (supervisor :: verify)",
        ]))
        self._handoff(dash_project, "supervisor-TODO-0001.md")
        write_state(
            dash_project, stage_name="verify", stage_kind="verify",
            stage_idx=2, todo_id="TODO-0001",
        )

        d = generate_state_dict(dash_project)
        sup = next(h for h in d["handoffs"] if h["role"] == "supervisor")

        # started with plan (earliest of the role's stages)
        assert sup["started_epoch"] == int(
            datetime(2026, 9, 19, 5, 16, 7, tzinfo=timezone.utc).timestamp()
        )
        assert sup["started_at"]
        # verify is still open (active entry) → merged end is None, no duration
        assert sup["ended_at"] == ""
        assert sup["duration"] == ""

    def test_order_is_newest_first_chronological(self, dash_project):
        self._log(dash_project, "\n".join([
            "[2026-09-19T05:16:07Z] Pipeline started with 3 stages: plan agent-dev verify",
            "[2026-09-19T05:16:07Z] Stage 0: plan (supervisor :: plan)",
            "[2026-09-19T05:17:00Z] Stage 1: agent-dev (developer :: execute)",
        ]))
        self._handoff(dash_project, "supervisor-TODO-0001.md")
        self._handoff(dash_project, "developer-TODO-0001.md")
        write_state(
            dash_project, stage_name="agent-dev", stage_kind="execute",
            stage_idx=1, todo_id="TODO-0001",
        )

        d = generate_state_dict(dash_project)
        roles = [h["role"] for h in d["handoffs"]]

        # active agent-dev on top, then the newer dev entry (05:17) above
        # the older plan/verify entry (05:16) — the old pipeline-order sort
        # put 'supervisor' (no span) on top.
        assert roles == ["agent-dev", "developer", "supervisor"]
        dev = d["handoffs"][1]
        sup = d["handoffs"][2]
        assert dev["started_epoch"] > sup["started_epoch"]
        assert sup["duration"] == "53s"  # plan: 05:16:07 → 05:17:00


class TestCheckpointStatus:
    """AUD10-08: the checkpoint state must be visible (dot + badge colored)."""

    def test_checkpoint_status_class(self, dash_project):
        write_state(dash_project, checkpoint_pending=True)

        out = generate_dashboard(dash_project)
        html = out.read_text(encoding="utf-8")

        assert 'status-badge checkpoint' in html
        assert 'status-dot checkpoint' in html
        assert ".status-badge.checkpoint" in html
        assert ".status-dot.checkpoint" in html


class TestDeadRulesRemoved:
    """AUD10-09: no dead CSS rules, no stale docstring mechanism."""

    def test_dead_css_removed(self):
        from awf.api.dashboard import _TEMPLATE_PATH

        src = _TEMPLATE_PATH.read_text(encoding="utf-8")

        for rule in (".chat-dur", ".stage-dur", ".todo-sep"):
            assert rule not in src, f"dead CSS rule survived: {rule}"

    def test_docstring_mentions_js_polling_not_meta(self):
        import awf.api.dashboard as dash_mod

        doc = dash_mod.__doc__ or ""
        assert "meta" not in doc
        assert "/api/state" in doc


class TestUnsafeLinks:
    """AUD10-10: markdown hrefs with executable schemes are neutralized."""

    def test_javascript_link_neutralized(self, tmp_git_repo):
        from awf.api.dashboard import _read_handoffs

        d = tmp_git_repo / ".agentic" / "handoff"
        d.mkdir(parents=True, exist_ok=True)
        (d / "agent-x-TODO-0002.md").write_text(
            "# T\n\n[click](javascript:alert(1))\n", encoding="utf-8",
        )

        html = _read_handoffs(tmp_git_repo, todo_id="TODO-0002")[0]["content_html"]

        assert "javascript:" not in html
        assert 'href="#"' in html

    def test_safe_links_kept(self):
        from awf.api.dashboard import _neutralize_unsafe_links as fix

        assert 'href="https://x.y"' in fix('<a href="https://x.y">a</a>')
        assert 'href="/rel/path"' in fix('<a href="/rel/path">a</a>')
        assert 'href="mailto:a@b.c"' in fix('<a href="mailto:a@b.c">a</a>')
        assert 'href="#"' in fix('<a href="javascript:alert(1)">a</a>')
        assert 'href="#"' in fix('<a href="JAVASCRIPT:alert(1)">a</a>')
        assert 'href="#"' in fix('<a href="data:text/html,x">a</a>')


class TestStateContract:
    """AUD10-06: every /api/state field the server emits must be read by
    the template/JS — dead contract fields are a drift trap."""

    def test_top_level_keys_consumed_by_template(self, dash_project):
        import re

        from awf.api.dashboard import _TEMPLATE_PATH

        used = set(re.findall(r"\bs\.(\w+)", _TEMPLATE_PATH.read_text(encoding="utf-8")))
        d = generate_state_dict(dash_project)

        unused = set(d) - used
        assert not unused, f"dead /api/state fields: {sorted(unused)}"

    def test_run_keys_consumed_by_template(self, dash_project):
        import re

        from awf import run_state
        from awf.api.dashboard import _TEMPLATE_PATH

        run_state.write_run(
            dash_project, active=True, queue=["TODO-0001"],
            index=0, current="TODO-0001",
        )
        used = set(re.findall(r"\brun\.(\w+)", _TEMPLATE_PATH.read_text(encoding="utf-8")))
        d = generate_state_dict(dash_project)

        assert d["run"] is not None
        unused = set(d["run"]) - used
        assert not unused, f"dead run.* fields: {sorted(unused)}"


class TestActualPipeline:
    """RUN6 #2: the dashboard draws the pipeline that ACTUALLY runs.

    Owner observation: a run pins a queue item on ``audit-llm`` while
    default.yaml is implement+qa — the dashboard drew the DEFAULT
    pipeline's stages and confused them with the running ones. Now the
    name resolves: state["pipeline"] (engine) → run queue item → the
    TODO's contract block → config default; an unresolvable name falls
    back to the default pipeline and never crashes.
    """

    DEFAULT_YAML = (
        "stages:\n"
        "  - name: plan\n    role: supervisor\n    kind: plan\n"
        "  - name: implement\n    role: agent-implementer\n    kind: execute\n"
        "  - name: qa\n    role: agent-qa-review\n    kind: execute\n"
    )
    AUDIT_YAML = (
        "stages:\n"
        "  - name: plan\n    role: supervisor\n    kind: plan\n"
        "  - name: llm-audit\n    role: agent-security-auditor\n    kind: execute\n"
        "  - name: verify\n    role: supervisor\n    kind: verify\n"
    )

    def _project(self, tmp_git_repo, audit=True):
        api.init_project(tmp_git_repo, project_name="PipeTest")
        pipes = tmp_git_repo / ".agentic" / "pipelines"
        pipes.mkdir(parents=True, exist_ok=True)
        (pipes / "default.yaml").write_text(self.DEFAULT_YAML, encoding="utf-8")
        if audit:
            (pipes / "audit-llm.yaml").write_text(self.AUDIT_YAML, encoding="utf-8")
        return tmp_git_repo

    def test_repro_run_pinned_pipeline_drawn(self, tmp_git_repo):
        """The owner's repro: default = implement+qa, the run is pinned on
        audit-llm — the dashboard must draw audit-llm's stages."""
        from awf import run_state

        proj = self._project(tmp_git_repo)
        run_state.write_run(
            proj, active=True,
            queue=[{"todo_id": "TODO-0010", "pipeline": "audit-llm"}],
            index=0, current="TODO-0010",
        )
        write_state(
            proj, stage_idx=1, stage_name="llm-audit", stage_kind="execute",
            todo_id="TODO-0010", pipeline="audit-llm",
        )

        d = generate_state_dict(proj)
        assert d["pipeline"] == "audit-llm"
        assert d["pipeline_note"] == ""
        names = [s["name"] for s in d["stages"]]
        assert names == ["plan", "llm-audit", "verify"]
        assert not ({"implement", "qa"} & set(names)), "default pipeline leaked in"
        current = [s for s in d["stages"] if s["status"] == "current"]
        assert len(current) == 1 and current[0]["name"] == "llm-audit"

    def test_single_launch_pipeline_from_state(self, tmp_git_repo):
        """Single launch (--pipeline X, no run): the engine-written state
        field carries the name."""
        proj = self._project(tmp_git_repo)
        write_state(
            proj, stage_idx=1, stage_name="llm-audit", stage_kind="execute",
            todo_id="TODO-0001", pipeline="audit-llm",
        )

        d = generate_state_dict(proj)
        assert d["pipeline"] == "audit-llm"
        assert [s["name"] for s in d["stages"]] == ["plan", "llm-audit", "verify"]

    def test_legacy_state_without_pipeline_falls_back_to_default(self, tmp_git_repo):
        """State predating the field (or written by an old engine) → the
        config default, as before."""
        proj = self._project(tmp_git_repo)
        write_state(
            proj, stage_idx=1, stage_name="implement", stage_kind="execute",
            todo_id="TODO-0001",
        )

        d = generate_state_dict(proj)
        assert d["pipeline"] == "default"
        assert [s["name"] for s in d["stages"]] == ["plan", "implement", "qa"]
        assert d["stages"][1]["status"] == "current"

    def test_queue_item_pipeline_used_without_state_name(self, tmp_git_repo):
        """Chain step 2: no state name, but the run queue item carries the
        per-item pipeline (RUN3 #2)."""
        from awf import run_state

        proj = self._project(tmp_git_repo)
        run_state.write_run(
            proj, active=True,
            queue=[
                {"todo_id": "TODO-0009", "pipeline": ""},
                {"todo_id": "TODO-0010", "pipeline": "audit-llm"},
            ],
            index=1, current="TODO-0010",
        )
        write_state(
            proj, stage_idx=1, stage_name="llm-audit", stage_kind="execute",
            todo_id="TODO-0010",
        )

        d = generate_state_dict(proj)
        assert d["pipeline"] == "audit-llm"
        assert [s["name"] for s in d["stages"]] == ["plan", "llm-audit", "verify"]

    def test_todo_contract_pipeline_used_without_state_or_run(self, tmp_git_repo):
        """Chain step 3: no state name, no run — the TODO's contract block
        declares the pipeline."""
        proj = self._project(tmp_git_repo)
        (proj / ".agentic" / "inbox" / "TODO-0010.md").write_text(
            "<!-- role_hint: agent-implementer -->\n"
            "---\n"
            "pipeline: audit-llm\n"
            "gates: [contracts]\n"
            "---\n\n"
            "# TODO-0010 — audit\n",
            encoding="utf-8",
        )
        write_state(
            proj, stage_idx=1, stage_name="llm-audit", stage_kind="execute",
            todo_id="TODO-0010",
        )

        d = generate_state_dict(proj)
        assert d["pipeline"] == "audit-llm"

    def test_no_state_no_crash(self, tmp_git_repo):
        """No state at all: the config default is drawn, no crash; without
        any pipelines directory the name is simply empty."""
        proj = self._project(tmp_git_repo)

        d = generate_state_dict(proj)
        assert d["pipeline"] == "default"
        assert d["pipeline_note"] == ""
        assert d["stages"]

        import shutil

        shutil.rmtree(proj / ".agentic" / "pipelines")
        d2 = generate_state_dict(proj)
        assert d2["pipeline"] == ""
        assert d2["stages"] == []

    def test_stale_state_dead_pid_keeps_existing_status(self, tmp_git_repo):
        """Stale state (dead pid): the existing liveness logic decides the
        status; the pipeline name still shows (it is the last known fact)."""
        import subprocess

        proj = self._project(tmp_git_repo)
        proc = subprocess.Popen(["true"])
        proc.wait()
        write_state(
            proj, stage_idx=1, stage_name="llm-audit", stage_kind="execute",
            todo_id="TODO-0010", pipeline="audit-llm", pipeline_pid=proc.pid,
        )

        d = generate_state_dict(proj)
        assert d["status"] == "dead"
        assert d["pipeline"] == "audit-llm"

    def test_mismatch_no_highlight_with_note(self, tmp_git_repo):
        """The actual pipeline's file is gone: fall back to the default
        stages, but do NOT highlight a foreign stage as current — a note
        explains why."""
        proj = self._project(tmp_git_repo)
        (proj / ".agentic" / "pipelines" / "audit-llm.yaml").unlink()
        write_state(
            proj, stage_idx=1, stage_name="llm-audit", stage_kind="execute",
            todo_id="TODO-0010", pipeline="audit-llm",
        )

        d = generate_state_dict(proj)
        assert d["pipeline"] == "default"
        assert d["pipeline_note"]
        assert "audit-llm" in d["pipeline_note"]
        assert all(s["status"] != "current" for s in d["stages"])
        assert d["next_stage"] is None
