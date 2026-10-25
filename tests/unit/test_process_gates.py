"""TODO-0240 — process gates (the stage-cost unit).

Covered surfaces:
(a) ``verify_pack`` section ``qa_findings``: none / green / red, the verdict
    follows the run;
(b) ``review_append``: a started unit gets a numbered review section, a
    backup lands in ``context/``; empty text / closed unit / live pipeline
    are refused;
(c) ``run_revise`` replace ``{'from', 'to'}``: success, missing target,
    duplicate, idempotent repeat;
(d) ``remove_todo`` refuses a TODO the active run queue still references,
    and succeeds once the queue no longer names it;
(e) ``check-contracts`` closure-matrix lock: an FS boundary without the
    matrix is red, with the matrix is green.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import awf.api.pipeline as api_pipeline
import awf.verify_pack as verify_pack_mod
from awf import api, run_state
from awf.api import hygiene
from awf.unit_contract import parse_todo_contract
from awf.verify_pack import verify_pack

REPO_ROOT = Path(__file__).resolve().parents[2]
T1, T2, T3, T4 = "TODO-0001", "TODO-0002", "TODO-0003", "TODO-0004"


@pytest.fixture
def proj(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="ProcessGates")
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str = T1, body: str | None = None) -> Path:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    md = inbox / f"{todo_id}.md"
    md.write_text(body or f"# {todo_id}\n", encoding="utf-8")
    return md


def _fake_start(monkeypatch, proj: Path) -> None:
    def fake_start(project_dir, **kw):
        return api_pipeline.StartResult(
            run_mode="background",
            run_id=1,
            log_file=str(proj / ".agentic" / "logs" / "awf-start.out"),
            exit_code=0,
            message="ok",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)


def _launch_first(proj: Path, monkeypatch, queue: list[str]) -> None:
    api.run_start(proj, queue=queue)
    _fake_start(monkeypatch, proj)
    assert api.run_next(proj).action == "started"


# ─── (a) qa_findings ─────────────────────────────────────────────────────


def _qa_project(
    tmp_path: Path, script: str | None, name: str | None = None
) -> Path:
    project = tmp_path / "qa"
    (project / ".agentic" / "inbox").mkdir(parents=True)
    (project / ".agentic" / "outbox").mkdir(parents=True)
    (project / ".agentic" / "context").mkdir(parents=True)
    (project / ".agentic" / "inbox" / f"{T1}.md").write_text("# t\n", encoding="utf-8")
    if script is not None:
        filename = name or f"QA-{T1[len('TODO-'):]}-findings-test.py"
        (project / ".agentic" / "context" / filename).write_text(
            script, encoding="utf-8"
        )
    return project


def _pytest_file(body: str) -> str:
    return "def test_finding():\n" + "".join(f"    {line}\n" for line in body.splitlines())


def _pack(project: Path):
    return verify_pack(
        project,
        T1,
        write_report=False,
        gate_timeout=30,
        cmd_timeout=60,
        lint_timeout=60,
    )


class TestQaFindings:
    def test_none_skips(self, tmp_path: Path) -> None:
        result = _pack(_qa_project(tmp_path, None))
        assert result.sections["qa_findings"] == "skipped"
        assert result.details["qa_findings"] == "none"

    def test_green_passes(self, tmp_path: Path) -> None:
        result = _pack(_qa_project(tmp_path, _pytest_file("assert True")))
        assert result.sections["qa_findings"] == "pass"
        assert result.verdict == "ok"

    def test_red_fails_the_pack(self, tmp_path: Path) -> None:
        result = _pack(
            _qa_project(tmp_path, _pytest_file("assert False, 'still broken'"))
        )
        assert result.sections["qa_findings"] == "fail"
        assert result.verdict == "failed"
        assert result.exit_code == 1

    def test_full_id_name_is_also_read(self, tmp_path: Path) -> None:
        """REVIEW (supervisor v2, V1): the canonical name drops ``TODO-`` but
        the full id form must stay readable — one reader accepts both."""
        result = _pack(
            _qa_project(
                tmp_path,
                _pytest_file("assert True"),
                name=f"QA-{T1}-findings-test.py",
            )
        )
        assert result.sections["qa_findings"] == "pass"
        assert result.verdict == "ok"

    def test_skipped_only_module_fails_as_no_tests(self, tmp_path: Path) -> None:
        """REVIEW (supervisor v4, X3): pytest exits 0 when every test in the
        module is skipped — rc alone would call that proof. A finding is only
        disproved by a test that actually ran."""
        source = (
            "import pytest\n\n\n"
            "@pytest.mark.skip(reason='not yet')\n"
            "def test_finding():\n"
            "    assert True\n"
        )
        result = _pack(_qa_project(tmp_path, source))
        assert result.sections["qa_findings"] == "fail"
        assert result.verdict == "failed"
        assert "no tests executed" in result.details["qa_findings"]

    def test_tripwire_plugin_is_loaded(self, tmp_path: Path) -> None:
        """REVIEW (supervisor v6, Z1): the QA file lives outside ``tests/``,
        so its pytest run does not load the suite's conftest tripwire. The
        runner must load the packaged plugin explicitly — this is the safe
        red probe (it cannot signal anything even on a regression)."""
        source = "import sys\n\n\ndef test_finding():\n    assert 'awf._safety_tripwire' in sys.modules\n"
        result = _pack(_qa_project(tmp_path, source))
        assert result.sections["qa_findings"] == "pass"

    def test_missing_timeout_plugin_does_not_red_a_green_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """REVIEW (supervisor v7, AA2): ``--timeout`` is a pytest-timeout
        flag and the plugin lives in the ``dev`` extra. A runtime without it
        answers rc=4 ("unrecognized arguments") and a GREEN QA file reads as
        a confirmed finding. With the plugin hidden the section must still
        measure the file."""
        monkeypatch.setattr(
            verify_pack_mod, "_pytest_timeout_available", lambda: False
        )
        result = _pack(_qa_project(tmp_path, _pytest_file("assert True")))
        assert result.sections["qa_findings"] == "pass"
        assert result.verdict == "ok"

    def test_hanging_file_is_killed_by_wall_clock(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """REVIEW (supervisor v7, AA2): without the plugin the guard is the
        runner's own wall-clock kill, not the flag. The section must answer
        ``timeout`` (the process-group kill), not a pytest failure. A test
        that sleeps past the deadline proves the kill fired."""
        monkeypatch.setattr(
            verify_pack_mod, "_pytest_timeout_available", lambda: False
        )
        project = _qa_project(
            tmp_path,
            "import time\n\n\ndef test_finding():\n    time.sleep(30)\n",
        )
        result = verify_pack(
            project,
            T1,
            write_report=False,
            gate_timeout=30,
            cmd_timeout=2,
            lint_timeout=30,
        )
        assert result.sections["qa_findings"] == "fail"
        assert result.details["qa_findings"] == "timeout"

    def test_qa_file_cannot_signal_the_session(self, tmp_path: Path) -> None:
        """REVIEW (supervisor v6, Z1): the natural repro of a session-kill
        finding must hit the tripwire, not the kernel. Signal ``0`` keeps
        the test safe by construction: even if the guard ever regresses,
        ``kill(-1, 0)`` only checks permission and kills nothing — the test
        still fails, but the owner's session survives."""
        source = (
            "import os\n\nimport pytest\n\n\n"
            "def test_finding():\n"
            "    with pytest.raises(RuntimeError, match='SAFETY'):\n"
            "        os.killpg(1, 0)\n"
        )
        result = _pack(_qa_project(tmp_path, source))
        assert result.sections["qa_findings"] == "pass"
        assert result.verdict == "ok"


# ─── (b) review_append ───────────────────────────────────────────────────


class TestReviewAppend:
    @staticmethod
    def _mark_started(proj: Path, todo_id: str = T1) -> None:
        outbox = proj / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / f"PROGRESS-{todo_id}.md").write_text("stage: dev\n", encoding="utf-8")

    def test_appends_numbered_sections_with_backup(self, proj: Path) -> None:
        md = _write_todo(proj)
        self._mark_started(proj)
        first = api.review_append(proj, T1, "первая правка")
        second = api.review_append(proj, T1, "вторая правка")

        assert first.round == 1 and first.section.endswith("виток 1)")
        assert second.round == 2 and second.section.endswith("виток 2)")
        text = md.read_text(encoding="utf-8")
        assert "## REVIEW-фикс (супервизор, виток 1)" in text
        assert "## REVIEW-фикс (супервизор, виток 2)" in text
        assert text.index("виток 1") < text.index("виток 2")
        for result in (first, second):
            assert (proj / result.backup).is_file()

    def test_round_number_never_repeats(self, proj: Path) -> None:
        """REVIEW (architect): the file may already carry a hand-written
        section ("… виток 2 — заметка") — the numbering must not repeat it.
        ``len(sections) + 1`` did exactly that on this repo's TODO-0240."""
        md = _write_todo(proj)
        self._mark_started(proj)
        md.write_text(
            md.read_text(encoding="utf-8")
            + "\n## REVIEW-фикс (супервизор, виток 2 — заметка)\n\nтекст\n",
            encoding="utf-8",
        )
        result = api.review_append(proj, T1, "третья правка")
        assert result.round == 3
        assert "## REVIEW-фикс (супервизор, виток 3)" in md.read_text(
            encoding="utf-8"
        )

    def test_same_operation_id_appends_once(self, proj: Path) -> None:
        """REVIEW (supervisor v6, Z2): a retry after a transport cut carries
        the same operation_id — it returns the recorded outcome instead of
        appending a second section and moving the round."""
        md = _write_todo(proj)
        self._mark_started(proj)
        first = api.review_append(proj, T1, "правка", operation_id="ra-1")
        again = api.review_append(proj, T1, "правка", operation_id="ra-1")
        assert again.as_dict() == first.as_dict()
        text = md.read_text(encoding="utf-8")
        assert text.count("## REVIEW-фикс (супервизор, виток 1)") == 1
        assert "виток 2" not in text
        # REVIEW (qa): the replay is also side-effect free — one backup,
        # the trace of the single real append; a second would prove the
        # replay re-ran the write.
        backups = sorted(
            (proj / ".agentic" / "context").glob(f"{T1}.md.bak-*")
        )
        assert len(backups) == 1, [b.name for b in backups]

    def test_same_operation_id_with_different_text_refused(
        self, proj: Path
    ) -> None:
        """REVIEW (supervisor v7, AA1): one key — one payload. A repeat with
        a DIFFERENT text is not the recorded operation; replaying the first
        result would silently drop the second request."""
        md = _write_todo(proj)
        self._mark_started(proj)
        api.review_append(proj, T1, "FIRST TEXT", operation_id="ra-dup")
        with pytest.raises(api.AwfApiError, match="different payload"):
            api.review_append(proj, T1, "SECOND TEXT", operation_id="ra-dup")
        text = md.read_text(encoding="utf-8")
        assert "FIRST TEXT" in text
        assert "SECOND TEXT" not in text
        assert text.count("виток 1") == 1

    def test_keeps_ready_baseline_and_contract(self, proj: Path) -> None:
        """REVIEW (qa): the tool's promise is "the number, ``.ready`` and
        the baseline stay untouched" — a started unit must keep all three,
        and the TODO contract (verify/gates) must still parse after the
        appended section. A regeneration of the baseline or a lost ``.ready``
        would silently change what the re-run proves."""
        body = (
            "---\nverify:\n  - python -m pytest -q\ngates: [contracts]\n---\n\n"
            "<!-- role_hint: agent-implementer -->\n\n# TODO\n\n"
            "## Требования\nx\n"
        )
        md = _write_todo(proj, T1, body)
        self._mark_started(proj)
        ready = proj / ".agentic" / "inbox" / f"{T1}.ready"
        ready.touch()
        baseline = proj / ".agentic" / "context" / f"BASELINE-{T1}.sha"
        baseline.write_text("deadbeef\n", encoding="utf-8")

        api.review_append(proj, T1, "фикс: проверка пустого входа")

        assert ready.is_file()
        assert baseline.read_text(encoding="utf-8") == "deadbeef\n"
        contract, unknown = parse_todo_contract(md.read_text(encoding="utf-8"))
        assert unknown == []
        assert contract is not None
        assert contract["verify"] == ["python -m pytest -q"]

    def test_empty_text_refused(self, proj: Path) -> None:
        _write_todo(proj)
        with pytest.raises(api.AwfApiError, match="empty"):
            api.review_append(proj, T1, "   ")

    def test_closed_unit_refused(self, proj: Path) -> None:
        _write_todo(proj)
        archived = proj / ".agentic" / "done" / T1
        archived.mkdir(parents=True)
        (archived / "TODO.md").write_text("# done\n", encoding="utf-8")
        with pytest.raises(api.AwfApiError, match="closed"):
            api.review_append(proj, T1, "fix")

    def test_archived_unit_refused_as_closed(self, proj: Path) -> None:
        """REVIEW (qa): the real archive MOVES the inbox file to
        ``done/<id>/TODO.md`` — ``test_closed_unit_refused`` above builds a
        state ``awf restore`` itself forbids (both files at once). With the
        real shape the "closed" branch is unreachable and the caller is told
        "not found in inbox" — no restore hint for an archived unit, exactly
        the diagnosis the tool's docstring promises."""
        md = _write_todo(proj)
        archived = proj / ".agentic" / "done" / T1
        archived.mkdir(parents=True)
        md.rename(archived / "TODO.md")
        with pytest.raises(api.AwfApiError, match="closed|archived|restore"):
            api.review_append(proj, T1, "fix")

    def test_never_started_refused(self, proj: Path) -> None:
        """REVIEW (supervisor v2, V7): the sibling writer of a queued unit is
        ``todo-update`` — a review fix on a unit with no PROGRESS/signals
        would rewrite a task nobody has touched."""
        _write_todo(proj)
        with pytest.raises(api.AwfApiError, match="has not started"):
            api.review_append(proj, T1, "fix")

    def test_live_pipeline_refused(self, proj: Path, monkeypatch) -> None:
        _write_todo(proj)
        monkeypatch.setattr(
            hygiene, "check_pipeline_running", lambda _p: (True, 4242, "x")
        )
        monkeypatch.setattr(hygiene, "read_state", lambda _p: {"todo_id": T1})
        with pytest.raises(api.AwfApiError, match="live pipeline"):
            api.review_append(proj, T1, "fix")

    def test_live_service_run_in_the_launch_window_refused(
        self, proj: Path, monkeypatch
    ) -> None:
        """REVIEW (qa): the liveness predicate says "the engine owns this
        file" — the SERVICE slot owns it too. ``run_service_start`` writes
        ``service-run.yaml`` with ``current`` BEFORE the first stage writes
        ``state.todo_id``; a predicate that reads only the main slot lets a
        review section land on a file the service engine is about to use.
        """
        self._mark_started(proj)
        _write_todo(proj)
        run_state.write_run(
            proj,
            slot="service",
            active=True,
            queue=[{"todo_id": T1, "pipeline": "svc"}],
            index=0,
            current=T1,
            generation=1,
        )
        monkeypatch.setattr(
            hygiene, "check_pipeline_running", lambda _p: (True, 4242, "x")
        )
        monkeypatch.setattr(hygiene, "read_state", lambda _p: {})
        with pytest.raises(api.AwfApiError, match="live pipeline"):
            api.review_append(proj, T1, "fix")


# ─── (b2) one liveness rule for every writer ─────────────────────────────


class TestLivePipelineSeam:
    """REVIEW (architect): "the engine owns this file" is ONE fact.

    The engine writes ``state.todo_id`` only after the first stage starts;
    ``run.yaml.current`` names the unit from the launch. Every writer of
    the same file must refuse on EITHER half, or a unit can be rewritten
    in the window between the launch and the first stage's state write.
    One helper serves all three, so the refusals cannot disagree.
    """

    @staticmethod
    def _arm_current(proj: Path, todo_id: str) -> None:
        run_state.write_run(
            proj,
            active=True,
            queue=[{"todo_id": todo_id, "pipeline": ""}],
            index=0,
            current=todo_id,
            generation=1,
        )

    @staticmethod
    def _running(monkeypatch, proj: Path) -> None:
        monkeypatch.setattr(
            hygiene, "check_pipeline_running", lambda _p: (True, 4242, "x")
        )
        monkeypatch.setattr(hygiene, "read_state", lambda _p: {})

    def test_update_refused_by_run_current_alone(
        self, proj: Path, monkeypatch
    ) -> None:
        _write_todo(proj)
        self._arm_current(proj, T1)
        self._running(monkeypatch, proj)
        with pytest.raises(api.AwfApiError, match="live pipeline"):
            api.update_todo(proj, T1, "v2")

    def test_retire_refused_by_run_current_alone(
        self, proj: Path, monkeypatch
    ) -> None:
        _write_todo(proj)
        self._arm_current(proj, T1)
        self._running(monkeypatch, proj)
        with pytest.raises(api.AwfApiError, match="live pipeline"):
            api.retire_todo(proj, T1, "abandoned")

    def test_review_refused_by_run_current_alone(
        self, proj: Path, monkeypatch
    ) -> None:
        _write_todo(proj)
        self._arm_current(proj, T1)
        self._running(monkeypatch, proj)
        with pytest.raises(api.AwfApiError, match="live pipeline"):
            api.review_append(proj, T1, "fix")

    def test_other_units_current_does_not_refuse(
        self, proj: Path, monkeypatch
    ) -> None:
        _write_todo(proj, T1)
        self._arm_current(proj, T2)
        self._running(monkeypatch, proj)
        result = api.update_todo(proj, T1, "v2")
        assert result.todo_id == T1


# ─── (c) run_revise replace ──────────────────────────────────────────────


class TestRunReviseReplace:
    def _run(self, proj: Path, monkeypatch) -> None:
        for tid in (T1, T2, T3, T4):
            _write_todo(proj, tid)
        _launch_first(proj, monkeypatch, [T1, T2, T3])

    def test_replace_in_place(self, proj: Path, monkeypatch) -> None:
        self._run(proj, monkeypatch)
        result = api.run_revise(
            proj, queue=[{"from": T2, "to": T4}], reason="swap", key="rep-1"
        )
        assert result.action == "applied"
        assert result.changes == [
            {"todo_id": T4, "from": T2, "to": T4, "class": "replaced"}
        ]
        state = run_state.read_run(proj)
        ids = [q["todo_id"] for q in state["queue"]]
        assert ids == [T1, T4, T3]
        assert state["index"] == 1 and state["current"] == T1
        assert state["revisions"][0]["changes"][0]["class"] == "replaced"

    def test_replace_keeps_the_element_pipeline(self, proj: Path, monkeypatch) -> None:
        """REVIEW (qa): the replace keeps the element's position AND its
        pipeline. The in-place test runs a queue whose pipelines are all the
        config default, so a lost pipeline would pass it — this one pins a
        non-default pipeline on the replaced element."""
        for tid in (T1, T2, T3):
            _write_todo(proj, tid)
        api.run_start(
            proj,
            queue=[
                {"todo_id": T1, "pipeline": ""},
                {"todo_id": T2, "pipeline": "audit-llm"},
            ],
        )
        _fake_start(monkeypatch, proj)
        assert api.run_next(proj).action == "started"

        result = api.run_revise(
            proj, queue=[{"from": T2, "to": T3}], key="rep-pipe"
        )
        assert result.action == "applied"
        queue = run_state.read_run(proj)["queue"]
        assert [q["todo_id"] for q in queue] == [T1, T3]
        assert queue[1]["pipeline"] == "audit-llm"

    def test_missing_target_refused_atomically(self, proj: Path, monkeypatch) -> None:
        self._run(proj, monkeypatch)
        result = api.run_revise(
            proj, queue=[{"from": T2, "to": "TODO-9999"}], key="rep-missing"
        )
        assert result.action == "refused"
        assert result.conflicts
        assert [q["todo_id"] for q in run_state.read_run(proj)["queue"]] == [
            T1,
            T2,
            T3,
        ]
        assert run_state.read_run(proj).get("revisions") in (None, [])

    def test_ready_without_a_file_is_not_launchable(
        self, proj: Path, monkeypatch
    ) -> None:
        """REVIEW (architect): the replace gate asks the ENGINE's launch
        question. ``run_next`` refuses a missing/empty ``.md``, so a stray
        ``.ready`` must not pass the target through — otherwise the queue
        gets an element that cannot start."""
        self._run(proj, monkeypatch)
        inbox = proj / ".agentic" / "inbox"
        (inbox / f"{T4}.md").unlink()
        (inbox / f"{T4}.ready").touch()
        result = api.run_revise(
            proj, queue=[{"from": T2, "to": T4}], key="rep-ready"
        )
        assert result.action == "refused"
        assert any("launchable" in c["reason"] for c in result.conflicts)
        assert [q["todo_id"] for q in run_state.read_run(proj)["queue"]] == [
            T1,
            T2,
            T3,
        ]

    def test_a_valid_change_is_not_applied_when_a_sibling_conflicts(
        self, proj: Path, monkeypatch
    ) -> None:
        """REVIEW (qa): the refusal is ATOMIC across the WHOLE request. A
        request carrying one applicable change and one conflict must leave
        the queue untouched — applying the valid half would contradict the
        answer ("Nothing was changed"). The single-item tests cannot see
        this: a conflict check placed after the mutator would still refuse
        them."""
        self._run(proj, monkeypatch)
        (proj / ".agentic" / "inbox" / f"{T4}.md").unlink()
        result = api.run_revise(
            proj,
            queue=[
                {"todo_id": T3, "pipeline": "audit-llm"},
                {"from": T2, "to": T4},
            ],
            key="rep-atomic",
        )
        assert result.action == "refused"
        assert result.conflicts
        state = run_state.read_run(proj)
        assert [q["todo_id"] for q in state["queue"]] == [T1, T2, T3]
        assert [q["pipeline"] for q in state["queue"]] == ["", "", ""]
        assert state.get("revisions") in (None, [])

    def test_duplicate_target_refused(self, proj: Path, monkeypatch) -> None:
        self._run(proj, monkeypatch)
        result = api.run_revise(
            proj, queue=[{"from": T2, "to": T3}], key="rep-dup"
        )
        assert result.action == "refused"
        assert any("duplicate" in c["reason"] for c in result.conflicts)

    def test_same_key_is_idempotent(self, proj: Path, monkeypatch) -> None:
        self._run(proj, monkeypatch)
        first = api.run_revise(proj, queue=[{"from": T2, "to": T4}], key="rep-idem")
        again = api.run_revise(proj, queue=[{"from": T2, "to": T4}], key="rep-idem")
        assert first.action == "applied"
        assert again.action == "noop"
        state = run_state.read_run(proj)
        assert len(state["revisions"]) == 1
        assert [q["todo_id"] for q in state["queue"]] == [T1, T4, T3]


    def test_same_key_with_a_different_queue_refused(
        self, proj: Path, monkeypatch
    ) -> None:
        """REVIEW (supervisor v7, AA1): the key names ONE request. A repeat
        carrying another queue is refused as a payload mismatch — not a
        silent no-op of the first revision and not a stale conflict."""
        self._run(proj, monkeypatch)
        first = api.run_revise(
            proj, queue=[{"from": T2, "to": T4}], key="rep-pay"
        )
        again = api.run_revise(
            proj, queue=[{"from": T3, "to": T4}], key="rep-pay"
        )
        assert first.action == "applied"
        assert again.action == "refused"
        assert "different payload" in again.message
        state = run_state.read_run(proj)
        assert len(state["revisions"]) == 1
        assert [q["todo_id"] for q in state["queue"]] == [T1, T4, T3]

    def test_idempotent_repeat_has_no_conflicts(self, proj: Path, monkeypatch) -> None:
        """REVIEW (qa): the repeat is a NO-OP, not a failed plan. The early
        replay returns before the conflicts matter, but the stale "source is
        not in the run queue" conflict (the first call moved it) still leaked
        into the answer — a caller reads ``conflicts`` to decide whether the
        plan is in force and would take a successful replay for a refusal."""
        self._run(proj, monkeypatch)
        first = api.run_revise(proj, queue=[{"from": T2, "to": T4}], key="rep-idem-c")
        again = api.run_revise(proj, queue=[{"from": T2, "to": T4}], key="rep-idem-c")
        assert first.action == "applied"
        assert again.action == "noop"
        assert again.conflicts == []


# ─── (d) todo_remove vs the run queue ────────────────────────────────────


class TestRemoveTodoRunReferences:
    def test_refused_while_in_the_run_queue(self, proj: Path) -> None:
        _write_todo(proj, T1)
        api.run_start(proj, queue=[T1])
        with pytest.raises(api.AwfApiError, match="active run queue"):
            api.remove_todo(proj, T1)
        assert (proj / ".agentic" / "inbox" / f"{T1}.md").is_file()

    def test_refused_while_in_the_service_run(self, proj: Path) -> None:
        """REVIEW (architect): both run slots hold references — the service
        run (its own one-unit queue) must refuse the removal too, or its
        slot keeps naming a file that is gone."""
        _write_todo(proj, T2)
        run_state.write_run(
            proj,
            slot="service",
            active=True,
            queue=[{"todo_id": T2, "pipeline": "svc"}],
            index=0,
            current="",
            generation=1,
        )
        with pytest.raises(api.AwfApiError, match="active run queue") as exc:
            api.remove_todo(proj, T2)
        assert "awf_run_service_finish" in str(exc.value)
        assert (proj / ".agentic" / "inbox" / f"{T2}.md").is_file()

    def test_progress_is_a_separate_fact_not_a_signal(self, proj: Path) -> None:
        """REVIEW (supervisor v4, X2): the helper's docstring says PROGRESS
        is not folded into the signal names — the ``*-<id>.*`` glob used to
        swallow it, so the PROGRESS refusal in ``remove_todo`` was dead and
        a PROGRESS-only unit got the generic signals hint."""
        _write_todo(proj, T1)
        outbox = proj / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / f"PROGRESS-{T1}.md").write_text("stage: dev\n", encoding="utf-8")

        with pytest.raises(api.AwfApiError, match="has PROGRESS") as refused:
            api.remove_todo(proj, T1)
        # REVIEW v5 (Y1): the hint must name the path that actually works —
        # ``reset --orphans`` skips every unit with PROGRESS by construction.
        assert "todo-retire" in str(refused.value)
        assert "reset --orphans" not in str(refused.value)
        assert (proj / ".agentic" / "inbox" / f"{T1}.md").is_file()

    def test_succeeds_after_the_queue_replaces_it(self, proj: Path) -> None:
        _write_todo(proj, T1)
        _write_todo(proj, T2)
        api.run_start(proj, queue=[T1])
        replaced = api.run_revise(
            proj, queue=[{"from": T1, "to": T2}], key="rep-remove"
        )
        assert replaced.action == "applied"
        result = api.remove_todo(proj, T1)
        assert "/done/" in result.trace_path
        assert not (proj / ".agentic" / "inbox" / f"{T1}.md").exists()
        assert T1 not in [
            q["todo_id"] for q in run_state.read_run(proj)["queue"]
        ]

    def test_completed_queue_element_does_not_block_remove(self, proj: Path) -> None:
        """REVIEW (supervisor v6, Z6): only the NOT-STARTED region is a live
        reference. A completed element is history; the broad "still holding"
        guard refused it with "replace it first" — advice replace cannot
        execute for an already completed id."""
        _write_todo(proj, T1)
        _write_todo(proj, T2)
        run_state.write_run(
            proj,
            active=True,
            queue=[
                {"todo_id": T1, "pipeline": ""},
                {"todo_id": T2, "pipeline": ""},
            ],
            index=1,
            current=T2,
            generation=1,
        )
        result = api.remove_todo(proj, T1)
        assert "/done/" in result.trace_path
        assert not (proj / ".agentic" / "inbox" / f"{T1}.md").exists()


# ─── (e) queue references on the other removers (REVIEW v5, Y2) ─────────


def _service_slot_holding(proj: Path, todo_id: str) -> None:
    run_state.write_run(
        proj,
        slot="service",
        active=True,
        queue=[{"todo_id": todo_id, "pipeline": "svc"}],
        index=0,
        current="",
        generation=1,
    )


class TestQueueReferencesOnEveryRemover:
    """The dangling-reference guard used to live on ``remove_todo`` alone:
    ``retire_todo`` and ``reset --orphans`` moved the same inbox files out
    from under an active queue. All three ask the same question now."""

    def test_retire_refused_while_in_the_run_queue(self, proj: Path) -> None:
        _write_todo(proj, T1)
        api.run_start(proj, queue=[T1])
        with pytest.raises(api.AwfApiError, match="active run queue"):
            api.retire_todo(proj, T1, reason="obsolete")
        assert (proj / ".agentic" / "inbox" / f"{T1}.md").is_file()

    def test_retire_refused_while_in_the_service_run(self, proj: Path) -> None:
        _write_todo(proj, T2)
        _service_slot_holding(proj, T2)
        with pytest.raises(api.AwfApiError, match="active run queue"):
            api.retire_todo(proj, T2, reason="obsolete")
        assert (proj / ".agentic" / "inbox" / f"{T2}.md").is_file()

    def test_retire_succeeds_after_the_queue_replaces_it(self, proj: Path) -> None:
        _write_todo(proj, T1)
        _write_todo(proj, T2)
        api.run_start(proj, queue=[T1])
        replaced = api.run_revise(
            proj, queue=[{"from": T1, "to": T2}], key="rep-retire"
        )
        assert replaced.action == "applied"
        api.retire_todo(proj, T1, reason="obsolete")
        assert (proj / ".agentic" / "done" / T1 / "TODO.md").is_file()
        assert not (proj / ".agentic" / "inbox" / f"{T1}.md").exists()

    def test_retire_of_the_current_unit_is_the_recovery_path(
        self, proj: Path, monkeypatch
    ) -> None:
        """Y2 guards PLANNED units only. Retiring a rejected/current unit is
        the documented way to move the run forward (pinned end-to-end by
        ``test_launch_unblocks_after_retire``) — the guard must not block it."""
        _write_todo(proj, T1)
        _write_todo(proj, T2)
        api.run_start(proj, queue=[T1, T2])
        _fake_start(monkeypatch, proj)
        assert api.run_next(proj).action == "started"
        api.retire_todo(proj, T1, reason="replanned")
        assert (proj / ".agentic" / "done" / T1 / "TODO.md").is_file()

    def test_orphans_refused_while_in_the_run_queue(self, proj: Path) -> None:
        _write_todo(proj, T1)
        (proj / ".agentic" / "inbox" / f"{T1}.ready").write_text("", encoding="utf-8")
        api.run_start(proj, queue=[T1])
        with pytest.raises(api.AwfApiError, match="active run queue"):
            api.remove_orphans(proj, [T1])
        assert (proj / ".agentic" / "inbox" / f"{T1}.md").is_file()

    def test_orphans_refused_while_in_the_service_run(self, proj: Path) -> None:
        _write_todo(proj, T2)
        (proj / ".agentic" / "inbox" / f"{T2}.ready").write_text("", encoding="utf-8")
        _service_slot_holding(proj, T2)
        with pytest.raises(api.AwfApiError, match="active run queue"):
            api.remove_orphans(proj, [T2])
        assert (proj / ".agentic" / "inbox" / f"{T2}.md").is_file()

    def test_orphans_succeed_after_the_queue_replaces_it(self, proj: Path) -> None:
        _write_todo(proj, T1)
        _write_todo(proj, T2)
        (proj / ".agentic" / "inbox" / f"{T1}.ready").write_text("", encoding="utf-8")
        api.run_start(proj, queue=[T1])
        replaced = api.run_revise(
            proj, queue=[{"from": T1, "to": T2}], key="rep-orphans"
        )
        assert replaced.action == "applied"
        result = api.remove_orphans(proj, [T1])
        assert result.orphan_ids == [T1]
        assert not (proj / ".agentic" / "inbox" / f"{T1}.md").exists()


# ─── (e) check-contracts closure matrix ──────────────────────────────────


def _contracts_project(tmp_path: Path, matrix: str) -> Path:
    project = tmp_path / "contracts"
    (project / "docs" / "contracts").mkdir(parents=True)
    (project / "scripts").mkdir()
    shutil.copy(
        REPO_ROOT / "scripts" / "check-contracts.sh",
        project / "scripts" / "check-contracts.sh",
    )
    (project / "x.py").write_text("x = 1\n", encoding="utf-8")
    (project / "docs" / "contracts" / "x.md").write_text(
        "# Contract: x\n\nOwns: x\nPath: `x.py`\nNever: no eval\nGate: true\n",
        encoding="utf-8",
    )
    inbox = project / ".agentic" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / f"{T1}.md").write_text(
        "# TODO\n\n## Границы\n`awf/x.py` — `write_text` результата\n\n" + matrix,
        encoding="utf-8",
    )
    return project


def _run_gate(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "scripts/check-contracts.sh"],
        cwd=project,
        capture_output=True,
        text=True,
    )


MATRIX = (
    "## Матрица замыкания\n\n"
    "| Операция | Поверхность | Корень/лист | Порядок |\n"
    "|---|---|---|---|\n"
    "| write_text | `awf/x.py` | корень+лист | preflight → write |\n"
)


class TestClosureMatrixLock:
    def test_fs_boundaries_without_matrix_are_red(self, tmp_path: Path) -> None:
        cp = _run_gate(_contracts_project(tmp_path, ""))
        assert cp.returncode == 1
        assert "Матрица замыкания" in cp.stdout

    def test_fs_boundaries_with_matrix_are_green(self, tmp_path: Path) -> None:
        cp = _run_gate(_contracts_project(tmp_path, MATRIX))
        assert cp.returncode == 0, cp.stdout + cp.stderr
        assert "1 unit contracts" in cp.stdout

    def test_stub_table_is_red(self, tmp_path: Path) -> None:
        """REVIEW (supervisor v6, Z3): a bare ``| stub |`` line is not the
        matrix. The gate checks the four required columns and non-empty
        cells, not just the presence of a pipe."""
        stub = "## Матрица замыкания\n\n| заглушка |\n"
        cp = _run_gate(_contracts_project(tmp_path, stub))
        assert cp.returncode == 1
        assert "Матрица замыкания" in cp.stdout

    def test_missing_column_is_red(self, tmp_path: Path) -> None:
        matrix = (
            "## Матрица замыкания\n\n"
            "| Операция | Поверхность | Порядок |\n"
            "|---|---|---|\n"
            "| write_text | `awf/x.py` | write |\n"
        )
        cp = _run_gate(_contracts_project(tmp_path, matrix))
        assert cp.returncode == 1
        assert "корень/лист" in cp.stdout

    def test_empty_cell_is_red(self, tmp_path: Path) -> None:
        matrix = (
            "## Матрица замыкания\n\n"
            "| Операция | Поверхность | Корень/лист | Порядок |\n"
            "|---|---|---|---|\n"
            "| write_text | `awf/x.py` | корень+лист |  |\n"
        )
        cp = _run_gate(_contracts_project(tmp_path, matrix))
        assert cp.returncode == 1
        assert "empty cell" in cp.stdout

    def test_fs_word_in_prose_does_not_trigger(self, tmp_path: Path) -> None:
        """REVIEW (supervisor v2, V6): only backticked tokens are operations.
        A prose sentence naming a verb ("replace the old check") must not
        demand the matrix, and the summary must print the denominator."""
        project = _contracts_project(tmp_path, "")
        (project / ".agentic" / "inbox" / f"{T1}.md").write_text(
            "# TODO\n\n## Границы\n`awf/x.py` — replace the old check\n",
            encoding="utf-8",
        )
        cp = _run_gate(project)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        assert "0 triggered / 1 units" in cp.stdout

    def test_archived_units_are_not_scanned(self, tmp_path: Path) -> None:
        """REVIEW (qa): the lock guards live plans only. An archived unit
        (``done/<id>/TODO.md``) with FS boundaries and no matrix must not
        turn the gate red — history is not a plan."""
        project = _contracts_project(tmp_path, MATRIX)
        archived = project / ".agentic" / "done" / "TODO-0009"
        archived.mkdir(parents=True)
        (archived / "TODO.md").write_text(
            "# old\n\n## Границы\n`x.py` — write_text\n", encoding="utf-8"
        )
        cp = _run_gate(project)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        assert "1 unit contracts" in cp.stdout
