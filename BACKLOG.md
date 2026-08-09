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
✅ `.4` — **Resolved by AUD-2026-08-09**: pipeline_engine импортирует напрямую
   из source-модулей. orchestrator больше не re-export hub. 15 re-exports удалено.

### AUD-2026-08-09 · Roundtable audit (9 принятых из 11, все закрыты)

**Source:** roundtable audit (Архитектор + Ревьюер + Чистильщик + Безопасник).
**Rejected:** mock-heavy orchestrator tests (catches real regressions), CSRF bypass
(local-only tool, token = overkill).

#### T2 — точечные фиксы

✅ `.1` **[HIGH] jinja2 undeclared dep** — добавлен в root pyproject.toml deps.
✅ `.2` **[MED] rollback path traversal** — regex `^TODO-\d{4,}$` в rollback().
✅ `.3` **[MED] commit_gate wrong baseline fallback** — warning + all untracked.
✅ `.4` **[MED] git commit no reset on failure** — git reset на commit failure.
✅ `.5` **[LOW] dashboard broad except** — return "dead" вместо false "running".

#### T1 — cleanup

✅ `.6` **Dead test classes** — удалены.
✅ `.7` **assert True test** — переписан с реальными assertions.

#### T3 — рефакторинг

✅ `.8` **Swallowed exceptions** — logging добавлен в 5 местах.
✅ `.9` **model_check refactor** — извлечены _load_opencode_config,
   _load_cli_models, _load_recent_models.
✅ `.10` **signal_watch refactor** — извлечены _build_pre_snapshot,
   _detect_new_signal, _sort_key_by_numeric_id.
✅ `.11` **supervisor refactor** — извлечён _prepare_supervisor_stage (4 kind branches).
✅ `.12` **orchestrator import proxy** — pipeline_engine импортирует из source.
   15 re-exports удалены. Per-file F401 ignore удалён.
✅ `.13` **test boilerplate** — _init_proj_dirs helper, убрано 5x дублирование.
ℹ️ `.14` **plugin coupled to awf internals** — vision не содержит "agnostic" claims
   (обновлён ранее). Coupling acknowledged, не блокирует.
ℹ️ `.15` **pytest-timeout** — pytest-timeout установлен, warning исчез.

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

- `tools/awf.py` (~940 строк) — разбить по зонам (pipeline/state/forms) или схлопнуть через helper
- `plan_checkpoint.py` (201 строка, 21% coverage) — добавить покрытие при dogfood
- `supervisor.py` — разделить `wait_for_supervisor_signal` до новых stage kind
- Два HTTP-сервера: one-shot core + long-lived plugin
- Event journal — `.agentic/state/journal.jsonl` для replay
- Model discovery consolidation — `awf` как единственный владелец знания об opencode-окружении
