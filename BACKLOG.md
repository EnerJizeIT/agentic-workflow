# BACKLOG

> История закрытых записей — в [BACKLOG-archive.md](BACKLOG-archive.md).

---

## Открытые задачи

### R5 · [HIGH] Два слоя TODO: Increment Brief + Stage-1 TODO

**Источник:** dogfood jira-epic-presenter, корневая ошибка сессии.

Один TODO смешивает два слоя: что показать пользователю (approve) и что
скормить агенту (pipeline start). Разделить:
- **BRIEF-NNNN.md** — цель инкремента + критерии успеха + что НЕ делаем. ~10-20 строк. Approve пользователем через форму.
- **TODO-NNNN.md** — задача 1-й стадии + handoff-контракт. Для агента.

Verify сверяет с Brief (contract), не с полным выводом pipeline.
**Файлы:** `awf/todos.py`, `awf/api/dispatch.py`, `awf/supervisor.py:build_prompt`,
`awf/api/planning.py` (переиспользовать pattern increment-planning-form).

### R6 · [HIGH] Sleep mode (supervisor idle после pipeline start)

Supervisor открывает dashboard → засыпает. Пользователь пишет при событиях
(salvage/blocked/checkpoint/verify) или по завершении. Auto-poll убирается.
**Файлы:** `awf.py:awf_wait_for_event`, `awf/api/wait_event.py`,
`templates/roles/supervisor.md`, `awf/api/dashboard.py` (wake-индикатор).

### R1 · [MEDIUM] Чистый init (runtime clean, config saved)

`awf init` должен чистить runtime (inbox/outbox/handoff/done/state/logs),
сохранять config (config.yaml, roles/*.md, pipelines/*.yaml, phases/).
Средний путь между `force=false` (ничего) и `force=true` (всё).
`awf init --hard` для полного сброса.
**Файлы:** `awf/cmd_init.py`, `awf/api/setup.py`, `awf/cmd_reset.py`, `awf/paths.py`.

### R8 · [LOW] Verify по Brief (contract)

Следует из R5. Verify сверяет: достигнута ли цель из Brief? Brief = contract.
**Файлы:** `awf/verify.py`, `awf/supervisor.py:build_prompt` (verify kind).

### R2 · [LOW] Goal elicitation step

Supervisor спрашивает цель ПЕРЕД load_context. Изучает проект под цель,
не вообще. Короткий диалог (1-3 вопроса) → рекомендация ролей.
**Файлы:** `templates/roles/supervisor.md`, `awf/api/setup.py`.

### R3 · [LOW] Форма с рекомендацией ролей (prefill)

Project-setup форма предзаполняется рекомендациями supervisor'а (на основе
цели + характера итерации). Сейчас форма пустая — пользователь выбирает вслепую.
**Файлы:** `awf.py:awf_open_project_setup_form`, `awf/api/setup.py`.

### R4 · [LOW] Skills normalization tool

3-частный checklist после формы: (а) адаптация под итерацию, (б) перекрытия
зон (уже есть `awf_analyze_roles`), (в) handoff-контракты.
Новый tool `awf_normalize_skills`.
**Файлы:** `awf/cmd_analyze_roles.py`, `awf/api/roles.py`, `templates/roles/`.

### R7 · [LOW] Phase-prompts (supervisor state machine)

`supervisor.md` (510 строк) → разбить на phase-секции (init/goal/form/
normalize/todo/run/verify). Tool `awf_supervisor_step` возвращает промт
текущего шага по state.
**Файлы:** `templates/roles/supervisor.md`, `awf/supervisor.py:build_prompt`,
`awf/pipeline_state.py` (добавить поле `phase`).

### AUD-12 · [T3] Рефакторинг

- **`_xdg_config_home` ×3 копии** → consolidate в `awf.xdg` (комментарий "avoid circular import" в state.py неверен)
- **`open_form` scans everything** → `scan_global_skills/roles` лениться по шаблону (нужны только project-setup)
- **`awf.py` 824 строк** → схлопнуть 19 wrapper'ов через `_wrap(api.fn)` helper (~824→~250)
- **Circular import orchestrator↔pipeline_engine** → поднять `_handle_*` в третий модуль (до T4)
- **`_extract_stage_info_regex` (cognitive 90) + `_detect_supervisor_signal`** → regex/mtime fallback'ы к structured state. State пишется с T4.1 — объявить дату удаления и убрать ~90 строк хрупкого кода

### BD-35 · Per-role contribution tracking

Каждая роль работает в той же директории. Free-rider / zone violation / audit opacity.
**Status:** ждать real failure в dogfooding.

### DAUD-6 · plan_checkpoint.py → Jinja2

~200 строк HTML через конкатенацию. Перевести на Jinja2 template.
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

- `tools/awf.py` — разбить по зонам до scenario 2 (или схлопнуть через helper — см. AUD-12)
- `supervisor.py` — разделить `wait_for_supervisor_signal` до новых stage kind
- Два HTTP-сервера: one-shot core + long-lived plugin
- Event journal — `.agentic/state/journal.jsonl` для replay (если проект растёт)
- Model discovery consolidation — `awf` как единственный владелец знания об opencode-окружении
