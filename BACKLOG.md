# BACKLOG

> История закрытых записей — в [BACKLOG-archive.md](BACKLOG-archive.md).

---

## Открытые задачи

### N4 · [LOW] config.yaml.bak — origin не исследован

F2 (*.bak gitignore) закрыл cosmetic. Но источник `.bak` файлов не найден:
`awf/api/setup.py:230` (backup config при записи), `setup.py:140` (default.yaml.bak),
`opencode_agents.py:120` (.bak-{ts}). Нужно определить кто и зачем создаёт backup
при каждом запуске стадии — если это worker's edit tool, шум будет в любом проекте.

**Status:** low priority — gitignore маскирует. Investigate when touching setup.py.

### R2 · [LOW] Goal elicitation — цель перед load_context

**Проблема:** Сейчас `awf_load_supervisor_context` даёт всё (vision, plan, status,
git-diff) — это перегружает. Supervisor изучает проект «вообще», а не под
конкретную задачу. В dogfood это привело к расфокусу.

**Что сделать:**
1. Supervisor спрашивает цель ПЕРЕД `load_supervisor_context` (короткий диалог
   1–3 вопроса).
2. Под цель — фильтрованно: какие артефакты читать, какие роли рекомендовать.
3. Цель сохраняется в `.agentic/state/goal.txt` или `BRIEF-NNNN.md`.
4. Связка «цель → recommended roles»: анализ = system-analyst + qa-review;
   разработка = developer + qa; ревью = project-auditor + qa-review.

**Файлы:** `templates/roles/supervisor.md` (новый шаг перед Step 1),
`awf/api/context.py:load_supervisor_context` (опциональный параметр `goal`).

### R3 · [LOW] Форма с рекомендацией ролей (prefill)

**Проблема:** Сейчас `awf_open_project_setup_form` auto-populates глобальными
skills/roles, но без рекомендаций под цель. Пользователь выбирает роли «вслепую».
N1 (user error в форме) — следствие этого.

**Что сделать:**
1. Supervisor рекомендует роли исходя из цели (R2) + характера итерации.
2. Форма project-setup **предзаполняется** рекомендацией: preselected roles,
   suggested models, recommended pipeline template.
3. Пользователь подтверждает/правит.

**Файлы:** `awf.py:awf_open_project_setup_form`, `awf/api/setup.py`,
`agent_workflow_ui/.../render/project-setup.html.j2`.
**Зависимость:** R2.

### R4 · [LOW] Skills normalization (3-part checklist + tool)

**Проблема:** После формы roles могут перекрываться, быть не адаптированы под
характер итерации, и не иметь формализованных handoff-контрактов.

**Что сделать:** 3-частный checklist после формы:
1. **Адаптация под итерацию.** Каждая роль: соответствует ли skill характеру запуска?
2. **Разруливание перекрытий.** `awf_analyze_roles` — обязательный шаг после формы.
3. **Handoff-контракты.** Каждая роль знает: что получает, что передаёт.

Новый tool `awf_normalize_skills(iteration_type, pipeline)`.

**Файлы:** `awf/cmd_analyze_roles.py`, `awf/api/roles.py`, `templates/roles/`,
новый tool в `awf.py`.

### R7 · [MEDIUM] Phase-prompts (supervisor state machine)

**Корневая проблема:** supervisor.md — один системный промт на 500+ строк.
Supervisor получает весь flow сразу и путается (S1: гадал вместо прямого вопроса;
S2: deep-debug вместо простого теста; смешение слоёв TODO/role.md — до R5).

**Решение:** Modular phase-prompts — supervisor = state machine.
На каждом шаге flow supervisor получает короткий промт (5-20 строк):
`init / goal / form / normalize / todo / run / verify`.

Текущий шаг — поле `phase` в `.agentic/state/current.yaml`.
Новый tool `awf_supervisor_step` — возвращает промт для текущего шага.

**Почему важно:** Меньше промт → меньше путаницы → меньше LLM-ошибок.
S1/S2 (prompt injections, уже добавлены в Quick Reference) растворятся в R7 —
каждый phase-prompt короче и сфокусированнее.

**Файлы:** `templates/roles/supervisor.md` → phase-секции, `awf/supervisor.py:build_prompt()`,
`awf/pipeline_state.py` (поле `phase`), новый tool `awf_supervisor_step`.

---

### AUD-12 · [T3] Рефакторинг

- **`_xdg_config_home` ×3 копии** → consolidate в `awf.xdg`
- **`open_form` scans everything** → lazy по шаблону
- **`awf.py` 824+ строк** → схлопнуть wrapper'ы через helper
- **Circular import orchestrator↔pipeline_engine** → третий модуль
- **`_extract_stage_info_regex` + `_detect_supervisor_signal`** → депрекация fallback'ов

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
