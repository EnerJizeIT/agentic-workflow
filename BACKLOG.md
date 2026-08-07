# BACKLOG

> История закрытых записей — в [BACKLOG-archive.md](BACKLOG-archive.md).

---

## Открытые задачи

### KA2-1 · [LOW] _background.py затирает лог при перезапуске

`start_in_background:77` — `open(log_file, "wb")` перезаписывает `awf-start.out` при каждом запуске.
**Фикс:** `"wb"` → `"ab"` (append). One-char fix.

### KA2-2 · [LOW] signal_watch.py — worker_log без context manager

`open(logs_dir / log_name, "w")` не обёрнут в `with`/try-finally.
Если `Popen` выбросит исключение → утечка file handle.
**Фикс:** try/finally или `with`.

### KA2-3 · [LOW] forms.py TTL — расхождение docs и кода

Docs (AGENTS.md, open_form) говорят "Default: no TTL".
Код применяет `ttl_seconds=86400` из config если не передано явно.
**Фикс:** согласовать — либо убрать default TTL из кода, либо исправить docs.

### KA2-4 · [LOW] cmd_init.py — отсутствует encoding="utf-8"

`cfg_path.open()` в двух местах (чтение/запись opencode.json).
На Windows/non-UTF8 локали может сломаться.
**Фикс:** добавить `encoding="utf-8"`.

### KA2-5 · [MEDIUM] read_signal_for_todo — фиксированный prefix order

`signals.py:96-109` — перебирает prefixes по порядку: DONE, BLOCKED, REVIEW-APPROVED, …
Если worker написал DONE, потом передумал и написал BLOCKED — DONE побеждает потому что первый в списке.
**Фикс:** выбирать signal с позднейшим mtime вместо первого совпадения.

### KA2-6 · [MEDIUM] --timeout не доходит до supervisor stages

`orchestrator.py` передаёт `agent_hard_timeout` в agent stages (KAUD-4),
но `run_supervisor_stage` / `wait_for_supervisor_signal` используют env-based timeout (`_safe_supervisor_timeout`).
CLI `--timeout` игнорируется для supervisor.
**Фикс:** пробросить `timeout` в `_run_supervisor_stage` → `wait_for_supervisor_signal`.

### KA2-7 · [MEDIUM] _env.py — E2BIG risk при большом opencode.json

`awf_subprocess_env` сериализует весь opencode.json в `OPENCODE_CONFIG_CONTENT` env var.
Linux env size limit ~128KB. Большой config → `subprocess.Popen` упадёт с E2BIG.
**Фикс:** писать merged config во временный файл, передавать путь через env var.

### KA2-8 · [LOW] Model cache — нет production-инвалидации

`_MODELS_CACHE` обновляется только по TTL (300s).
Юзер добавил провайдера → project-setup форма не видит его 5 минут.
**Фикс:** добавить `_invalidate_models_cache()` вызов при `awf_check_model_config` или при изменении mtime opencode.json.

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
