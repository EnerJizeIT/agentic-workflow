"""A-06 (аудит 2026-09-25, слой 1): загрузчик проверяет форму до Stage.

Дефект (воспроизведён): ``load_stages`` (``awf/pipeline.py``) после
``yaml.safe_load`` делает ``(data or {}).get("stages")`` — корень-список
даёт ``AttributeError``, элементы ``stages`` не валидируются как схема
(``stages: [42]`` тихо возвращает ``[]``), числовая политика
``on_blocked: 5`` позже падает в ``.startswith`` — ошибка всплывает
только при старте пайплайна.

Инварианты:
1. Единый валидатор (общий для ``load_stages`` и write-пути
   ``awf.api.write_pipeline``) проверяет типы: корень — mapping;
   ``stages`` — непустой список mapping'ов; ``role`` — непустая строка;
   политики — строки из допустимого набора или отсутствуют;
   ``max_retries``/``max_rollbacks`` — неотрицательные целые;
   неизвестные ключи стадии — отклоняются (как в write).
2. Невалидный YAML → ``AwfApiError`` при загрузке, без побочных записей.
3. Валидные пайплайны (включая default репозитория) грузятся как раньше.

Не тронут AUD06-16: порча на уровне байт (non-UTF-8, битый YAML) по-
прежнему деградирует до ``[]`` — см. test_audit06_incidents.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf.api._errors import AwfApiError
from awf.pipeline import load_stages

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

VALID_MINIMAL = (
    "stages:\n"
    "  - name: plan\n    role: supervisor\n"
    "  - name: impl\n    role: worker\n    max_retries: 2\n"
    "    on_rejected: rollback_to:plan\n"
    "  - name: verify\n    role: supervisor\n    on_approved: commit_and_next\n"
)


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "pipeline.yaml"
    p.write_text(content, encoding="utf-8")
    return p


# ── prove_red: дефект аудита — форма не проверялась ────────────────────────


def test_list_root_yaml_gives_clear_error(tmp_path):
    """A-06.1: корень — список → понятный AwfApiError, не AttributeError.

    Baseline (дефект): AttributeError('list' object has no attribute 'get')
    — красный.
    """
    p = _write(
        tmp_path,
        "- name: plan\n  role: supervisor\n- name: verify\n  role: supervisor\n",
    )
    with pytest.raises(AwfApiError, match="root must be a mapping"):
        load_stages(p)


def test_bad_stage_item_type_gives_clear_error(tmp_path):
    """A-06.1: ``stages: [42]`` → понятный AwfApiError, не тихий ``[]``.

    Baseline (дефект): load_stages возвращает [] — пайплайн «без стадий»
    уходит дальше и падает при старте — красный.
    """
    p = _write(tmp_path, "stages: [42]\n")
    with pytest.raises(AwfApiError, match="stage #0 must be a mapping"):
        load_stages(p)


# ── Типы: корень и список стадий ───────────────────────────────────────────


def test_valid_minimal_pipeline_loads_unchanged(tmp_path):
    """A-06.3: валидный минимальный пайплайн грузится как раньше."""
    stages = load_stages(_write(tmp_path, VALID_MINIMAL))
    assert [s.name for s in stages] == ["plan", "impl", "verify"]
    assert [s.kind for s in stages] == ["plan", "execute", "verify"]
    assert stages[1].max_retries == 2
    assert stages[1].on_rejected == "rollback_to:plan"
    assert stages[2].on_approved == "commit_and_next"
    # политики, не заданные в YAML, остались дефолтными
    assert stages[0].on_blocked == "escalate"


def test_repo_default_pipeline_loads_unchanged(tmp_path):
    """A-06.3: реальный .agentic/pipelines/default.yaml репозитория."""
    src = REPO_ROOT / ".agentic" / "pipelines" / "default.yaml"
    dst = tmp_path / ".agentic" / "pipelines" / "default.yaml"
    dst.parent.mkdir(parents=True)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    stages = load_stages(dst)

    assert [s.name for s in stages] == [
        "plan", "agent-implementer", "agent-qa-review", "verify",
    ]
    assert stages[-1].on_approved == "commit_and_next"


def test_root_scalar_rejected(tmp_path):
    """A-06.1: корень — скаляр (не mapping) → отказ."""
    p = _write(tmp_path, "just a string\n")
    with pytest.raises(AwfApiError, match="root must be a mapping"):
        load_stages(p)


def test_stages_absent_rejected(tmp_path):
    """A-06.1: документ без stages → отказ, а не тихий пустой пайплайн."""
    p = _write(tmp_path, "name: x\n")
    with pytest.raises(AwfApiError, match="'stages' must be a non-empty list"):
        load_stages(p)


def test_stages_empty_list_rejected(tmp_path):
    p = _write(tmp_path, "stages: []\n")
    with pytest.raises(AwfApiError, match="'stages' must be a non-empty list"):
        load_stages(p)


def test_stages_not_a_list_rejected(tmp_path):
    p = _write(tmp_path, "stages:\n  name: plan\n  role: supervisor\n")
    with pytest.raises(AwfApiError, match="'stages' must be a non-empty list"):
        load_stages(p)


def test_stage_not_mapping_rejected(tmp_path):
    p = _write(tmp_path, 'stages:\n  - "plan"\n')
    with pytest.raises(AwfApiError, match="stage #0 must be a mapping"):
        load_stages(p)


# ── Типы: role ─────────────────────────────────────────────────────────────


def test_role_missing_rejected(tmp_path):
    p = _write(
        tmp_path,
        "stages:\n  - name: plan\n  - name: verify\n    role: supervisor\n",
    )
    with pytest.raises(AwfApiError, match="role"):
        load_stages(p)


def test_role_not_string_rejected(tmp_path):
    p = _write(
        tmp_path,
        "stages:\n  - name: plan\n    role: 42\n  - name: verify\n    role: supervisor\n",
    )
    with pytest.raises(AwfApiError, match="role"):
        load_stages(p)


def test_role_empty_rejected(tmp_path):
    p = _write(
        tmp_path,
        'stages:\n  - name: plan\n    role: ""\n  - name: verify\n    role: supervisor\n',
    )
    with pytest.raises(AwfApiError, match="role"):
        load_stages(p)


# ── Типы: политики ─────────────────────────────────────────────────────────


def test_policy_not_string_rejected(tmp_path):
    """A-06 (дефект): ``on_blocked: 5`` раньше падал в .startswith."""
    p = _write(
        tmp_path,
        "stages:\n  - name: plan\n    role: supervisor\n    on_blocked: 5\n"
        "  - name: verify\n    role: supervisor\n",
    )
    with pytest.raises(AwfApiError, match="on_blocked"):
        load_stages(p)


def test_policy_unknown_word_rejected(tmp_path):
    """A-06: опечатка в слове политики — отказ, не предупреждение."""
    p = _write(
        tmp_path,
        "stages:\n  - name: plan\n    role: supervisor\n    on_blocked: halt\n"
        "  - name: verify\n    role: supervisor\n",
    )
    with pytest.raises(AwfApiError, match="on_blocked='halt'"):
        load_stages(p)


def test_policy_rollback_on_wrong_key_rejected(tmp_path):
    """A-06: rollback_to: на ключе, который резолвер не читает — отказ."""
    p = _write(
        tmp_path,
        "stages:\n  - name: plan\n    role: supervisor\n"
        "  - name: verify\n    role: supervisor\n"
        "    on_approved: rollback_to:plan\n",
    )
    with pytest.raises(AwfApiError, match="rollback_to:<stage> is only supported"):
        load_stages(p)


def test_policy_rollback_on_supported_key_ok(tmp_path):
    """A-06.3: rollback_to: на on_rejected (цель существует) — грузится."""
    p = _write(tmp_path, VALID_MINIMAL)
    stages = load_stages(p)
    assert stages[1].on_rejected == "rollback_to:plan"


# ── Типы: бюджеты ──────────────────────────────────────────────────────────


def test_budget_negative_rejected(tmp_path):
    """A-06: отрицательный бюджет — отказ (раньше предупреждение)."""
    p = _write(
        tmp_path,
        "stages:\n  - name: plan\n    role: supervisor\n"
        "  - name: impl\n    role: worker\n    max_retries: -3\n"
        "  - name: verify\n    role: supervisor\n",
    )
    with pytest.raises(AwfApiError, match="max_retries must be non-negative"):
        load_stages(p)

    p2 = _write(
        tmp_path,
        "stages:\n  - name: plan\n    role: supervisor\n"
        "  - name: impl\n    role: worker\n    max_rollbacks: -1\n"
        "  - name: verify\n    role: supervisor\n",
    )
    with pytest.raises(AwfApiError, match="max_rollbacks must be non-negative"):
        load_stages(p2)


def test_budget_not_integer_rejected(tmp_path):
    """A-06: бюджет — целое; float/строка/bool отвергаются."""
    for value in ("2.5", '"3"', "true"):
        p = _write(
            tmp_path,
            f"stages:\n  - name: plan\n    role: supervisor\n"
            f"  - name: impl\n    role: worker\n    max_retries: {value}\n"
            f"  - name: verify\n    role: supervisor\n",
        )
        with pytest.raises(AwfApiError, match="max_retries must be an integer"):
            load_stages(p)


# ── Неизвестные ключи ──────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["on_passed", "action", "kind"])
def test_unknown_stage_key_rejected(tmp_path, key):
    """A-06: неизвестный ключ (опечатка, legacy action:/kind:) — отказ,
    как в awf_write_pipeline (AUD13-04)."""
    p = _write(
        tmp_path,
        f"stages:\n  - name: plan\n    role: supervisor\n    {key}: x\n"
        f"  - name: verify\n    role: supervisor\n",
    )
    with pytest.raises(AwfApiError, match="unknown keys"):
        load_stages(p)


def test_allowed_keys_set_pinned():
    """A-06: набор разрешённых ключей один для загрузки и записи."""
    from awf.pipeline import ALLOWED_STAGE_KEYS

    assert ALLOWED_STAGE_KEYS == {
        "name", "role", "description",
        "on_blocked", "on_approved", "on_rejected", "on_failed",
        "max_retries", "max_rollbacks",
    }


# ── Write-путь: общие правила, без побочных записей при отказе ─────────────


def test_write_pipeline_rejects_bad_stage_shape(tmp_path):
    """A-06: write-путь идёт через тот же валидатор (единый код)."""
    from awf import api

    proj = tmp_path / "proj"
    (proj / ".agentic" / "pipelines").mkdir(parents=True)
    (proj / ".agentic" / "config.yaml").write_text(
        "project:\n  name: T\n", encoding="utf-8"
    )

    with pytest.raises(AwfApiError, match="unknown keys"):
        api.write_pipeline(proj, "p", [{"role": "worker", "on_done": "commit"}])
    with pytest.raises(AwfApiError, match="non-negative"):
        api.write_pipeline(
            proj, "p", [{"role": "worker", "max_retries": -1}]
        )
    # отказ не оставляет файла
    assert not (proj / ".agentic" / "pipelines" / "p.yaml").exists()


def test_load_does_not_write_on_rejection(tmp_path):
    """A-06.2: невалидный пайплайн не создаёт runtime-записей."""
    p = _write(tmp_path, "stages: [42]\n")
    outbox = tmp_path / ".agentic" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    before = {q.name for q in tmp_path.rglob("*")}
    with pytest.raises(AwfApiError):
        load_stages(p)
    after = {q.name for q in tmp_path.rglob("*")}
    assert after == before
