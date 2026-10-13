"""REPORTS33: awf_init — vision_excerpt из фактического vision_path.

Source: awf-bug-20261012-awf-init-vision-path-readme-md-pri-pustom-vision-excerpt.md (1.8.0).
Re-init существующего проекта: ``vision_path = "…/README.md"`` (файл ~5 КБ
существует), но ``vision_excerpt = ""``. Корень: re-init и dry-run ветки
``init_project`` хардкодили ``vision_excerpt=""`` — файл читала только
fresh-init ветка.

Invariants:
(a) проект с README.md (без VISION.md) → re-init: excerpt непуст,
    первые 4000 символов README (+ маркер truncation, как в fresh-init);
(b) проект с VISION.md → как раньше: excerpt читается в fresh-init И
    в re-init, побайтов одинаковый;
(c) якорь отсутствует (ни README, ни VISION) → excerpt пуст + явная
    строка-причина в warnings, а не молчание;
(d) якорь нечитаем → excerpt пуст + явная строка-причина в warnings;
(e) dry-run на существующем проекте с README → тот же непустой excerpt
    (dry-run — pure read, молчать не должен);
(f) dry-run свежего проекта с README → превью называет тот же excerpt.
"""
from __future__ import annotations

from pathlib import Path

from awf import api

README_TEXT = "Записи проекта. " * 260  # 4160 символов, > 4000
EXCERPT = README_TEXT[:4000] + "\n...[truncated]"


def _init_fresh(repo: Path, name: str = "T") -> None:
    api.init_project(repo, project_name=name)


def _reinit_readme(tmp_git_repo: Path) -> Path:
    """Гит-репо с README >4000 символов (без VISION.md) после fresh-init."""
    (tmp_git_repo / "README.md").write_text(README_TEXT)
    _init_fresh(tmp_git_repo)
    return tmp_git_repo


class TestReinitReadmeAnchor:
    """(a)/(e) README как якорь: excerpt непуст и честный."""

    def test_reinit_excerpt_from_readme(self, tmp_git_repo):
        repo = _reinit_readme(tmp_git_repo)

        result = api.init_project(repo, project_name="T")

        assert result.vision_path is not None
        assert result.vision_path.endswith("README.md")
        assert result.vision_excerpt != ""
        assert result.vision_excerpt == EXCERPT

    def test_dry_run_reinit_excerpt_from_readme(self, tmp_git_repo):
        repo = _reinit_readme(tmp_git_repo)

        result = api.init_project(repo, project_name="T", dry_run=True)

        assert result.vision_path is not None
        assert result.vision_path.endswith("README.md")
        assert result.vision_excerpt == EXCERPT


class TestFreshDryRunPreview:
    """(f) dry-run свежего проекта: превью называет тот же excerpt."""

    def test_fresh_dry_run_excerpt_from_readme(self, tmp_git_repo):
        (tmp_git_repo / "README.md").write_text(README_TEXT)

        result = api.init_project(tmp_git_repo, project_name="T", dry_run=True)

        assert result.vision_path is not None
        assert result.vision_path.endswith("README.md")
        assert result.vision_excerpt == EXCERPT
        assert not (tmp_git_repo / ".agentic").exists()  # pure read


class TestVisionMdUnchanged:
    """(b) VISION.md: поведение как раньше, re-init теперь не молчит."""

    def test_vision_md_fresh_and_reinit_same_excerpt(self, tmp_git_repo):
        vision = "# Vision\n\n" + ("цель проекта " * 400)
        (tmp_git_repo / "VISION.md").write_text(vision)
        _init_fresh(tmp_git_repo)

        fresh_excerpt = api.init_project(tmp_git_repo, project_name="T").vision_excerpt
        result = api.init_project(tmp_git_repo, project_name="T")

        expected = vision[:4000] + "\n...[truncated]"
        assert fresh_excerpt == expected, "fresh-init behavior changed"
        assert result.vision_path is not None
        assert result.vision_path.endswith("VISION.md")
        assert result.vision_excerpt == expected


class TestMissingAnchorHonest:
    """(c) ни README, ни VISION: excerpt пуст + явная причина, не молчание."""

    def test_reinit_no_anchor_warns(self, tmp_git_repo):
        (tmp_git_repo / "README.md").unlink()  # fixture создаёт README — убираем
        _init_fresh(tmp_git_repo)

        result = api.init_project(tmp_git_repo, project_name="T")

        assert result.vision_path is None
        assert result.vision_excerpt == ""
        assert any("vision file not found" in w for w in result.warnings), (
            "re-init без якоря молчит: "
            f"warnings={result.warnings!r}"
        )


class TestUnreadableAnchorHonest:
    """(d) якорь существует, но читать нельзя: excerpt пуст + причина."""

    def test_reinit_unreadable_anchor_warns(self, tmp_git_repo, monkeypatch):
        repo = _reinit_readme(tmp_git_repo)
        vision = repo / "README.md"
        real_read_text = Path.read_text

        def guarded(self: Path, *args, **kwargs):
            if self == vision:
                raise OSError(13, "Permission denied")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", guarded)

        result = api.init_project(repo, project_name="T")

        assert result.vision_path is not None
        assert result.vision_path.endswith("README.md")
        assert result.vision_excerpt == ""
        assert any("unreadable" in w for w in result.warnings), (
            "нечитаемый якорь без строки-причины: "
            f"warnings={result.warnings!r}"
        )


class TestBrokenByteNoFall:
    """(g) битый байт в якоря: errors=replace — не падать, маркер в excerpt."""

    def test_reinit_broken_byte_uses_replace(self, tmp_git_repo):
        repo = _reinit_readme(tmp_git_repo)
        (repo / "README.md").write_bytes(b"ok \xff\xfe bad \xff end")

        result = api.init_project(repo, project_name="T")

        assert result.vision_path is not None
        assert result.vision_path.endswith("README.md")
        assert result.vision_excerpt != ""
        assert "\ufffd" in result.vision_excerpt
