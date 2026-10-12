"""REPORTS33 (TODO-0208): меньше церемонии — awf-блок в ролях из скиллов
+ brief узнаёт продолжение проекта.

Source: awf-feature-20261012-menshe-ruchnoy-tseremonii-roli-stadii-prodolzhenie-proekta.md
(1.8.0), пункты (1) и (3).

(1) Роли, материализованные из скиллов, несут стэндалон-инструкции
«Commit. Stage только нужные файлы» — конфликт с awf-циклом, где коммитит
engine после verify (риск: слабая модель коммитит в обход engine).
(3) Проект с живым .agentic и историей (10 завершённых юнитов) получал от
awf_brief «New project (setup chain applies)» и шёл через цель → форма →
нормализация.

Invariants:
(a) add_role(from_skill) → awf-блок в конце роле-файла (engine владеет
    коммитом: не git add/commit/push; нет вложенных агентов; границы TODO +
    сигнал, не коммит); тело скилла не тронуто; повторная материализация
    (force) не дублирует блок; preset-материализация несёт тот же блок;
(b) проект с done-историей (или прошлым раном) → brief: шапка
    «продолжение проекта», не «New project», next_action — рабочие шаги
    без setup-цепочки; пустой проект и «составленный, но ни разу не
    запущенный» → как раньше (new project, setup-цепочка);
(c) awf-блок сосуществует с BD-31-аддендумом: analyze дописывает BD-31
    после него, оба идемпотентны, тело скилла побайтов нетронуто.
"""
from __future__ import annotations

from pathlib import Path

from awf import api
from awf.api.roles import analyze_roles_core

AWF_BLOCK = "## AWF-OVERRIDE: awf pipeline overrides (added by awf add_role)"
BD31_BLOCK = "## BD-31: Pipeline-specific disambiguation"

# Тело скилла со стэндалон-инструкцией о коммите — как в реальных agent-*
# скиллах (именно она конфликтует с awf-циклом).
_SKILL = """---
name: demo-skill
description: "A demo skill with standalone commit instructions."
---

# Demo Skill

Do the demo thing.

## Commit

Commit. Stage только нужные файлы.
"""

_PIPELINE = (
    'name: "default"\nstages:\n'
    '  - name: "plan"\n    role: "supervisor"\n'
    '  - name: "work"\n    role: "developer"\n'
    '  - name: "verify"\n    role: "supervisor"\n'
)


def _make_project(base: Path, monkeypatch, with_pipeline: bool = False) -> Path:
    """Проект с .agentic + глобальный скилл под приватным XDG_CONFIG_HOME."""
    proj = base / "proj"
    ag = proj / ".agentic"
    (ag / "roles").mkdir(parents=True)
    (ag / "config.yaml").write_text("project_name: CeremonyTest\n", encoding="utf-8")
    if with_pipeline:
        (ag / "pipelines").mkdir()
        (ag / "pipelines" / "default.yaml").write_text(_PIPELINE, encoding="utf-8")
    xdg = base / "xdg"
    skill_dir = xdg / "opencode" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    return proj


def _role_file(proj: Path, slug: str) -> Path:
    return proj / ".agentic" / "roles" / f"{slug}.md"


def _add_done_history(proj: Path, todo_id: str = "TODO-0001") -> None:
    done = proj / ".agentic" / "done" / todo_id
    done.mkdir(parents=True)
    (done / "TODO.md").write_text(f"# {todo_id}\n", encoding="utf-8")


# ─── (a) awf-блок в ролях из скиллов ─────────────────────────────────────


class TestAwfBlockOnSkillRole:
    def test_block_present_with_engine_commit(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path, monkeypatch)

        api.add_role(proj, "developer", from_skill="demo-skill")

        content = _role_file(proj, "developer").read_text(encoding="utf-8")
        assert AWF_BLOCK in content
        # engine владеет коммитом: запрет git add/commit/push в юните
        assert "the awf engine commits after the supervisor's verify" in content
        assert "never run `git add`, `git commit`, or `git push`" in content
        # вложенные агенты запрещены
        assert "No nested agents" in content
        assert "nested opencode" in content
        # границы TODO + сигнал, не коммит
        assert "TODO's boundaries" in content
        assert "not a commit" in content

    def test_skill_body_preserved_and_block_last(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path, monkeypatch)

        api.add_role(proj, "developer", from_skill="demo-skill")

        content = _role_file(proj, "developer").read_text(encoding="utf-8")
        # provenance + тело скилла (включая конфликтующий раздел Commit)
        assert content.startswith("<!-- copied from skill `demo-skill`")
        assert "Do the demo thing." in content
        assert "Commit. Stage только нужные файлы." in content
        # front-matter скилла снят, model-строка есть, блок — последняя секция
        assert not content.splitlines()[1].lstrip().startswith("---")
        assert "**Модель:**" in content
        assert content.index("Do the demo thing.") < content.index(AWF_BLOCK)
        assert content.rstrip().endswith("not a commit.")

    def test_repeated_materialization_no_duplicate(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path, monkeypatch)

        api.add_role(proj, "developer", from_skill="demo-skill")
        first = _role_file(proj, "developer").read_text(encoding="utf-8")
        api.add_role(proj, "developer", from_skill="demo-skill", force=True)
        second = _role_file(proj, "developer").read_text(encoding="utf-8")

        assert second.count(AWF_BLOCK) == 1
        # helper идемпотентен сам по себе
        from awf.api.roles import apply_awf_overrides

        assert apply_awf_overrides(second) == second
        # тело скилла выжило повторную материализацию
        assert "Do the demo thing." in second

    def test_preset_materialization_carries_block(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path, monkeypatch)
        # скилл из каталога preset (qa-review ← agent-qa-review)
        skill_dir = tmp_path / "xdg" / "opencode" / "skills" / "agent-qa-review"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(_SKILL, encoding="utf-8")

        result = api.seed_roles(proj)

        created = {row["role"] for row in result.created}
        assert "qa-review" in created
        content = _role_file(proj, "qa-review").read_text(encoding="utf-8")
        assert AWF_BLOCK in content
        assert "never run `git add`, `git commit`, or `git push`" in content

    def test_draft_candidate_carries_block(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path, monkeypatch)

        api.add_role(proj, "qa", from_skill="demo-skill", draft=True)

        draft = proj / ".agentic" / "roles" / "draft" / "qa.md"
        content = draft.read_text(encoding="utf-8")
        # маркер кандидата — первая строка, блок — в конце
        assert content.splitlines()[0].startswith("<!-- awf-draft source=skill:demo-skill")
        assert AWF_BLOCK in content
        # adopt: блок переходит в живую роль, маркер снимается
        api.adopt_role_draft(proj, "qa")
        live = _role_file(proj, "qa").read_text(encoding="utf-8")
        assert AWF_BLOCK in live
        assert not live.startswith("<!-- awf-draft")


class TestNoBlockOnOtherSources:
    def test_template_role_gets_no_block(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path, monkeypatch)

        api.add_role(proj, "editor", description="edits docs")

        content = _role_file(proj, "editor").read_text(encoding="utf-8")
        assert AWF_BLOCK not in content

    def test_existing_role_untouched(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path, monkeypatch)
        (proj / ".agentic" / "roles" / "handwritten.md").write_text(
            "# Handwritten\n\nMy own protocol.\n", encoding="utf-8"
        )

        result = api.add_role(proj, "developer", from_skill="demo-skill")
        # чужой файл не тронут, создание других ролей его не меняет
        assert (
            (proj / ".agentic" / "roles" / "handwritten.md").read_text(encoding="utf-8")
            == "# Handwritten\n\nMy own protocol.\n"
        )
        assert result.role_file.endswith("developer.md")


# ─── (b) brief: детект продолжения проекта ───────────────────────────────


class TestBriefContinuation:
    def test_done_history_header_continues(self, tmp_path):
        proj = tmp_path / "proj"
        (proj / ".agentic").mkdir(parents=True)
        _add_done_history(proj)

        r = api.brief(proj)

        assert r.is_live_project is True
        assert "Продолжение проекта" in r.text
        assert "New project" not in r.text
        assert r.setup_hint == ""
        # фаза — рабочий цикл, не setup
        assert r.phase == "run"
        # next_action — рабочие шаги, без setup-цепочки
        assert r.next_action
        for needle in ("setup", "awf_init", "awf_set_goal", "project_setup_form"):
            assert needle not in r.next_action, f"{needle!r} in next_action"
        # рабочие шаги: создать TODO и запустить пайплайн
        assert "TODO" in r.next_action
        assert ("awf start" in r.next_action) or ("awf_start" in r.next_action)

    def test_past_run_header_continues(self, tmp_path):
        proj = tmp_path / "proj"
        state = proj / ".agentic" / "state"
        state.mkdir(parents=True)
        # завершённый ран: непустая очередь, index на границе, active: false
        (state / "run.yaml").write_text(
            "active: false\nqueue:\n- TODO-0001\nindex: 1\n", encoding="utf-8"
        )

        r = api.brief(proj)

        assert r.is_live_project is True
        assert "Продолжение проекта" in r.text
        assert "New project" not in r.text
        assert r.phase == "run"
        assert "run (closed)" in r.text  # ран-рекорд прочитан, не потерялся

    def test_empty_project_stays_new(self, tmp_path):
        proj = tmp_path / "fresh"  # вообще без .agentic

        r = api.brief(proj)

        assert r.is_live_project is False
        assert "New project (setup chain applies)." in r.text
        assert r.setup_hint
        assert r.phase == "init"
        assert "setup chain" in r.next_action

    def test_bare_agentic_stays_new(self, tmp_path):
        proj = tmp_path / "proj"
        (proj / ".agentic").mkdir(parents=True)  # живый .agentic, но истории нет

        r = api.brief(proj)

        assert r.is_live_project is False
        assert "New project (setup chain applies)." in r.text
        assert r.setup_hint in r.text

    def test_configured_but_never_run_stays_new(self, tmp_path):
        proj = tmp_path / "proj"
        (proj / ".agentic" / "pipelines").mkdir(parents=True)
        (proj / ".agentic" / "pipelines" / "default.yaml").write_text(
            _PIPELINE, encoding="utf-8"
        )

        r = api.brief(proj)

        assert r.is_live_project is False
        assert "New project (setup chain applies)." in r.text
        assert r.setup_hint in r.text


# ─── (c) сосуществование с BD-31-аддендумом ────────────────────────────────


class TestCoexistWithBd31:
    def test_analyze_appends_bd31_after_awf_block(self, tmp_path, monkeypatch):
        proj = _make_project(tmp_path, monkeypatch, with_pipeline=True)
        api.add_role(proj, "developer", from_skill="demo-skill")
        body_line = "Do the demo thing."

        analyze_roles_core(proj, dry_run=False)
        content = _role_file(proj, "developer").read_text(encoding="utf-8")

        assert AWF_BLOCK in content
        assert BD31_BLOCK in content
        # порядок: тело скилла → awf-блок → BD-31 (дописан после)
        assert content.index(body_line) < content.index(AWF_BLOCK)
        assert content.index(AWF_BLOCK) < content.index(BD31_BLOCK)

        # повторный analyze: оба блока не дублируются, тело не тронуто
        analyze_roles_core(proj, dry_run=False)
        content2 = _role_file(proj, "developer").read_text(encoding="utf-8")
        assert content2.count(AWF_BLOCK) == 1
        assert content2.count(BD31_BLOCK) == 1
        assert body_line in content2
        assert "Commit. Stage только нужные файлы." in content2

    def test_awf_block_refresh_does_not_eat_bd31(self, tmp_path, monkeypatch):
        """Повторная материализация (force) после analyze: refresh awf-блока
        не должен съесть BD-31-блок и тело скилла."""
        proj = _make_project(tmp_path, monkeypatch, with_pipeline=True)
        api.add_role(proj, "developer", from_skill="demo-skill")
        analyze_roles_core(proj, dry_run=False)

        api.add_role(proj, "developer", from_skill="demo-skill", force=True)
        content = _role_file(proj, "developer").read_text(encoding="utf-8")

        # force пересоздаёт файл из скилла: BD-31 (добавка analyze) не
        # восстанавливается автоматически — но и awf-блок единственный,
        # тело скилла целое
        assert content.count(AWF_BLOCK) == 1
        assert "Do the demo thing." in content
        # и BD-31-механизм поверх этого файла всё ещё идемпотентен
        analyze_roles_core(proj, dry_run=False)
        content2 = _role_file(proj, "developer").read_text(encoding="utf-8")
        assert content2.count(AWF_BLOCK) == 1
        assert content2.count(BD31_BLOCK) == 1
