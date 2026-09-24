# BACKLOG Archive

> Закрытые, отложенные и исторические записи. История работы.
> Активные items — только в BACKLOG.md; открытая строка в архиве
> (пометка OPEN) — ошибка, её дом BACKLOG.md.

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

**Status:** отложен — ждать real failure в dogfooding. Активный пункт — в BACKLOG.md (один дом); ниже — история обсуждения. **Priority:** HIGH — fundamental to "pipeline as conveyor".

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


## ✅ Done · AUD-2026-08-07 — Audit fixes

**Source:** `/home/pklochkov/Desktop/agentic-workflow-audit-2026-08-07.md`
Закрыто в одной сессии (коммиты `51336f4`..`4b2cc24`).

### AUD-1 · [CRITICAL] verify.py: false-positive auto-DONE на untracked файлах — ✅ FIXED

`detect_work_evidence()` считал ЛЮБОЙ untracked файл «работой» без фильтра по baseline.
Worker не сделал ничего → pre-existing untracked файл → auto-DONE.
**Fix:** добавлен `todo_id` параметр, фильтр через `BASELINE-{todo_id}.untracked` snapshot.

### AUD-2 · [HIGH] Полный прогон тестов зависает (>25 мин) — ✅ FIXED

Root cause: тесты без mock `subprocess.run` спавнили реальный `opencode models` + commit_gate approve wait 1800s.
**Fix:** conftest autouse intercepts `['opencode', 'models']` + `AWF_APPROVE_TIMEOUT_SECONDS=5`.
**Result:** 1046 tests in 6:53 (was >25min hang). Plugin tests 57s (was 182s = 3.2x speedup).

### AUD-3 · [MEDIUM] 5 мест читают pipeline.yaml в обход resolve_pipeline_file — ✅ FIXED

`context.py` ×4 + `dashboard.py` ×1 хардкодили `default.yaml`.
**Fix:** `_load_pipeline_stages` helper через `resolve_pipeline_file` + `load_stages` (-79 строк).

### AUD-4 · [MEDIUM] _reconcile сортирует по mtime — ✅ FIXED

**Fix:** сортировка по номеру TODO (regex extraction), не mtime.

### AUD-5 · [MEDIUM] read_recent_models игнорирует XDG_DATA_HOME — ✅ FIXED

**Fix:** `opencode_config.py` теперь уважает `XDG_DATA_HOME`.

### AUD-6 · [LOW] Мёртвый код (3 пункта) — ✅ FIXED

- `context.py:63` — no-op expression удалён
- `context.py:93-94` — избыточный `if stages:` удалён
- `plan_checkpoint.py:278` — мёртвая `file://` ветка удалена

### AUD-7 · [LOW] Dashboard autoescape=False → XSS — ✅ FIXED

**Fix:** `autoescape=True` (handoff preview из LLM-контента больше не исполняет `<script>`).

### AUD-8 · [LOW] _background.py PID reuse — ✅ FIXED

**Fix:** `/proc/<pid>/cmdline` проверка добавлена в `check_pipeline_running`.

### AUD-9 · [LOW] signal_watch.py message врёт — ✅ FIXED

**Fix:** два сообщения: «signal detected but process hung» vs «no signal at all».

### AUD-10 · [LOW] CI comment stale + docs counter drift — ✅ FIXED

**Fix:** stale comment удалён, ручные счётчики тестов/функций убраны из README и architecture.md.

### AUD-11 · [DECISION] Packaging: editable-only — ✅ CLOSED

Документировано в README: editable-only install, wheel не поддерживается.

## ✅ Done · KA2 — Kimi re-audit fixes (2026-08-07)

**Source:** `/home/pklochkov/Desktop/kimi - audit_report_agentic_workflow-2026-08-07.md`
Закрыто в одной сессии (коммиты `08096c3`..`383ec80`).

### KA2-1 · _background.py log truncation — ✅ FIXED
`open(log_file, "wb")` → `"ab"` (append).

### KA2-2 · signal_watch.py worker_log leak — ✅ FIXED
`worker_log = open(...)` wrapped in try/finally.

### KA2-3 · forms.py TTL docs — ✅ NO-OP
Docs already correct ("Default: 24h from config").

### KA2-4 · cmd_init.py encoding — ✅ FIXED
Added `encoding="utf-8"` to both `cfg_path.open()` calls.

### KA2-5 · read_signal_for_todo prefix order — ✅ FIXED
Now selects signal with latest mtime instead of first prefix match.
+ 2 regression tests.

### KA2-6 · --timeout for supervisor stages — ✅ FIXED
`AWF_SUPERVISOR_TIMEOUT` set from `agent_hard_timeout` at pipeline start.

### KA2-7 · _env.py E2BIG risk — ✅ FIXED
Guards against >100KB config: strips to permissions-only if too large.

### KA2-8 · Model cache invalidation — ✅ FIXED
Cache invalidated on opencode.json mtime change (no more 5-min wait).

### 3 findings already fixed before Kimi's audit:
- #2 `--pipeline` flag → fixed in `aac5692`
- 7.2 state persistence → fixed in `aac5692`
- 7.4 uncommitted wait_event → committed in `51336f4`

### 4 findings disagreed (intentional design):
- #5 bash/webfetch:allow (workers need them)
- #16 except Exception in MCP tools (must return error dict)
- #18 HTTP auth (localhost + origin check sufficient)
- #20 model_check generic Exception (best-effort probe)


---

## 🗄 2026-09-19 · Перенос из активного BACKLOG

### SMO · .1–.6 (завершено перед .7)

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



### AUD-12 · [T3] Рефакторинг (закрыто)

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


### QA-2026-08-10 · закрытые пункты

P0:
✅ `.1` **Python 3.10+ syntax vs >=3.9** — `requires-python = ">=3.10"`.
✅ `.2` **Jinja2 XSS via from_string** — `autoescape=True` (was OFF).
✅ `.3` **pytest tests/unit/ hangs** — conftest intercepts `subprocess.Popen`.

P1 закрытые:
✅ `.4` **OPENCODE_CONFIG_CONTENT leak** — only permission field serialized.
✅ `.5` **yaml.safe_load config.py** — try/except with graceful error.
✅ `.6` **yaml.safe_load pipeline.py** — try/except with graceful error.
✅ `.7` **Signal watch blindspot** — all 6 prefixes, not just DONE/BLOCKED.
✅ `.8` **TODO archived without commit check** — maybe_commit returns bool.
✅ `.9` **Submitting forms stuck** — TTL auto-revert after 10min.
✅ `.10` **inputs/*.yaml chmod** — 0o600 after write.
✅ `.11` **Subprocess timeouts** — git_utils, context, lifecycle (30s).
✅ `.12` **Frontmatter regex** — `[ \t]*` вместо `\s*` (precise delimiter).
✅ `.13` **HTTP unicode form_id** — `isascii()` check added.
✅ `.16` **[HIGH] Entry points coverage** — закрыт (FU-19/TODO-0023).
   cmd_add_role и cmd_report получили CLI smoke-тесты
   (`tests/integration/test_cli_entrypoints.py::TestAddRoleEntrypoint`,
   `::TestReportEntrypoint`): rc 0 + файл на happy-path, rc 1 без traceback
   на traversal, OK/BLK в `awf report` на живом проекте. cmd_baseline
   покрывался с 28b30fe (`tests/unit/test_cmd_baseline.py` — shell-инъекции,
   таймауты). Все entry points из .16 теперь имеют CLI-вход.

P2 закрытые:
✅ `.17` **AWF_SUPERVISOR_TIMEOUT** — restored after pipeline.
✅ `.18` **forms.py deepcopy** — prevents LLM mutation.
✅ `.19` **pipeline_engine plan→verify** — explicit return.
✅ `.20` **roles_processor path validation** — symlink escape check.
✅ `.21` **todo_id validation** — regex in approve_commit + create_baseline.
✅ `.22` **signal_watch worker log chmod** — 0o600.
✅ `.23` **state.py _load_persisted** — filters expired/submitted.
✅ `.26` **agent_stage handoff** — sort by numeric ID, not mtime.
✅ `.27` **plan_checkpoint TOCTOU** — port=0, OS assigns free port.
✅ `.28` **forms.py template whitelist** — project-setup, increment-planning, ack.
ℹ️ `.24` **[MED] CSRF token** — закрыт как deferred (2026-09, AUD13-07): в `http_endpoint.py`
   есть origin-проверка (A2, `_check_url` + ALLOWED_HOSTS) поверх 127.0.0.1-binding;
   `True` остаётся только для curl-кейса без Origin/Referer (backward compat).
   По решению аудита §7 origin-проверки достаточно; token не вводим.
   (ID-коллизия с ТИРАЖ .24 снята: секции различаются заголовками.)
✅ `.32` **Coverage критических путей** — закрыт (2026-09): CI-гейты ≥80%
   (core: `tests/unit tests/negative tests/integration tests/e2e --cov-fail-under=80`,
   plugin: `tests/agent_workflow_ui/ --cov-fail-under=80`); на момент закрытия
   core TOTAL 88% (plan_checkpoint 87%, verify/wait_event/setup — все ≥80%).

P3:
✅ `.33` **orchestrator int(cli_timeout)** — try/except.
✅ `.34` **signals.py _short_id** — removed alias.
✅ `.35` **commit_gate APPROVE_TIMEOUT** — lazy _get_approve_timeout().
✅ `.36` **transitions unknown signal** — escalate (was dead-end stop).
✅ `.37` **Test quality** — as_dict content check, log_tail proper test.
ℹ️ `.38` **Accepted** — with-block close is standard pattern, child fd inherited at fork.
✅ `.39` **FIXED** — distinguishes null vs missing key.
✅ `.40` **FIXED** — requires markdown context (bold, checkbox, start-of-line).
✅ `.41` **FIXED** — timestamped backups, keep last 3.
✅ `.42` **FIXED** — reject whitespace-only .md (was accepted).
✅ `.43` **FIXED** — duplicate removed.
ℹ️ `.44` **_atomic.py cross-filesystem** — accepted (mkstemp(dir=...) гарантирует same FS).
ℹ️ `.45` **cmd_baseline depends on git binary** — by design (git is prerequisite).


### NEG-2026-09 · слои 1–2 и находки (закрыто)

#### Слой 1 — матрица отказов воркера (`tests/negative/test_worker_failure_matrix.py`) ✅

| Сценарий | Ожидаемая реакция awf | Статус |
|---|---|---|
| Тихий выход ×2, затем DONE | 2 авторетрая с push → успех | ✅ |
| Тихий выход с реальным diff | без ретрая → salvage | ✅ |
| Stale-сигнал от прошлой попытки | не считается успехом | ✅ |
| Крэш (exit≠0) / зависание | hard stop | ✅ |
| DONE в окне ожидания | успех, без ретрая | ✅ |
| DONE в зазоре перед cleanup | сигнал не затирается | ✅ FIX |
| Отказ с фантомной rollback-целью | эскалация вместо stop | ✅ FIX |

⬜ **Вопрос к дизайну:** крэш/зависание сейчас — hard stop без salvage-записки,
супервизор узнаёт только из статуса. Авторетраить или писать заметку —
решить до включения.

#### Слой 2 — матрицы решений (`tests/negative/test_transition_matrix.py`) ✅

| Что | Проверка | Статус |
|---|---|---|
| `resolve_transition`: 8 политик × 9 сигналов | валидные действия, цель только у rollback | ✅ |
| Неизвестный сигнал | всегда escalate (P3-гарантия) | ✅ |
| `signal_type`: 23 мусорных имени | тотальность + детерминизм | ✅ |
| Множественные сигналы | побеждает свежий (mtime) | ✅ |
| `.ready` + пустой `.md` | не сигнал | ✅ |
| `clean_stage_signals` | не трогает чужие префиксы, идемпотентен | ✅ |
| Rollback-цели в pipeline.yaml | warning при загрузке | ✅ |
| Генератор пайплайна | явные безопасные политики | ✅ FIX |

#### Находки прогона

✅ **NEG-1 · TOCTOU: сигнал затирается в retry-cleanup.** Между таймаутом
ожидания и `clean_stage_signals` мог лечь валидный DONE — очистка его
удаляла, стадия перезапускалась впустую, а при крэше следующей попытки
сигнал терялся. Фикс: повторное чтение сигналов перед очисткой
(`pipeline_engine.py`).

✅ **NEG-2 · Фантомная rollback-цель по умолчанию.** `on_rejected/on_failed`
по умолчанию указывали на стадию `implement`, которой нет в сгенерированных
пайплайнах (стадии названы по ролям: `agent-implementer`). Любой
REVIEW-REJECTED / TEST-FAILED → «Rollback target 'implement' not found» →
жёсткая остановка посреди прогона. Фикс: дефолт `escalate`, генератор пишет
явные политики, loader предупреждает о несуществующих целях, диспетчер при
фантомной цели эскалирует вместо остановки.

✅ **NEG-3 · Флейк `test_maybe_commit_auto_accepts_ack_signal_bd17`.**
Тест патчил глобальный `time.sleep` и требовал пустой список вызовов, но
`git`-команды идут через `subprocess.run(..., timeout=...)`, а CPython
внутри `Popen.wait(timeout)` busy-wait'ит микро-sleep'ами (1 µs → ×2 → кап
50 мс). На нагруженной машине git живёт дольше → в список попадают десятки
чужих снов → ложное падение (2 раза за день, оба под нагрузкой). Фикс:
проверяем отсутствие именно `APPROVE_POLL_INTERVAL` (2 с) — сабпроцессные
микро-sleep'ы до 50 мс его не имитируют. Механизм доказан демо-скриптом:
запись `[0.001, 0.002, 0.004, ..., 0.05]`.


### NEG-2026-09-18 · День 2: сигналы, auto-DONE, восстановление после BLOCKED

**Source:** `awf-bug-report-day2-signals.md` (TODO-0009, topic-trainer).

✅ **DAY2-1 · Auto-DONE на чужом диффе (критично).** Реальный механизм
инцидента: `attempt_auto_done` видел НЕПУСТОЙ дифф (2 строки, оставленные
QA-стадией) и «работа + verify зелёный» → синтезировал DONE для стадии,
чей воркер не написал ни строки. Пайплайн «прошёл» стадию реализации
с пустым результатом. Версия репорта про «старый сигнал» не подтвердилась:
файл стадии 1 чистился при старте стадии 2.

Фикс: `verify.work_fingerprint` — хэш `git diff <baseline>` + untracked
(минус baseline-снапшот). Снимок на входе в стадию, сравнение после
прогона: auto-DONE и F7-ретрай смотрят только на работу ЭТОЙ стадии.
Плюс `NEG-4`: потребление `.ready` на переходе (`DONE.ready` удаляется,
`DONE.md` остаётся как улика) — одно имя больше не валидно для всех
последующих стадий.

✅ **DAY2-2 · Replan-ожидание самоудовлетворялось (критично).**
Orphan-pickup (UX «создал TODO → awf start») работал и в replan: мгновенно
«завершал» ожидание тем самым TODO, который ретраился, а `_find_active_todo`
его отфильтровывал (BLOCKED) → «Supervisor did not create a new TODO.
Stopping.» за секунду. ACK супервизора потом читать было некому.

Фикс: для replan принимаются только сигналы, СОЗДАННЫЕ ПОСЛЕ эскалации
(mtime), плюс ACK/APPROVE текущего TODO. `_handle_escalate` по ACK:
`_unblock_todo` (BLOCKED → `.agentic/context/`) и ретрай той же стадии.

✅ **DAY2-3 · `awf continue` не оживлял blocked TODO.** Закрытый ACK/BLOCKED
TODO не виден `newest_active`, CLI рано печатал «No active TODO found»,
а `--from-stage` и ACK-файл не помогали. Плюс ACK, написанный после смерти
процесса, был тупиком.

Фикс: `continue` резолвит pending-закрытие — потребляет ACK/APPROVE
(только когда TODO реально закрыт: APPROVE живого verify не трогается),
переносит BLOCKED в context/, резюмирует с `state.stage_name`. Без ответа —
внятная инструкция вместо тупика. Новый флаг: `awf continue --ack TODO-NNNN`
(api + CLI + MCP `awf_continue(ack=...)`). Убран преждевременный CLI-пречек.

✅ **DAY2-4 · `awf_retry_stage` сломан** — уже исправлен вчера (fbe61b7);
проверено в живом окружении плагина: `awf.api.retry_stage` доступен.

**Тесты:** +17 негативных сценариев (`tests/negative/test_escalation_recovery.py`,
`TestAutoDoneScope` в `test_worker_failure_matrix.py`). Всего 1221, ruff чист.


### DAY4-2026-09-19 · Дашборд волна 4 + спека-2 (N1/N2)

**Источники:** живые наблюдения владельца на борде (TODO-0013) и
`awf-improvement-spec-2.md`.

✅ **Чат агентов переработан.** Активная роль — сверху, с «печатающими»
точками, стартовым временем и живым таймером; завершённые — ниже, новое
выше старого, у каждой записи `начало → конец · длительность` (из run-scoped
интервалов стадий); коннектор «эстафета от …» между записями; активная
verify-запись подсвечена. Обновление — на переходах (ключ role|rev).
✅ **Elapsed — единый формат** `1d 23h 54m` / `5h 12m` / `12m 30s` / `45s`
(сервер и JS-тикер; раньше JS показывал «2865m», сервер «47h»).
✅ **Вкладка «🎯 Итерация»**: сводка задачи одним абзацем для человека
(первый не-заголовочный абзац TODO, без markdown), ниже diff-stat, полный
агентский текст — под `<details>`.
✅ **Verify — невозможно пропустить:** пульсирующий баннер на всю ширину
(«VERIFY — ждёт решения супервизора по TODO-NNNN») + подсветка записи в
чате + прежние уведомление/бейдж.
✅ **Мелкие факты:** worker-панель показывает Read/Write MB; события
«⏸ Checkpoint auto-approved» больше не выглядят как «нужен человек»;
вместо «PID X not in /proc» — понятное «No worker running».

**SPEC-2 (N1):** permission-профиль воркера — `external_directory`:
`*: deny`, `/tmp/opencode/**`, `/tmp/pytest-*`, `/tmp/pytest-*/**`: allow.
`opencode_agents.apply` добавляет/мёржит профиль при инициализации; профиль
применён к текущему `~/.config/opencode/opencode.json` (бэкап рядом).
Головастик-«ask» в headless больше не тупик: санкционированные пути
разрешены, остальное — мгновенный deny.
**SPEC-2 (N2):** блок WORKSPACE DISCIPLINE в промпте каждого воркера:
temp-файлы — только `/tmp/opencode/**` или `tmp_path`; живые прогоны
внешних процессов — только по явному требованию задачи.

**Day-4 (доп.):** `diff_stat` теперь видит новые untracked-файлы
(живой случай: `scripts/` и `tests/test_smoke_c3.py` были невидимы на
verify) — с учётом baseline-снапшота.

**Тесты:** +14 (чат-записи, спаны, сводка, формат, события, untracked,
permission-профиль, канон в промпте). Всего 1287+, ruff чист.


### DAY3-2026-09-18 · Ревью дашборда (волна 1)

**Source:** разбор кода + реальный проект topic-trainer (папка `handoff/`).

✅ **1. Чат-свалка handoff'ов.** `_read_handoffs` читал весь каталог: файлы
   старых TODO и легаси-имена агентов показывались как текущие. Причины: awf
   сам учил агентов писать `{role}.md` (`agent_stage.py`), агенты писали
   `-final`-варианты, архивация брала только `*-{todo}.md`. Фикс: инструкция
   переведена на PROGRESS/DONE-заметки (handoff собирает awf), архивация
   ловит `*-{todo}.md` и `*-{todo}-*.md` двумя точными глобами, дашборд
   фильтрует по текущему TODO и дедуплицирует роли (канон побеждает).
✅ **2. Чат не обновлялся** при перезаписи handoff'а или смене TODO с теми же
   длительностями. Ключ перерисовки теперь `role|rev` (mtime + размер).
✅ **3. Порт переживает рестарт:** оркестратор переиспользует прежний порт из
   `state/dashboard_port` (fallback на случайный, если занят) — старая вкладка
   оживает сама вместо «Pipeline exited».
✅ **4. Timeline: реальные commit sha** одним `git log` (поиск
   `awf(verify): TODO-NNNN`); раньше искался несуществующий
   `done/{id}/BASELINE.sha` — tooltip'ы были всегда пустыми.
✅ **5. XSS:** markdown-исходники (TODO/handoff) экранируются до рендера;
   `<` в embedded INITIAL_STATE JSON экранируется (`\u003c`).
✅ **6. CORS `*` снят** с `/api/state` (страница отдаётся с того же origin).

✅ **Волна 2 (сделано).**
- `elapsed_frozen` рендерится в нижнем регистре (`'true'`), JS больше не
  пропускает заморозку до первого поллинга.
- Initial paint: страница делает немедленный `poll()` сразу после первой
  отрисовки — встроенный снапшот может быть на стадию старым.
- Notifications просятся на первом жесте (клик/клавиша) — Chrome молча
  отказывает автоматическим запросам.
- Чат автоскроллит вниз только если пользователь уже был у нижней кромки.
- `poll()` считает HTTP 500 сбоем (не только сетевые ошибки).
- Worker PID: выбирается ребёнок с `opencode` в cmdline (фолбэк — первый);
  раньше панель могла показать мёртвый чужой PID.
- **Handoff по стадии, а не по роли** (`{stage}-{todo}.md`): два QA-этапа
  больше не перезаписывают handoff друг друга, длительности в чате
  совпадают со стадией; для файлов до перехода — легаси-фолбэк по роли.
- **Структурный рефактор:** `generate_dashboard` строит Jinja-контекст из
  одного источника (`generate_state_dict`) — удалено ~180 строк дублирующей
  логики (статус/elapsed/handoffs считались дважды и уже расходились).


### SPEC A-run · Автономный забег (v1) — сделан

**Source:** `awf-autonomous-run-spec.md` (супервизор topic-trainer). Владелец
выбрал вариант (а): супервизор-чат в цикле ожидания, awf даёт механику.

✅ **A-run.1** — состояние забега `.agentic/state/run.yaml` (`awf/run_state.py`),
   `awf_run_start/status/next/finish` (api + MCP), статус в `awf_status.run_state`,
   чип «🏃 забег 2/3 · ~40м» в дашборде.
✅ **A-run.2** — цикл через `awf_wait_for_event`: `suggested_timeout` (медиана
   длительностей стадий / 3, кламп [60,300]), `actionable_only` (не будить на
   stage_changed), next_action забега в каждом событии; verify-payload несёт
   diff-stat против baseline.
✅ **A-run.3/A-run.8** — evidence-gate: в забеге `awf_approve` без evidence
   отклоняется; evidence → `RUN-EVIDENCE-{todo}.md`, отчёт ссылается на него.
✅ **A-run.4** — механические гейты: queue exhausted, бюджет, stop-флаги
   (манифест очереди), два reject'а, `rollback(hard)` в забеге запрещён.
✅ **A-run.5** — reject-счётчик: второй отказ по TODO останавливает забег.
✅ **A-run.6** — `RUN-REPORT-{ts}.md` в outbox: очередь, завершённые, отказы,
   дневник вердиктов, health (salvage-события).
✅ **A-run.7** — состояние переживает смерть процесса (run.yaml, awf_run_status).
⬜ Не сделано сознательно: «вероятность мусора» в callback (эвристика);
   авто-стоп «verify красный дважды» (перекрыт reject-лимитом и stop-флагами).


✅ **SPEC-2026-09-18 · Спека супервизора topic-trainer — взятое в работу.**
- **D9**: дизамбигуация ролей обновляется автоматически после сборки
  пайплайна (`apply_project_setup` → `analyze_roles_core`); тесты: первое
  наполнение + пересборка со сменой позиций (FIRST/LAST agent).
- **Валидация ролей** при загрузке `pipeline.yaml`: warning, если роль
  не резолвится в .md (project/global) или пустая — опечатка больше не
  даёт «стадию без роли» молча.
- **Бонус-находка**: кастомные роли с заглавных/пробелов («Auditor»,
  «My Agent») сохранялись как slug-файлы (`auditor.md`), а в пайплайн
  писались сырыми — рантайм бы упал на «role file not found». Сборка
  пайплайна теперь slug-нормализует роли.
- **Dashboard**: `todo_diff_stat` (`git diff --stat` vs baseline) в
  `/api/state` и в панели «Задача» — одно место вместо ручного diff.
- **`continue` и REVIEW**: лежащий REVIEW-файл больше не тупик — в ответе
  подсказка «REVIEW для TODO-NNNN ждёт: доработай TODO / `--ack`».
- D8 (роли ≠ стадии пайплайна) — отложено решением владельца.


### NEG-2026-09-19 · Воркер: пустой «ask» — РЕШЕНО

**Status:** DONE в DAY4 (SPEC-2 N1): permission-профиль + канон в промпте.

⬜ **Проблема:** воркер awf пробует писать во внешний путь
`/tmp/c3-test-abort/*`; opencode дважды за 7 секунд эмитит
`permission=external_directory action=ask` (05:25:01, 05:25:08), но у
воркера `question` запрещён — «ask» в пустоту: вопрос никто не прочитает,
воркер тратит циклы на повторные пробы и обходы (повезло, что модель
догадалась обойти сама).

⬜ **Фикс:** в профиле запуска воркера awf явно разрешать
`external_directory` для санкционированных временных путей
(`/tmp/opencode/**`, `/tmp/pytest-*`). Одна строка в permission-конфиге
воркера (`agent_stage.py` / профиль worker'а).

⬜ **Бонус:** в инструкцию воркера добавить строку «временные файлы —
`/tmp/opencode` или pytest tmp_path», чтобы роли не изобретали
собственные /tmp-пути.


### ТИРАЖ-2026-08-11 · пункты 1–18, 20 (сделано в v1.0.0)

⬜ `.1` **Удалить `qa-audit-*.md` из root** — мусорный файл, подрывает впечатление.
   Добавить `qa-audit-*` в .gitignore.

⬜ `.2` **Починить badges** — `tests` badge ведёт на `#`. Заменить на shields.io
   CI badge: `https://github.com/EnerJizeIT/agentic-workflow/actions/workflows/test.yml/badge.svg`.

⬜ `.3` **Дополнить pyproject.toml metadata** — classifiers, keywords, project.urls
   (Homepage, Repository, Issues). Для PyPI и поиска.

⬜ `.4` **Requirements/Compatibility секция в README** — Python 3.10+, opencode,
   OS (Linux tested, macOS probably, Windows unknown), модели (model-agnostic,
   tested Qwen vLLM, должно работать с Claude/GPT).

⬜ `.5` **Quick Start секция в README** — 5-минутный блок: install → configure →
   first pipeline. Один copy-paste блок.

⬜ `.6` **Troubleshooting секция в USAGE** — pipeline завис (как сбросить state),
   порт занят, orphan TODO, commit failed.

⬜ `.7` **Limitations секция в USAGE** — средние затраты токенов (~2M per session),
   pipeline depth tested (5 stages), single-machine (не distributed).

⬜ `.8` **Comparison table в README** — awf vs Aider vs Claude Code vs Devin.
   Планирование, human-in-the-loop, кастомные пайплайны, MCP-native, self-hosted.

⬜ `.9` **Пример pipeline.yaml + role в USAGE** — показать как создать кастомный
   пайплайн (1 stage, 3 stages, 5 stages) + пример файла роли.

⬜ `.10` **"Dogfood" → "Real-world results"** — жаргон, не все поймут. Везде в README.

⬜ `.11` **GitHub topics/tags + social preview** — `opencode`, `mcp`, `ai-agent`,
   `pipeline`, `multi-agent`, `orchestrator`, `developer-tools`. + social preview
   image (Settings → Social preview).

⬜ `.12` **ASCII → Mermaid диаграммы** — GitHub рендерит нативно. В README и
   architecture.md.

⬜ `.13` **CONTRIBUTING.md** — dev setup, тесты, линтеры, PR process, стиль коммитов.

⬜ `.14` **CHANGELOG.md** — Keep a Changelog формат. Начать с v1.0.0.

⬜ `.15` **GitHub Release v1.0.0** — description: SMO, 29 tools, dashboard v2,
   6 dogfood sessions.

⬜ `.16` **SECURITY.md** — базовая политика (для pet-проекта).

⬜ `.17` **Issue templates** — `.github/ISSUE_TEMPLATE/`: bug_report, feature_request.

⬜ `.18` **Roadmap секция в README** — краткий список (3-5 пунктов) что планируется.
   Упомянули Qwen + Kimi. Отдельно от BACKLOG.md (который — полный список).
   Например: SMO.7 escape-hatch'и, coverage critical paths, PyPI, GitHub Pages.


---

## 🗄 2026-09-19 · A-run safety (NEG-2026-09-19)

Bugs from the first real run (`awf-bug-report-arun-home.md`) — all fixed
red-first in `tests/negative/test_run_queue_safety.py` (11 tests).

✅ **R1** — queue order: the pipeline picked "newest active TODO"; now
   `start_pipeline(todo_id=)` / `awf_run_next` pin the queue item end-to-end.
✅ **R2/R2a** — `_reconcile` archived queued TODOs (with their `.ready`) at
   pipeline start; reconcile is now warn-only and never archives — archiving
   is exclusively the verify/approve flow's right.
✅ **A1** — ghost run in `$HOME/.agentic`: `run_*` refuse a dir without
   `.agentic/config.yaml`, echo the path; the start message carries the root.
✅ **A2** — "missing or empty" now names the exact path it looked in.
✅ **A3/R4** — position shows the RUNNING item (1/4, not 2/4).
✅ **A4/R3** — `wait_for_event` clamps the transport-unsafe timeout (≤600s)
   and flags `timeout_clamped`; opencode.json MCP entry got `"timeout": 600000`.
✅ **A5/R5** — run note: `awf_run_start(note=)` / `awf_run_note`; rendered in
   the run chip and the Итерация tab.
✅ **A6** — `run_start(force=true)` replaces a stale/wrong-dir run;
   `project_root` stored in run state.
✅ **Safety net** — `awf_restore` / `awf restore TODO-NNNN` brings an archived
   TODO back (md + ready + handoffs).
✅ **Coverage** — core awf had NO CI floor; gate added (≥80%, now 83%).
   CLI entry points got real smoke tests (cmd_init was 0%).
⬜ Открыто: heartbeat для wait_for_event (R3-в), CLI `awf run`, покрытие
   cmd_analyze_roles/cmd_approve.


---

## 🗄 2026-09-19 · QA-долг закрыт (NEG-2026-09-19)

✅ **QA .14 PID reuse TOCTOU** — identity-проверка ПЕРВОЙ (`_read_pid_cmdline`),
   liveness второй; unsafe-фолбэк «assume ours» убран. Тесты:
   `TestPidReuseDefense`.
✅ **QA .15 threading cleanup** — единственный оставшийся поток в
   `test_signals.py` уже с `join(timeout=2)`; в `test_plan_checkpoint.py`
   потоков нет (пункт был устаревшим).
✅ **QA .25 Disk I/O under lock** — `FormRegistry`: сериализация под локом
   (`_serialize`), запись ПОСЛЕ (`_write_payload`); тест проверяет
   `lock.locked() is False` во время записи.
✅ **QA .29 in-memory only** — добавлены file-based тесты claim/finalize с
   проверкой персистентности по файлу.
✅ **QA .30 verify edge cases** — partial failure со short-circuit, all-pass,
   missing binary (ABORTED в логе).
✅ **QA .31 full pipeline flow** — покрыт e2e (`test_pipeline_e2e.py`
   happy path: plan → worker → verify → commit → archive) + CLI smoke.


---

## RUN2–RUN10 · Программа 2026-09-22/24 (закрытая история)

**Решение владельца (24.09): в BACKLOG только ожидающая работа; закрытое живёт здесь.**

### RUN10-2026-09-24 · Пакетные мини-фиксы (6 отчётов topic-trainer + 3 предохранителя)

**Status:** закрыто 24.09 (5 юнитов очереди + 1 перевыдача; 1 отклонение → 0076; 0 салважей). Группировка по решению владельца: мини-фиксы пакуем, не гоним по одному циклу.

✅ **0071 · Подсказки внутри забега** — wait/approve в активном забеге зовут продолжить цикл (`awf_run_next`), а не «ждать пользователя»; фаза при активном забеге — `run`; предохранитель: ложный `done` после смерти пайплайна в забеге исключён (`_run_allows_done`). `13b6d14`
✅ **0072 · Честный потолок + фидбек** — совет называет точную ручку (`wait.cap_seconds: <T-30>` / `AWF_WAIT_CAP`), не «подними mcp timeout»; `suggested_timeout` не залипает на 55 (0.9·cap); фидбек без пустых заголовков + флаги `--expected/--got/--why/--proposal`. `07d0f38`
✅ **0073 → 0076 · Метрики по проекту** — первый фильтр (по `session.directory`) отклонён: у всех воркеров `directory=$HOME`, дискриминатора нет. Перевыдача: сопоставление через содержимое `part.data` (LIKE-предфильтр + точная сверка); живой догфуд: наши 32.5M токенов вернулись, 114 чужих сессий исключены с честным предупреждением. `339315b` (0073-атрибуция) + `a6c17ad`
✅ **0074 · Untracked до baseline** — dispatch перечисляет pre-existing untracked (не попадут в коммит); `include_untracked=[...]` включает их (запись `BASELINE-<id>.include`); verify-pack показывает исключённые. `339315b`
✅ **0075 · Предохранители kill** — `caller_ancestry`: kill отказывает, если вызван изнутри своего пайплайна; повторный сбор late-воркеров после TERM пайплайна (добивание, reparented ppid=init). `785010d`

### RUN9-2026-09-23 · Pre-release мелочи (перед релизом 1.3.0)

**Status:** закрыто 23.09 забегом RUN9 (6 юнитов, 0 отказов; одна пауза по флапу сети — движок пережил её сам, U6a; один инцидент — воркер убил свой пайплайн догфудом, см. секцию инцидента).

✅ **CLI `kill`** — рекавери без MCP (тот же API, что у тула; rc-карта; защита идентичности процесса). `c4f59a7`
✅ **Консольная команда `awf`** — `[project.scripts]` в колесе + смоук в CI package-job; установка в чистом venv проверена. `408da8d`
✅ **Фланг foreground+`no_checkpoints`** — run-флаг пробрасывается в guard запуска; ложный отказ убран. `018f16c`
✅ **Флейк salvage-теста** — устранена настоящая гонка freshness-гейта (сигнал пишется после старта ожидания). `8db9747`
✅ **Кириллица в slugify** — одна реализация в ядре, плагин делегирует; полный паритет. `ef48b53`
✅ **Атомарная ротация логов** — уникальные архивы `<name>.<stamp>-<pid>` + prune; двух ротаторов не теряют строки. `ae3c49a`

### Инцидент RUN9 (23.09): воркер убил свой пайплайн + ложный `done` после смерти

✅ **Предохранитель: `awf kill` отказывает изнутри пайплайна.** Закрыто RUN10 #5 (`785010d`): `caller_ancestry` (цепочка ppid из /proc) — отказ с текстом; деградация без /proc — пометка в ответе. Инцидент: воркер 0065 убил свой пайплайн догфудом.
✅ **Ложный `done` после смерти пайплайна в активном забеге.** Закрыто RUN10 #1 (`13b6d14`): `_run_allows_done` — done только если current-элемент finished; иначе death → idle, без done за прошлый цикл.
✅ **Гонка в kill: воркер, заспавненный в момент kill, выживает.** Закрыто RUN10 #5 (`785010d`): повторный сбор late-воркеров после TERM пайплайна (включая reparented ppid=init) + добивание; ответ называет late worker.

### Модели: model-agnosticism + эксперимент с другими воркерами (решение владельца 23.09)

⬜ **awf работает идентично с любой моделью из конфигурации** — продуктовое требование (у другого пользователя awf будут совсем другие модели). Механика есть (`models:` по ролям + `awf_check_model_config`), жёстких завязок на vllm в коде нет; **живьём на не-локальной модели не проверялось**. Приёмка: end-to-end юнит на другой модели без правок кода; отчёт — что отличалось.
⬜ **Эксперимент: следующий аудит topic-trainer на других воркерах** — супервизор фиксирует (протокол от awf-супервизора): модели по ролям; отличия поведения (ошибки команд, парсинг, таймауты, инструменты); где проверка пропустила/поймала; ретраи и на какой модели починилось. Фидбек — через `awf feedback` (bug/feature на стол), триаж — в этот бэклог.

ℹ️ **Идея без плана: лестница эскалации моделей внутри юнита** (провал → следующая модель). Данных нет, предпосылок не было (отклонённые юниты успешно переделывала та же модель). Возвращаться ТОЛЬКО по данным эксперимента.

### RUN8-2026-09-23 · Восстановление не теряет и не путает единицы (отчёты topic-trainer)

**Status:** закрыто 23.09 забегом RUN8 (2 юнита, 0 салважей, ~90 мин, `no_checkpoints`). Класс «пути восстановления пинят СВОЮ единицу» закрыт полностью.

✅ **#1 `awf_continue --ack` пинит единицу из ack** — приоритет резолва: явный `todo_id` > ack > state > newest_active; ACK потребляется штатно (не остаётся stale-closure); паритет `todo_id` у continue. `88d1f2b`
✅ **#2 `awf_kill` гасит воркер стадии** — `kill_pid_tree`: pid<=1 → skipped (guard инцидента), группа только для своего лидера (`getpgid==pid`), TERM→grace→KILL, «survived» → предупреждение с pid; воркеры убиваются явно при kill/retry/continue; осиротевший воркер предупреждается при следующем старте. `ba23dfb`

### RUN7-2026-09-23 · Супервизор-фокус: хвосты

✅ Ложный `done` на остатках salvage — `5025961`; `retry_stage` пинит salvage-единицу — `10059fc`. (Закрыто 23.09.)

### RUN6-2026-09-23 · Супервизор-фокус: фидбек с аудита topic-trainer + дашборд

**Status:** закрыто 23.09 забегом RUN6 (5 юнитов, 0 салважей, ~200 мин, `no_checkpoints`). Принцип владельца: «супервизор не думает об инструменте».
**Источник:** 4 отчёта `awf feedback` (22.09) + наблюдение владельца по дашборду.

✅ **#1 `done`-событие после approve** — wait_for_event отдаёт `done` с точной следующей командой (state очищен/phase=done + мёртвый pid + коммит/архив); супервизор больше не смотрит git log. `922c188`
✅ **#7 BUG дашборд «путал роли»** — движок пишет имя разрешённого пайплайна в pipeline_state; дашборд рисует стадии фактического TODO (state → очередь забега → front-matter → дефолт) и показывает имя чипом. `716e29a`
✅ **#2+#3 Тихий status + честный cap** — в забеге очередь «ждёт хода» (без «rollback or reset»); потолок ожидания из `wait.cap_seconds`/`AWF_WAIT_CAP` (наш: 300), suggested_timeout адаптивен до потолка, устаревший совет убран. `eb9fd9e`
✅ **#4+#5 `awf todo-update` + MCP `awf_tree_sha`** — правка TODO с сохранением номера (бэкап, отказ на стартовавшем); tree-sha в типизированном контуре (MCP == CLI). `b9332b8`
✅ **#6 Онбординг без подсказок** — бриф: Defaults + Scenarios + ссылка USAGE; `agents_md.py` (единый источник блока в глобальном AGENTS.md, «начни с awf_brief», самолечение при старте плагина); глобальный SKILL `awf-supervisor` (роль/цикл/ритуалы/карта/5 сценариев); описания всех тулов и next_action-свип под тестами. `2407f27`

✅ **Хвост RUN6 (из QA 0056): ложный `done` после kill на остатках salvage** — закрыт RUN7: `_state_is_exited` (exited = state отсутствует ИЛИ `phase=done`; kill-остаток со `salvage_count` → старое ожидание, без `done` за прошлый цикл). `5025961`

### RUN5-2026-09-22 · leak-гейт + todo-retire + релиз 1.2.0

**Status:** закрыто 22.09 забегом RUN5 (3 юнита, 1 сетевой салваж, 271 мин). Релиз 1.2.0 опубликован (PyPI: awf + agent-workflow-ui, установка из чистого venv проверена).

✅ **#1 Leak-гейт** — `REJECT-<todo>.files` при отклонении, `carry_over_from` при перевыдаче (пути исключаются из baseline → коммит их включает), WARNING в verify-pack при «утечке»; QA починил high-дефект (`run_next` съедал carry-over при пере-базелиновании). `a9b16aa`
✅ **#2 `awf todo-retire`** — архив отклонённого/брошенного активного TODO в `done/<id>/` с RETIRED-заметкой; наш призрак TODO-0035 убран этим инструментом. `2c37d3f`
✅ **#3 Релиз 1.2.0** — версии в 5 пиннингах, CHANGELOG [1.2.0] (14 пунктов), счётчики 45; PR #6 влит (`683e5f4`), тег `v1.2.0`. `9aa074a`

✅ **БАГ: `retry_stage` не закреплял TODO** — закрыт RUN7: salvage-состояние читается ДО kill, явный пин уходит в `continue_pipeline` («wins over both»), ответ называет перезапущённую единицу; без salvage — прежнее поведение + WARNING. `10059fc`

✅ Из практики: `awf kill` добавлен и в CLI (RUN9 #1, `c4f59a7`) — рекавери при недоступном MCP больше не требует ручного поиска PID.

✅ **Мелочь упаковки: консольная команда `awf`** — `[project.scripts] awf = "awf.cli:main"` в колесе + смоук в CI package-job; установка в чистом venv проверена (RUN9 #2, `408da8d`).

### RUN4-2026-09-22 · `awf brief` + фидбек-контур супервизора (решения владельца 22.09)

**Status:** закрыто 22.09 забегом RUN4 (2 юнита, 0 салважей, 31 мин).

✅ **#1 `awf brief`** — карточка погружения/восстановления (CLI+MCP): шапка (версия/проект/фаза), «что дальше», состояние (забег/бюджет/`no_checkpoints`/активные/флаги), **карта всех инструментов по ситуациям** (данные `awf/data/tool_map.yaml`, покрытие проверяется тестом в обе стороны), ритуалы, рецепты восстановления (`awf/data/recovery.md`) + доктрина проекта, «что нового» из CHANGELOG, фидбек-строка; ≤900 слов, детерминирован. `8df1b0d`
✅ **#2 Фидбек-контур** — `awf feedback --type bug|feature` (CLI+MCP): структурированный отчёт на рабочий стол (`feedback.dir`, факты собираются сами, транслит-слаг, суффикс при повторе, env не читается); строка-приглашение в промптах супервизора и USAGE. `ba3aa59`

✅ **Дыра гигиены (из фидбека 22.09): отклонённый TODO остаётся активным** — закрыто RUN5: `awf todo-retire` (архив в `done/` с причиной, без ложных сигналов); наш призрак TODO-0035 убран этим инструментом. `2c37d3f`

### RUN3-2026-09-22 · Отчёт супервизора topic-trainer — сценарий «аудит по доменам»

**Status:** закрыто 22.09 забегом RUN3 (6 юнитов, 0 салважей, 208 мин). Источник: `~/Desktop/awf-supervisor-report-2026-09-22.md`.

✅ **#1 Именованные пайплайны** — `awf pipeline-write <имя> --role …` (CLI+MCP): пишет только `.agentic/pipelines/<имя>.yaml`, config/supervisor не трогает; `awf pipelines` + запуск по имени с внятной ошибкой на неизвестное. `2018003`
✅ **#2 Пайплайн на элемент очереди забега** — очередь принимает `{todo_id, pipeline}` (строки — совместимость, старые state читаются), `awf_dispatch_todo(pipeline=…)` пишет front-matter, `run_next` пробрасывает. `ad640b9`
✅ **#3 Роль из глобального скилла** — `awf add-role <роль> --from-skill <скилл>`: тело SKILL.md без YAML-шапки + провенанс; проектные скиллы затеняют глобальные; traversal закрыт. `b2ca7e9`
✅ **#4+#5 Гигиена состояния** — `awf unblock TODO-NNNN` (снимает stale BLOCKED/ACK, DONE неприкосновенен) + автоснятие при перевыдаче; `awf todo-remove` для не стартовавших (след в `done/`). `56af3d1`
✅ **#6 `no_checkpoints` на одиночный старт** — параметр у `start`/`continue` (CLI+MCP), процессный скоуп (env ребёнка), в config/state не пишется. `68771b7`
✅ **#7 `awf_current_step` и живой проект** — «живой» = настроен (пайплайн/роли, init-стаб не считается) И непустой `done/` → рабочая фаза вместо setup-ритуала. `2d71168`

ℹ️ Фланг foreground+no_checkpoints (из RUN2) остаётся открытым: в `run_next` флаг забега в guard не пробрасывается (через MCP недостижимо) — см. запись RUN2.

### RUN2-2026-09-22 · Баг-репорт из topic-trainer (забег 2, awf 1.1.0)

**Status:** закрыто 22.09 забегом RUN2 (3 юнита, 0 салважей). Источник: `~/Desktop/awf-bug-report-run2-checkpoints.md`.

✅ **B1+B4 · Чекпоинты забега** — флаг `no_checkpoints` (start→state→движок, уважается только активным забегом)
   + решение формы переживает смерть пайплайна (`context/CHECKPOINT-<todo>.json`, подхват без формы, `.consumed`).
   `f40b988`
✅ **B2 · Бюджет** — продуктивные минуты (elapsed − простой: чекпоинт-ожидание, salvage-обработка, net-backoff);
   status/brief/RUN-REPORT показывают оба числа. `bc7af08`
✅ **B3 · Потолок ожидания** — `TRANSPORT_CAP=55` в suggested_timeout, потолок + рецепт в `next_action`
   каждого ответа, заметки в USAGE (EN+RU). `db47cc0`
✅ **Право супервизора** — «replan/переписывание ТЗ/сплит без approve владельца; эскалация — только стоп-лист»
   в шаблонах supervisor.md и phase-run.md (обе копии) + USAGE (EN+RU). `db47cc0`

✅ **Фланг B4:** прямой API `run_next(background=False)` в активном забеге с `no_checkpoints` больше не получает ложный отказ — run-флаг пробрасывается в guard запуска (RUN9 #3, `018f16c`).

**Работает хорошо (не трогать):** очередь забега по порядку; `awf_run_note` на дашборде;
`awf_run_finish` (внятный RUN-REPORT); salvage/retire-stage (трижды спас забег);
commit-механика verify (15 коммитов без сбоев).

**Закрыто в программе аудита и RUN'ах:** подписи коммитов (пересборка, head ffa5e12), герметичность тестов (U7a), rollback-таймауты (FU-19), Actions Node20 (FU-21), `files:`/verify_pack/доки/ратчет-BACKLOG/DONE-пример/CI-дубль (FU-20/21); **кириллица в slugify** (RUN9 #5, `ef48b53`), **атомарная ротация логов** (RUN9 #6, `ae3c49a`), **флейк test_salvage_signal** (RUN9 #4, `8db9747`).
