# BACKLOG

> История закрытых записей — в [BACKLOG-archive.md](BACKLOG-archive.md).

---

## Открытые задачи

### SMO · State-Machine Orchestration — завершён (.1-.6), .7 после dogfood

**Status:** .1-.6 DONE. .7 parked (after dogfood).

✅ `.1` **Foundation:** `awf/phase.py` — `detect_phase()` + `get_phase_prompt()`
   + `advance_phase()`. Phase в state. 3 новых tool: `awf_current_step`,
   `awf_set_goal`, `awf_confirm_normalized`. Backward compat: нет phase → full supervisor.md.

✅ `.2` **Split supervisor.md:** `templates/roles/supervisor/_core.md` (~40 строк)
   + `phase-{init,goal,form,normalize,brief,run,verify}.md` (each ~30-60 строк).
   Старый `supervisor.md` сохранён для backward compat.

✅ `.3` **Goal step:** `awf_set_goal(goal)` → stores in state → advances goal→form.

✅ `.4` **Form step:** `phase-form.md` — supervisor recommends roles based on goal,
   opens `awf_open_project_setup_form`.

✅ `.5` **Normalize step:** `phase-normalize.md` — 3-part checklist.
   `awf_confirm_normalized()` — gate, advances normalize→brief.

✅ `.6` **Brief step:** `phase-brief.md` + existing R5 pipeline_engine detection.
   Orchestrator writes phase at transitions (brief→run→verify→done).

⬜ `.7` **Escape-hatch'и:** ПОСЛЕ dogfood. Собрать edge-cases с реальных сессий.
   **НЕ проектировать upfront.**

### AUD-12 · [T3] Рефакторинг (закрытые пункты)

✅ `.1` — `_xdg_config_home` ×3 → consolidated в `awf.xdg`
✅ `.2` — `open_form` scans → lazy (только project-setup)
✅ `.3` — `awf.py` wrappers → `_exec` helper
✅ `.5` — `_extract_stage_info_regex` → removed (90 строк)
⬜ `.4` — **Deferred**: circular import orchestrator↔pipeline_engine.
   Текущий lazy import pattern работает. 15 символов — тесная связь,
   но не баг. Перенос в третий модуль = высокий риск без немедленной пользы.
   Revisit when adding new module that needs shared handlers.

### AUD-2026-08-09 · Roundtable audit (9 принятых из 11)

**Source:** roundtable audit (Архитектор + Ревьюер + Чистильщик + Безопасник).
**Rejected:** mock-heavy orchestrator tests (catches real regressions), CSRF bypass
(local-only tool, token = overkill).

#### T2 — точечные фиксы

⬜ `.1` **[HIGH] jinja2 undeclared dep** — `awf/api/dashboard.py:36` импортирует
   jinja2, но root `pyproject.toml` содержит только `PyYAML>=6.0`.
   `pip install awf` без plugin → `ModuleNotFoundError` при первом dashboard.
   Фикс: добавить `jinja2>=3.1` в `dependencies` root pyproject.toml.

⬜ `.2` **[MED] rollback path traversal** — `awf/api/pipeline.py:310` использует
   `todo_id` в пути `BASELINE-{todo_id}.sha` без regex-валидации (в отличие от
   `dispatch.py:95` где есть `^TODO-\d{4,}$`). MCP tool принимает произвольные
   строки от LLM. Фикс: тот же regex в `rollback()`.

⬜ `.3` **[MED] commit_gate wrong baseline fallback** — `awf/commit_gate.py:108-116`
   fallback на most-recent `BASELINE-*.untracked` по mtime, если нужного нет.
   При нескольких активных TODO подхватит чужой baseline → в коммит попадёт
   чужое. Фикс: если конкретный не найден — warning + включить все untracked.

⬜ `.4` **[MED] git commit no reset on failure** — `awf/commit_gate.py:160-166`
   `git commit` без `check=True`; при rejection pre-commit hook'ом staged files
   остаются в index → следующий run подхватит чужой staged state.
   Фикс: `git reset` (unstage) на commit failure.

⬜ `.5` **[LOW] dashboard broad except** — `awf/api/dashboard.py:285`
   `except Exception: pass` после специфичных excepts → corrupt `pipeline_pid`
   даёт статус "running" для мёртвого pipeline. Фикс: return "dead" в broad except.

#### T1 — cleanup

⬜ `.6` **Dead test classes** — `tests/integration/test_pipeline_e2e.py:182-188,229-234`
   два пустых класса (`TestFullPipelineBlocked`, `TestFullPipelineSalvage`) с только
   docstring + `pass`. Покрыты e2e тестами. Удалить.

⬜ `.7` **assert True test** — `tests/unit/test_reconcile.py:67-71`
   `test_no_state_noop` делает `assert True`. Проверить контракт: inbox/outbox/
   state не изменены после `_reconcile()` без state file.

#### T3 — рефакторинг (после dogfood)

⬜ `.8` **Swallowed exceptions без logging** (5 мест): `model_check.py:103,116,142`,
   `opencode_config.py:148`, `dashboard.py:406`. Graceful degradation без `_log()` →
   пользователь видит "provider not found" вместо "config file unreadable".
   Добавить logging на каждый catch.

⬜ `.9` **model_check refactor** — `awf/api/model_check.py:14` `check_model_config`
   cognitive=86, 196 строк, 4 SRP. Извлечь `_load_opencode_config()`,
   `_load_recent_models()`, `_load_cli_models()` → ~60 строк чистой логики.
   (подтверждено 2 ролями: Чистильщик + Ревьюер)

### BD-35 · Per-role contribution tracking
**Status:** ждать real failure в dogfooding.

### DAUD-6 · plan_checkpoint.py → Jinja2
**Status:** отложить до scenario 2/3.

---

## Future scenarios

| Сценарий | Что | Сложность |
|---|---|---|
| 2 · Decision fork | Runtime ad-hoc forms | Low |
| 3 · Blockage recovery | Multi-step flow | Medium |
| 5 · Priority planning | Drag-and-drop UI | High |
| 6 · Onboarding wizard | Multi-form logic | High |

---

## Architecture notes

- `tools/awf.py` — разбить по зонам или схлопнуть через helper (см. AUD-12)
- `supervisor.py` — разделить `wait_for_supervisor_signal` до новых stage kind
- Два HTTP-сервера: one-shot core + long-lived plugin
- Event journal — `.agentic/state/journal.jsonl` для replay
- Model discovery consolidation — `awf` как единственный владелец знания об opencode-окружении
