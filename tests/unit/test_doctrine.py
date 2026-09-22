"""U9: doctrine injection + deterministic prompt assembly.

Validates that:
- .agentic/doctrine/*.md are assembled into one «Доктрина проекта»
  section (sorted by filename, deterministic)
- missing/empty catalog → no section, prompt unchanged (backward compat)
- the assembled section lands BETWEEN the role file and the task file in
  both worker stages and supervisor stages (same position for every role)
- assembly is byte-identical: same input → same output, regardless of
  file creation order or mtimes on disk (no time, no randomness)
"""
from __future__ import annotations

import os

from awf import agent_stage, doctrine, supervisor
from awf.pipeline import Stage

# Single line on purpose: the todos ratchet counts occurrences of the
# uppercase identifier, inlining it per test would push the counter up.
TASK_ID = "TODO-0001"


def _project(tmp_path, with_doctrine: bool = True, names: tuple = ("a.md", "b.md")):
    proj = tmp_path / "proj"
    for d in ("inbox", "outbox", "logs", "roles"):
        (proj / ".agentic" / d).mkdir(parents=True, exist_ok=True)
    (proj / ".agentic" / "roles" / "developer.md").write_text(
        "# Developer role\n", encoding="utf-8"
    )
    (proj / ".agentic" / "inbox" / f"{TASK_ID}.md").write_text(
        f"# {TASK_ID}\n", encoding="utf-8"
    )
    if with_doctrine:
        ddir = proj / ".agentic" / "doctrine"
        ddir.mkdir(parents=True)
        for name in names:
            (ddir / name).write_text(f"# lesson {name}\n", encoding="utf-8")
    return proj


class TestLoadDoctrineFiles:
    def test_no_catalog(self, tmp_path):
        proj = tmp_path / "proj"
        (proj / ".agentic").mkdir(parents=True)
        assert doctrine.load_doctrine_files(proj) == []

    def test_empty_catalog(self, tmp_path):
        proj = _project(tmp_path, with_doctrine=False)
        (proj / ".agentic" / "doctrine").mkdir()
        assert doctrine.load_doctrine_files(proj) == []

    def test_only_md_files(self, tmp_path):
        proj = _project(tmp_path, names=("a.md",))
        ddir = proj / ".agentic" / "doctrine"
        (ddir / "notes.txt").write_text("not doctrine\n", encoding="utf-8")
        (ddir / "sub").mkdir()
        (ddir / "sub" / "nested.md").write_text("ignored\n", encoding="utf-8")
        names = [n for n, _ in doctrine.load_doctrine_files(proj)]
        assert names == ["a.md"]

    def test_sorted_by_name(self, tmp_path):
        proj = _project(tmp_path, names=("z.md", "a.md", "m.md"))
        names = [n for n, _ in doctrine.load_doctrine_files(proj)]
        assert names == ["a.md", "m.md", "z.md"]


class TestAssembleDoctrine:
    def test_empty_when_no_doctrine(self, tmp_path):
        proj = _project(tmp_path, with_doctrine=False)
        assert doctrine.assemble_doctrine(proj) == ""

    def test_section_heading_and_content(self, tmp_path):
        proj = _project(tmp_path, names=("a.md", "b.md"))
        text = doctrine.assemble_doctrine(proj)
        assert text.startswith("## Доктрина проекта")
        assert "### a.md" in text
        assert "### b.md" in text
        assert "# lesson a.md" in text
        assert "# lesson b.md" in text
        # a before b — sorted order
        assert text.index("### a.md") < text.index("### b.md")

    def test_assemble_twice_identical(self, tmp_path):
        """Part B: same input → byte-identical output."""
        proj = _project(tmp_path)
        assert doctrine.assemble_doctrine(proj) == doctrine.assemble_doctrine(proj)

    def test_creation_order_and_mtimes_do_not_matter(self, tmp_path):
        """Part B: disk order (creation, mtime) must not leak into the prompt."""
        p1 = tmp_path / "p1"
        p2 = tmp_path / "p2"
        for p in (p1, p2):
            (p / ".agentic" / "doctrine").mkdir(parents=True)
        # p1: a created first (older mtime), p2: b created first
        (p1 / ".agentic" / "doctrine" / "a.md").write_text("# A\n", encoding="utf-8")
        (p1 / ".agentic" / "doctrine" / "b.md").write_text("# B\n", encoding="utf-8")
        (p2 / ".agentic" / "doctrine" / "b.md").write_text("# B\n", encoding="utf-8")
        (p2 / ".agentic" / "doctrine" / "a.md").write_text("# A\n", encoding="utf-8")
        # flip mtimes in p2 so b.md is OLDER than a.md there
        b = p2 / ".agentic" / "doctrine" / "b.md"
        a = p2 / ".agentic" / "doctrine" / "a.md"
        os.utime(b, (1000, 1000))
        os.utime(a, (2000, 2000))

        s1 = doctrine.assemble_doctrine(p1)
        s2 = doctrine.assemble_doctrine(p2)
        assert s1 == s2
        assert s1.index("### a.md") < s1.index("### b.md")


class TestMaterializeDoctrine:
    def test_empty_returns_none_and_writes_nothing(self, tmp_path):
        proj = _project(tmp_path, with_doctrine=False)
        assert doctrine.materialize_doctrine(proj) is None
        assert not (proj / ".agentic" / "context" / doctrine.DOCTRINE_ASSEMBLED_NAME).exists()

    def test_writes_assembled_section(self, tmp_path):
        proj = _project(tmp_path)
        out = doctrine.materialize_doctrine(proj)
        assert out is not None
        assert out.read_text(encoding="utf-8") == doctrine.assemble_doctrine(proj)

    def test_two_materializations_byte_identical(self, tmp_path):
        """Part B: the file injected into prompts is stable across runs."""
        proj = _project(tmp_path)
        first = doctrine.materialize_doctrine(proj).read_bytes()
        second = doctrine.materialize_doctrine(proj).read_bytes()
        assert first == second


class TestWorkerPromptInjection:
    """The worker cmd: role → doctrine → task → handoffs → prompt."""

    def _capture(self, monkeypatch):
        captured: dict = {}

        def fake_run(cmd, cwd, watch_paths=None, **kwargs):
            captured["cmd"] = cmd

            class _R:
                returncode = 0

            return _R()

        monkeypatch.setattr(
            "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True
        )
        monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
        monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)
        return captured

    def _run(self, captured, proj):
        stage = Stage(name="agent-dev", role="developer", kind="execute")
        agent_stage.run_agent_stage(
            stage, TASK_ID, proj, {}, proj / ".agentic" / "logs"
        )
        return captured["cmd"]

    def _file_positions(self, cmd, proj):
        files = []
        i = 0
        while i < len(cmd) - 1:
            if cmd[i] == "--file":
                files.append(cmd[i + 1])
                i += 2
            else:
                i += 1
        return files

    def test_doctrine_between_role_and_todo(self, tmp_path, monkeypatch):
        captured = self._capture(monkeypatch)
        proj = _project(tmp_path)
        cmd = self._run(captured, proj)
        files = self._file_positions(cmd, proj)
        role = str(proj / ".agentic" / "roles" / "developer.md")
        doctrine_file = str(proj / ".agentic" / "context" / "DOCTRINE.md")
        todo = str(proj / ".agentic" / "inbox" / f"{TASK_ID}.md")
        assert files == [role, doctrine_file, todo]
        # the doctrine file on disk holds the section with both lessons
        assert "## Доктрина проекта" in (proj / ".agentic" / "context" / "DOCTRINE.md").read_text()

    def test_no_catalog_no_doctrine_file(self, tmp_path, monkeypatch):
        """Backward compat: without .agentic/doctrine the cmd is as before."""
        captured = self._capture(monkeypatch)
        proj = _project(tmp_path, with_doctrine=False)
        cmd = self._run(captured, proj)
        files = self._file_positions(cmd, proj)
        assert all("DOCTRINE" not in f for f in files)
        assert len(files) == 2  # role + todo only

    def test_execute_prompt_deterministic(self, tmp_path):
        """Part B: build_prompt has no time/randomness — same input, same bytes."""
        proj = _project(tmp_path)
        p1 = supervisor.build_prompt("execute", TASK_ID, project_dir=proj)
        p2 = supervisor.build_prompt("execute", TASK_ID, project_dir=proj)
        assert p1 == p2


class TestSupervisorPromptInjection:
    """The supervisor cmd: role → doctrine → stage context → prompt."""

    def test_doctrine_after_role_before_context(self, tmp_path, monkeypatch):
        captured: dict = {}

        def fake_run(cmd, cwd, watch_paths=None, watch_new_glob=None,
                     logs_dir=None, env=None, signal_holder=None, **kwargs):
            captured["cmd"] = cmd
            if signal_holder is not None:
                signal_holder["signal"] = ""

            class _R:
                returncode = 0

            return _R()

        monkeypatch.setattr(
            "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True
        )

        proj = tmp_path / "sup"
        for d in ("inbox", "outbox", "logs", "roles"):
            (proj / ".agentic" / d).mkdir(parents=True)
        (proj / ".agentic" / "roles" / "supervisor.md").write_text(
            "# Supervisor\n", encoding="utf-8"
        )
        (proj / ".agentic" / "phases").mkdir()
        phases = proj / ".agentic" / "phases" / "plan.md"
        phases.write_text("# Plan\n", encoding="utf-8")
        (proj / ".agentic" / "doctrine").mkdir()
        (proj / ".agentic" / "doctrine" / "01-lesson.md").write_text(
            "# lesson\n", encoding="utf-8"
        )

        supervisor.run_supervisor_via_subprocess(
            "plan", "", proj, {}, str(phases), proj / ".agentic" / "logs"
        )

        files = []
        i = 0
        cmd = captured["cmd"]
        while i < len(cmd) - 1:
            if cmd[i] == "--file":
                files.append(cmd[i + 1])
                i += 2
            else:
                i += 1
        role = str(proj / ".agentic" / "roles" / "supervisor.md")
        doctrine_file = str(proj / ".agentic" / "context" / "DOCTRINE.md")
        assert files == [role, doctrine_file, str(phases)]
