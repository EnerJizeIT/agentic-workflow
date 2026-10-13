"""TODO-0213 (REPORTS33, BD-30): false replan hijacks the live pipeline.

Incident 12.10 (dogfood run 10): a mid-run dispatch of TODO-0211 while
TODO-0204 was escalated (stage 2 blocked) → the replan wait treated ANY
TODO created after the escalation as the supervisor's answer → the live
pipeline switched to the unrelated unit → the replan unit started at the
blocked stage index (implementer skipped) → QA-0211 documented the whole
thing as BLOCKED.

Fix under test:
- pickup only on the declared front-matter marker
  ``replan_for: <escalated unit>`` — an unmarked mid-run dispatch does
  not touch the live pipeline, the escalation keeps waiting;
- the replanned unit restarts the full pipeline from stage 0;
- ``dispatch_todo(replan_for=...)`` writes the marker and refuses a
  reference to a missing / archived / done unit;
- the pickup log line names the marker and the replaced unit.
"""
from __future__ import annotations

import os
import time as _time

import pytest

import awf.pipeline_engine as engine
from awf import api
from awf.pipeline import Stage
from awf.supervisor import wait_for_supervisor_signal
from awf.unit_contract import replan_for_of

# ── helpers ──────────────────────────────────────────────────────────────


def _project(tmp_path):
    proj = tmp_path / "proj"
    for sub in ("inbox", "outbox", "logs", "context"):
        (proj / ".agentic" / sub).mkdir(parents=True, exist_ok=True)
    return proj


def _todo(
    proj, todo_id="TODO-0001", mtime: float | None = None, content="# Task\n",
):
    inbox = proj / ".agentic" / "inbox"
    ready = inbox / f"{todo_id}.ready"
    ready.write_text("", encoding="utf-8")
    (inbox / f"{todo_id}.md").write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(ready, (mtime, mtime))
    return ready


def _blocked(proj, todo_id="TODO-0001"):
    outbox = proj / ".agentic" / "outbox"
    (outbox / f"BLOCKED-{todo_id}.ready").write_text("", encoding="utf-8")
    (outbox / f"BLOCKED-{todo_id}.md").write_text("# Blocked\n", encoding="utf-8")


def _log_text(proj) -> str:
    log = proj / ".agentic" / "logs" / "orchestrator.log"
    return log.read_text(encoding="utf-8") if log.is_file() else ""


def _stage():
    return Stage(name="agent-qa-review", role="agent-qa-review", kind="execute")


@pytest.fixture
def awf_project(tmp_git_repo):
    """A dispatchable project: git repo + .agentic/ + config.yaml."""
    api.init_project(tmp_git_repo, project_name="Test")
    return tmp_git_repo


# ── (a) unmarked mid-run dispatch does not switch the pipeline ──────────


class TestUnmarkedDispatchIgnoresWait:
    def test_fresh_unmarked_todo_does_not_complete_replan_wait(
        self, tmp_path, monkeypatch,
    ):
        """(a) a fresh UNMARKED TODO (a normal mid-run dispatch) is not the
        supervisor's answer — the wait times out and the blocked unit is
        untouched."""
        proj = _project(tmp_path)
        _todo(proj, "TODO-0001", mtime=_time.time() - 1000)
        _blocked(proj, "TODO-0001")
        _todo(proj, "TODO-0002")  # fresh, unmarked
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        with pytest.raises(TimeoutError):
            wait_for_supervisor_signal(
                "replan", "TODO-0001", proj, proj / ".agentic" / "logs", timeout=1,
            )
        assert "replan signal detected" not in _log_text(proj)
        assert (proj / ".agentic" / "outbox" / "BLOCKED-TODO-0001.ready").exists()

    def test_unmarked_todo_appearing_during_wait_is_ignored(
        self, tmp_path, monkeypatch,
    ):
        """(a) the incident scenario: an unmarked TODO-0002 lands while the
        escalation for TODO-0001 is waiting — the wait must not pick it
        up (before the fix this switched the pipeline)."""
        proj = _project(tmp_path)
        _todo(proj, "TODO-0001", mtime=_time.time() - 1000)
        _blocked(proj, "TODO-0001")
        inbox = proj / ".agentic" / "inbox"
        written = {"n": 0}

        def fake_sleep(*_a, **_kw):
            if written["n"] == 0:
                written["n"] = 1
                # a normal mid-run dispatch — explicit FRESH mtime: the
                # filesystem mtime quantization (~ms) can make a "just
                # created" file look older than wall_start
                _todo(proj, "TODO-0002", mtime=_time.time() + 0.01)

        monkeypatch.setattr("time.sleep", fake_sleep)

        with pytest.raises(TimeoutError):
            wait_for_supervisor_signal(
                "replan", "TODO-0001", proj, proj / ".agentic" / "logs", timeout=1,
            )
        assert (inbox / "TODO-0002.ready").exists(), "the dispatch itself is fine"
        assert "replan signal detected" not in _log_text(proj), (
            "an unmarked TODO must not be logged as a replan pickup"
        )

    def test_escalation_keeps_waiting_for_its_own_answer(
        self, tmp_path, monkeypatch,
    ):
        """(a) after the unmarked TODO is ignored, the SAME wait still
        accepts the supervisor's marked answer — the escalation was
        waiting, not lost."""
        proj = _project(tmp_path)
        _todo(proj, "TODO-0001", mtime=_time.time() - 1000)
        _blocked(proj, "TODO-0001")
        written = {"n": 0}

        def fake_sleep(*_a, **_kw):
            if written["n"] == 0:
                written["n"] = 1
                _todo(proj, "TODO-0002", mtime=_time.time() + 0.01)
            elif written["n"] == 1:
                written["n"] = 2
                _todo(
                    proj, "TODO-0003", mtime=_time.time() + 0.01,
                    content=(
                        "---\nreplan_for: TODO-0001\n---\n# Refined task\n"
                    ),
                )

        monkeypatch.setattr("time.sleep", fake_sleep)

        sig = wait_for_supervisor_signal(
            "replan", "TODO-0001", proj, proj / ".agentic" / "logs", timeout=5,
        )
        assert sig == "TODO-0003", (
            "the marked answer is picked up, not the unmarked dispatch"
        )

    def test_handler_stops_on_replan_timeout_unit_and_stage_untouched(
        self, tmp_path, monkeypatch,
    ):
        """(a) engine level: when the replan wait times out (no marked
        answer), the pipeline STOPS on the same unit and the same stage —
        nothing is switched."""
        proj = _project(tmp_path)
        _todo(proj, "TODO-0001", mtime=_time.time() - 1000)
        _blocked(proj, "TODO-0001")
        _todo(proj, "TODO-0002")  # the unmarked mid-run dispatch
        # even the newest active unit is the unmarked one:
        assert engine._find_active_todo(proj) == "TODO-0002"

        def boom(*a, **kw):
            raise TimeoutError("Supervisor replan signal not received within 5s")

        monkeypatch.setattr(engine, "_run_supervisor_stage", boom)

        result = engine._handle_escalate(
            proj, proj / ".agentic" / "logs", "agent-qa-review", "TODO-0001",
            auto=False, stage=_stage(), retry_counts=[0, 0, 0], stage_idx=1,
            pipeline_name=None,
        )
        assert result == (1, "TODO-0001", 1), (
            "stop on the blocked unit and stage — no switch to TODO-0002"
        )


# ── (b) marked TODO: pickup as before, restart from stage 0 ────────────


class TestMarkedReplanPickup:
    def test_marked_todo_is_picked_up_and_logged(
        self, tmp_path, monkeypatch,
    ):
        """(b)/(d) the marked fresh TODO is picked up; the orchestrator log
        names the marker and the replaced unit."""
        proj = _project(tmp_path)
        _todo(proj, "TODO-0001", mtime=_time.time() - 1000)
        _blocked(proj, "TODO-0001")
        _todo(
            proj, "TODO-0002", mtime=_time.time() + 0.5,
            content="---\nreplan_for: TODO-0001\n---\n# Refined task\n",
        )
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        sig = wait_for_supervisor_signal(
            "replan", "TODO-0001", proj, proj / ".agentic" / "logs", timeout=5,
        )
        assert sig == "TODO-0002"
        log = _log_text(proj)
        assert "replan signal detected: TODO-0002" in log
        assert "replan_for=TODO-0001" in log, "the pickup line names the marker"
        assert "replacing TODO-0001" in log, "…and the replaced unit"

    def test_handler_restarts_replan_unit_at_stage_0(
        self, tmp_path, monkeypatch,
    ):
        """(b) the replanned unit starts the FULL pipeline at stage 0
        (implementer first), not the blocked stage."""
        proj = _project(tmp_path)
        _todo(proj, "TODO-0001", mtime=_time.time() - 1000)
        _blocked(proj, "TODO-0001")
        monkeypatch.setattr(
            engine, "_run_supervisor_stage", lambda *a, **kw: "TODO-0002"
        )

        result = engine._handle_escalate(
            proj, proj / ".agentic" / "logs", "agent-qa-review", "TODO-0001",
            auto=False, stage=_stage(), retry_counts=[0, 0, 0], stage_idx=2,
            pipeline_name=None,
        )
        assert result == (0, "TODO-0002", 0), (
            "the replanned unit runs from the first stage, not the blocked one"
        )
        assert "stage 0" in _log_text(proj)

    def test_handler_keeps_signal_todo_not_newest_active(
        self, tmp_path, monkeypatch,
    ):
        """(b) the unit the wait picked up (the signal) is the one run — a
        newer unmarked dispatch must not outrank the replanned unit."""
        proj = _project(tmp_path)
        _todo(proj, "TODO-0001", mtime=_time.time() - 1000)
        _blocked(proj, "TODO-0001")
        monkeypatch.setattr(
            engine, "_run_supervisor_stage", lambda *a, **kw: "TODO-0002"
        )
        monkeypatch.setattr(engine, "_find_active_todo", lambda *a: "TODO-0009")

        result = engine._handle_escalate(
            proj, proj / ".agentic" / "logs", "agent-qa-review", "TODO-0001",
            auto=False, stage=_stage(), retry_counts=[0, 0, 0], stage_idx=2,
            pipeline_name=None,
        )
        assert result == (0, "TODO-0002", 0)

    def test_handler_signal_is_blocked_unit_itself_stops_cleanly(
        self, tmp_path, monkeypatch,
    ):
        """The auto-mode fallback detection can return the STALE
        TODO-*.ready of the blocked unit itself — a replan REPLACES the
        unit, it never is it: not an answer, a clean stop (the blocked
        unit and stage stay, the BLOCKED closure is intact — e2e
        test_pipeline_blocked_then_replan)."""
        proj = _project(tmp_path)
        _todo(proj, "TODO-0001", mtime=_time.time() - 1000)
        _blocked(proj, "TODO-0001")
        monkeypatch.setattr(
            engine, "_run_supervisor_stage", lambda *a, **kw: "TODO-0001"
        )

        result = engine._handle_escalate(
            proj, proj / ".agentic" / "logs", "agent-qa-review", "TODO-0001",
            auto=True, stage=_stage(), retry_counts=[0, 0, 0], stage_idx=1,
            pipeline_name=None,
        )
        assert result == (1, "TODO-0001", 1), "clean stop, no self-replan loop"
        assert (proj / ".agentic" / "outbox" / "BLOCKED-TODO-0001.ready").exists()


# ── (c) dispatch_todo(replan_for=...) ───────────────────────────────────


class TestDispatchReplanFor:
    def _blocked_unit(self, proj):
        _todo(proj, "TODO-0001")
        _blocked(proj, "TODO-0001")

    def test_marker_written_into_contract_block(self, awf_project):
        """(c) replan_for= writes the marker; the dispatch succeeds."""
        self._blocked_unit(awf_project)
        result = api.dispatch_todo(
            awf_project, "# Refined task\n\nBody.\n", replan_for="TODO-0001",
        )
        md = (awf_project / ".agentic" / "inbox" / f"{result.todo_id}.md").read_text(
            encoding="utf-8"
        )
        assert "replan_for: TODO-0001" in md
        assert replan_for_of(md) == "TODO-0001"
        assert (awf_project / ".agentic" / "inbox" / f"{result.todo_id}.ready").is_file()

    def test_existing_contract_block_is_preserved(self, awf_project):
        """(c) the marker joins an existing block without touching it."""
        self._blocked_unit(awf_project)
        result = api.dispatch_todo(
            awf_project,
            (
                "---\n"
                "verify: [\"python3 -m pytest tests/x.py -q\"]\n"
                "---\n"
                "# Refined task\n"
            ),
            replan_for="TODO-0001",
        )
        md = (awf_project / ".agentic" / "inbox" / f"{result.todo_id}.md").read_text(
            encoding="utf-8"
        )
        assert "verify: [\"python3 -m pytest tests/x.py -q\"]" in md
        assert replan_for_of(md) == "TODO-0001"

    def test_explicit_parameter_wins_over_copy_pasted_marker(
        self, awf_project,
    ):
        """(c) the explicit argument replaces a copy-pasted marker."""
        self._blocked_unit(awf_project)
        _todo(awf_project, "TODO-0002")
        _blocked(awf_project, "TODO-0002")
        result = api.dispatch_todo(
            awf_project,
            (
                "---\n"
                "replan_for: TODO-0002\n"
                "---\n"
                "# Refined task\n"
            ),
            replan_for="TODO-0001",
        )
        md = (awf_project / ".agentic" / "inbox" / f"{result.todo_id}.md").read_text(
            encoding="utf-8"
        )
        assert replan_for_of(md) == "TODO-0001"
        assert "replan_for: TODO-0002" not in md

    def test_hand_written_marker_without_parameter_works(self, awf_project):
        """(c) a manual ``replan_for:`` line in the content is enough."""
        self._blocked_unit(awf_project)
        result = api.dispatch_todo(
            awf_project,
            (
                "---\n"
                "replan_for: TODO-0001\n"
                "---\n"
                "# Refined task\n"
            ),
        )
        md = (awf_project / ".agentic" / "inbox" / f"{result.todo_id}.md").read_text(
            encoding="utf-8"
        )
        assert replan_for_of(md) == "TODO-0001"

    def test_missing_todo_refused_without_side_effects(self, awf_project):
        """(c) replan_for pointing at a nonexistent unit is refused — no
        TODO, no baseline, no signal."""
        result_refusal = pytest.raises(
            api.AwfApiError, match="no such TODO",
            # type: ignore[arg-type]
        )
        with result_refusal:
            api.dispatch_todo(
                awf_project, "# Refined task\n", replan_for="TODO-0999",
            )
        inbox = awf_project / ".agentic" / "inbox"
        assert list(inbox.glob("TODO-*.md")) == []
        assert list(inbox.glob("TODO-*.ready")) == []
        assert not list((awf_project / ".agentic" / "context").glob("BASELINE-*"))

    def test_invalid_id_refused_without_side_effects(self, awf_project):
        """(c) a value that is not a TODO id is refused before anything."""
        with pytest.raises(api.AwfApiError, match="replan_for"):
            api.dispatch_todo(awf_project, "# Refined task\n", replan_for="oops")
        inbox = awf_project / ".agentic" / "inbox"
        assert list(inbox.glob("TODO-*.md")) == []
        assert list(inbox.glob("TODO-*.ready")) == []

    def test_archived_unit_refused_without_side_effects(self, awf_project):
        """(c) a replan replaces a WAITING unit — an archived one is a
        refusal."""
        done = awf_project / ".agentic" / "done" / "TODO-0001"
        done.mkdir(parents=True)
        (done / "TODO.md").write_text("archived", encoding="utf-8")
        with pytest.raises(api.AwfApiError, match="archived"):
            api.dispatch_todo(
                awf_project, "# Refined task\n", replan_for="TODO-0001",
            )
        inbox = awf_project / ".agentic" / "inbox"
        assert list(inbox.glob("TODO-*.md")) == []
        assert list(inbox.glob("TODO-*.ready")) == []

    def test_done_closed_unit_refused_without_side_effects(self, awf_project):
        """(c) a unit closed as done (DONE closure in outbox) is a
        refusal."""
        _todo(awf_project, "TODO-0001")
        outbox = awf_project / ".agentic" / "outbox"
        (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")
        with pytest.raises(api.AwfApiError, match="closed as done"):
            api.dispatch_todo(
                awf_project, "# Refined task\n", replan_for="TODO-0001",
            )
        inbox = awf_project / ".agentic" / "inbox"
        assert [p.name for p in inbox.glob("TODO-*.md")] == ["TODO-0001.md"]
        assert [p.name for p in inbox.glob("TODO-*.ready")] == ["TODO-0001.ready"]
        assert not list((awf_project / ".agentic" / "context").glob("BASELINE-*"))


# ── (d) marker parsing: body text is not a marker ───────────────────────


class TestMarkerParsing:
    def test_body_mention_is_not_a_marker(self):
        """A TODO ABOUT the replan feature quotes the marker in its body —
        body text never marks a unit (only the front-matter block)."""
        content = (
            "# TODO — replan fix\n\n"
            "The pickup must check the front-matter marker "
            "`replan_for: TODO-0001` — see the engine.\n"
        )
        assert replan_for_of(content) is None

    def test_front_matter_marker_is_read(self):
        content = "---\nreplan_for: TODO-0001\n---\n# Task\n"
        assert replan_for_of(content) == "TODO-0001"

    def test_other_key_contract_error_does_not_mask_marker(self):
        """A sibling key that violates the contract (verify not a list)
        still parses as YAML — the pickup reads ``replan_for`` only and
        the valid marker is not masked by it."""
        content = (
            "---\n"
            "verify: \"not-a-list\"\n"
            "replan_for: TODO-0001\n"
            "---\n"
            "# Task\n"
        )
        assert replan_for_of(content) == "TODO-0001"

    def test_unreadable_block_is_not_a_marker(self):
        """An unparseable block (broken YAML) is not a marker — never
        raises."""
        content = "---\nreplan_for: [unclosed\n---\n# Task\n"
        assert replan_for_of(content) is None

    def test_invalid_marker_value_is_not_a_marker(self):
        content = "---\nreplan_for: nope\n---\n# Task\n"
        assert replan_for_of(content) is None

    def test_no_content_is_not_a_marker(self):
        assert replan_for_of(None) is None
        assert replan_for_of("") is None


# ── REVIEW fix: the auto-subprocess surface (watcher + fallback) ────────


class TestAutoSurfaceReplanMarker:
    """REVIEW fix (12.10, iter 2): the same false positive lived on the
    auto-subprocess surface — the watcher (watch_new_glob) and the fallback
    ``_detect_supervisor_signal`` accepted ANY fresh TODO-*.ready, marker
    unchecked. QA repro: blocked TODO-0001 (old) + fresh unmarked
    TODO-0002 → the fallback returned 'TODO-0002' (hijack)."""

    def _proj(self, tmp_path):
        proj = _project(tmp_path)
        _todo(proj, "TODO-0001", mtime=_time.time() - 1000)  # blocked unit
        _blocked(proj, "TODO-0001")
        return proj

    def test_fallback_rejects_fresh_unmarked_todo(self, tmp_path):
        """(a) the QA repro: the fallback detection must not return a fresh
        UNMARKED TODO — only the marker names the answer."""
        proj = self._proj(tmp_path)
        _todo(proj, "TODO-0002", mtime=_time.time() + 0.5)  # fresh, unmarked
        from awf.supervisor import _detect_supervisor_signal

        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"
        assert _detect_supervisor_signal(
            "replan", "TODO-0001", inbox, outbox,
        ) == "", "a fresh unmarked TODO must not be the replan answer"

    def test_fallback_accepts_marked_todo_as_before(self, tmp_path):
        """(b) the marked fresh TODO is consumed as before."""
        proj = self._proj(tmp_path)
        _todo(
            proj, "TODO-0002", mtime=_time.time() + 0.5,
            content="---\nreplan_for: TODO-0001\n---\n# Refined task\n",
        )
        from awf.supervisor import _detect_supervisor_signal

        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"
        assert _detect_supervisor_signal(
            "replan", "TODO-0001", inbox, outbox,
        ) == "TODO-0002"

    def test_fallback_never_returns_the_blocked_unit_itself(self, tmp_path):
        """A replan REPLACES the unit — the unit's own (stale) .ready is
        not an answer even when it is the newest file by mtime."""
        proj = self._proj(tmp_path)
        # make the blocked unit's own .ready the NEWEST file in the inbox
        _todo(proj, "TODO-0001", mtime=_time.time() + 1.0)
        from awf.supervisor import _detect_supervisor_signal

        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"
        assert _detect_supervisor_signal(
            "replan", "TODO-0001", inbox, outbox,
        ) == ""

    def test_fallback_plan_kind_unchanged(self, tmp_path):
        """The plan kind creates the first unit (there is no unit to
        replace) — newest-by-mtime stays, no marker needed."""
        proj = _project(tmp_path)
        _todo(proj, "TODO-0001", mtime=_time.time() + 0.2)
        _todo(proj, "TODO-0002", mtime=_time.time() + 0.5)
        from awf.supervisor import _detect_supervisor_signal

        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"
        assert _detect_supervisor_signal(
            "plan", "", inbox, outbox,
        ) == "TODO-0002"

    def test_watcher_filter_ignores_unmarked_accepts_marked(self, tmp_path):
        """(a)/(b) the watcher (watch_new_glob) fires only on the marked
        TODO — an unmarked fresh .ready does not end the wait."""
        proj = self._proj(tmp_path)
        inbox = proj / ".agentic" / "inbox"
        from awf.signal_watch import _detect_new_signal
        from awf.supervisor import make_replan_glob_accept

        accept = make_replan_glob_accept(inbox, "TODO-0001")
        # the blocked unit's .ready existed before the subprocess start
        snapshot = {"TODO-0001.ready"}

        # an unmarked mid-run dispatch appears — not a signal
        _todo(proj, "TODO-0002", mtime=_time.time() + 0.1)
        assert _detect_new_signal(
            [], set(), (inbox, "TODO-*.ready"), snapshot, new_glob_accept=accept,
        ) is None, "an unmarked TODO must not end the replan wait"

        # the marked answer appears — the signal, even though the unmarked
        # file has the smaller id
        _todo(
            proj, "TODO-0003", mtime=_time.time() + 0.2,
            content="---\nreplan_for: TODO-0001\n---\n# Refined task\n",
        )
        assert _detect_new_signal(
            [], set(), (inbox, "TODO-*.ready"), snapshot, new_glob_accept=accept,
        ) == "TODO-0003.ready"

    def test_watcher_filter_is_optional(self, tmp_path):
        """The new parameter is backward compatible: without a predicate
        any new file is a signal (the plan kind relies on this)."""
        proj = _project(tmp_path)
        inbox = proj / ".agentic" / "inbox"
        from awf.signal_watch import _detect_new_signal

        _todo(proj, "TODO-0002")
        assert _detect_new_signal(
            [], set(), (inbox, "TODO-*.ready"), set(),
        ) == "TODO-0002.ready"


# ── REVIEW fix: the engine no longer rescues a replan with the newest ───


class TestEngineNoUnmarkedFallback:
    """REVIEW fix: the engine used to fall back to the NEWEST ACTIVE unit
    when the replan signal was empty — an unmarked mid-run dispatch
    (newest by construction) hijacked the pipeline on the auto surface.
    The marked signal is the only answer; a rollback without one re-runs
    the SAME unit, never a third one."""

    def _stages(self):
        return [
            Stage(name="plan", role="supervisor", kind="plan"),
            Stage(name="implement", role="worker", kind="execute"),
            Stage(name="verify", role="supervisor", kind="verify"),
        ]

    def test_escalate_empty_signal_stops_unmarked_unit_not_run(
        self, tmp_path, monkeypatch,
    ):
        """The auto replan surface returns '' (no marked answer) while a
        fresh UNMARKED dispatch is the newest active unit — the pipeline
        stops on the blocked unit, the dispatch is untouched."""
        proj = _project(tmp_path)
        _todo(proj, "TODO-0001", mtime=_time.time() - 1000)
        _blocked(proj, "TODO-0001")
        _todo(proj, "TODO-0002")  # the unmarked mid-run dispatch
        assert engine._find_active_todo(proj) == "TODO-0002"

        monkeypatch.setattr(engine, "_run_supervisor_stage", lambda *a, **kw: "")

        result = engine._handle_escalate(
            proj, proj / ".agentic" / "logs", "agent-qa-review", "TODO-0001",
            auto=True, stage=_stage(), retry_counts=[0, 0, 0], stage_idx=1,
            pipeline_name=None,
        )
        assert result == (1, "TODO-0001", 1), (
            "no marked answer — stop on the blocked unit, no switch"
        )
        assert (proj / ".agentic" / "outbox" / "BLOCKED-TODO-0001.ready").exists()

    def test_rollback_empty_signal_reruns_same_unit(self, tmp_path, monkeypatch):
        """A rollback without a marked answer re-runs the SAME unit at the
        target stage — never a fresh unmarked dispatch."""
        proj = _project(tmp_path)
        _todo(proj, "TODO-0001", mtime=_time.time() - 1000)
        _todo(proj, "TODO-0002")  # the unmarked mid-run dispatch
        monkeypatch.setattr(engine, "_run_supervisor_stage", lambda *a, **kw: "")

        result = engine._handle_rollback(
            proj, proj / ".agentic" / "logs", self._stages(), "TODO-0001",
            auto=True, target="implement",
        )
        assert result == (1, "TODO-0001", 0), (
            "the same unit at the target stage, not the newest dispatch"
        )

    def test_rollback_marked_signal_runs_the_marked_unit(
        self, tmp_path, monkeypatch,
    ):
        """A marked replan answer after rollback runs that unit."""
        proj = _project(tmp_path)
        _todo(proj, "TODO-0001", mtime=_time.time() - 1000)
        _todo(
            proj, "TODO-0002", mtime=_time.time() + 0.5,
            content="---\nreplan_for: TODO-0001\n---\n# Refined task\n",
        )
        monkeypatch.setattr(
            engine, "_run_supervisor_stage", lambda *a, **kw: "TODO-0002"
        )

        result = engine._handle_rollback(
            proj, proj / ".agentic" / "logs", self._stages(), "TODO-0001",
            auto=True, target="implement",
        )
        assert result == (1, "TODO-0002", 0)
