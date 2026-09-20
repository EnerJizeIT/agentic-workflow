"""BD-33: _mark_plan_step_done — auto-mark [x] in phases/plan.md after verify."""
from __future__ import annotations

from pathlib import Path

from awf.plan_progress import (
    extract_step_id_from_todo as _extract_step_id_from_todo,
)
from awf.plan_progress import (
    mark_plan_step_done as _mark_plan_step_done,
)
from awf.plan_progress import (
    print_progress_report as _print_progress_report,
)


def _make_proj(tmp_path: Path, plan_md: str = "") -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".agentic").mkdir()
    (proj / ".agentic" / "phases").mkdir()
    (proj / ".agentic" / "inbox").mkdir()
    (proj / ".agentic" / "outbox").mkdir()
    (proj / ".agentic" / "context").mkdir()
    (proj / ".agentic" / "logs").mkdir()
    (proj / ".agentic" / "roles").mkdir()
    (proj / ".agentic" / "config.yaml").write_text(
        'project:\n  name: test\nphases:\n  current: ".agentic/phases/plan.md"\n'
    )
    if plan_md:
        (proj / ".agentic" / "phases" / "plan.md").write_text(plan_md)
    else:
        (proj / ".agentic" / "phases" / "plan.md").write_text(
            "# Plan\n\n"
            "- [x] **Step 0**: Spike — DONE\n"
            "- [ ] Step 1: First task\n"
            "- [ ] Step 2: Second task\n"
            "- [ ] Step 3: Third task\n"
        )
    return proj


# ── _extract_step_id_from_todo ────────────────────────────────────────────────


class TestExtractStepId:

    def test_yaml_frontmatter_step_id(self, tmp_path: Path) -> None:
        todo = tmp_path / "TODO-0001.md"
        todo.write_text("---\nstep_id: 7\n---\n\n# Body")
        assert _extract_step_id_from_todo(todo) == 7

    def test_phase_line_step_n(self, tmp_path: Path) -> None:
        todo = tmp_path / "TODO-0001.md"
        todo.write_text("# TODO\n\n**Phase:** Week 1 — Step 3\n\nbody")
        assert _extract_step_id_from_todo(todo) == 3

    def test_bold_step_in_header(self, tmp_path: Path) -> None:
        todo = tmp_path / "TODO-0001.md"
        todo.write_text("# TODO\n\n**Step 1**: do something\n")
        assert _extract_step_id_from_todo(todo) == 1

    def test_no_step_returns_none(self, tmp_path: Path) -> None:
        todo = tmp_path / "TODO-0001.md"
        todo.write_text("# TODO\n\nNo step mentioned anywhere\n")
        assert _extract_step_id_from_todo(todo) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _extract_step_id_from_todo(tmp_path / "nonexistent.md") is None

    def test_step_in_body_far_from_header_not_matched(self, tmp_path: Path) -> None:
        """Only first 20 lines scanned — Step 99 deep in body is ignored."""
        todo = tmp_path / "TODO-0001.md"
        lines = ["# TODO", ""] + ["filler"] * 25 + ["Step 99 should be ignored"]
        todo.write_text("\n".join(lines))
        assert _extract_step_id_from_todo(todo) is None

    def test_first_step_wins_when_multiple(self, tmp_path: Path) -> None:
        todo = tmp_path / "TODO-0001.md"
        todo.write_text("# TODO\n\nStep 1 first\nThen Step 2\n")
        # First match wins
        assert _extract_step_id_from_todo(todo) == 1


# ── _mark_plan_step_done ──────────────────────────────────────────────────────


class TestMarkPlanStepDone:

    def test_marks_unchecked_step(self, tmp_path: Path) -> None:
        proj = _make_proj(tmp_path)
        todo_path = proj / ".agentic" / "inbox" / "TODO-0001.md"
        todo_path.write_text("# TODO\n\n**Phase:** Step 2\n")
        logs = proj / ".agentic" / "logs"

        result = _mark_plan_step_done(proj, "TODO-0001", logs)

        assert result is True
        plan = (proj / ".agentic" / "phases" / "plan.md").read_text()
        assert "- [x] Step 2: Second task  — TODO-0001" in plan
        # Other steps untouched
        assert "- [ ] Step 1: First task" in plan
        assert "- [ ] Step 3: Third task" in plan

    def test_marks_bold_step_format(self, tmp_path: Path) -> None:
        """Format '- [ ] **Step N**:' also works (supervisor writes bold)."""
        plan_md = "# Plan\n\n- [ ] **Step 5**: Important task\n"
        proj = _make_proj(tmp_path, plan_md=plan_md)
        todo_path = proj / ".agentic" / "inbox" / "TODO-0042.md"
        todo_path.write_text("step_id: 5\n\n# Body")
        logs = proj / ".agentic" / "logs"

        result = _mark_plan_step_done(proj, "TODO-0042", logs)
        assert result is True
        plan = (proj / ".agentic" / "phases" / "plan.md").read_text()
        assert "- [x] **Step 5**:" in plan

    def test_already_checked_step_no_change(self, tmp_path: Path) -> None:
        plan_md = "# Plan\n\n- [x] Step 1: Already done\n"
        proj = _make_proj(tmp_path, plan_md=plan_md)
        todo_path = proj / ".agentic" / "inbox" / "TODO-0001.md"
        todo_path.write_text("**Phase:** Step 1\n")
        logs = proj / ".agentic" / "logs"

        result = _mark_plan_step_done(proj, "TODO-0001", logs)
        assert result is False  # no match for [ ] pattern

    def test_no_step_in_todo_returns_false(self, tmp_path: Path) -> None:
        proj = _make_proj(tmp_path)
        todo_path = proj / ".agentic" / "inbox" / "TODO-0001.md"
        todo_path.write_text("# TODO without step reference\n")
        logs = proj / ".agentic" / "logs"

        result = _mark_plan_step_done(proj, "TODO-0001", logs)
        assert result is False

    def test_no_plan_file_returns_false(self, tmp_path: Path) -> None:
        proj = _make_proj(tmp_path)
        (proj / ".agentic" / "phases" / "plan.md").unlink()
        todo_path = proj / ".agentic" / "inbox" / "TODO-0001.md"
        todo_path.write_text("**Phase:** Step 1\n")
        logs = proj / ".agentic" / "logs"

        result = _mark_plan_step_done(proj, "TODO-0001", logs)
        assert result is False

    def test_step_not_in_plan_returns_false(self, tmp_path: Path) -> None:
        """TODO references Step 99 but plan only has Steps 0-3."""
        proj = _make_proj(tmp_path)
        todo_path = proj / ".agentic" / "inbox" / "TODO-0001.md"
        todo_path.write_text("**Phase:** Step 99\n")
        logs = proj / ".agentic" / "logs"

        result = _mark_plan_step_done(proj, "TODO-0001", logs)
        assert result is False

    def test_todo_id_appended_to_line(self, tmp_path: Path) -> None:
        proj = _make_proj(tmp_path)
        todo_path = proj / ".agentic" / "inbox" / "TODO-0001.md"
        todo_path.write_text("**Phase:** Step 1\n")
        logs = proj / ".agentic" / "logs"

        _mark_plan_step_done(proj, "TODO-0001", logs)
        plan = (proj / ".agentic" / "phases" / "plan.md").read_text()
        assert "TODO-0001" in plan

    def test_concurrent_edit_not_lost(self, tmp_path: Path, monkeypatch) -> None:
        """AUD14-08: a colleague's edit landing between read and write must
        survive the step-marking (old code overwrote it — whole-file write)."""
        import time

        proj = _make_proj(tmp_path)
        todo_path = proj / ".agentic" / "inbox" / "TODO-0001.md"
        todo_path.write_text("**Phase:** Step 1\n")
        logs = proj / ".agentic" / "logs"
        plan_path = proj / ".agentic" / "phases" / "plan.md"

        real_read = Path.read_text
        state = {"edits": 0}

        def racy_read(self, *a, **kw):
            result = real_read(self, *a, **kw)
            if self == plan_path and state["edits"] == 0:
                # the colleague appends a line right after OUR first read
                time.sleep(0.02)
                with open(self, "a", encoding="utf-8") as f:
                    f.write("\n- colleague edit must survive\n")
                state["edits"] += 1
            return result

        monkeypatch.setattr(Path, "read_text", racy_read)

        result = _mark_plan_step_done(proj, "TODO-0001", logs)

        assert result is True
        plan = plan_path.read_text()
        assert "- colleague edit must survive" in plan, "concurrent edit was lost"
        assert "- [x] Step 1: First task  — TODO-0001" in plan


# ── _print_progress_report ────────────────────────────────────────────────────


class TestPrintProgressReport:

    def test_counts_done_and_todo(self, tmp_path: Path, capsys) -> None:
        proj = _make_proj(tmp_path)
        logs = proj / ".agentic" / "logs"

        _print_progress_report(proj, logs)
        out = capsys.readouterr().out

        assert "PROGRESS REPORT" in out
        assert "Steps done:     1 / 4" in out
        assert "Steps remaining: 3" in out

    def test_lists_next_3_unfinished(self, tmp_path: Path, capsys) -> None:
        plan_md = "# Plan\n"
        for i in range(1, 6):
            plan_md += f"- [ ] Step {i}: task {i}\n"
        proj = _make_proj(tmp_path, plan_md=plan_md)
        logs = proj / ".agentic" / "logs"

        _print_progress_report(proj, logs)
        out = capsys.readouterr().out

        assert "Next steps:" in out
        assert "Step 1: task 1" in out
        assert "Step 2: task 2" in out
        assert "Step 3: task 3" in out
        assert "... and 2 more" in out

    def test_all_done_message(self, tmp_path: Path, capsys) -> None:
        plan_md = "# Plan\n\n- [x] Step 1\n- [x] Step 2\n"
        proj = _make_proj(tmp_path, plan_md=plan_md)
        logs = proj / ".agentic" / "logs"

        _print_progress_report(proj, logs)
        out = capsys.readouterr().out
        assert "All steps complete" in out

    def test_no_plan_file(self, tmp_path: Path, capsys) -> None:
        proj = _make_proj(tmp_path)
        (proj / ".agentic" / "phases" / "plan.md").unlink()
        logs = proj / ".agentic" / "logs"

        _print_progress_report(proj, logs)
        out = capsys.readouterr().out
        assert "no plan file" in out.lower()
