"""RUN4 #2: awf feedback — фидбек-контур (отчёт супервизора владельцу).

Реальный проект в tmp_path (стиль negative-тестов): действие через
``api.feedback`` / ``cli.main`` — assert на файл, его факты, имя и
ошибки валидации.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from conftest import _git_init  # AUD12-08: shared git boilerplate

import awf
from awf import api, cli


@pytest.fixture
def project(tmp_path) -> Path:
    """Git repo with .agentic/ set up via api.init_project."""
    repo = tmp_path / "proj"
    repo.mkdir()
    _git_init(repo)
    api.init_project(repo, project_name="FeedbackProj")
    return repo


def _set_feedback_dir(project: Path, dir_path: Path) -> None:
    cfg_file = project / ".agentic" / "config.yaml"
    cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
    cfg["feedback"] = {"dir": str(dir_path)}
    cfg_file.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")


class TestFacts:
    """Шапка-факты: версия, проект, фаза, забег, задача, awf-репо."""

    def test_file_created_with_facts(self, project, tmp_path, capsys):
        out_dir = tmp_path / "desk"
        _set_feedback_dir(project, out_dir)
        capsys.readouterr()

        result = api.feedback(
            project, ftype="bug", title="Pipeline hangs", body="описал что делал"
        )

        dest = Path(result.file)
        assert dest.is_file(), f"report not written: {dest}"
        assert dest.parent == out_dir.resolve() or dest.parent == out_dir
        # имя: awf-bug-YYYYMMDD-<slug>.md
        assert re.fullmatch(r"awf-bug-\d{8}-pipeline-hangs\.md", dest.name), dest.name
        text = dest.read_text(encoding="utf-8")
        assert awf.__version__ in text  # версия awf
        assert "FeedbackProj" in text  # проект
        assert "Фаза: " in text  # фаза
        assert "Забег: " in text  # забег
        assert "Текущая задача: " in text  # текущая задача
        assert "Дата: " in text  # дата

    def test_body_only_prints_no_empty_headings(self, project, tmp_path, capsys):
        """RUN10 #2: один текст — только «Что пытался», пустых заголовков нет."""
        out_dir = tmp_path / "desk"
        _set_feedback_dir(project, out_dir)
        capsys.readouterr()

        api.feedback(project, ftype="feature", title="Need X", body="тело")

        files = list(out_dir.glob("awf-feature-*.md"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert "## Что пытался" in text
        assert "тело" in text  # body вставлен в отчёт
        for section in ("Ожидал", "Что получил", "Почему мешает", "Предложение"):
            assert f"## {section}" not in text

    def test_section_params_fill_their_sections(self, project, tmp_path, capsys):
        """RUN10 #2: expected/got/why/proposal заполняют свои секции."""
        out_dir = tmp_path / "desk"
        _set_feedback_dir(project, out_dir)
        capsys.readouterr()

        api.feedback(
            project, ftype="bug", title="All sections",
            body="тело", expected="ожидал", got="получил",
            why="мешает", proposal="предложение",
        )

        text = list(out_dir.glob("awf-bug-*.md"))[0].read_text(encoding="utf-8")
        for heading, content in (
            ("## Что пытался", "тело"),
            ("## Ожидал", "ожидал"),
            ("## Что получил", "получил"),
            ("## Почему мешает", "мешает"),
            ("## Предложение", "предложение"),
        ):
            assert heading in text
            assert content in text

    def test_severity_in_header(self, project, tmp_path, capsys):
        out_dir = tmp_path / "desk"
        _set_feedback_dir(project, out_dir)
        capsys.readouterr()

        api.feedback(project, ftype="bug", title="Crash", severity="high")

        text = list(out_dir.glob("awf-bug-*.md"))[0].read_text(encoding="utf-8")
        assert "Важность: high" in text

    def test_run_state_in_header(self, project, tmp_path, capsys):
        """Активный забег → позиция и no_checkpoints в шапке."""
        out_dir = tmp_path / "desk"
        _set_feedback_dir(project, out_dir)
        capsys.readouterr()

        from awf import run_state

        run_state.write_run(
            project,
            queue=[{"todo_id": "TODO-0001", "pipeline": ""}],
            index=0,
            current="TODO-0001",
            active=True,
            no_checkpoints=True,
            started_at=run_state.now_iso(),
        )

        result = api.feedback(project, ftype="bug", title="Run problem")
        text = Path(result.file).read_text(encoding="utf-8")
        assert "Забег: активен" in text
        assert "no_checkpoints" in text
        assert re.search(r"позиция \d+/\d+", text)


class TestSlug:
    """Слаг из title: ASCII, кириллица — транслитерация, иначе report."""

    def test_cyrillic_title_transliterated(self, project, tmp_path, capsys):
        out_dir = tmp_path / "desk"
        _set_feedback_dir(project, out_dir)
        capsys.readouterr()

        result = api.feedback(project, ftype="bug", title="Глитч с дашбордом")

        assert "glitch" in Path(result.file).name
        assert "dashbordom" in Path(result.file).name
        assert "report" not in Path(result.file).name.split("-", 3)[-1]

    def test_non_letter_title_falls_back_to_report(self, project, tmp_path, capsys):
        out_dir = tmp_path / "desk"
        _set_feedback_dir(project, out_dir)
        capsys.readouterr()

        result = api.feedback(project, ftype="feature", title="!!!")

        assert Path(result.file).name.startswith("awf-feature-")
        assert Path(result.file).name.endswith("-report.md")

    def test_repeat_same_day_same_slug_gets_suffix(self, project, tmp_path, capsys):
        out_dir = tmp_path / "desk"
        _set_feedback_dir(project, out_dir)
        capsys.readouterr()

        first = api.feedback(project, ftype="bug", title="Same title")
        second = api.feedback(project, ftype="bug", title="Same title")

        assert Path(first.file).is_file()
        assert Path(second.file).is_file()
        assert Path(first.file).name.endswith("-same-title.md")
        assert Path(second.file).name.endswith("-same-title-2.md")
        # первый не перезаписан
        assert Path(first.file).read_text(encoding="utf-8")  # ещё на месте


class TestValidation:
    """Валидация: type/title обязательны, severity из списка, ошибки внятные."""

    def test_bad_type_raises(self, project, capsys):
        capsys.readouterr()
        with pytest.raises(api.AwfApiError, match="type"):
            api.feedback(project, ftype="hotfix", title="x")

    def test_missing_title_raises(self, project, capsys):
        capsys.readouterr()
        with pytest.raises(api.AwfApiError, match="title"):
            api.feedback(project, ftype="bug", title="   ")

    def test_bad_severity_raises(self, project, capsys):
        capsys.readouterr()
        with pytest.raises(api.AwfApiError, match="severity"):
            api.feedback(project, ftype="bug", title="x", severity="critical")

    def test_cli_bad_type_is_nonzero_with_message(self, project, capsys):
        """Неверный --type — argparse отказ (exit 2) с внятным сообщением."""
        with pytest.raises(SystemExit) as exc:
            cli.main(
                ["feedback", "--type", "hotfix", "--title", "x", "--project-dir", str(project)]
            )
        assert exc.value.code == 2
        out = capsys.readouterr()
        assert "invalid choice" in out.err
        assert "bug" in out.err and "feature" in out.err


class TestStdout:
    """--stdout — печатает, файл не пишет."""

    def test_api_stdout_writes_nothing(self, project, tmp_path, capsys):
        out_dir = tmp_path / "desk"
        _set_feedback_dir(project, out_dir)
        capsys.readouterr()

        result = api.feedback(project, ftype="bug", title="No file", stdout=True)

        out = capsys.readouterr().out
        assert not list(out_dir.glob("*.md")) if out_dir.exists() else True
        assert result.file == ""
        # RUN10 #2: без body секция «Что пытался» не печатается вовсе
        assert "## Что пытался" not in result.report
        assert "No file" in result.report  # заголовок H1 на месте
        # CLI печатает отчёт
        rc = cli.main(
            [
                "feedback", "--type", "bug", "--title", "No file",
                "--project-dir", str(project), "--stdout",
            ]
        )
        out2 = capsys.readouterr().out
        assert rc == 0
        assert "## Что пытался" not in out2
        assert "No file" in out2

    def test_cli_stdout_writes_no_file(self, project, tmp_path, capsys):
        out_dir = tmp_path / "desk"
        _set_feedback_dir(project, out_dir)
        capsys.readouterr()

        rc = cli.main(
            [
                "feedback", "--type", "feature", "--title", "Only print",
                "--project-dir", str(project), "--stdout",
            ]
        )
        capsys.readouterr()
        assert rc == 0
        assert not out_dir.exists() or not list(out_dir.glob("*.md"))


class TestSectionFlags:
    """RUN10 #2: CLI-флаги под секции + совместимость со старым вызовом."""

    def test_cli_section_flags_fill_sections(self, project, tmp_path, capsys):
        out_dir = tmp_path / "desk"
        _set_feedback_dir(project, out_dir)
        capsys.readouterr()

        rc = cli.main(
            [
                "feedback", "--type", "feature", "--title", "Cli sections",
                "--body", "b", "--expected", "e", "--got", "g",
                "--why", "w", "--proposal", "p",
                "--project-dir", str(project), "--stdout",
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0
        for heading in (
            "## Что пытался", "## Ожидал", "## Что получил",
            "## Почему мешает", "## Предложение",
        ):
            assert heading in out

    def test_cli_old_call_shape_still_works(self, project, tmp_path, capsys):
        """Совместимость: вызов в старом виде (только --body) — та же
        семантика: одна секция, без пустых заголовков."""
        out_dir = tmp_path / "desk"
        _set_feedback_dir(project, out_dir)
        capsys.readouterr()

        rc = cli.main(
            [
                "feedback", "--type", "bug", "--title", "Old shape",
                "--body", "old body", "--project-dir", str(project),
            ]
        )
        assert rc == 0
        text = list(out_dir.glob("awf-bug-*.md"))[0].read_text(encoding="utf-8")
        assert "## Что пытался" in text
        assert "old body" in text
        assert "## Ожидал" not in text
        assert "## Предложение" not in text


class TestLogTail:
    """Хвост последнего лога ≤20 строк; без логов — без секции."""

    def test_tail_last_20_lines(self, project, tmp_path, capsys):
        out_dir = tmp_path / "desk"
        _set_feedback_dir(project, out_dir)
        capsys.readouterr()

        logs = project / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        lines = [f"LOGLINE-{i:02d}" for i in range(1, 26)]  # 25 строк
        (logs / "awf-start.out").write_text("\n".join(lines) + "\n", encoding="utf-8")

        result = api.feedback(project, ftype="bug", title="With log")
        text = Path(result.file).read_text(encoding="utf-8")
        assert "Хвост" in text
        assert "LOGLINE-25" in text  # последняя
        assert "LOGLINE-06" in text  # первая из хвоста (25-20+1)
        assert "LOGLINE-05" not in text  # старше хвоста
        assert "LOGLINE-01" not in text

    def test_no_logs_no_section(self, project, tmp_path, capsys):
        out_dir = tmp_path / "desk"
        _set_feedback_dir(project, out_dir)
        capsys.readouterr()

        result = api.feedback(project, ftype="feature", title="No log")
        text = Path(result.file).read_text(encoding="utf-8")
        assert "Хвост" not in text


class TestSecrets:
    """Секреты в отчёт не тащить: env не дампится."""

    def test_env_not_in_report(self, project, tmp_path, monkeypatch, capsys):
        out_dir = tmp_path / "desk"
        _set_feedback_dir(project, out_dir)
        capsys.readouterr()
        monkeypatch.setenv("AWF_FEEDBACK_TEST_SECRET", "hunter2-do-not-leak")

        result = api.feedback(project, ftype="bug", title="Secret check", body="обычный текст")

        text = Path(result.file).read_text(encoding="utf-8")
        assert "hunter2-do-not-leak" not in text
        assert "AWF_FEEDBACK_TEST_SECRET" not in text
