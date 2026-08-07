# BACKLOG

> История закрытых записей — в [BACKLOG-archive.md](BACKLOG-archive.md).

---

## Открытые задачи

### AUD-11 · [DECISION] Packaging: editable-only

`lifecycle.py` ищет supervisor.md через `__file__/../../..` (вне пакета);
`dashboard.html.j2`, `role_zones.yaml` не в package-data; `jinja2` не в deps awf.
**Решение:** editable-only (README уже предписывает `pip install -e`). Документировать явно.

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
