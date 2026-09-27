"""ORCH M3.1 (контракт забега и поручения): модель стадии — ``id`` и ``task``.

Дефект (воспроизведён): схема пайплайна (ALLOWED_STAGE_KEYS, A-06)
отвергает ключи ``id`` и ``task`` как неизвестные, поэтому одну роль
нельзя использовать дважды с разными поручениями: ``name`` по
умолчанию равен ролевой слуге, а дублирующиеся имена стадий грузятся
с предупреждением, где движок резолвит первое вхождение
(_find_stage_index).

Инварианты:
1. Стадия принимает опциональные ``id`` (уникальный слаг) и ``task``
   (короткое поручение); загрузка и запись — один ruleset; неизвестные
   ключи по-прежнему отвергаются; старый YAML без новых полей грузится
   как раньше.
2. Одна роль дважды с разными ``id``: грузится, стадии адресуются по
   ``id`` (имя стадии = id, handoff-файлы ``<имя>-<todo>.md``
   различаются); дубли ``id`` отвергаются с понятной ошибкой.
3. ``task`` доступен на Stage и сохраняется сквозь load.
4. ``kind`` по позиции не меняется: план первый, verify последний,
   середина — execute.
5. Существующие тесты пайплайнов зелёные (back-compat).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.api._errors import AwfApiError
from awf.pipeline import load_stages

# Одна роль (editor) дважды с разными id + task; план/verify по
# углам, чтобы kind по позиции остался проверяемым.
TWO_EDITORS = (
    "stages:\n"
    "  - name: plan\n    role: supervisor\n"
    "  - id: outline\n    role: editor\n    task: first draft of the outline\n"
    "  - id: final_edit\n    role: editor\n    task: final polish pass\n"
    "  - name: verify\n    role: supervisor\n"
)

LEGACY = (
    "stages:\n"
    "  - name: plan\n    role: supervisor\n"
    "  - name: impl\n    role: worker\n    description: does the work\n"
    "    max_retries: 2\n"
    "  - name: verify\n    role: supervisor\n"
)


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "pipeline.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".agentic" / "pipelines").mkdir(parents=True)
    (proj / ".agentic" / "config.yaml").write_text(
        "project:\n  name: T\n", encoding="utf-8"
    )
    return proj


# ── prove_red: роль дважды с id + task ─────────────────────────────────────


def test_same_role_twice_with_ids_loads(tmp_path):
    """ORCH M3.1.2: одна роль дважды с разными id грузится; имя стадии
    = id (handoff-файлы различаются), kind по позиции не изменился."""
    stages = load_stages(_write(tmp_path, TWO_EDITORS))

    assert [s.role for s in stages] == [
        "supervisor", "editor", "editor", "supervisor",
    ]
    assert [s.name for s in stages] == ["plan", "outline", "final_edit", "verify"]
    assert [s.id for s in stages] == ["", "outline", "final_edit", ""]
    # инвариант 4: kind по позиции — id не влияет
    assert [s.kind for s in stages] == ["plan", "execute", "execute", "verify"]


def test_stage_task_lands_in_stage(tmp_path):
    """ORCH M3.1.3: task доступен на Stage и сохраняется сквозь load."""
    stages = load_stages(_write(tmp_path, TWO_EDITORS))

    assert stages[1].task == "first draft of the outline"
    assert stages[2].task == "final polish pass"
    # ключ отсутствует — дефолт пусто (не None, не KeyError)
    assert stages[0].task == ""
    assert stages[3].task == ""


# ── Дубли id: понятный отказ ───────────────────────────────────────────────


def test_duplicate_id_rejected_at_load(tmp_path):
    """ORCH M3.1.2: два id-дубля — AwfApiError с текстом, а не тишина."""
    content = (
        "stages:\n"
        "  - name: plan\n    role: supervisor\n"
        "  - id: same\n    role: editor\n"
        "  - id: same\n    role: editor\n"
        "  - name: verify\n    role: supervisor\n"
    )
    with pytest.raises(AwfApiError, match="duplicate stage id 'same'"):
        load_stages(_write(tmp_path, content))


def test_duplicate_id_rejected_on_write(tmp_path):
    """ORCH M3.1.1: write-путь — тот же валидатор; отказ не пишет файл."""
    from awf import api

    proj = _project(tmp_path)
    with pytest.raises(AwfApiError, match="duplicate stage id"):
        api.write_pipeline(
            proj,
            "p",
            [
                {"role": "supervisor", "name": "plan"},
                {"role": "editor", "id": "same"},
                {"role": "editor", "id": "same"},
                {"role": "supervisor", "name": "verify"},
            ],
        )
    assert not (proj / ".agentic" / "pipelines" / "p.yaml").exists()


# ── Типы и формат id / task ────────────────────────────────────────────────


def test_id_not_string_rejected(tmp_path):
    p = _write(
        tmp_path,
        "stages:\n  - id: 42\n    role: editor\n  - name: verify\n"
        "    role: supervisor\n",
    )
    with pytest.raises(AwfApiError, match="'id'"):
        load_stages(p)


def test_id_empty_rejected(tmp_path):
    for value in ('""', "'   '"):
        p = _write(
            tmp_path,
            f"stages:\n  - id: {value}\n    role: editor\n"
            "  - name: verify\n    role: supervisor\n",
        )
        with pytest.raises(AwfApiError, match="'id'"):
            load_stages(p)


def test_id_not_a_slug_rejected(tmp_path):
    """ORCH M3.1: id — слаг (будет именем стадии и частью имени файла)."""
    for value in ("my id", "a/b", "../x"):
        p = _write(
            tmp_path,
            f'stages:\n  - id: "{value}"\n    role: editor\n'
            "  - name: verify\n    role: supervisor\n",
        )
        with pytest.raises(AwfApiError, match="stage slug"):
            load_stages(p)


def test_task_not_string_rejected(tmp_path):
    p = _write(
        tmp_path,
        "stages:\n  - id: outline\n    role: editor\n    task: 42\n"
        "  - name: verify\n    role: supervisor\n",
    )
    with pytest.raises(AwfApiError, match="'task'"):
        load_stages(p)


def test_task_empty_rejected(tmp_path):
    p = _write(
        tmp_path,
        'stages:\n  - id: outline\n    role: editor\n    task: ""\n'
        "  - name: verify\n    role: supervisor\n",
    )
    with pytest.raises(AwfApiError, match="'task'"):
        load_stages(p)


# ── Back-compat: старый YAML без новых полей ────────────────────────────────


def test_legacy_yaml_loads_unchanged(tmp_path):
    """ORCH M3.1.1/5: без id/task — имена, роли, kind, бюджеты как раньше;
    новые поля на Stage — пустые строки."""
    stages = load_stages(_write(tmp_path, LEGACY))

    assert [s.name for s in stages] == ["plan", "impl", "verify"]
    assert [s.kind for s in stages] == ["plan", "execute", "verify"]
    assert stages[1].description == "does the work"
    assert stages[1].max_retries == 2
    assert all(s.id == "" for s in stages)
    assert all(s.task == "" for s in stages)


def test_same_role_twice_without_ids_keeps_warning(tmp_path, capsys):
    """ORCH M3.1.5: без id повтор роли — старое поведение (предупреждение
    о дубле имени, загрузка проходит); с id — нормальный случай."""
    content = (
        "stages:\n"
        "  - name: editor\n    role: editor\n"
        "  - name: editor\n    role: editor\n"
    )
    stages = load_stages(_write(tmp_path, content))
    err = capsys.readouterr().err

    assert len(stages) == 2
    assert "duplicate stage name 'editor'" in err


# ── Имя стадии = id; write-путь ────────────────────────────────────────────


def test_id_becomes_stage_name(tmp_path):
    """ORCH M3.1.2: явный id переопределяет name (адрес = id)."""
    content = (
        "stages:\n"
        "  - id: outline\n    name: ignored\n    role: editor\n"
        "  - name: verify\n    role: supervisor\n"
    )
    stages = load_stages(_write(tmp_path, content))

    assert stages[0].id == "outline"
    assert stages[0].name == "outline"


def test_write_name_defaults_to_id(tmp_path):
    """ORCH M3.1.1: write-путь — name по умолчанию = id, затем роль;
    id и task попадают в YAML."""
    from awf import api

    proj = _project(tmp_path)
    api.write_pipeline(
        proj,
        "p",
        [
            {"role": "supervisor", "name": "plan"},
            {"role": "editor", "id": "outline", "task": "draft the outline"},
            {"role": "supervisor", "name": "verify"},
        ],
    )

    data = yaml.safe_load(
        (proj / ".agentic" / "pipelines" / "p.yaml").read_text(encoding="utf-8")
    )
    assert [s["name"] for s in data["stages"]] == [
        "plan", "outline", "verify",
    ]
    assert data["stages"][1]["id"] == "outline"
    assert data["stages"][1]["task"] == "draft the outline"


def test_written_id_pipeline_loads(tmp_path):
    """ORCH M3.1.2: то, что написал write-путь, грузится load-путём
    (roundtrip одного ruleset'а)."""
    from awf import api

    proj = _project(tmp_path)
    api.write_pipeline(
        proj,
        "p",
        [
            {"role": "supervisor", "name": "plan"},
            {"role": "editor", "id": "outline", "task": "draft"},
            {"role": "editor", "id": "final_edit", "task": "polish"},
            {"role": "supervisor", "name": "verify"},
        ],
    )
    stages = load_stages(proj / ".agentic" / "pipelines" / "p.yaml")

    assert [s.name for s in stages] == [
        "plan", "outline", "final_edit", "verify",
    ]
    assert [s.task for s in stages[1:3]] == ["draft", "polish"]
