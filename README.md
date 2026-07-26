# Agentic Workflow Framework

> Декларативный multi-agent фреймворк: **Supervisor планирует → Worker реализует → Supervisor проверяет**. Коммуникация — через файлы на диске (file bus). Состоит из двух независимых продуктов в одном monorepo:

- **`awf`** v0.4.0 — Python-оркестратор пайплайнов (CLI, file bus, state machine). Тонкий bash-wrapper.
- **`agent-workflow-ui`** v0.1.0 — standalone MCP plugin для opencode: HTML-формы для визуального взаимодействия пользователя с агентом.

Оба продукта публикуются отдельно на PyPI (планируется). Awf-core не зависит от plugin'а и работает без него.

---

## Ключевые особенности

### awf (orchestrator)

- **Supervisor ↔ Worker через file-bus.** Задачи и отчёты передаются через `.agentic/inbox/` и `.agentic/outbox/` с `.ready`-сигналами. Никаких HTTP, WebSocket, очередей.
- **Pipeline как YAML.** Стадии, роли, transition-политики — всё декларативно в `.agentic/pipelines/default.yaml`.
- **4 роли.** supervisor (ты), worker (агент), reviewer и tester (опционально, `--template full`).
- **Auto-DONE.** Если worker не успел записать сигнал, но verify-команды прошли и есть work evidence (git diff) — оркестратор синтезирует DONE автоматически.
- **Auto-commit.** Стадии с `on_approved: commit_and_next` автоматически коммитят инкремент.
- **Python core.** 22 модуля (~2100 строк) + тонкий bash-wrapper (36 строк).

### agent-workflow-ui (MCP plugin)

- **5 MCP tools:** `open_form`, `read_submit`, `cancel_form`, `list_pending_forms`, `list_templates`.
- **Composite template `project-setup`** — одна HTML-форма для полной настройки проекта: контекст + ТЗ-файлы + supervisor + команда агентов с моделями.
- **HTTP endpoint** (всегда включён) — browser POST'ит submit автоматически.
- **Custom roles persistence** в `~/.config/awf/roles/` — сохранение/удаление через форму, переиспользование между проектами.
- **Lazy skill install** — SKILL.md автоматически копируется в opencode skills dir при первом старте plugin'а.
- **Cross-platform** — xdg-open / open / explorer.

Подробнее: [`agent_workflow_ui/README.md`](agent_workflow_ui/README.md), [`vision/agent-ui-plugin.md`](vision/agent-ui-plugin.md).

---

## Установка

### Требования

- **bash** 4+ (только wrapper).
- **python3** ≥ 3.9 для awf, ≥ 3.10 для plugin (зависимость `mcp>=1.0`).
- **PyYAML** (устанавливается автоматически).
- **git** (целевой проект должен быть git-репозиторием).
- **opencode** CLI в `$PATH` (нужен только для `awf start` / `awf continue`).

### awf

```bash
# 1) Клонировать
git clone git@github.com:EnerJizeIT/agentic-workflow.git
cd agentic-workflow

# 2) Установить Python-зависимости
pip install -e .

# 3) Вариант A — вызывать напрямую
./bin/awf

# Вариант B — symlink для доступа из любого каталога
ln -s "$PWD/bin/awf" ~/.local/bin/awf
awf
```

Если `~/.local/bin/` нет в `$PATH`, добавь в `~/.bashrc`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### agent-workflow-ui plugin (опционально)

```bash
pip install -e ./agent_workflow_ui
```

После установки `awf init` предложит автоматически добавить MCP-конфиг в `~/.config/opencode/opencode.json`. SKILL.md установится автоматически при первом старте opencode (lazy install).

### Обновление

```bash
cd agentic-workflow
git pull && pip install -e . && pip install -e ./agent_workflow_ui
```

---

## Быстрый старт

```bash
# 1. Установить awf (один раз)
git clone git@github.com:EnerJizeIT/agentic-workflow.git
cd agentic-workflow && pip install -e .
ln -s "$PWD/bin/awf" ~/.local/bin/awf

# 2. В любом git-проекте:
cd /path/to/your-project
awf init --template simple         # отвечай на вопросы
vim .agentic/phases/plan.md        # напиши план
awf start                          # запусти пайплайн
```

### Что произойдёт

1. **`awf init`** создаст `.agentic/` с ролями, пайплайном и конфигами.
2. **План** (`.agentic/phases/plan.md`) — чеклист шагов, который читает Supervisor.
3. **`awf start`** запустит оркестратор:
   - **Supervisor stage (ты):** создай TODO, положи в `.agentic/inbox/`, нажми Enter.
   - **Worker stage (автоматически):** агент запустится, реализует задачу, напишет DONE или BLOCKED.
   - **Verify stage (ты):** проверь результат, подтверди или запроси исправления.

---

## Команды awf

| Команда | Описание |
|---|---|
| `awf` или `awf help` | Показать справку |
| `awf help <command>` | Справка по конкретной команде |
| `awf init [--template simple\|full]` | Создать `.agentic/` в проекте |
| `awf init --force` | Пересоздать без подтверждения |
| `awf init --dry-run` | Показать что будет создано |
| `awf start [опции]` | Запустить пайплайн |
| `awf start --pipeline <name>` | Конкретный пайплайн |
| `awf start --from-stage <name>` | Начать с указанной стадии |
| `awf start --auto` | Пропустить интерактивные паузы supervisor'а |
| `awf start --timeout <sec>` | Таймаут агента (по умолчанию 3600) |
| `awf start --background` | Фоновый запуск (detached через setsid, лог в `.agentic/logs/`) |
| `awf continue [опции]` | Продолжить прерванный пайплайн |
| `awf status` | Текущее состояние воркфлоу |
| `awf status --project-dir <path>` | Статус другого проекта |
| `awf report` | Сводный отчёт о работе |
| `awf add-role <name>` | Создать шаблон новой роли |
| `awf add-role <name> --description "..."` | С описанием |
| `awf baseline <id>` | Снимок состояния перед задачей |
| `awf rollback <id>` | Откат к baseline |
| `awf rollback <id> --hard` | Жёсткий откат (git reset --hard) |
| `awf rollback <id> --soft` | Мягкий откат (git reset --soft) |
| `awf rollback <id> --dry-run` | Показать что будет откатано |
| `awf reset` | Очистить runtime-данные |
| `awf reset --tasks-only` | Очистить только inbox/outbox |
| `awf reset --full` | Очистить все runtime-директории (по умолчанию) |
| `awf reset --orphans [--force]` | Удалить TODO без прогресс-лога |

---

## Архитектура

```
agentic-workflow/             # monorepo (два независимых продукта)
├── awf/                      # orchestrator (Python package, ~2100 строк)
│   ├── __init__.py
│   ├── __main__.py           # entry: python3 -m awf
│   ├── cli.py                # argparse dispatcher
│   ├── paths.py              # realpath-резолвинг для symlink install
│   ├── config.py             # загрузка config.yaml
│   ├── yaml_utils.py
│   ├── pipeline.py           # Stage dataclass, load_stages()
│   ├── orchestrator.py       # state machine пайплайна
│   ├── signals.py            # polling сигналов, prefix-filtering
│   ├── transitions.py        # resolve_transition (policy lookup)
│   ├── verify.py             # auto-DONE, detect_work_evidence
│   ├── git_utils.py          # git-операции
│   ├── todos.py              # list_active_todos, has_progress
│   ├── opencode_agents.py    # ~/.config/opencode/opencode.json management
│   └── cmd_*.py              # 9 команд (init, start, status, reset, ...)
├── agent_workflow_ui/        # MCP plugin (standalone dist, требует Python ≥3.10)
│   ├── pyproject.toml        # отдельный package
│   ├── README.md
│   └── src/agent_workflow_ui/
│       ├── __main__.py       # entry: python -m agent_workflow_ui
│       ├── server.py         # FastMCP server (5 tools)
│       ├── http_endpoint.py  # localhost HTTP для form submits
│       ├── browser.py        # xdg-open / open wrapper
│       ├── config.py         # env vars, paths
│       ├── state.py          # in-memory registry of open forms
│       ├── opencode_config.py# models discovery + custom roles CRUD
│       ├── roles_processor.py# save/delete custom roles from submit data
│       ├── skill_installer.py# lazy install SKILL.md
│       ├── tools/            # MCP tool implementations
│       ├── render/           # Jinja2 engine + frontmatter + default_templates/
│       └── SKILL.md          # LLM policy (auto-copied to ~/.config/opencode/skills/)
├── bin/awf                   # тонкий bash-wrapper (36 строк) → python3 -m awf
├── templates/                # шаблоны для awf init (roles/, pipelines/, config)
├── protocols/communication.md# спецификация file bus
├── vision/                   # product vision + architecture docs
├── tests/
│   ├── e2e/                  # 12 E2E (subprocess через bin/awf)
│   ├── unit/                 # 152 unit для awf/*.py
│   └── agent_workflow_ui/    # 180 unit/integration для plugin'а
├── docs/history/             # исторические дизайн-документы (до реализации)
├── pyproject.toml            # workspace metadata + ruff/pytest config
├── BACKLOG.md                # план развития
└── README.md                 # этот файл
```

---

## Как это работает

### Pipeline

Оркестратор читает стадии из YAML-файла пайплайна и выполняет их последовательно. Каждая стадия определяет роль и action:

```yaml
stages:
  - name: "plan"
    role: "supervisor"
    action: "create_todo"

  - name: "implement"
    role: "worker"
    action: "execute_todo"
    on_blocked: "escalate"
    max_retries: 3

  - name: "verify"
    role: "supervisor"
    action: "verify_result"
    on_approved: "commit_and_next"
```

### Стадии

- **Supervisor stage** — интерактивная пауза (или `--auto` для пропуска).
- **Agent stage** — оркестратор порождает `opencode run --auto --agent <role>`.

### Сигналы

Worker пишет сигналы в `.agentic/outbox/`. Каноничный формат — `{PREFIX}-TODO-{NNNN}` (например `DONE-TODO-0001`). Legacy-форма `{PREFIX}-{NNNN}` тоже принимается.

| Сигнал | Файл | Кто пишет |
|---|---|---|
| `TASK_READY` | `inbox/TODO-{NNNN}.md` + `.ready` | Supervisor |
| `TASK_DONE` | `outbox/DONE-TODO-{NNNN}.md` + `.ready` | Worker/Reviewer/Tester |
| `TASK_BLOCKED` | `outbox/BLOCKED-TODO-{NNNN}.md` + `.ready` | Worker/Reviewer/Tester |
| `TASK_ACK` | `inbox/ACK-TODO-{NNNN}.ready` | Supervisor |
| `TASK_PROGRESS` | `outbox/PROGRESS-TODO-{NNNN}.md` (append-only) | Worker |
| `REVIEW_APPROVED`/`REVIEW_REJECTED` | `outbox/REVIEW-{APPROVED\|REJECTED}-TODO-{NNNN}.md` | Reviewer |
| `TEST_PASSED`/`TEST_FAILED` | `outbox/TEST-{PASSED\|FAILED}-TODO-{NNNN}.md` | Tester |

Подробнее: [protocols/communication.md](protocols/communication.md) (включая §3.6 «Signal naming — canonical vs legacy»).

### Transition policies

Правила `on_*` в YAML определяют переход между стадиями:

- `on_approved: next` — перейти к следующей стадии.
- `on_approved: commit_and_next` — закоммитить и перейти дальше.
- `on_approved: commit_and_report` — закоммитить и показать отчёт.
- `on_blocked: escalate` — эскалация на supervisor.
- `on_blocked: rollback_to:<stage>` — откат к указанной стадии.
- `on_blocked: stop` — остановка пайплайна.
- `on_rejected: rollback_to:<stage>` — откат при ревью-отклонении.
- `on_failed: rollback_to:<stage>` — откат при ошибке тестов.

### Auto-DONE

Если worker не записал сигнал, но выполнены оба условия, оркестратор синтезирует DONE:

1. Прошли **структурированные verify-команды** из `config.yaml` (`test_cmd`, `build_cmd`, `typecheck_cmd`).
2. Есть **work evidence** — `git diff` показывает изменения или появились untracked-файлы.

Отключается через `automation.auto_done: false` в `config.yaml`.

---

## Файлы `.agentic/`

```
.agentic/
├── config.yaml              # модели, verify-команды, default_pipeline
├── roles/                   # инструкции для ролей
│   ├── supervisor.md
│   ├── worker.md
│   ├── reviewer.md          # (только --template full)
│   └── tester.md            # (только --template full)
├── pipelines/
│   └── default.yaml         # стадии пайплайна
├── phases/
│   └── plan.md              # твой план реализации
├── templates/               # project-level form templates (agent-driven, опционально)
├── inbox/                   # TODO от supervisor → worker [gitignored]
├── outbox/                  # DONE/BLOCKED/PROGRESS от worker [gitignored]
├── inputs/                  # form submits от agent-workflow-ui plugin [gitignored]
├── dashboards/              # rendered dashboards (future) [gitignored]
├── context/                 # baseline SHA и тест-логи [gitignored]
├── logs/                    # orchestrator.log [gitignored]
└── reports/                 # сводные отчёты [gitignored]
```

Runtime-директории (`inbox/`, `outbox/`, `inputs/`, `dashboards/`, `context/`, `logs/`, `reports/`) не коммитятся в git. Статические файлы (`config.yaml`, `roles/`, `pipelines/`, `phases/`, `templates/`) — коммитятся. `.gitignore` создаётся автоматически при `awf init`.

---

## agent-workflow-ui plugin

[`agent-workflow-ui`](agent_workflow_ui/) — standalone MCP plugin для opencode.
Даёт supervisor-агенту инструменты визуального взаимодействия с пользователем:
HTML-формы для структурированного ввода (когда chat неэффективен).

**Status:** v0.1.0 — реализован (5 MCP tools, HTTP endpoint, composite template `project-setup`, custom roles persistence, lazy skill install, 180 тестов, 93% coverage).

### Установка

```bash
pip install -e ./agent_workflow_ui
awf init      # предложит автоматически добавить MCP-конфиг в opencode.json
```

### Что даёт

После установки supervisor-агент видит 5 tools:

| Tool | Описание |
|---|---|
| `open_form` | Открыть HTML-форму в браузере (non-blocking) |
| `read_submit` | Прочитать ответ пользователя |
| `cancel_form` | Отменить pending форму |
| `list_pending_forms` | Список открытых форм |
| `list_templates` | Список доступных шаблонов форм |

**MVP interface** — composite template `project-setup`: контекст проекта + ТЗ-файлы + supervisor + команда агентов с моделями. Остальные 5 templates зарезервированы для будущих сценариев (decision fork, monitoring dashboard, onboarding wizard — см. BACKLOG).

Подробнее: [`agent_workflow_ui/README.md`](agent_workflow_ui/README.md), [`vision/agent-ui-plugin.md`](vision/agent-ui-plugin.md), [`vision/architecture.md`](vision/architecture.md).

---

## Роли

### Supervisor (ты)

Работает в текущей сессии, не отдельный процесс. Задачи:

- Изучить состояние проекта и план.
- Определить следующий шаг.
- Создать TODO с описанием задачи (не Find/Replace, а «что построить»).
- Проверить результат работы worker'а.

Инструкция: `.agentic/roles/supervisor.md`.

### Worker (агент)

Запускается как отдельный процесс opencode. Получает TODO, самостоятельно проектирует и реализует решение. Возвращает DONE или BLOCKED.

Worker — capable developer model. Он получает **«what to build»** и сам решает **«how»**. Supervisor опускается до точных Find/Replace только при повторных неудачах.

Инструкция: `.agentic/roles/worker.md`.

### Reviewer / Tester (опционально, `--template full`)

Reviewer проверяет качество кода. Tester запускает тесты и сравнивает с baseline.

---

## Task modes (Supervisor → Worker)

| Mode | Когда использовать | Что получает Worker |
|---|---|---|
| **A: High-level** (по умолчанию) | Обычные задачи | Описание «что построить», ограничения, verify |
| **B: Detailed** | Сложные задачи, несколько файлов | Архитектурные заметки, референсы, паттерны |
| **C: Find/Replace** (fallback) | Предыдущие попытки не сработали | Точные блоки кода для замены |

---

## Прогресс и восстановление

### PROGRESS-логи

Worker пишет `.agentic/outbox/PROGRESS-TODO-{NNNN}.md` после каждого выполненного Task. Файл append-only — Supervisor может читать его в любое время.

```bash
awf status                                     # прогресс активных задач
cat .agentic/outbox/PROGRESS-TODO-0001.md     # полный лог
```

### 3-Strike Error Protocol

Worker не эскалирует на первую ошибку. Протокол описан в `worker.md`:

```
Attempt 1: Diagnose & Fix → Attempt 2: Alternative Approach → Attempt 3: Broader Rethink → Escalate
```

Каждая попытка логируется. Supervisor видит историю в BLOCKED-отчёте.

### Session Recovery

Если worker-процесс упал, он восстанавливается с `PROGRESS-TODO-{NNNN}.md` при перезапуске — пропускает выполненные задачи, продолжает с первого незавершённого.

---

## Тестирование

```bash
# Все тесты (e2e + unit + plugin):
python3 -m pytest tests/ -v

# Только awf E2E (запуск bin/awf как subprocess):
python3 -m pytest tests/e2e/ -v

# Только awf unit:
python3 -m pytest tests/unit/ -v

# Только plugin:
python3 -m pytest tests/agent_workflow_ui/ -v

# Coverage plugin:
python3 -m pytest tests/agent_workflow_ui/ --cov=agent_workflow_ui --cov-report=term-missing
```

**344 теста:** 12 E2E + 152 awf-unit + 180 plugin.

E2E-тесты используют `tests/stubs/opencode` для mock'а worker'а — реальный opencode не требуется.

**Покрытие:**
- **agent-workflow-ui:** **93%** (target ≥80%, enforced в CI через `--cov-fail-under=80`).
- **awf-core helper modules** (`signals.py`, `pipeline.py`, `transitions.py`, `verify.py`, `todos.py`, `git_utils.py`, `config.py`, `paths.py`, `yaml_utils.py`) — 91-100% unit-покрытие.
- **awf-core cmd_*.py** — формально ~0% через pytest-cov, потому что execute через subprocess (bin/awf). Реальное покрытие обеспечивается E2E.

**Линт:** `ruff check tests/ awf/ agent_workflow_ui/src/agent_workflow_ui/` — весь репо проходит чисто (CI lint job).

---

## Спецификации и история

- [protocols/communication.md](protocols/communication.md) — спецификация файловой шины (сигналы, naming canonical vs legacy, task lifecycle, safety).
- [vision/agent-ui-plugin.md](vision/agent-ui-plugin.md) — Product Vision plugin'а (v0.4).
- [vision/architecture.md](vision/architecture.md) — Architecture plugin'а (v1.1).
- `docs/history/` — исходные дизайн-документы awf (до реализации, bash era).
- [BACKLOG.md](BACKLOG.md) — план развития.

---

## Что нового

### agent-workflow-ui v0.1.0 (2026-07-26)

Реализован standalone MCP plugin для opencode. См. [agent_workflow_ui/README.md](agent_workflow_ui/README.md).

- **5 MCP tools** (`open_form`, `read_submit`, `cancel_form`, `list_pending_forms`, `list_templates`).
- **Composite template `project-setup`** — primary для MVP: контекст + ТЗ + supervisor + команда агентов с моделями. Pipeline НЕ выбирается явно — выводится supervisor'ом.
- **HTTP endpoint всегда включён** (единственный способ принять submit из браузера).
- **Custom roles persistence** в `~/.config/awf/roles/` (save/delete через форму).
- **Inline conflict resolution** через JS `confirm()` перед перезаписью роли.
- **Lazy skill install** при каждом старте plugin'а (заменяет ненадёжные setuptools post-install hooks).
- **Models auto-discovery** через `opencode models` CLI + recent models из SQLite history.
- **180 тестов, 93% coverage.**

### awf v0.4.0 (2026-07-20)

**Миграция bash→Python завершена.** Все 9 команд работают через Python core; `lib/*.sh` (10 файлов, ~1700 строк) удалены.

- **Closes [#1](https://github.com/EnerJizeIT/agentic-workflow/issues/1):** `bin/awf` резолвит симлинки через `os.path.realpath` — install через `ln -s` работает нативно.
- **`awf/` Python package:** 22 модуля, ~2100 строк.
- **164 теста:** 12 E2E + 152 unit.
- **`tests/run.sh` удалён** — заменён на pytest.

### Timeline awf v0.3.x (миграция по волнам)

- **v0.3.4** — weak-spots closure (find_active_todo, status warns, reset --orphans, init model prompt).
- **v0.3.5** — pytest E2E harness + mock opencode stub.
- **v0.3.6** — Wave 4a: `awf status` ported.
- **v0.3.7** — Wave 4b: orchestrator ported (8 модулей).
- **v0.3.8** — Wave 4c: 6 remaining commands ported.
- **v0.3.9** — Wave 4d-part1: 152 unit tests.
- **v0.4.0** — Wave 4d-part2: bash retired.

---

## На что смотреть дальше

Подробный roadmap — в [BACKLOG.md](BACKLOG.md). Кратко:

1. **agent-workflow-ui scenarios 2-6** — Decision fork, Blockage recovery, Monitoring dashboard, Priority planning, Onboarding wizard. См. [vision/agent-ui-plugin.md](vision/agent-ui-plugin.md) §6.
2. **`awf-mcp`** — отдельный MCP server для awf state queries (`get_active_todos`, `get_pipeline_state`, ...).
3. **PyPI publish** — `agent-workflow-ui` и `awf` как separate packages (пока не опубликованы).

> Старые Tasks 1-3, 7 (HTML SPA dashboard, `awf serve` REST API, `awf bootstrap`, real-time SSE) — **deprecated**, см. [BACKLOG.md §"Deprecated"](BACKLOG.md#-deprecated-старый-backlog-tasks-1-7).

---

## Лицензия

MIT (см. `pyproject.toml`).
