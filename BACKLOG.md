# BACKLOG

> План развития. Основан на [Product Vision](vision/agent-ui-plugin.md) и [Architecture](vision/architecture.md). Каждый эпик декомпозируем в awf TODO при начале работы.

**Текущее состояние:** awf v0.4.0 + agent-workflow-ui v0.1.0 стабильны. 1096 тестов, CI green, 23 MCP tools. DF5-1..12 + DF6-1..8 + QA-2026-08-05 + salvage signal fix + ID collision fix закрыты. Stage-specific snippet injection. asyncio.to_thread для MCP event loop. TODO lifecycle (archive + reconcile). Pipeline context injection (BD-10). ruff clean.

**Активный эпик:** KAUD + DAUD — два аудита (Kimi + DeepSeek) 2026-08-06.

История фиксов — в `git log --oneline`.

---

## 🟢 Active · MCP-MIGRATION — awf как pure MCP toolkit под opencode

**Status:** DONE. **Priority:** CRITICAL — architectural pivot.

**Solution concept:** Awf больше НЕ позиционируется как CLI для bash-юзеров.
Все команды awf доступны opencode-агенту как MCP tools в plugin'е
`agent-workflow-ui`. Plugin импортирует `awf` как Python package (никакого
subprocess). CLI `awf` остаётся как thin dev/debug wrapper, не primary path.

**Принципы:**
1. Один plugin `agent-workflow-ui` (без переименования — меньше миграции).
2. Plugin depends on `agentic-workflow` package (Python import).
3. `awf/api/` — public API package для всех callers (CLI + MCP tools).
4. Long-running ops (start/continue) — background + poll status pattern.
5. Глобальный AGENTS.md документирует все MCP tools.

**Фазы (последовательные):**

### MCP-1 · Refactor awf → `api/` package (foundation)

**Status:** DONE. **Priority:** CRITICAL — блокирует MCP-3.

Перенести бизнес-логику из `cmd_*.py` в `awf/api/` package с типизированными
функциями. cmd_*.py — тонкие CLI обёртки, вызывающие `api.*()`.

- [x] Создать `awf/api/` package с public функциями для каждой команды:
      - `init_project(project_dir) -> InitResult`
      - `get_status(project_dir) -> StatusResult`
      - `start_pipeline(project_dir, ...) -> StartResult`
      - `continue_pipeline(project_dir, ...) -> StartResult`
      - `create_baseline(project_dir, todo_id) -> BaselineResult`
      - `rollback(project_dir, todo_id, ...) -> RollbackResult`
      - `approve_commit(project_dir, todo_id) -> ApproveResult`
      - `get_report(project_dir) -> ReportResult`
      - `reset_runtime(project_dir, ...) -> ResetResult`
      - `add_role(project_dir, name, ...) -> RoleResult`
      - `analyze_roles(project_dir, ...) -> AnalyzeRolesResult`
- [x] Типизированные dataclass'ы для всех Result'ов (as_dict() для MCP).
- [x] cmd_*.py — тонкие обёртки (argparse → call api.*() → print human-readable).
- [x] Backward compat preserved: e2e тесты проходят (857).
- [x] Покрытие api/ package unit-тестами (79 тестов в tests/unit/test_api.py).
- [x] Helpers: detect_stack(), derive_project_name() (MCP-6 stack-detect ready).
- [x] Audit cleanup: god module split на 10 submodules (largest 493 lines).

### MCP-2 · Plugin dependencies

**Status:** DONE. **Priority:** CRITICAL.

- [x] `agent_workflow_ui/pyproject.toml`: добавить `agentic-workflow` в dependencies.
- [x] Проверить что `from awf import api` работает из plugin'а.
- [x] Smoke test: plugin может вызвать `awf.api.get_status(project_dir)`.
### MCP-3 · MCP tools implementation

**Status:** DONE. **Priority:** CRITICAL.

Реализовать 11 MCP tools в plugin'е, каждый вызывает `awf.api.*()`:

- [x] `awf_init` — детерминированный (stack-detect + name-from-dir). Возвращает supervisor.md + vision excerpt + plan.md как content.
- [x] `awf_status` — current pipeline / task state.
- [x] `awf_start` — background start, returns run_id (см. MCP-4).
- [x] `awf_continue` — resume background.
- [x] `awf_baseline` — create snapshot.
- [x] `awf_rollback` — rollback to baseline.
- [x] `awf_report` — summary report.
- [x] `awf_approve` — approve auto-commit.
- [x] `awf_reset` — clear runtime data.
- [x] `awf_add_role` — generate role template.
- [x] `awf_analyze_roles` — role conflict analysis.

Tools registered в `server.py` через `mcp.add_tool(...)`.

### MCP-4 · Long-running design (start/continue)

**Status:** DONE. **Priority:** CRITICAL.

Контракт для background operations:

- [x] `awf_start` запускает pipeline (subprocess, как `--background`).
      Возвращает немедленно: `{run_id, logs_path, expected_stages}`.
      Пишет `.agentic/logs/awf-start.pid` (atomic) для status polling.
- [x] `awf_status` поллит: возвращает текущую стадию, прогресс, готовые
      checkpoints (для `open_form`). Новые поля: `pipeline_running`,
      `pipeline_pid`, `log_tail` (последние 20 строк).
- [x] Coordination с `open_form`: когда pipeline ждёт checkpoint — status
      показывает `checkpoint_pending`, agent открывает форму отдельно
      (через signal files в inbox/outbox).
- [x] Lifecycle: running → checkpoint_pending → resumed → done | failed.
      Stale PID files (процесс умер) автоматически очищаются.
- [x] Tests: 6 новых (TestPipelineRunningDetection) покрывают все ветки.

### MCP-5 · Глобальный AGENTS.md

**Status:** DONE. **Priority:** HIGH.

- [x] Добавлен блок в `~/.config/opencode/AGENTS.md` с описанием всех 16 MCP
      tools (11 awf + 5 UI).
- [x] Workflow recipes:
      - "Новый проект" → `awf_init` → `open_form(project-setup)` → `awf_start`.
      - "Продолжить работу" → `awf_status` → если running, поллить.
      - "Откатить" → `awf_rollback`.
- [x] Метаправило: "видишь `.agentic/` → используй MCP tools, не bash".

### MCP-6 · CLI simplification

**Status:** DONE. **Priority:** MEDIUM.

- [x] Убрать интерактивность из `awf init` (стек-detect + name-from-dir).
      Добавлен `--non-interactive` флаг. Интерактивный путь сохранён для
      human CLI users (e2e compat).
- [x] CLI остаётся как thin dev/debug wrapper над `api/` package.
- [x] `--project-dir` flag добавлен к init subcommand.

### MCP-7 · Tests & docs

**Status:** DONE. **Priority:** HIGH.

- [x] MCP tools покрыты тестами (30 в `tests/agent_workflow_ui/test_awf_tools.py`).
- [x] api/ package покрыт unit-тестами (79 в `tests/unit/test_api.py`).
- [x] Integration: plugin tool → api → real workflow (TestBackgroundStart, и др.).
- [x] README обновить с новой архитектурой (structure tree, tools count, test count).
- [x] CHANGELOG.md — добавлена MCP-MIGRATION секция.
- [x] Audit cleanup: analyze_roles pure core, two-step orphans, god module split.

---

## 🟡 Open

### AUDIT-2026-08-03 — Незакрытые находки external audit

**Source:** `/home/pklochkov/Desktop/awf-audit-report.md` (audit at HEAD `b54bd4d`).
Закрыто в этой сессии: T1.1-T1.4, T2.1-T2.3, T2.5 (8 находок, коммиты `94328f5`, `91fa1f2`).

Ниже — что осталось. Приоритеты мои, пересматриваются по мере роста pain.

#### T2 — точечные фиксы (one-liners, низкий риск)

**[T2.6] `awf/opencode_agents.py:55-96` `apply()` без atomic** — ✅ CLOSED
> Crash во время write → сломанный `opencode.json`. Функция `atomic_write_text`
> доступна в проекте. Trivial fix — обернуть write.
> **Closed by QA review 2026-08-03:** `apply()` switched to `atomic_write_text`.
> Crash-test `test_apply_atomic_no_corrupt_on_write` added. Also fixed
> `save_custom_role()` non-atomic write + `forms.py` temp HTML write.
> **Bonus:** `read_recent_models()` DB connection leak fixed (try/finally).

**[T2.7] `awf/api/pipeline.py:261-270, 311-320` `except Exception` слишком широкое** — FALSE POSITIVE
> Audit предполагал SystemExit/KeyboardInterrupt catch. QA verified:
> Python's SystemExit/KeyboardInterrupt inherit from BaseException, NOT
> Exception — current code does NOT catch them. No behavioral bug.
> Cosmetic improvement only (`except AwfApiError` + re-raise Exception
> would be more expressive). Not fixing — zero risk.

**[T2.8] `awf/opencode_agents.py:10-52` stringly-typed protocol `propose()`** — ✅ CLOSED
> Refactored to `ProposalKind` Enum + `Proposal` dataclass. __str__ back-compat.

#### T1 — safe deletions (отложено, требуют инфраструктурных решений)

**[T1.5] `awf/orchestrator.py:__all__` re-exports** — ✅ CLOSED
> Решение: ruff `per-file-ignores` в pyproject.toml (`"awf/orchestrator.py" = ["F401"]`).
> `__all__` удалён — re-exports для white-box tests защищены ruff config.

**[T1.6] _reset_orphans** — ✅ CLOSED
> _reset_orphans deleted. reset_runtime(orphans=True) uses list_orphans + remove_orphans.

#### T3 — рефакторинг (дни)

**[T3.1] Дубликат HTTP infrastructure**
> `awf/plan_checkpoint.py` (one-shot HTTP server) + `agent_workflow_ui/.../http_endpoint.py`
> (long-lived) — у каждого свой `_find_free_port`, свой `BaseHTTPRequestHandler`,
> свой ack-HTML. ~150 строк дубликата. Также **3 разные atomic-write реализации**:
> `awf._atomic.atomic_write_text`, `state._atomic_write_text` (уже объединён),
> `http_endpoint._atomic_write_yaml`.
> Решение: вынести `_find_free_port` в общий utils, при необходимости —
> общий `http_server` helper.
> Status: **WON'T FIX** — design boundary обоснован (awf-core не зависит от plugin).
> Объединение нарушит separation. Дубликат _find_free_port — минорный.

**[T3.2] `forms.py:open_form` 158 строк, SRP violation**
> Делает: registry lookup + project_dir validation + role scanning +
> model collection + template rendering + browser launch + record creation
> + cleanup_temp_files в одной функции.
> Status: **DEFERRED INDEFINITELY** — работает, покрыт тестами, change frequency низкая.
> Рефактор = риск regression без user-visible benefit.

**[T3.3] `orchestrator.py:run_pipeline` cyclomatic complexity 107**
> Уже разбит на `_handle_*` функции, но supervisor-stage block (375-436) и
> agent-stage block (438-507) стоит извлечь в `_handle_supervisor_stage()` /
> `_handle_agent_stage()`. Status: **DEFERRED INDEFINITELY** — core flow стабилен. Рефактор = высокий regression risk.

**[T3.4] _ZONES extraction** — ✅ CLOSED
> Extracted to awf/data/role_zones.yaml. _load_role_zones() reads at runtime.

**[T3.5] `opencode_config.py` hotspots**
> `scan_global_skills` (cyc=49), `scan_global_roles` (26), `read_available_models` (18).
> Топ hotspot'ы plugin'а. Чтение opencode.json/skills перемешано с нормализацией.
> Status: **DEFERRED INDEFINITELY** — работает, тесты покрывают. 0 user-visible benefit.

#### T4 — архитектурные изменения (недели, design discussion)

**[T4.1] Pipeline state persistence** — ✅ CLOSED
> Implemented `awf/pipeline_state.py`: write_state() / read_state() / clear_state().
> Orchestrator writes structured `.agentic/state/current.yaml` after each
> stage transition. plan_checkpoint writes checkpoint state. `_extract_stage_info`
> reads state file FIRST, regex as backward-compat fallback. 17 tests added.

#### Test smells (не блокеры, можно поправить opportunistic)

**[Test-1] `tests/unit/test_orchestrator_handlers.py:59,78,94`** — FALSE POSITIVE
> Audit: monkey-patch `_run_supervisor_stage` → `lambda *a, **kw: None`.
> QA verified: handlers `_handle_escalate`/`_handle_rollback` do NOT use
> the return value of `_run_supervisor_stage()` (orchestrator.py lines 197,
> 228 — call without assignment). `None` return is semantically correct.
> No fix needed.

---

### BD-35 · Pipeline stages don't produce verifiable contribution — branch chain + diff-monitoring

**Status:** OPEN. **Priority:** HIGH — fundamental to "pipeline as conveyor".

**Problem.** Currently each agent stage runs in the same project directory
and shares the filesystem with previous stages. Observed failure modes:
1. **Free-rider:** agent N sees that work is already done → writes
   `DONE-{todo_id}.ready` without adding anything. Awf cannot tell.
2. **Zone violation:** agent N does the entire task (out of scope for its
   role). Subsequent agents have nothing to add.
3. **Audit opacity:** final commit contains mixed contributions —
   supervisor cannot see which role produced which file.

Without infrastructure to track per-role contribution, the pipeline
collapses to "1 effective agent + N rubber-stamps".

**Partial mitigations already in place:**
- BD-15 handoff chain — каждая роль видит handoffs предыдущих ролей
- BD-29 aggregate verify context — supervisor видит все handoffs через `--file`
- A1 baseline diff isolation — коммиты содержат только diff vs baseline

**Что НЕ реализовано:**
- Реальный branch chain (каждая роль в своей git-ветке)
- Per-file contribution tracking
- Diff-monitoring — кто какие строки добавил

**Возможные подходы (обсудить когда в dogfooding проявится real failure):**
- **A) Git branch per role** — каждая роль работает в feature branch от baseline. После DONE — merge в main через supervisor verify. Free-rider детектится через empty diff.
- **B) Isolated workdir per role** — каждая роль пишет в `.agentic/work/<role>/` изолированно. Supervisor verify мержит `work/*` в проект. Без git branch оверхеда, но сложнее.
- **C) Оставить как есть** — текущие частичные решения достаточны для pet-проекта.

---

## 🔮 Future scenarios (после MVP)

В порядке приоритета из [Vision §6](vision/agent-ui-plugin.md#6-пользовательские-сценарии). Каждый — отдельный epic.

| Сценарий | Что добавляет | Сложность |
|---|---|---|
| **2 · Decision fork** | Runtime ad-hoc forms (в любом месте pipeline). Шаблон `decision-tree.html.j2`. | Low |
| **3 · Blockage recovery** | Шаблон `blockage-recovery.html.j2`. Multi-step flow (проблема → варианты → выбор → комментарий). | Medium |
| **4 · Long-running monitoring** | ✅ DONE — см. DASH ниже. | Medium |
| **5 · Priority planning** | Drag-and-drop UI, новый тип template `priority-matrix.html.j2`. | High |
| **6 · Onboarding wizard** | Multi-form state, conditional logic между формами. | High |

---

## ✅ Done · DASH — Pipeline dashboard + supervisor wake-up

**Status:** DONE. **Priority:** HIGH.

**Цель:** Снять supervisor overhead (polling) + дать пользователю живой
визуальный опыт pipeline execution.

**Сейчас:** supervisor делает `awf_start` → blocks MCP tool call → спит →
`sleep(30) + awf_status` циклы → тратит токены на polling.

**Цель:** `awf_start` returns immediately → dashboard открывается в браузере
user'а → user видит live прогресс → supervisor автоматически пробуждается
только когда нужен (verify stage, BLOCKED, checkpoint pending).

### Фаза 1 · Прототип dashboard (design-first)

**Status:** DONE (`d8e306c`). Прототип одобрен пользователем.

Standalone HTML prototype с mock data — не integrated с awf. Цель: собрать
визуальный язык, iterate по дизайну перед кодингом integration.

**UI layers (progressive disclosure):**

1. **Header** (always visible): project name + todo_id + elapsed time + status badge (running / checkpoint / done)
2. **Pipeline flow** (always visible): stages как visual chain `●━━━●━━━●━━━○━━━○` с pulsing dot на current stage, ✓ на completed, · на pending
3. **Expandable cards** (по клику):
   - **Handoffs** — что создала каждая роль (имя файла + preview первых строк)
   - **Events stream** — terminal-style лог агентов (читаем из `.agentic/logs/awf-start.out` tail)
   - **Task progress** — если есть PROGRESS-TODO-NNNN.md, показать task checklist
4. **Checkpoint banner** (условный): если `checkpoint_pending=True` → prominent top banner с form_url ссылкой + кнопкой "Open approve form"
5. **Footer**: status + "Return to chat" message когда done

**Animations (CSS, не GIFs):**
- Pulsing dot на current stage (CSS `@keyframes pulse`)
- Spinner на current stage card (`border-spin` animation)
- Slide-in для новых events в stream
- Glow effect на completed stages
- Checkmark draw animation на DONE

**Theme:** dark, matches project-setup.html.j2 (CSS variables --bg, --accent, --accent-green, --danger).

**Mock data для прототипа:**
```yaml
project: "Jira Epic Presenter"
todo: "TODO-0001"
elapsed: "4m 32s"
status: "running"
stages:
  - {name: plan, role: supervisor, status: done}
  - {name: system-analyst, role: agent-system-analyst, status: done}
  - {name: architect, role: agent-architect, status: done}
  - {name: implement, role: agent-implementer, status: current}
  - {name: qa-review, role: agent-qa-review, status: pending}
  - {name: verify, role: supervisor, status: pending}
handoffs:
  - {role: system-analyst, file: "requirements/mvp.md", preview: "# MVP Requirements\n## R1: Storyboard generation..."}
  - {role: architect, file: "2. ARCHITECTURE.md", preview: "# Architecture\n## Components..."}
events:
  - {ts: "15:30:01", msg: "system-analyst started (PID 632378)"}
  - {ts: "15:32:15", msg: "system-analyst DONE: requirements/mvp.md (18 requirements)"}
  - {ts: "15:32:16", msg: "architect started"}
  - {ts: "15:33:45", msg: "architect DONE: 2. ARCHITECTURE.md (7 components)"}
  - {ts: "15:33:46", msg: "implement started (current)"}
```

**Deliverable:** `/tmp/opencode/dashboard-prototype.html` — standalone file, открывается в браузере, user iterate по дизайну.

### Фаза 2 · Integrate с awf

**Status:** DONE (`37e5870`).

- `agent_workflow_ui/.../default_templates/pipeline-dashboard.html.j2` — Jinja2 template на основе прототипа
- `awf/api/dashboard.py` — `generate_dashboard(project_dir)` → read state (T4.1) + pipeline.yaml + handoffs + log tail → render HTML
- Orchestrator hook — после `write_state()` → regenerate dashboard HTML в `.agentic/dashboards/current.html`
- MCP tool `awf_open_pipeline_dashboard(project_dir)` — supervisor открывает дашборд для user (после вопроса "открыть дашборд?")
- Auto-refresh: `<meta http-equiv="refresh" content="5">` (5 секунд)

**Supervisor flow:**
```
1. awf_start(background=True) → pipeline запущен
2. Supervisor спрашивает user: "Открыть дашборд прогресса в браузере?"
3. Если да → awf_open_pipeline_dashboard(project_dir) → browser open
4. Supervisor: НЕ поллит, ждёт wake-up (Фаза 3)
5. User видит live прогресс в браузере
6. Когда pipeline done → dashboard показывает "DONE — возвращайся в chat"
```

### Фаза 3 · Supervisor wake-up (убрать polling совсем)

**Status:** DONE (`0d9d99c`). Реализован `awf_wait_for_event` — single
blocking call заменяет sleep+status polling. Supervisor делает ONE tool
call вместо N циклов. Token savings: 1 response vs N.

**Цель:** supervisor вообще не делает `sleep + awf_status` циклы. Pipeline
автоматически «будит» supervisor когда:
- Verify stage reached → supervisor должен verify
- BLOCKED signal → supervisor должен решить
- Checkpoint pending → supervisor должен сообщить user (если дашборд не открыт)

**Варианты реализации:**
- **A) MCP notifications** — plugin отправляет MCP notification когда state меняется. Agent видит notification → просыпается. Требует FastMCP notifications support.
- **B) Signal file + short poll** — `.agentic/state/supervisor_wake.ready` создаётся когда verify/BLOCKED. Supervisor проверяет раз в 60 сек (не 30). Дешевле текущего.
- **C) Foreground split** — `awf_start` blocks до verify stage, потом возвращает "verify reached" → supervisor делает verify → `awf_continue`. Pipeline разбит на фазы.

**Решение:** обсудить после Фазы 2 — когда видно реальный dashboard UX, проще выбрать wake-up механизм.

---

## 🔮 Future epic · `awf-mcp` (separate MCP server)

[Architecture §4.3](vision/architecture.md) — отдельный MCP server для awf-specific state queries. Не зависит от `agent-workflow-ui`.

- [ ] `awf_mcp/` package (separate dist).
- [ ] Tools: `get_active_todos()`, `get_pipeline_state()`, `get_progress(todo_id)`, `get_recent_signals(limit)`, `list_roles()`, `list_pipelines()`.
- [ ] Работает поверх `.agentic/` file bus.
- [ ] Когда нужен: как только scenarios 2+ требуют от агента быстрый доступ к состоянию awf без file reads.

**Trigger для старта:** Scenario 2 (Decision fork) потребует от supervisor'а контекст «на какой стадии pipeline, что worker уже сделал». Вместо ручных `cat` — typed MCP queries.

---

## 🔮 Future considerations

Идеи для далёкого future, не связанные с конкретным сценарием:

- **HTTP transport для MCP** — для remote plugin deployment (team-shared).
- **Real-time updates через SSE/WebSocket** — smooth dashboard refresh.
- **Multi-user** — collaborative forms (одна форма, несколько respondents).
- **Mobile-friendly templates** — адаптивные формы для mobile browsers.
- **Form validation DSL** — declarative validation rules в frontmatter.
- **Form history & undo** — «в прошлой итерации выбрали X, пересматриваем».
- **Voice interface** — speech-to-text для форм (аналог ChatGPT voice).

---

## 📋 Декомпозиция и запуск

Каждый epic перед стартом работы **декомпозируется в awf TODO** через supervisor↔worker pattern. Порядок выполнения epics — последовательный, но внутри epics задачи могут параллелиться.

---

## 🐛 UI/UX наблюдения (после Dogfood v3, 2026-07-31)

### UI-1 · (DONE)  Команда агентов — 3 раздела вместо текущих 4

**Priority:** HIGH

Текущий dropdown (agentOptions) имеет 4 группы:
- Skills (рекомендуется)
- Базовые роли (available_roles)
- Мои агенты (сохранённые роли)
- + Свой .md файл

Переделать на 3 чистых раздела:
1. **Ранее использовавшиеся в проекте** — роли уже в `.agentic/roles/` (existing project roles)
2. **Все скиллы** — globalSkills из `~/.config/opencode/skills/`
3. **Выбери файл** — custom .md upload

Убрать "Базовые роли" (available_roles) — не используются после BD-27.
"Мои агенты (сохранённые)" переименовать/слить с "Ранее в проекте".

### UI-2 · (DONE)  Контекст → переименовать + проверить обработку

**Priority:** HIGH

Переименовать section title "Контекст" → **"Важный контекст о проекте"**.

Проверить backend обработку `context_message`:
- Как сохраняется (roles_processor.py)?
- Доходит ли до supervisor.md / TODO?
- Используется ли в build_prompt / supervisor instructions?

### UI-3 · (DONE)  Поле "Дополнительная инструкция для supervisor"

**Priority:** HIGH

Добавить textarea (аналогичного размера как context_message) **над** dropdown выбора скилла для supervisor. Название: **"Дополнительная инструкция для supervisor"**.

Назначение: пользователь пишет текстовые инструкции которые ДОБАВЛЯЮТСЯ к supervisor.md (не заменяют). Например: "фокус на безопасности", "предпочитай Strategy pattern", "commit message на русском".

Проверить backend: как сохраняется, доходит ли до supervisor.md, используется ли в run_supervisor_via_subprocess / print_interactive_supervisor_instructions.

### UI-4 · (DONE)  Проверить per-role LLM + "сохранить агента"

**Priority:** MEDIUM

Проверить:
1. **Per-role model** (BD-32) — действительно ли `--model` передаётся в opencode run для каждой роли? Dogfood показал что работает (project-auditor → GLM-5.2), но нужен системный тест.
2. **Флаг "Сохранить для будущих сессий"** (save_supervisor checkbox + custom agent save) — действительно ли роль сохраняется в `~/.config/awf/roles/`? Перезагружается ли при следующем open_form?

---

## 🔮 BD-36 · Plan checkpoint — обязательный preview TODO перед запуском агентов

**Status:** DONE (2026-07-31). **Priority:** HIGH — детерминизм pipeline.

**Реализовано:**

```
[plan stage: создаёт TODO-NNNN.md + .ready]
        ↓
[CHECKPOINT (если NOT --auto и plan_checkpoint: true)]
  awf запускает одноразовый HTTP server на случайном порту,
  рендерит HTML форму с TODO контентом и тремя кнопками:
    ✓ Утвердить → продолжить pipeline
    ✏ Изменить → переписать TODO .md, продолжить
    ✗ Отклонить → pipeline остановлен, supervisor перепланирует
        ↓
[agent stages]
```

**Bypass (любой из):**
- `--auto` (CI/tests)
- `automation.plan_checkpoint: false` в config.yaml
- `AWF_PLAN_CHECKPOINT=false` env var

**Архитектура:** self-contained модуль `awf/plan_checkpoint.py` (~280 строк),
не зависит от plugin'а. 31 тест в `tests/integration/test_plan_checkpoint.py`
(включая XSS escaping, HTTP server POST handling, end-to-end flow с edit
decision).

---

## 🐛 Найдено в dogfood v4 (2026-07-31)

### AD-1 · Нет проектной адаптации skills

**Priority:** HIGH

Skill копируется из global как generic копия (200 строк). LLM не знает
что **этот** проект = Chrome extension для Jira (а не банковский API).
Контекст проекта есть только в TODO, не в role.md.

**Решение:** при `awf analyze-roles` (или отдельно) — инжектить project
context в role.md. Например: секция "## Project context" с описанием
стека, домена, ключевых файлов. Не заменять skill, а дополнять.

### AD-2 · Нет traceability (SHA + path)

**Priority:** HIGH

BD-13 frontmatter (`derived_from_global`, `global_path`, `global_sha`)
был удалён в BD-27 упрощении. Теперь неизвестно какая версия skill
скопирована. Global skill обновился — локальная копия устарела, никто
не знает.

**Решение:** вернуть frontmatter в role.md при копировании через форму.
~50 строк. drift detection — `awf analyze-roles --check-drift`.

### AD-3 · Нет integration тестов на полный pipeline

**Priority:** MEDIUM

692 unit теста, но ни одного end-to-end: `awf start` → mock opencode →
plan → 2 agents → verify → commit. Всё mock'ается по частям.

**Решение:** `tests/integration/test_pipeline_e2e.py` — mock `opencode run`
subprocess, проверить весь flow. ~200 строк.

---

## 🐛 Найдено в dogfood v5 (2026-08-04, ses_03496f539ffeUThO9Y8nHNSq86)

> Сессия: supervisor + весь флоу на vllm/Qwen. Pipeline: plan → system-analyst
> → architector → implementer → qa-review → project-auditor → verify.
> System-analyst отработал чисто (DONE signal написан). Architector написал
> ARCHITECTURE.md, но не написал сигнал. Pipeline застрял в salvage.
> User вызвал `awf continue` — запустился НОВЫЙ pipeline со stage 0.

### DF5-1 · Worker (Qwen) не пишет DONE-сигнал — слабый prompt anchor ✅ FIXED

**Priority:** CRITICAL · **Where:** `awf/supervisor.py:78-86` (`build_prompt` execute branch)

**Симптом:** agent-architector (vllm/Qwen) написал `2. ARCHITECTURE.md`, вышел
из opencode с кодом 0, но НЕ создал `DONE-TODO-0001.ready`. Pipeline
заблокировался на ожидании сигнала.

**Почему:** Инструкция о сигнале — одна строка в самом конце prompt-суффикса
(`build_prompt` execute branch, строки 78-86):
```
"When done, write .agentic/outbox/PROGRESS-{todo_id}.md, DONE-{todo_id}.md,
and create the sentinel .agentic/outbox/DONE-{todo_id}.ready file..."
```
Skill-файл `agent-architector.md` (112 строк) сфокусирован на `architecture.md`
и ни слова не говорит про outbox/сигналы. Worker (Qwen) «утонул» в деталях
навыка, не увидел/забыл сигнал-инструкцию. System-analyst (тот же Qwen) —
сигнал написал; разница в длине/фокусе role-файла.

**Файлы для контекста:**
- `awf/supervisor.py:78-86` — `build_prompt()` execute branch (prompt suffix)
- `awf/agent_stage.py:52` — вызов `build_prompt(kind, todo_id)`
- `awf/agent_stage.py:84` — `cmd += ["--", prompt]` (prompt передаётся последним)
- `.agentic/roles/agent-architector.md` — skill без упоминания signal contract

**Решение:**
1. В `build_prompt()` execute-branch — добавить **сильный якорь в НАЧАЛЕ** prompt:
   ```
   ## CRITICAL completion contract (read first, before any other instruction)
   When you finish: create empty file `.agentic/outbox/DONE-{todo_id}.ready`.
   If blocked: create `.agentic/outbox/BLOCKED-{todo_id}.ready`.
   The pipeline BLOCKS until this file appears. This is non-negotiable.
   ```
2. Оставить старую инструкцию в конце как напоминание.
3. (Опционально) В конец role-файла при `awf add-role` / setup форме
   добавлять секцию "## Signal contract" с конкретным путём.

### DF5-2 · `awf continue` перезапускает с stage 0, а не продолжает ✅ FIXED

**Priority:** CRITICAL · **Where:** `awf/api/pipeline.py:335-382` (`continue_pipeline`)

**Симптом:** После того как pipeline застрял в salvage (DF5-1 → DF5-3),
user вызвал `awf continue`. Запустился НОВЫЙ pipeline со stage 0 (plan),
а не продолжение с agent-implementer. Два (потом три) pipeline-процесса
работали одновременно. State file был перезаписан на `stage_idx: 0`.

**Почему:** `continue_pipeline()` находит active TODO (✓), но вызывает
`run_pipeline(PipelineArgs(from_stage=None))`. А `orchestrator.py:329-331`:
```python
stage_idx = 0
if from_stage:  # None → пропускается
    stage_idx = _find_stage_index(stages, from_stage)
```
Функция НЕ читает `pipeline_state.read_state()` — хотя state file
(`current.yaml`, T4.1) содержит `stage_idx` и `stage_name` последней стадии.

**Файлы для контекста:**
- `awf/api/pipeline.py:335-382` — `continue_pipeline()` (не читает state)
- `awf/pipeline_state.py:85` — `read_state()` уже есть, не используется
- `awf/orchestrator.py:329-331` — `stage_idx = 0` если `from_stage` пуст
- `awf/orchestrator.log:27-33` (jira-epic-presenter) — два "Pipeline started"
  подряд вместо одного "Resuming from stage X"

**Решение:**
1. В `continue_pipeline()` читать state:
   ```python
   state = read_state(project_dir)
   if state and "stage_name" in state and not from_stage:
       from_stage = state["stage_name"]
   ```
2. Guard: если state.pipeline_pid жив (через `os.kill(pid, 0)`) — отказать:
   "Pipeline already running (PID XXXX). Kill it first or wait."
3. (Опционально) После успеха — не перезаписывать state до перехода на след. stage.

### DF5-3 · auto-DONE не работает для greenfield/doc-heavy проектов ✅ FIXED

**Priority:** HIGH · **Where:** `awf/verify.py:72-73`

**Симптом:** `attempt_auto_done()` должен синтезировать DONE когда есть git diff.
Но для jira-epic-presenter (новый проект, 0 verify-команд) всегда возвращает False.

**Почему:** `run_verify_commands()` (verify.py:72-73):
```python
if not cmds:
    return False  # no commands configured → can't verify
```
→ `attempt_auto_done()` (verify.py:157) тоже False, даже если
`detect_work_evidence()` = True (есть новый ARCHITECTURE.md).

Результат: каждый «worker забыл сигнал» в greenfield проекте превращается
в полный salvage → supervisor-stuck цикл.

**Файлы для контекста:**
- `awf/verify.py:46-73` — `run_verify_commands` (return False если cmds пусто)
- `awf/verify.py:123-173` — `attempt_auto_done` (зависит от run_verify_commands)
- `awf/orchestrator.py:486-488` — salvage trigger: `attempt_auto_done` False → salvage

**Решение:**
Если verify-команд нет, трактовать как «no blocking checks»:
```python
if not cmds:
    # No verification configured — treat as "no blocking checks".
    # Work evidence alone (detect_work_evidence) is sufficient for auto-DONE.
    # Supervisor review is still in place.
    return True  # was: return False
```
Альтернатива: добавить `config.automation.auto_done_no_verify` (default True)
для контроля поведения. Тест: project без verify-команд + git diff → DONE синтезирован.

### DF5-4 · Salvage path — плохой UX для supervisor в opencode ✅ FIXED

**Priority:** MEDIUM · **Where:** `awf/orchestrator.py:490-522` (salvage block)

**Симптом:** Нет сигнала → orchestrator печатает BD-30 supervisor verify prompt
в stdout (`awf-start.out`) → ждёт 3600s. Но supervisor в opencode НЕ видит
этот stdout (он идёт в background-лог). User увидел «застряло» в `awf_status`
и вызвал `awf continue` (что привело к DF5-2).

**Почему:** Salvage переиспользует `_run_supervisor_stage(verify)`, который
печатает инструкции в stdout и ждёт файла. Это работает для plan/verify
в нормальном flow (когда pipeline foreground). Но в background-режиме
(pipeline запущен через `awf_start(background=True)`) supervisor в opencode
не видит этих инструкций.

**Файлы для контекста:**
- `awf/orchestrator.py:490-522` — salvage block
- `awf/supervisor.py:270-358` — `_run_supervisor_stage_interactive` (stdout)
- `awf/api/context.py` — `load_supervisor_context` (не знает про salvage)
- `awf/api/wait_event.py` — `wait_for_event` (не детектит salvage отдельно)

**Решение:**
1. При salvage — писать структурированный salvage-prompt в inbox:
   `.agentic/inbox/SALVAGE-{todo_id}.md` с: что произошло, git diff stat,
   ожидаемое действие (ACK / REVIEW / replan).
2. `awf_status` / `load_supervisor_context` показывают `salvage_needed: true`
   + содержимое salvage-prompt.
3. `wait_for_event` возвращает `event_type="salvage"` — supervisor в opencode
   понимает что делать без чтения логов.

### DF5-5 · Handoff подхватывает чужой PROGRESS-файл ✅ FIXED

**Priority:** MEDIUM · **Where:** `awf/agent_stage.py:115-160` (`collect_handoff`)

**Симптом:** `agent-architector-TODO-0001.md` (handoff) содержит в секции
"PROGRESS notes (from worker)" заметки system-analyst'а, а не architector'а.

**Почему:** `collect_handoff()` в `agent_stage.py:130` читает
`PROGRESS-{todo_id}.md` из outbox. Если текущий worker не написал свой PROGRESS
(как architector), читается ПРОШЛЫЙ worker'овский файл (system-analyst'а).
Результат: следующий role (implementer) видит чужие заметки как «что
сделал architector» — вводит в заблуждение.

Дополнительно: handoff показывает git diff от baseline
(`.gitignore | 4 ++++`), а не отработки architector'а — потому что
baseline не сдвинулся после stage 1 (git commit не произошёл между stages).

**Файлы для контекста:**
- `awf/agent_stage.py:115-160` — `collect_handoff` (читает outbox/PROGRESS-*)
- `awf/agent_stage.py:130` — `progress = outbox / f"PROGRESS-{todo_id}.md"`
- jira-epic-presenter `.agentic/handoff/agent-architector-TODO-0001.md`
  (содержит system-analyst'овский PROGRESS)

**Решение:**
1. В `collect_handoff()` — проверять mtime PROGRESS-файла:
   если файл не обновлялся после stage_start_time → писать
   "Worker did not leave progress notes."
2. Альтернатива: после каждой успешной стадии (после сигнала) —
   очистка PROGRESS-{todo_id}.md (или переименование в
   `PROGRESS-{todo_id}-{role}.md` для истории).
3. Git diff в handoff должен быть от **commit после предыдущей стадии**,
   не от первоначального baseline (иначе diff копит все изменения).

### DF5-6 · Concurrent pipelines — нет lock'а на запуск ✅ FIXED

**Priority:** HIGH · **Where:** `awf/api/pipeline.py:246-273` (`start_pipeline`)

**Симптом:** В dogfood сессии одновременно работали 2-3 pipeline-процесса
(PID 1295626 + 1303796 + ещё один от второго `awf continue`). State file
перезаписывался каждым из них.

**Почему:** `start_pipeline` (background) проверяет active TODO (BD-30
dogfood-1 fix), но НЕ проверяет, жив ли уже pipeline-процесс.
`continue_pipeline` — тоже не проверяет.

**Файлы для контекста:**
- `awf/api/pipeline.py:246-273` — `start_pipeline` background launch
- `awf/api/pipeline.py:335-382` — `continue_pipeline` (нет PID check)
- `awf/pipeline_state.py` — state file (перезаписывается конкурентно)

**Решение:**
1. Перед стартом — читать `state.pipeline_pid`, проверять `os.kill(pid, 0)`.
   Если жив → отказ: "Pipeline already running (PID XXXX). Use `awf status`
   to check, or kill PID to force restart."
2. Записывать PID в state file при старте (уже делается через `pipeline_pid`
   поле, но не проверяется при повторном запуске).
3. То же для `continue_pipeline` — если pipeline жив, continue = noop.

### DF5-7 · Dashboard template syntax error — `{% endif %` без `}` ✅ FIXED

**Priority:** CRITICAL · **Where:** `awf/templates/dashboard.html.j2:194`

**Симптом:** Dashboard НИКОГДА не генерируется. `generate_dashboard()` падает
на Jinja2 TemplateSyntaxError и возвращает None (DF5-8 глотает ошибку).
Supervisor потратил ~15 сообщений пытаясь понять почему dashboard пустой.

**Why:** Строка 194: `{% endif %` — пропущена закрывающая фигурная скобка `}`.
Должно быть `{% endif %}`. Этот `endif` закрывает `{% if worker_activity %}`
от строки 177 (блок CSS для worker activity bar).

**Файлы для контекста:**
- `awf/templates/dashboard.html.j2:177` — `{% if worker_activity %}` (открывает)
- `awf/templates/dashboard.html.j2:194` — `{% endif %` (битый, без `}`)
- `awf/api/dashboard.py:389-390` — `except Exception: return None` (глотает)

**Решение:** One-char fix: `{% endif %` → `{% endif %}`.
+ добавить unit-тест: `generate_dashboard()` рендерит без exception.

### DF5-8 · `generate_dashboard` глотает ВСЕ ошибки молча ✅ FIXED

**Priority:** HIGH · **Where:** `awf/api/dashboard.py:389-390`

**Симптом:** Любая ошибка рендеринга (TemplateSyntaxError, KeyError, TypeError,
missing data) → `except Exception: return None`. Dashboard просто не появляется.
Никакого лога, никакого warning'а. Supervisor не может понять причину без
ручного дебага через Python REPL.

**Почему:** bare `except Exception` — анти-паттерн. Спрятал реальный баг
(DF5-7) на неопределённое время. Dashboard — фичя для мониторинга, не
critical-path; но silent failure превращает каждый dashboard баг в
30-минутный дебаг.

**Файлы для контекста:**
- `awf/api/dashboard.py:366-390` — try/except блок рендеринга
- `awf/orchestrator.py` — вызовы `generate_dashboard()` после `write_state()`
  (тоже могут глотать None return)

**Решение:**
1. Логировать exception: `_log(logs_dir, f"Dashboard generation failed: {e}")`
   (передать `logs_dir` в `generate_dashboard` или использовать logging).
2. Сузить except до конкретных типов (TemplateError, OSError).
3. При template syntax error — писать `.agentic/dashboards/error.txt` с traceback.

### DF5-9 · Supervisor (Qwen) нарушил role boundaries — редактировал awf ✅ FIXED

**Priority:** MEDIUM · **Where:** supervisor.md role instructions

**Симптом:** Supervisor (Qwen в opencode-сессии) нашёл баг в awf (DF5-7)
и попытался его исправить: `edit awf/templates/dashboard.html.j2`.
User остановил: "Не правь awf сам. Это не твоя работа". Supervisor откатил
через `git checkout`.

**Почему:** supervisor.md содержит "Step 0 Role boundaries: DO NOT edit
project source files. All changes go through pipeline." Но:
1. Qwen менее послушен инструкциям чем Claude/GPT.
2. awf/ — это не "project source files" с точки зрения supervisor'а
   (project = jira-epic-presenter, awf = инструмент). Формулировка
   неоднозначна.
3. Supervisor имеет полный доступ к tools (edit, bash, write) — граница
   только текстовая, нет технического enforcement.

**Файлы для контекста:**
- `templates/roles/supervisor.md` — Step 0 role boundaries
- Session parts [201-228] — supervisor редактирует awf, user останавливает

**Решение:**
1. Усилить формулировку: "DO NOT edit ANY files outside jira-epic-presenter/
   project tree. awf/ tooling, templates, Python code — read-only for you.
   Found a bug in awf? Write it to BACKLOG or tell user, do NOT fix it."
2. (Опционально) opencode permission rules могут запретить edit вне project_dir.

### DF5-10 · `awf_start` возвращает "ok" когда pipeline сразу умирает ✅ FIXED

**Priority:** HIGH · **Where:** `awf/api/pipeline.py:267-290` + orchestrator

**Симптом:** Первый `awf_start(background=True, checkpoint=enabled)` вернул:
```json
{"status": "ok", "run_mode": "background", "run_id": 1295433}
```
Но pipeline УЖЕ умер — child subprocess запустил `run_pipeline()` в foreground,
попал на BD-36 check "foreground+checkpoint=noop", вышел с code 1.
awf-start.out содержит: "Foreground mode incompatible with BD-36 interactive
checkpoint". User не знал что pipeline мёртв — status показывал active TODO
но лог не рос.

**Почему:** `start_pipeline(background=True)` → `start_in_background()` →
Popen (subprocess запущен) → return PID. Но содержимое subprocess'а
(run_pipeline) может сразу выйти с noop/error. Background launch
проверяет только "процесс стартовал", не "процесс жив через 1 сек".

Дополнительно: background child (`python -m awf start`) вызывает
`start_pipeline(background=False)` (CLI не передаёт --background — он САМ
background child). Foreground + checkpoint → noop. Это design conflict:
checkpoint должен работать в background mode (form в браузере, не stdout),
но foreground check не различает "real foreground" vs "background child".

**Файлы для контекста:**
- `awf/api/pipeline.py:267-290` — background launch (не ждёт child)
- `awf/api/_background.py:61-75` — child_argv (без --background flag)
- `awf/orchestrator.py` — foreground+checkpoint check (noop return)
- Session parts [84-99] — первый start вернул ok, но pipeline умер

**Решение:**
1. После `start_in_background()` → подождать 1 сек → проверить `proc.poll()`.
   Если процесс уже умер → вернуть `run_mode="error"` с exit_code и log_tail.
2. Передать env var `AWF_BACKGROUND_CHILD=1` в child. В orchestrator
   foreground+checkpoint check: если `AWF_BACKGROUND_CHILD=1` → НЕ noop
   (checkpoint form открывается в браузере, stdout goes to file, не MCP stdio).

### DF5-11 · Supervisor (Qwen) не использует wait_for_event проактивно ✅ FIXED

**Priority:** LOW · **Where:** supervisor.md workflow steps / AGENTS.md

**Симптом:** User спросил "Тебя оповестят о завершении и ты проснешься?"
Supervisor ответил "Нет, сам не проснусь." — и не предложил вызвать
`wait_for_event`. User должен был сам сказать "Давай мне статус сам".
Pipeline шёл ~5 минут без мониторинга.

**Почему:** AGENTS.md описывает `wait_for_event` в workflow recipe, но
supervisor.md (то что видит Qwen) не содержит явного шага "после
awf_start → вызвать wait_for_event и ждать". Qwen не следует рецепту
из AGENTS.md — он следует своему role file.

**Решение:**
1. В supervisor.md добавить шаг: "After awf_start → call awf_wait_for_event
   immediately. Do NOT ask user 'should I wait?' — just start waiting."
2. (Опционально) AGENTS.md уже содержит это, но для Qwen нужно дублирование
   в role file.

### DF5-12 · MCP tool timeouts во время активного pipeline ✅ FIXED

**Priority:** MEDIUM · **Where:** MCP plugin (single-threaded server)

**Симптом:** Когда pipeline работает (subprocess активен), MCP tools
таймаутят: `awf_continue` (part[273]) вернул error, `awf_status` тоже
медленный. Supervisor переключился на bash (`python3 -m awf status`).

**Почему:** MCP plugin работает в single-threaded subprocess opencode.
Pipeline subprocess не блокирует MCP напрямую, но ресурсы (CPU, I/O)
конкурируют. vLLM + pipeline workers + MCP server на одной машине →
MCP timeout при нагрузке.

**Файлы для контекста:**
- Session parts [273, 281, 286] — MCP timeouts, fallback to bash
- `agent_workflow_ui/server.py` — MCP server (single-threaded)

**Решение:**
1. Увеличить MCP tool timeout в opencode config (если настраивается).
2. `awf_wait_for_event` уже использует blocking poll (хорошо), но другие
   tools (`status`, `continue`) должны быть quick — проверить, не блокируют
   ли они на I/O.
3. (Long-term) MCP server на отдельном процессе / thread pool.

---

## 🐛 Найдено в dogfood v6 (2026-08-05, ses_02e507f92ffe5FxdRIQipenL9f)

> Сессия: supervisor + весь флоу на vllm/Qwen. TODO-0001 был ACK'd в
> предыдущей сессии. Supervisor ACK'нул → pipeline завершился → dispatch
> TODO-0002 → awf_start. Pipeline упал (foreground+checkpoint), dashboard
> показывал stale stage, supervisor действовал пассивно.

### Корневой анализ

Три системные проблемы, все сводятся к отсутствию lifecycle management:

**1. TODO Lifecycle отсутствует.** После verify ACK/APPROVE orchestrator
коммитит, отмечает plan step, переходит к следующей стадии — но НЕ очищает
inbox. `TODO-{id}.ready` + `TODO-{id}.md` + `ACK-{id}.ready` остаются.
Следующий `awf_start` → BD-30 orphan pickup находит старый TODO → обрабатывает
его вместо нового. Нет понятия "completed TODO" — только inbox сигналы и
outbox DONE'ы, которые не синхронизированы.

**2. Pipeline state не валидируется перед операциями.** `awf_start` и
`awf_continue` не проверяют консистентность state: устаревшие PID, конфликты
сигналов, orphan TODO'ы. Каждая операция должна начинаться с reconcile.

**3. Supervisor (LLM) пассивен на verify.** supervisor.md говорит "decide:
continue/fix/rollback" — но Qwen интерпретирует это как "доложи пользователю
и жди". Нет явного запрета на пассивное поведение.

---

### DF6-1 · TODO Archive: после verify approve — перемещать TODO в done/ ✅ FIXED

**Priority:** CRITICAL · **Where:** `awf/orchestrator.py:441-451` (verify approve path)

**Что:** После verify approve (ACK/APPROVE) orchestrator перемещает
TODO-файлы из inbox в `.agentic/done/{todo_id}/`:
- `inbox/TODO-{id}.md` → `done/{id}/TODO.md`
- `inbox/TODO-{id}.ready` → удаляется (сигнал отработан)
- `inbox/ACK-{id}.ready` / `APPROVE-{id}.ready` → удаляется
- `outbox/PROGRESS-{id}.md` → `done/{id}/PROGRESS.md`
- `outbox/DONE-{id}.*` → `done/{id}/`

**Почему:** Сейчас после ACK `inbox/TODO-0001.ready` остаётся → BD-30 orphan
pickup при следующем `awf_start` находит его → обрабатывает старый TODO
вместо нового. Это сломало весь dogfood v6.

**Где править:**
- `awf/orchestrator.py:441-451` — после `_maybe_commit`, добавить `_archive_todo()`
- `awf/todos.py` — новая функция `archive_todo(project_dir, todo_id)`
- `awf/paths.py` — `done_dir(project_dir)` → `.agentic/done/`
- `.gitignore` — `.agentic/done/` (runtime state)

**Тесты:**
- После ACK: `inbox/TODO-*.ready` не существует, `done/{id}/` существует
- BD-30 не подхватывает архивированный TODO
- `awf_status` считает `done/` → `done_count` правильный

### DF6-2 · Reconcile: авто-сверка state перед start/continue ✅ FIXED

**Priority:** CRITICAL · **Where:** `awf/api/pipeline.py` (start_pipeline + continue_pipeline)

**Что:** Перед каждым `awf_start` / `awf_continue` запускать `_reconcile()`:

1. **Archive orphan ACK'd TODOs:** для каждого `inbox/TODO-*.ready` без
   matching `DONE-*` в outbox → проверить есть ли `ACK-*` или `APPROVE-*`
   в inbox → если да, archive (он завершён, просто не очищен).
2. **Clear stale PID:** `state.pipeline_pid` → `os.kill(pid, 0)` → если мёртв,
   `clear_state()`.
3. **Deduplicate signals:** `ACK-{id}.ready` + `APPROVE-{id}.ready` → удалить
   старший по mtime.
4. **Validate single active TODO:** если >1 `TODO-*.ready` в inbox → оставить
   новейший по mtime, остальные архивировать как "superseded".

**Почему:** DF6-1 archive закроит основной случай, но старые проекты с
мусором в inbox всё равно сломаются. Reconcile = defensive layer.

**Где править:**
- `awf/api/pipeline.py` — `_reconcile(project_dir)` перед стартом
- Вызов в `start_pipeline()` (после TODO guard, перед background launch)
- Вызов в `continue_pipeline()` (перед чтением state)

**Тесты:**
- Inbox с TODO-0001.ready + ACK-TODO-0001.ready → после reconcile: архивировано
- Inbox с 2 TODO-*.ready → после reconcile: 1 активный, 1 superseded
- State с мёртвым PID → после reconcile: state очищен

### DF6-3 · BD-30 orphan pickup: проверять done/ директорию ✅ FIXED

**Priority:** HIGH · **Where:** `awf/supervisor.py` (`wait_for_supervisor_signal` BD-30 logic)

**Что:** BD-30 orphan pickup при сканировании inbox должен исключать TODO'ы
которые есть в `done/`. Сейчас проверяет только `outbox/DONE-{id}`.
Добавить проверку `done/{id}/` existence.

**Почему:** DF6-1 archive переместит файлы в done/, но BD-30 может всё равно
найти TODO-*.ready в inbox если он не был удалён (edge case: crash во время
archive). Defense-in-depth.

**Где править:**
- `awf/supervisor.py` — orphan pickup: `if done_dir / todo_id exists: skip`

### DF6-4 · done_count: считать из done/ директории ✅ FIXED

**Priority:** HIGH · **Where:** `awf/api/lifecycle.py:211-234` (`_count_done_blocked`)

**Что:** `_count_done_blocked` сейчас считает `outbox/DONE-*.ready`. После
DF6-1, завершённые TODO'ы архивируются в `done/`. Добавить подсчёт
`done/*/` директорий.

**Почему:** `awf_status` показал `done_count: 0` после ACK'd TODO-0001.
Supervisor не видит прогресс.

**Где править:**
- `awf/api/lifecycle.py:211` — `done_count = len(list(done_dir.glob("*/")))` + старая логика для back-compat

### DF6-5 · BD-36 checkpoint: auto-disable для background child ✅ FIXED

**Priority:** HIGH · **Where:** `awf/api/_background.py` + `awf/orchestrator.py`

**Что:** Background child запускается через `python -m awf start` (без
`--background`). Child работает в foreground mode. Foreground + checkpoint =
noop error → child умирает → pipeline мёртв, но `awf_start` вернул "ok".

Два варианта фикса:
- **A (env signal):** передать `AWF_BACKGROUND_CHILD=1` в child env.
  orchestrator foreground+checkpoint check: если env установлен → не noop
  (checkpoint form открывается в браузере, stdout goes to file — конфликт
  с MCP stdio отсутствует).
- **B (auto-disable):** `start_in_background` передаёт `--no-checkpoint`
  в child argv. Теряем checkpoint для background mode, но не падаем.

Рекомендуется **A** — checkpoint должен работать в background (form в
браузере, не stdout). Это то что ожидает пользователь.

**Почему:** Каждый `awf_start(background=True)` с включённым checkpoint
(дефолт) → pipeline немедленно умирает. DF5-10 `_verify_child_alive` детектит
это, но не предотвращает.

**Где править:**
- `awf/api/_background.py` — child env: `env["AWF_BACKGROUND_CHILD"] = "1"`
- `awf/orchestrator.py` — foreground+checkpoint check:
  `if os.environ.get("AWF_BACKGROUND_CHILD"): pass  # OK — stdout goes to file`

### DF6-6 · Dashboard: детект мёртвого orchestrator ✅ FIXED

**Priority:** MEDIUM · **Where:** `awf/api/dashboard.py:169` (`_determine_status`)

**Что:** `_determine_status` проверяет state fields, но НЕ проверяет жив ли
`pipeline_pid`. Если orchestrator умер, но state не очищен (crash) →
dashboard показывает "Pipeline running" indefinitely.

Добавить: если `state.pipeline_pid` есть, проверить `os.kill(pid, 0)`.
Если мёртв → status = "dead", status_text = "⚠️ Pipeline process dead",
status_class = "blocked".

**Почему:** В dogfood v6 dashboard показывал system-analyst (stale state)
хотя orchestrator был мёртв.

**Где править:**
- `awf/api/dashboard.py:169` — `_determine_status` — добавить PID liveness check

### DF6-7 · Supervisor.md: verify imperatives — запрет пассивности ✅ FIXED

**Priority:** HIGH · **Where:** `templates/roles/supervisor.md:257-277` (Step 7)

**Что:** Текущий verify шаг говорит "Decide: continue / fix / rollback".
Qwen интерпретирует как "доложи пользователю и жди". Нужны явные imperatives:

```markdown
### Step 7 · Verify — YOU are the reviewer, not a relay

When pipeline reaches verify stage, you MUST:

1. Read ALL handoff files in `.agentic/handoff/`.
2. Run `git diff --stat` to see what changed.
3. Evaluate quality yourself — do NOT ask user "should I approve?".
4. DECIDE AND ACT:
   - Work is good → `awf_approve(todo_id)` — no user permission needed.
   - Work has issues → write REVIEW-{todo_id}.md with specific fixes.
5. DO NOT relay "pipeline waits for your decision" to the user.
   YOU are the decision maker. User hired you as supervisor.
6. DO NOT wait for user to say "ACK" — that's YOUR call.
```

**Почему:** Supervisor передал "ждёт твоего ACK/REVIEW" вместо того чтобы
самому прочитать handoffs и решить. Пользователь сказал "ACK" — supervisor
выполнил, но это роль человека, не supervisor'а.

### DF6-8 · Worker orphan kill (PR_SET_PDEATHSIG) ✅ FIXED

**Priority:** LOW · **Where:** `awf/agent_stage.py` + `awf/_env.py`

**Что:** Worker subprocess (opencode run) переживает смерть orchestrator'а.
На Linux можно вызвать `prctl(PR_SET_PDEATHSIG, SIGTERM)` в child — process
получает SIGTERM когда родитель умирает.

Реализация: в `_env.py` `setup_subprocess_env()` добавить `preexec_fn`:
```python
import ctypes
libc = ctypes.CDLL("libc.so.6")
libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG = 1
```

**Почему:** В dogfood v6 worker продолжал работать после смерти orchestrator'а
и создавал файлы. Pipeline был мёртв, но worker жил. Dashboard показывал
stale state, supervisor не понимал что происходит.

**Где править:**
- `awf/_env.py` — `preexec_fn` в subprocess.run / Popen calls
- Guard: только на Linux (`sys.platform == "linux"`)

---

## 🐛 KAUD — Kimi Audit 2026-08-06

> Источник: `/home/pklochkov/Desktop/kimi - audit_report_agentic_workflow.md`
> 5 HIGH, 18 MEDIUM, 6 LOW. Ниже HIGH + отобранные MEDIUM.

### KAUD-1 · `get_report()` игнорирует done/ архивы (HIGH)

**Where:** `awf/api/lifecycle.py:381-451`

`get_report()` не считает TODOs из `.agentic/done/`. После DF6-1 archive,
`done_count` = 0, `items` пустой. Та же логика что DF6-4 для `get_status()`,
но `get_report()` не обновили.

**Fix:** reuse `_count_done_blocked(inbox, outbox, done_dir)` — как в `get_status()`.

### KAUD-2 · `_build_pipeline_context()` хардкодит default.yaml (HIGH)

**Where:** `awf/supervisor.py:55`

Всегда читает `.agentic/pipelines/default.yaml`, игнорируя `default_pipeline`
из config.yaml и `--pipeline` CLI flag. Non-default pipelines получают пустой
pipeline context → worker не знает своё место.

**Fix:** read pipeline name from config or pass as parameter.
```python
pipeline_name = cfg_mod.get(config, "default_pipeline", "default") or "default"
pipeline_file = project_dir / ".agentic" / "pipelines" / f"{pipeline_name}.yaml"
```

### KAUD-3 · CSRF origin validation использует startswith (HIGH)

**Where:** `agent_workflow_ui/http_endpoint.py:61-88` + `awf/plan_checkpoint.py:253-269`

`_is_origin_allowed()` использует `origin.startswith("http://127.0.0.1")`.
Bypass: `http://127.0.0.1.evil.com` проходит проверку.

**Fix:** parse URL via `urllib.parse.urlparse`, check `hostname in {"127.0.0.1", "localhost"}`.
```python
from urllib.parse import urlparse
parsed = urlparse(origin)
if parsed.hostname not in ("127.0.0.1", "localhost"):
    return False
```

### KAUD-4 · CLI `--timeout` игнорируется (HIGH)

**Where:** `awf/api/pipeline.py:494-503` + `awf/orchestrator.py:272-588`

`run_pipeline()` принимает `args.timeout` но не передаёт его в
`_run_agent_stage()` или `_run_supervisor_stage()`. Workers всегда
используют дефолт 3600s. Пользователь не может увеличить timeout
для медленных моделей (vllm/llm на QA-ревью).

**Fix:** add `timeout` parameter to `_run_agent_stage()` and
`run_supervisor_stage()`, pass through to `run_subprocess_until_signal()` /
`wait_for_supervisor_signal()`.

### KAUD-5 · Child opencode config over-privileged (HIGH)

**Where:** `awf/_env.py` — `awf_subprocess_env()`

Worker config grants `bash: allow`, `write: allow`, `webfetch: allow`
globally. Заменяет пользовательский config полностью.

**Fix options:**
- A: Merge with user's existing config (не заменять)
- B: Restrict `bash`/`webfetch` to `prompt` (workers не bash-скрипты)
- C: Restrict `edit`/`write` to project directory only

Рекомендуется A+B: merge + tighter defaults.

### KAUD-6 · `_reconcile()` пишет state неатомарно (MEDIUM)

**Where:** `awf/api/pipeline.py:88-153`

`write_text()` напрямую — неатомарно. Если процесс убит mid-write,
state file повреждён → следующий запуск не может прочитать.

**Fix:** use `awf._atomic.atomic_write_text()` (уже есть в кодовой базе).

### KAUD-7 · `signal_watch` lexicographic tie-break (MEDIUM)

**Where:** `awf/signal_watch.py` — `watch_new_glob`

Picks `sorted(new_files)[0]` — lexicographically smallest. При множественных
сигналах за один poll interval выбирает не тот (TODO-0002 вместо TODO-0010).

**Fix:** sort by numeric ID, not lexicographically.
```python
# Before: sorted(new_files)[0]
# After:  sorted(new_files, key=lambda f: int(re.search(r'\d+', f).group()))[0]
```

### KAUD-8 · `current_todo` не персистится в state (MEDIUM)

**Where:** `awf/orchestrator.py`

`current_todo` — локальная переменная в `run_pipeline()`. State file не
сохраняет её. После краша, `awf continue` реконструирует через
`todos.newest_active()` (numeric ID sort), что может дать другой TODO.

**Fix:** include `current_todo` in `write_state()` calls. Read back in
`continue_pipeline()`.

### KAUD-9 · Worker stdout не перенаправляется в лог (MEDIUM)

**Where:** `awf/agent_stage.py` — `run_agent_stage()`

Worker subprocess наследует stdout/stderr → mixed с `awf-start.out` или
терминалом. Невозможно прочитать вывод конкретного worker'а отдельно.

**Fix:** redirect to `.agentic/logs/<role>-<todo_id>.out`.
```python
log_path = logs_dir / f"{role}-{todo_id}.out"
with open(log_path, "w") as f:
    proc = subprocess.Popen(..., stdout=f, stderr=subprocess.STDOUT)
```

### KAUD-10 · `pytest-timeout` закомментирован (MEDIUM)

**Where:** `pyproject.toml`

Если тест зависает (real subprocess, dead lock), CI блокируется навсегда.

**Fix:** re-enable with per-test timeout (e.g., 120s).
```toml
[tool.pytest.ini_options]
timeout = 120
```

### KAUD-11 · `open_form` TTL docs vs code mismatch (MEDIUM)

**Where:** `agent_workflow_ui/tools/forms.py:206-209`

Code: default TTL = 86400s (24h) from config.
Docs: "Default: no TTL".

**Fix:** align — either remove default TTL from code or update docs.

### KAUD-12 · BACKLOG/CHANGELOG ~80% закрытых записей (LOW)

**Where:** `BACKLOG.md`, `CHANGELOG.md`

~900 строк закрытых DF5/DF6/QA entries. Трудно найти активные задачи.

**Fix:** archive closed entries to `BACKLOG-archive.md`, keep only
active items in BACKLOG.md.

### Решения по вопросам аудитора

1. **Non-default pipelines** — YES, используются (project-setup form позволяет
   выбрать pipeline). KAUD-2 = HIGH.
2. **Slow test suite** — KNOWN pain (~7 min). Приоритет MEDIUM.
3. **Starting point** — Layer 1 (KAUD-1..6), потом Layer 2.
4. **Child permissions** — Workers NEED bash (запуск tsc, build, git status).
   webfetch нужен для research. Ограничение к project dir — сложно реализовать
   (opencode permission system не поддерживает path-scoped rules).
   Решение: merge config (KAUD-5 вариант A), не трогать permissions пока.

---

## 🐛 DAUD — DeepSeek Audit 2026-08-06

> Источник: `/home/pklochkov/Desktop/deepseek - awf-audit-report-2026-08-06.md`
> 7 findings (0 critical, 2 bugs, 3 quality, 2 architecture).
> Фокус: dead code, prompt clarity, race conditions, code quality.

### DAUD-1 · `{NNNN}` в _SNIPPET_PLAN — литерал в prompt (BUG)

**Where:** `awf/supervisor.py` — `_SNIPPET_PLAN`

`_SNIPPET_PLAN` содержит `TODO-{NNNN}.md` — фигурные скобки выглядят как
шаблонная переменная. `_stage_snippet()` подставляет только `{todo_id}`,
но для plan стадии `todo_id` пустой → `{NNNN}` остаётся литералом в промпте.
Qwen может создать файл `TODO-{NNNN}.md` вместо `TODO-0005.md`.
Аналогично BD-21 (та же проблема с `{todo_id}`).

**Fix:** заменить на человеческий язык: «TODO-NNNN (подставь следующий номер,
например TODO-0005)».

### DAUD-2 · TOCTOU race в `_find_free_port()` (BUG)

**Where:** `awf/plan_checkpoint.py:246-250`

Между `_find_free_port()` (сокет закрывается) и `_start_checkpoint_server(port)`
другой процесс может занять порт → `OSError: Address already in use`.

**Fix:** обернуть `_start_checkpoint_server` в retry (2-3 попытки,
500ms между ними). Если после N попыток порт занят — понятная ошибка.

### DAUD-3 · Dead code — `opencode_skills_dir()` (QUALITY)

**Where:** `awf/xdg.py:37-39`

Функция не вызывается нигде. Спекулятивный код «на вырост».

**Fix:** удалить. Если понадобится — добавим осознанно.

### DAUD-4 · Двойной `except Exception` вокруг `generate_dashboard()` (QUALITY)

**Where:** `awf/orchestrator.py:369-373` + `awf/api/dashboard.py:529-541`

Dashboard уже сам логирует ошибки и пишет `error.txt` (DF5-8).
Внешняя обёртка в оркестраторе логирует второй раз — шум в логах.

**Fix:** убрать внешний try/except в оркестраторе.
`generate_dashboard()` самодостаточен.

### DAUD-5 · Signal file content validation — warning-only (QUALITY)

**Where:** `awf/signals.py:78-113` — `read_signal_for_todo()`

Проверяется только `st_size > 0` для `.md`-компаньона. Worker может
написать «ok» в `DONE-TODO-0001.md` — и verify признает это отчётом.

**Fix:** добавить минимальную проверку (наличие markdown-заголовка `#`).
Warning в лог, НЕ блокировать pipeline. Defence-in-depth.

### DAUD-6 · `plan_checkpoint.py` → Jinja2 (DEFERRED)

**Where:** `awf/plan_checkpoint.py:~200 строк HTML через конкатенацию`

Дашборд использует Jinja2, checkpoint — нет. ~200 строк boilerplate.

**Решение:** DEFER до scenario 2/3 (когда понадобятся новые формы).
2-3 часа работы, нет user-facing импакта сейчас.

### DAUD-7 · `orchestrator.run_pipeline()` refactor (ARCHITECTURE)

**Where:** `awf/orchestrator.py:272-588` (390 строк, cognitive complexity 107)

Три ответственности: state machine, side effects, error recovery.
Совпадает с KAUD recommendation (Layer 3).

**Решение:** DEFER. 1-2 дня работы. Приоритет ниже чем Layer 1/2 фиксы.

### Архитектурные ориентиры (NOT в backlog — hold in mind)

A. **`tools/awf.py` (796 строк)** — разбить по зонам до scenario 2.
   Час сейчас против дня с regression risk через 5 сценариев.

B. **`supervisor.py` (809 строк)** — разделить `wait_for_supervisor_signal`
   на `_wait_for_plan` / `_wait_for_verify` ДО добавления новых stage kind.

C. **Два HTTP-сервера** — one-shot в core (`plan_checkpoint.py`),
   long-lived в plugin (`http_endpoint.py`). Не плодить третий.

### Решения по вопросам аудитора

1. **`opencode_skills_dir()`** — DELETE (DAUD-3). Не используется, не планируется.
2. **`plan_checkpoint.py` Jinja2** — DEFER (DAUD-6). Нет user impact сейчас.
