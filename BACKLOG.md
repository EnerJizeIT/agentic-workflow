# BACKLOG

> План развития. Основан на [Product Vision](vision/agent-ui-plugin.md) и [Architecture](vision/architecture.md). Каждый эпик декомпозируем в awf TODO при начале работы.

**Текущее состояние:** awf v0.4.0 + agent-workflow-ui v0.1.0 стабильны. 927 тестов (e2e + unit + integration + plugin), CI green на Python 3.10/3.11/3.12, coverage 90%+ на plugin. Все BD-* баги, A-* архитектурные долги, D1-D5 + M1-M5 отчёта glm-5.2, QA-report пробелы (BD-22/APPROVE timeout/H6 argv), BD-36 plan checkpoint и audit-v2 (HIGH verify.py bool bug) закрыты.

**Активный эпик:** MCP-MIGRATION — миграция awf из standalone CLI в pure MCP toolkit под opencode.

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

**[T2.8] `awf/opencode_agents.py:10-52` stringly-typed protocol `propose()`**
> Возвращает `"ERR:"`, `"NOTHING:"`, `"PROPOSE:"` — caller проверяет через
> `startswith`. Хрупко. Refactor: Enum или dataclass (`Proposal(type=..., detail=...)`).
> Severity: LOW (работает, но каждое новое состояние = риск забыть branch).

#### T1 — safe deletions (отложено, требуют инфраструктурных решений)

**[T1.5] `awf/orchestrator.py:__all__` re-exports 17 приватных имён**
> Audit предлагал удалить. Реально: tests импортируют
> `from awf.orchestrator import _foo` (white-box testing). Без `__all__`
> ruff `--fix` агрессивно удаляет эти re-exports (treats как unused).
> Решение: либо ruff `per-file-ignores` для orchestrator.py, либо
> оставить `__all__` как легитимный "white-box public surface" marker.
> Severity: LOW (не баг, cosmetic disagreement с audit).

**[T1.6] `awf/api/lifecycle.py:_reset_orphans` legacy one-shot path**
> Duplicate of `list_orphans` + `remove_orphans` two-step protocol.
> Сейчас `_reset_orphans` вызывается из `reset_runtime(orphans=True)`
> как fallback. Refactor: переписать `reset_runtime(orphans=True)` на
> вызов two-step protocol, удалить `_reset_orphans`.
> Severity: LOW (работает корректно, just dead-ish code).

#### T3 — рефакторинг (дни)

**[T3.1] Дубликат HTTP infrastructure**
> `awf/plan_checkpoint.py` (one-shot HTTP server) + `agent_workflow_ui/.../http_endpoint.py`
> (long-lived) — у каждого свой `_find_free_port`, свой `BaseHTTPRequestHandler`,
> свой ack-HTML. ~150 строк дубликата. Также **3 разные atomic-write реализации**:
> `awf._atomic.atomic_write_text`, `state._atomic_write_text` (уже объединён),
> `http_endpoint._atomic_write_yaml`.
> Решение: вынести `_find_free_port` в общий utils, при необходимости —
> общий `http_server` helper.
> Severity: MEDIUM (DRY violation, но два сервера обоснованно separate).

**[T3.2] `forms.py:open_form` 158 строк, SRP violation**
> Делает: registry lookup + project_dir validation + role scanning +
> model collection + template rendering + browser launch + record creation
> + cleanup_temp_files в одной функции.
> Разбить на `_prepare_form_data() → _render() → _launch_and_record()`.
> Severity: MEDIUM (читаемость, тестируемость).

**[T3.3] `orchestrator.py:run_pipeline` cyclomatic complexity 107**
> Уже разбит на `_handle_*` функции, но supervisor-stage block (375-436) и
> agent-stage block (438-507) стоит извлечь в `_handle_supervisor_stage()` /
> `_handle_agent_stage()`. Это опустит `run_pipeline` до ~80 строк и cyc~30.
> Severity: MEDIUM (поддержка, тестируемость).

**[T3.4] `_ZONES_OF_RESPONSIBILITY` extraction**
> Словарь 20+ пар (en + ru keywords) в `awf/api/roles.py`. Разросся.
> Кандидат на extraction в data-файл (`awf/data/role_zones.yaml`) или config.
> Особенно если будет support других языков.
> Severity: LOW (работает, просто растет).

**[T3.5] `opencode_config.py` hotspots**
> `scan_global_skills` (cyc=49), `scan_global_roles` (26), `_read_models_from_config` (18).
> Топ hotspot'ы plugin'а. Чтение opencode.json/skills перемешано с нормализацией.
> Разделить на `reader.py` (raw read) + `normalize.py` (filter + transform).
> Severity: MEDIUM (readability, future modifications).

#### T4 — архитектурные изменения (недели, design discussion)

**[T4.1] Pipeline state persistence (replace regex log parsing)**
> Сейчас `awf/api/context.py:_extract_stage_info` парсит `awf-start.out`
> через regex для восстановления pipeline state. Любой change в `print()`
> формате orchestrator ломает status detection без ошибки.
> Предлагаемое решение: после каждой stage transition orchestrator пишет
> `.agentic/state/current.yaml` (`{stage_idx, stage_name, stage_kind,
> started_at, last_signal, checkpoint_pending}`). `api.get_status` читает
> structured state, не логи.
> Эффект: debug проще, форматы логов свободны для изменения, тесты проще.
> **Требует design discussion** — это change в core flow.
> Severity: HIGH architectural (но не user-visible bug — текущая regex
> реализация работает пока формат логов не меняется).

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
| **4 · Long-running monitoring** | Dashboard rendering, `dashboard.html.j2`, meta-refresh. | Medium |
| **5 · Priority planning** | Drag-and-drop UI, новый тип template `priority-matrix.html.j2`. | High |
| **6 · Onboarding wizard** | Multi-form state, conditional logic между формами. | High |

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
