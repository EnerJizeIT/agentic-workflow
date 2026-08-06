# BACKLOG

> План развития. Основан на [Product Vision](vision/agent-ui-plugin.md) и [Architecture](vision/architecture.md).
> История закрытых записей — в [BACKLOG-archive.md](BACKLOG-archive.md).

**Текущее состояние:** awf v0.4.0 + agent-workflow-ui v0.1.0. 1115 тестов, CI green, 24 MCP tools. ruff clean.

**Активный эпик:** завершён. Все KAUD + DAUD + SELF findings закрыты (кроме deferred).

---

## 🐛 KAUD — Kimi Audit 2026-08-06

> 5 HIGH + 7 MEDIUM. Layer 1 (KAUD-1..6) — изолированные безопасные фиксы.

### KAUD-1 · `get_report()` игнорирует done/ архивы (HIGH) ✅ FIXED

**Where:** `awf/api/lifecycle.py:381-451`
`get_report()` не считает TODOs из `.agentic/done/`. После DF6-1 archive, `done_count` = 0.
**Fix:** reuse `_count_done_blocked(inbox, outbox, done_dir)`.

### KAUD-2 · `_build_pipeline_context()` хардкодит default.yaml (HIGH) ✅ FIXED

**Where:** `awf/supervisor.py:55`
Всегда читает `default.yaml`, игнорируя `default_pipeline` из config.yaml.
**Fix:** read pipeline name from config.

### KAUD-3 · CSRF origin validation — startswith bypass (HIGH) ✅ FIXED

**Where:** `http_endpoint.py:61-88` + `plan_checkpoint.py:253-269`
`http://127.0.0.1.evil.com` проходит `startswith("http://127.0.0.1")`.
**Fix:** `urlparse` + check hostname.

### KAUD-4 · CLI `--timeout` игнорируется (HIGH) ✅ FIXED

**Where:** `awf/api/pipeline.py` + `awf/orchestrator.py`
`args.timeout` не доходит до `_run_agent_stage()` / `_run_supervisor_stage()`.
**Fix:** pass timeout through the chain.

### KAUD-5 · Child opencode config over-privileged (HIGH) ✅ FIXED

**Where:** `awf/_env.py` — `awf_subprocess_env()`
Worker config заменяет пользовательский. Grants bash/write/webfetch globally.
**Fix:** merge with user's config (не заменять).

### KAUD-6 · `_reconcile()` пишет state неатомарно (MEDIUM) ✅ FIXED

**Where:** `awf/api/pipeline.py:88-153`
`write_text()` напрямую. Crash mid-write → повреждённый state.
**Fix:** use `atomic_write_text()`.

### KAUD-7 · `signal_watch` lexicographic tie-break (MEDIUM) ✅ FIXED

**Where:** `awf/signal_watch.py` — `watch_new_glob`
`sorted()[0]` выбирает лексикографически меньший (TODO-0002 вместо TODO-0010).
**Fix:** sort by numeric ID.

### KAUD-8 · current_todo не персистится в state (MEDIUM) ✅ FIXED

**Where:** `awf/orchestrator.py`
Локальная переменная. После краша — эвристическая реконструкция.
**Fix:** include `current_todo` in `write_state()`.

### KAUD-9 · Worker stdout не перенаправляется в лог (MEDIUM) ✅ FIXED

**Where:** `awf/agent_stage.py`
Worker наследует stdout → mixed с awf-start.out.
**Fix:** redirect to `.agentic/logs/<role>-<todo_id>.out`.

### KAUD-10 · `pytest-timeout` закомментирован (MEDIUM) ✅ FIXED

**Where:** `pyproject.toml`
Hanging test блокирует CI навсегда.
**Fix:** re-enable with 120s per-test timeout.

### KAUD-11 · `open_form` TTL docs vs code mismatch (MEDIUM) ✅ FIXED

**Where:** `agent_workflow_ui/tools/forms.py:206-209`
Code: 86400s default. Docs: "no TTL".
**Fix:** align.

### KAUD-12 · BACKLOG/CHANGELOG trim (LOW) ✅ DONE

---

## 🐛 DAUD — DeepSeek Audit 2026-08-06

> 2 bugs + 3 quality + 2 deferred.

### DAUD-1 · `{NNNN}` в _SNIPPET_PLAN — литерал в prompt (BUG) ✅ FIXED

**Where:** `awf/supervisor.py` — `_SNIPPET_PLAN`
`TODO-{NNNN}.md` остаётся литералом. Qwen может создать `TODO-{NNNN}.md`.
**Fix:** заменить на «TODO-NNNN (подставь следующий номер)».

### DAUD-2 · TOCTOU race в `_find_free_port()` (BUG) ✅ FIXED

**Where:** `awf/plan_checkpoint.py:246-250`
Порт может быть занят между check и bind.
**Fix:** retry 2-3 попытки, 500ms между ними.

### DAUD-3 · Dead code — `opencode_skills_dir()` (QUALITY) ✅ FIXED

**Where:** `awf/xdg.py:37-39`
Не вызывается нигде.
**Fix:** удалить.

### DAUD-4 · Двойной `except Exception` вокруг dashboard (QUALITY) ✅ FIXED

**Where:** `awf/orchestrator.py:369-373` + `dashboard.py:529-541`
Dashboard сам логирует. Внешний try/except — двойное логирование.
**Fix:** убрать внешний try/except.

### DAUD-5 · Signal file content validation (QUALITY)

**Where:** `awf/signals.py:78-113`
Только `st_size > 0`. Worker пишет «ok» → проходит verify.
**Fix:** check markdown heading. Warning only, не блокировать.

### DAUD-6 · `plan_checkpoint.py` → Jinja2 (DEFERRED)

2-3 часа, нет user impact сейчас. Отложить до scenario 2/3.

### DAUD-7 · `orchestrator.run_pipeline()` refactor (DEFERRED)

Совпадает с KAUD Layer 3. 1-2 дня. Низкий приоритет.

---

## 🟡 Open — крупные фичи

### BD-35 · Per-role contribution tracking (OPEN)

Каждая роль работает в той же директории. Free-rider / zone violation / audit opacity.
Возможные подходы: git branch per role / isolated workdir / оставить как есть.
**Status:** ждать real failure в dogfooding.

### AD-3 · Integration тесты на полный pipeline (OPEN)

Нет end-to-end теста: `awf start` → mock opencode → plan → agents → verify → commit.
**Решение:** `tests/integration/test_pipeline_e2e.py`. ~200 строк.

---

## 🔍 Self-identified (не найдено аудитами)

### SELF-1 · `wait_for_event` не детектит stage transitions (HIGH) ✅ FIXED

**Where:** `awf/api/wait_event.py` — `_check_for_event()`

Pipeline: analyst → architector → implementer. Каждый переход — новый stage.
Но `wait_for_event` возвращает `timeout` на каждом переходе (нет события для
"stage changed"). Supervisor видит "timeout" 10 раз пока 5 агентов работают.

**Fix:** в `_check_for_event()` добавить проверку: если `stage_name` изменился
с прошлого poll → вернуть `event_type="stage_changed"` с именем нового stage.
Нужен `prev_stage_name` параметр или сравнение с последним возвращённым state.

### SELF-2 · Нет `awf_kill` tool (MEDIUM) ✅ FIXED

**Where:** MCP tools (`agent_workflow_ui/tools/awf.py`)

Supervisor не может cleanly остановить pipeline. Использует bash `pkill` —
нарушает role boundaries, оставляет zombie процессы.

**Fix:** `awf_kill(project_dir)` — читает `pipeline_pid` из state,
отправляет SIGTERM, ждёт 5 сек, SIGKILL если не умер, чистит state.

### SELF-3 · Dashboard data-epoch update (LOW) ✅ FIXED

**Where:** `awf/templates/dashboard.html.j2` — smart refresh JS

Когда pipeline переходит на новый stage, `started_at` в state меняется.
Smart refresh патчит innerHTML секций, но не атрибут `data-epoch` на
`#elapsed-timer`. Timer продолжает считать от старого start time.

**Fix:** в smart refresh JS, после DOM patch, обновить `data-epoch`
из свежего HTML:
```javascript
const freshEpoch = doc.querySelector('#elapsed-timer')?.dataset.epoch;
if (freshEpoch) document.querySelector('#elapsed-timer').dataset.epoch = freshEpoch;
```

---

## 🔮 Future scenarios

| Сценарий | Что добавляет | Сложность |
|---|---|---|
| **2 · Decision fork** | Runtime ad-hoc forms | Low |
| **3 · Blockage recovery** | Multi-step problem→solution flow | Medium |
| **5 · Priority planning** | Drag-and-drop UI | High |
| **6 · Onboarding wizard** | Multi-form conditional logic | High |

---

## 📐 Architecture notes (hold in mind)

- **tools/awf.py (796 строк)** — разбить по зонам до scenario 2
- **supervisor.py (809 строк)** — разделить `wait_for_supervisor_signal` до новых stage kind
- **Два HTTP-сервера** — one-shot в core, long-lived в plugin. Не плодить третий
- **Event journal** — `.agentic/state/journal.jsonl` для replay/determinism (только если проект растёт)
