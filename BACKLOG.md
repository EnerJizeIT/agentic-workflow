# BACKLOG

> План развития. Основан на [Product Vision v0.4](vision/agent-ui-plugin.md) и [Architecture v1.1](vision/architecture.md). Каждый эпик декомпозируем в awf TODO при начале работы.

---

## ✅ Done

### awf v0.4.0 (текущий релиз)

- Wave 4: миграция bash→Python завершена. `lib/*.sh` удалены, `awf/` Python package = 22 модуля.
- `bin/awf` — thin wrapper через `os.path.realpath`. Closes [#1](https://github.com/EnerJizeIT/agentic-workflow/issues/1).
- 164 теста (12 E2E + 152 unit), CI на Python 3.10-3.12.
- README на русском, LICENSE, GitHub Actions.
- Vision и Architecture для `agent-workflow-ui` plugin'а зафиксированы.

### agent-workflow-ui v0.1.0 (MVP)

- 5 MCP tools: `open_form`, `read_submit`, `cancel_form`, `list_pending_forms`, `list_templates`.
- HTTP endpoint для form submits (атомарные YAML-записи) — **всегда включён**, не опциональный.
- Jinja2 rendering с YAML frontmatter + ChoiceLoader (project → defaults).
- **Composite template `project-setup`** (primary для MVP): context + ТЗ files + supervisor + team + models. Остальные 5 templates (`role-assignment`, `skill-picker`, `model-picker`, `pipeline-picker`, `conflict-resolver`) зарезервированы для будущих сценариев.
- Custom roles persistence (`~/.config/awf/roles/`): save/delete через `roles_processor.py` (вынесен из HTTP handler).
- Path traversal defense в `delete_custom_role` (отвергает `../`, `/`, `\`).
- Inline conflict resolution через JS `confirm()` перед перезаписью роли.
- **Lazy skill install** (через `skill_installer.py`) — SKILL.md автоматически копируется в `~/.config/opencode/skills/` при каждом старте plugin'а (idempotent). Заменяет изначально запланированный setuptools post-install hook.
- Cross-platform browser open.
- **147 тестов + 33 slugify cross-check cases = 180 тестов, 93% coverage**.
- SKILL.md с инструкцией для LLM (non-blocking pattern, agent-driven templates).
- Slugify Python↔JS cross-check test (гарантирует consistency conflict detection).
- `awf init` — prompt для plugin install.

### Прошлые волны (хронология)

- **v0.3.4** — weak-spots closure (find_active_todo, status warns, reset --orphans, init model prompt).
- **v0.3.5** — pytest E2E harness + mock opencode stub.
- **v0.3.6** — Wave 4a: `awf status` ported to Python.
- **v0.3.7** — Wave 4b: orchestrator ported (8 modules).
- **v0.3.8** — Wave 4c: 6 remaining commands ported.
- **v0.3.9** — Wave 4d-part1: 152 unit tests.
- **v0.4.0** — Wave 4d-part2: bash retired.

---

## ✅ Done: MVP — agent-workflow-ui plugin

**Goal:** реализовать [Сценарий 1 — Конструктор конфигурации](vision/agent-ui-plugin.md) из Product Vision. Пользователь начинает новый проект → открывается composite HTML-форма `project-setup` → submit → supervisor генерирует `.agentic/config.yaml` + roles.

**Scope:** [`vision/architecture.md`](vision/architecture.md) §12.1. Вне scope — dashboards, runtime forms, pipeline-declared forms, `awf-mcp`.

### Epic 1 · Package skeleton & infrastructure

Создать структуру `agent_workflow_ui/` как отдельный Python package внутри monorepo.

- [x] **1.1** Создать `agent_workflow_ui/` package с `__init__.py`, `__main__.py`, `pyproject.toml` (separate dist).
- [x] **1.2** Зависимости: `mcp>=1.0`, `jinja2>=3.1`, `pyyaml>=6.0`. Python ≥3.10 (требование mcp dep).
- [x] **1.3** `config.py` — чтение env vars (`AWF_INPUTS_DIR`, `AWF_TEMPLATES_DIR`, `AWF_HTTP_PORT`, ...), path resolution, idempotent directory creation.
- [x] **1.4** `state.py` — in-memory registry открытых форм (form_id → metadata). Form ID: `FORM-YYYYMMDDHHMMSS-XXXX` (timestamp + random).
- [x] **1.5** Запуск plugin'а локально: `python -m agent_workflow_ui` запускается без ошибок, логирует startup + lazy skill install.
- [x] **1.6** Workspace setup: root `pyproject.toml` без пакетов, dev-install через `pip install -e ./awf -e ./agent_workflow_ui`.

**Verify:** `python -m agent_workflow_ui --help` работает, `pip install -e ./agent_workflow_ui` succeeds.

### Epic 2 · MCP server (stdio transport)

Реализовать MCP server через official `mcp` SDK.

- [x] **2.1** `server.py` — MCP server lifecycle (start, register tools, handle requests, shutdown).
- [x] **2.2** Stdio transport (JSON-RPC over stdin/stdout).
- [x] **2.3** Tool registry: 5 tools зарегистрированы (`open_form`, `read_submit`, `cancel_form`, `list_pending_forms`, `list_templates`).
- [x] **2.4** Integration manual test: plugin подключён в opencode config, agent видит tools через `/mcp` или аналог.

**Verify:** при подключении в opencode agent видит 5 tools с корректными schemas.

### Epic 3 · HTTP endpoint

Localhost HTTP server для приёма form submits.

- [x] **3.1** `http_endpoint.py` — HTTP server на `127.0.0.1:AWF_HTTP_PORT` (default: auto-select free port).
- [x] **3.2** `POST /submit/<form_id>` — парсит form-encoded body, валидирует form_id, пишет `.agentic/inputs/<form_id>.yaml`.
- [x] **3.3** `GET /health` — health check.
- [x] **3.4** Limits: max body size 1MB, only POST on `/submit/<form_id>`, остальные запросы отбрасываются.
- [x] **3.5** Submit acknowledgement page: после POST browser показывает «Submitted! Form ID: ...» с кнопкой «вернуться в CLI».

**Verify:** POST через `curl` создаёт `.agentic/inputs/FORM-001.yaml` с правильным содержимым.

### Epic 4 · Rendering layer

Jinja2 rendering с frontmatter-aware templates.

- [x] **4.1** `render/engine.py` — Jinja2 Environment с autoescape, custom globals (`submit_url`, `form_id`, `template_name`).
- [x] **4.2** `render/frontmatter.py` — YAML frontmatter parser для `.html.j2` файлов.
- [x] **4.3** Template discovery: project templates (`.agentic/templates/*.html.j2`) override defaults (`render/default_templates/*.html.j2`) по имени.
- [x] **4.4** Plugin-injected variables доступны всем templates.

**Verify:** `render_template("role-assignment", data={...})` возвращает валидный HTML с `<form action="{{ submit_url }}">`.

### Epic 5 · Browser integration

Cross-platform browser open.

- [x] **5.1** `browser.py` — wrapper для `xdg-open` (Linux) / `open` (macOS) / `auto` (detect platform).
- [x] **5.2** Fallback error handling с понятным сообщением («browser open failed, проверь окружение»).
- [x] **5.3** Temp HTML files cleanup на plugin shutdown.

**Verify:** на Linux `xdg-open temp.html` открывает системный browser.

### Epic 6 · Default templates (5 templates для Сценария 1)

Шаблоны форм, ships with plugin.

- [x] **6.1** `role-assignment.html.j2` — multi-select ролей (worker/reviewer/tester) + file picker для кастомных.
- [x] **6.2** `skill-picker.html.j2` — multi-select скиллов + file picker.
- [x] **6.3** `model-picker.html.j2` — dropdown моделей для каждой выбранной роли.
- [x] **6.4** `pipeline-picker.html.j2` — radio (simple / full / custom file upload).
- [x] **6.5** `conflict-resolver.html.j2` — мини-форма (replace / save-as / cancel) для случаев когда кастомная роль конфликтует с дефолтной.
- [x] **6.6** Все templates с YAML frontmatter (description, required_data_keys, optional_data_keys).
- [x] **6.7** Минимальный inline CSS для readability (без внешних зависимостей).

**Verify:** каждый template рендерится с minimum required data, HTML валидный.

### Epic 7 · MCP tools implementation

5 tools полностью реализованы (specs в [`architecture.md`](vision/architecture.md) §6).

- [x] **7.1** `open_form(template, data, ttl_seconds?)` → `{form_id, browser_opened, submit_url}`.
- [x] **7.2** `read_submit(form_id)` → `{submitted, data?, status, ...}`.
- [x] **7.3** `cancel_form(form_id)` → `{cancelled, form_id}`.
- [x] **7.4** `list_pending_forms()` → `{pending: [...], count}`.
- [x] **7.5** `list_templates()` → `{templates: [...]}` с metadata из frontmatter.
- [x] **7.6** Form ID generator: `FORM-YYYYMMDDHHMMSS-XXXX` (timestamp + 4 random alphanumeric). Не sequence — избегаем коллизий со stale-файлами прошлых сессий.

**Verify:** integration test — полный lifecycle формы от open до read.

### Epic 8 · SKILL.md

LLM policy: когда/как использовать формы.

- [x] **8.1** `agent_workflow_ui/SKILL.md` — инструкция для LLM (когда форма, когда chat, patterns, mistakes to avoid). Draft в [`architecture.md`](vision/architecture.md) §9.
- [x] **8.2** Установка SKILL.md в `~/.config/opencode/skills/agent-workflow-ui/SKILL.md` (или аналог для текущего opencode).

**Verify:** agent при тестовом сценарии «настрой проект» осознанно выбирает форму вместо chat.

### Epic 9 · Testing

Покрытие ≥80%.

- [x] **9.1** Unit tests в `tests/agent_workflow_ui/`: `test_render.py`, `test_forms.py`, `test_browser.py`, `test_http_endpoint.py`, `test_templates.py`, `test_frontmatter.py`.
- [x] **9.2** Integration tests в `tests/integration/`: `test_form_lifecycle.py` (full MCP+HTTP+file lifecycle), `test_submit_via_http.py`.
- [x] **9.3** Coverage report ≥80% на `agent_workflow_ui/`.
- [x] **9.4** CI matrix: добавить `agent_workflow_ui/` в существующий GitHub Actions workflow.

**Verify:** `pytest tests/agent_workflow_ui/ tests/integration/ -v` проходит, coverage ≥80%.

### Epic 10 · Documentation & release

Финальная полировка для release.

- [x] **10.1** `agent_workflow_ui/README.md` — quick start, installation, usage examples.
- [x] **10.2** Обновить `protocols/communication.md` — добавить секции про `.agentic/inputs/` и `.agentic/templates/`.
- [x] **10.3** Обновить корневой `README.md` — упомянуть plugin.
- [ ] **10.4** PyPI publish: `agent-workflow-ui` как separate package (пока не опубликован — `pip install agent-workflow-ui` вернёт 404).
- [x] **10.5** `awf init` prompt: «Установить agent-workflow-ui plugin? [y/N]» → если yes, добавляет MCP block в `~/.config/opencode/opencode.json`.
- [x] **10.6** Release notes (CHANGELOG.md или GitHub Release).

**Verify:** новый пользователь ставит plugin по инструкции, открывает форму через агента, получает submit.

---

## 🐛 Known bugs / Tech debt

Обнаружено при dogfooding на jira-epic-presenter.

### BD-1 · `_slugify` молча глотает кириллицу → `unnamed.md`

**Symptom:** Custom agent с именем на кириллице (например «Аудиитор») сохраняется в
`~/.config/awf/roles/unnamed.md`. Пользователь не получает ошибки, но роль не находится
по имени и засоряет global dir.

**Root cause:** `_slugify` в `opencode_config.py:205` режет non-ASCII символы, оставляя
пустую строку. Нет fallback'а на транслитерацию или явной ошибки.

**Fix options:**
- A) Транслитерация через `python unicodedata` + транслит-таблицу (ru→lat).
- B) Если после slugify строка пуста — raise `ValueError("Role name must contain ASCII chars")` с user-friendly message в форме.
- C)两者: транслитерация, а если не получилось — raise.

**Found during:** dogfood session 2026-07-28, форма `project-setup`, агент «Аудиитор».

### BD-2 · `available_roles` принимает только dict, не string — нет валидации

**Symptom:** `open_form(template="project-setup", data={"available_roles": ["worker"]})`
падает с `'str' object has no attribute 'get'` (template line 410:
`r.get('description', '')`).

**Root cause:** Template ожидает `[{id, title, description}, ...]`. Plugin не валидирует
тип `data` перед рендером. MCP tool signature принимает `dict` без schema — agent
не знает контракта.

**Fix options:**
- A) В `tools/forms.py:open_form` — нормализовать `available_roles`: если строка → `{id: s, title: s, description: ""}`.
- B) JSON-schema валидация в `open_form` с понятной ошибкой.
- C) Документировать контракт в docstring + SKILL.md (минимум).

**Found during:** dogfood session 2026-07-28, first call fell, пришлось дебажить исходники plugin'а.

### BD-3 · Custom roles сохраняются в global, awf-core не видит их в проекте

**Architectural gap:** Plugin writes custom roles to `~/.config/awf/roles/`, but
awf-core resolves roles only from `.agentic/roles/`. No bridge. Для проекта с custom
agent'ами supervisor должен вручную копировать `.md` в `.agentic/roles/` после submit'а.

**Fix options:**
- A) awf-core: при резолвинге роли fallback на `~/.config/awf/roles/<name>.md` если нет в project.
- B) Plugin: после submit'а `project-setup` опционально копировать выбранные custom roles в `.agentic/roles/`.
- C) Supervisor prompt в SKILL.md: «после submit'а custom agent — скопируй в `.agentic/roles/`».

A — правильное архитектурно, но feature-work. C — workaround сейчас.

**Found during:** dogfood session 2026-07-28.

### BD-9 · `project-setup` form selections do NOT become pipeline stages (CRITICAL)

**Critical architectural gap.** User opens `project-setup` form, selects
supervisor + team (worker, tester, custom agents like system-analysis,
project-auditor). Form saves role files to global/project, updates
config.yaml models section. But `awf start` reads `.agentic/pipelines/default.yaml`
which is **static** — created by `awf init --template simple|full`. The
selected roles never become pipeline stages.

**Result:** user thinks "I configured my pipeline", actually configured
nothing. Worker runs alone, tester/reviewer/custom agents are ignored.
Form promises one thing, runtime does another.

**Architecture decision required (3 options):**

- **A) Form = pipeline.** Form presents an ordered list (drag-and-drop or
  numbered). Each role becomes a stage in this order. supervisor (plan)
  auto-prepended, supervisor (verify) auto-appended. Plugin writes
  `.agentic/pipelines/default.yaml` after submit. Custom agents get
  `action: "execute_todo"` (same as worker — receive TODO, do work, write
  DONE). Intuitive, but custom agents with very different purposes
  (analysis vs audit) may need different actions later.

- **B) Form splits pipeline-roles vs advisory-agents.** Two sections in
  form: "Pipeline team" (worker/tester/reviewer in order) and "Advisory
  agents" (custom — available to supervisor ad-hoc, not pipeline stages).
  Plugin writes pipeline from team section only. Semantically clean but
  more UX complexity.

- **C) Hybrid.** Form shows ordered list of any roles (default + custom),
  drag-and-drop, plugin writes pipeline. Each role = stage. User can put
  tester before worker (pipeline accepts it, may not be smart). Maximum
  flexibility, no semantic guardrails.

**Recommended: A** — closest to user mental model. UI change + plugin
pipeline-writer. No awf-core changes needed (orchestrator already iterates
stages from YAML).

**Sub-tasks for option A:**
1. Add ordering to form (drag-and-drop library or simple up/down arrows).
2. Plugin: `_write_pipeline_yaml(team_order: list[str]) -> Path` in
   roles_processor.py or new pipelines_writer.py. Writes to
   `.agentic/pipelines/default.yaml` (with project_dir from BD-6).
3. Pipeline structure: supervisor(plan) → [team in order] → supervisor(verify).
   Each team stage: `{name: <role>, role: <role>, action: "execute_todo",
   on_blocked: "escalate", max_retries: 3}`.
4. Tests: pipeline-writer unit tests, e2e verify form → awf start uses
   correct stages.
5. Update SKILL.md: explain that form order = pipeline order.

**Found during:** dogfood session 2026-07-28, jira-epic-presenter Step 1.
Worker ran solo despite user selecting worker + tester + 2 custom agents.

---

### BD-8 · `--background --auto` auto-commits without explicit supervisor approval + mixes unrelated files

**Symptom:** `awf start --background` ran TODO-0002. Worker created 4 files
(manifest, content.js, background.js, README.md). Orchestrator hit verify
stage with `on_approved: commit_and_next`. In `--auto` mode (forced by
`--background` per BD-7), no human approval gate — orchestrator ran
`git add -A && git commit`. The commit included not just worker output but
also supervisor's prior in-progress changes (config.yaml update, copied
role files). Mixed sources, no review checkpoint.

**Root cause:** `commit_and_next` policy is documented as "auto-commit on
supervisor approval", but `--auto` skips the supervisor pause entirely —
so "approval" is implicit, not actual. Also `git add -A` captures every
uncommitted change in the worktree, not just the files worker touched.

**Fix options:**
- A) `--auto` skips supervisor **pause** (input), NOT supervisor **approval**.
  Pipeline should pause at verify stage waiting for explicit ACK file
  (`ACK-TODO-NNNN.ready`) even in `--auto` mode. Supervisor (in another
  session) inspects, then creates ACK → orchestrator proceeds with commit.
- B) Orchestrator's auto-commit should only stage files worker touched
  (compute diff vs baseline, add only those). Avoids mixing unrelated
  changes. Requires git diff logic in orchestrator.
- C) Both A + B. Belt and suspenders.

**Recommended: A.** Aligns with awf's stated philosophy: supervisor
approves explicitly. Background mode just means "no live terminal", not
"no supervisor". Supervisor reviews asynchronously via ACK files.

**Found during:** dogfood session 2026-07-28, commit `0614b8d` mixed
worker output + supervisor's role/config changes; rolled back via
`git reset --hard 40bcb4a`.

---

### BD-7 · `awf start --background` EOFError on supervisor input()

**Symptom:** `awf start --background` crashes immediately:
```
File ".../awf/orchestrator.py", line 171, in _run_supervisor_stage
  input()
EOFError: EOF when reading a line
```

**Root cause:** `--background` runs detached via setsid — stdin is closed
(/dev/null or actual EOF). Supervisor stage calls `input()` to wait for
human confirm. With no stdin → EOFError → pipeline crashes.

**Fix:** In `awf start`, `--background` should imply `--auto` (skip
supervisor interactive pauses). Detached mode has no human at the wheel
by definition — there's nothing to wait for.

In `cmd_start.run` (around args parsing): if `args.background` is set,
force `args.auto = True`. Add a unit test that exercises this.

Alternative: in `_run_supervisor_stage`, if `auto` is True OR background
mode is active, skip the `input()` call. Less centralized but explicit.

**Found during:** dogfood session 2026-07-28, first `awf start --background`
on TODO-0002.

---

### BD-6 · Plugin runs from HOME, not project — `.agentic/` polluted in HOME, project_dir wrong

**Symptom:** After `awf start` and form submit, plugin wrote submit YAMLs to
`/home/pklochkov/.agentic/inputs/` (HOME), NOT to project's `.agentic/inputs/`.
`detect_project_dir()` returned HOME because `/home/pklochkov/.agentic/` exists
(ironically, created by this very bug). Custom roles copied to
`/home/pklochkov/.agentic/roles/` instead of project's `.agentic/roles/`.

**Root cause:** `opencode` launches MCP subprocess with `cwd=$HOME`, not with
the project directory the user invoked opencode from. Plugin uses `Path.cwd()`
in `config.load()` and `detect_project_dir()` — both wrong in this context.

**Fix options:**
- A) **Plugin reads `AWF_PROJECT_DIR` env var** (set by opencode/agent before
  spawning MCP). `detect_project_dir()` checks env first, then falls back to
  cwd. Opencode may need a wrapper to set this from its project root.
- B) **Agent passes `project_dir` in `open_form(data=...)`** — plugin stores it
  on the FormRecord and uses it for `roles_processor` paths at submit time.
  No env var needed; agent (supervisor) is responsible for providing it.
  Cleanest — works without opencode changes.
- C) **Plugin walks up from cwd looking for `.agentic/`** — but cwd=HOME means
  it finds the polluted one. Useless until cleanup + extra guard against HOME.

**Recommended: B + cleanup.** Add `project_dir` field to FormRecord, default
to `detect_project_dir()` for back-compat; if agent provides it via form data,
override. Cleanup: `rm -rf ~/.agentic/`.

**Found during:** dogfood session 2026-07-28, restart verification — после
restart port сменился (53921→54073), но кдw всё равно HOME.

---

### BD-5 · Custom supervisor variant selected from dropdown not copied to project; empty content allowed

**Symptoms (2 issues from dogfood):**
1. User selected existing `supervisor-architect` from dropdown in `project-setup` form (no new content). Plugin did NOT copy the variant into project's `.agentic/roles/`. As a result, awf-core's generic `.agentic/roles/supervisor.md` is what the supervisor agent reads — not the chosen variant.
2. `supervisor-architect.md` in global was empty (just `# Supervisor Architect` heading, no instructions). `save_custom_role` accepts empty content silently.

**Root cause:**
- `roles_processor.process_role_saves` saves supervisor variant only when `supervisor_content` is non-empty AND `save_supervisor=="true"`. There's no path for "use existing selected variant" → just copy from global to project.
- `opencode_config.save_custom_role` writes content as-is without validating non-emptiness.

**Fix:**
- **BD-5-A:** `save_custom_role` raises `ValueError` if content empty after strip.
- **BD-5-B:** `process_role_saves` reads `supervisor_role` form field (existing variant id). If non-empty and not "default" — copy `<global>/<id>.md` to project `.agentic/roles/`.
- **BD-5-C:** Same logic for `agent[]` field — selected existing custom agents (without new content) get copied to project too. Currently only newly-saved custom agents are copied (BD-3-B).

**Found during:** dogfood session 2026-07-28, supervisor-architect role selected, file empty, project uses generic supervisor.md.

---

### BD-4 · Submit confirmation page: EN, light theme, blue accent

**Symptoms (3 issue from dogfood):**
1. Текст на английском — основная форма на русском, confirmation отвалился.
2. Светлая тема — основная форма тёмная. Расхождение стиля.
3. Блок "Next step" на голубом фоне — должен быть на красном (visibility/urgency).

**Where:** HTTP endpoint response после POST `/submit/{form_id}`. См. `http_endpoint.py`
confirmation HTML.

**Fix:** единый стиль с `project-setup.html.j2` (dark theme + RU + red accent для next-step).

**Found during:** dogfood session 2026-07-28.

---

## 🔮 Future scenarios (после MVP)

В порядке приоритета из [Vision §6](vision/agent-ui-plugin.md#6-пользовательские-сценарии). Каждый — отдельный epic, после MVP.

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

## ❌ Deprecated (старый BACKLOG Tasks 1-7)

Следующие задачи из предыдущей версии BACKLOG **более не актуальны** после стратегического pivot (см. [vision/agent-ui-plugin.md §1.2](vision/agent-ui-plugin.md)) — переход от «HTML-over-CLI dashboard» к «CLI primary + HTML supplement через MCP plugin».

| Старая задача | Статус | Что вместо неё |
|---|---|---|
| Task 1: HTML dashboard как SPA | ❌ Deprecated | Сценарий 4 (Dashboard rendering) — static HTML, не SPA. |
| Task 2: HTTP API (`awf serve`) | ❌ Deprecated | HTTP endpoint внутри MCP server, не REST API. См. Epic 3. |
| Task 3: `awf bootstrap <requirements>` | ❌ Deprecated | Сценарий 6 (Onboarding wizard) — через forms, не CLI команду. |
| Task 4: Цепочки worker'ов | ✅ Сохраняется | Актуально для awf-core, вне scope plugin'а. |
| Task 5: Шаблоны ролей (6 штук) | ✅ Сохраняется | Актуально для awf-core, частично через plugin (file picker в `role-assignment`). |
| Task 6: Выбор моделей | ✅ Интегрировано | В MVP через `model-picker.html.j2` (Epic 6.3). |
| Task 7: Real-time SSE | ❌ Deprecated на сейчас | Meta-refresh в Сценарии 4. SSE/WebSocket — far future. |

---

## 📋 Декомпозиция и запуск

Каждый epic перед стартом работы **декомпозируется в awf TODO** через supervisor↔worker pattern. Порядок выполнения epics в MVP — последовательный (1 → 2 → 3 → ...), но внутри epics задачи могут параллелиться.

Когда начинать: после согласования этого BACKLOG'а и фиксации через git commit. Первый epic (1. Package skeleton) — отправная точка.
