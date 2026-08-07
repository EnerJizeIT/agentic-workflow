# BACKLOG

> История закрытых записей — в [BACKLOG-archive.md](BACKLOG-archive.md).

---

## Открытые задачи

### AUD-1 · [CRITICAL] verify.py: false-positive auto-DONE на untracked файлах

`detect_work_evidence:42` считает ЛЮБОЙ untracked файл «работой» без фильтра по baseline.
Worker не сделал ничего → pre-existing untracked файл → auto-DONE → pipeline едет дальше.
Тот же баг-класс, что в commit_gate (починен через `BASELINE-*.untracked` snapshot).
**Фикс:** фильтровать untracked через snapshot, как в `commit_gate._files_changed_since_baseline`.

### AUD-2 · [HIGH] Полный прогон тестов зависает (>25 мин)

1120 тестов по частям = 9 мин; одним прогоном — сталл на 40-52%.
Подозреваемые: (1) тесты без mock `subprocess.run` спавнят реальный `opencode models`;
(2) commit_gate poll-loop с таймаутом 1800с без pre-created APPROVE-файла.
**Фикс:** mock `opencode models` в autouse-фикстуре (быстрый empty fallback); прогнать `--durations=20`.

### AUD-3 · [MEDIUM] 5 мест читают pipeline.yaml в обход resolve_pipeline_file

`context.py:73,284,379,399` + `dashboard.py:398` хардкодят `.agentic/pipelines/default.yaml`.
Проект с `default_pipeline: custom` получит молча неверные `next_stage_role`, dashboard, `_compute_stage_kind`.
**Фикс:** везде использовать `resolve_pipeline_file` + `load_stages`.

### AUD-4 · [MEDIUM] _reconcile сортирует по mtime, list_active_todos — по номеру

`_reconcile:140`: `sorted(..., key=lambda p: p.stat().st_mtime)`.
Редактирование TODO-0005 после создания TODO-0007 → архивируется не тот.
**Фикс:** сортировать по номеру TODO, как в `list_active_todos`.

### AUD-5 · [MEDIUM] read_recent_models игнорирует XDG_DATA_HOME

`opencode_config.py:56` хардкодит `~/.local/share/opencode/opencode.db`,
а `model_check.py:74-78` уважает XDG_DATA_HOME → расхождение.
**Фикс:** использовать `awf.xdg` для обоих.

### AUD-6 · [LOW] Мёртвый код (3 пункта)

- `context.py:63` — `checkpoint_port if checkpoint_port is not None else None` (no-op)
- `context.py:93-94` — вложенный `if stages:` внутри `elif stages:` (избыточен)
- `plan_checkpoint.py:278` — `origin.startswith("file://")` (браузеры шлют `null`, не `file://`)

### AUD-7 · [LOW] Dashboard autoescape=False → XSS в handoff preview

`dashboard.py:40` рендерит с `autoescape=False`, `_read_handoffs` тащит LLM-контент в HTML.
`<script>` в handoff.md исполнится в браузере.
**Фикс:** `autoescape=True` для handoff-секции (или selective escaping).

### AUD-8 · [LOW] _background.py PID-liveness без защиты от PID reuse

`check_pipeline_running` делает `os.kill(pid, 0)` без проверки `/proc/<pid>/cmdline`,
в отличие от `pipeline.py:_is_pipeline_running`.
**Фикс:** унифицировать probe (вынести в общий helper).

### AUD-9 · [LOW] signal_watch.py:162 — сообщение врёт при timeout

«Subprocess did not produce signal» даже когда сигнал БЫЛ пойман, но процесс завис на выходе.
**Фикс:** развести два сообщения (signal_seen vs not_seen).

### AUD-10 · [LOW] CI threshold comment stale + docs counter drift

- `.github/workflows/test.yml`: комментарий «lowered from 80% to 75%» протух (код уже 80%)
- README «1118 tests» / фактически 1120; architecture.md «18 функций» / 22
**Фикс:** убрать ручные счётчики из docs или автоматизировать через CI badge.

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
