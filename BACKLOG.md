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

### AUD-12 · [T3] Рефакторинг

- **`_xdg_config_home` ×3 копии** → consolidate в `awf.xdg`
- **`open_form` scans everything** → lazy по шаблону
- **`awf.py` 850+ строк** → схлопнуть wrapper'ы через helper
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
