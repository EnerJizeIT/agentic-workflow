# Agentic Workflow Framework

> Декларативный пайплайн для multi-agent разработки: **Supervisor планирует → Worker реализует → Supervisor проверяет**. Коммуникация — через файлы на диске. Ядро на Python, тонкий bash-обёртка.

**Текущая версия:** v0.4.0 (см. [Что нового](#что-нового)).

---

## Ключевые особенности

- **Supervisor ↔ Worker через file-bus.** Задачи и отчёты передаются через `.agentic/inbox/` и `.agentic/outbox/` с `.ready`-сигналами. Никаких HTTP, WebSocket, очередей.
- **Pipeline как YAML.** Стадии, роли, transition-политики — всё декларативно в `.agentic/pipelines/default.yaml`.
- **4 роли.** supervisor (ты), worker (агент), reviewer и tester (опционально, `--template full`).
- **Auto-DONE** (v0.3.2+). Если worker не успел записать сигнал, но verify-команды прошли и есть work evidence — оркестратор синтезирует DONE автоматически.
- **Auto-commit.** Стадии с `on_approved: commit_and_next` автоматически коммитят инкремент.
- **Python core.** 22 модуля (~2100 строк) + тонкий bash-wrapper (36 строк).
- **164 теста.** 12 E2E (subprocess через `bin/awf`) + 152 unit (все модули `awf/`).

---

## Установка

### Требования

- **bash** 4+ (только для тонкого wrapper-скрипта).
- **python3** ≥ 3.9 (ядро логики).
- **PyYAML** (устанавливается автоматически через `pip install -e .`).
- **git** (целевой проект должен быть git-репозиторием).
- **opencode** CLI в `$PATH` (нужен только для `awf start` / `awf continue`).

### Установка

```bash
# 1) Клонируй фреймворк
git clone git@github.com:EnerJizeIT/agentic-workflow.git
cd agentic-workflow

# 2) Установи Python-зависимости
pip install -e .

# 3) Вариант A — вызывай напрямую
./bin/awf

# Вариант B — symlink для доступа из любого каталога
ln -s "$PWD/bin/awf" ~/.local/bin/awf
awf
```

Если `~/.local/bin/` нет в `$PATH`, добавь в `~/.bashrc`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Обновление

```bash
cd agentic-workflow
git pull && pip install -e .
```

### Примечание про `cp`-установку

Рекомендуемый способ — `ln -s`. Если скопировал `bin/awf` в другое место и удалил исходное дерево, установи переменную `AWF_FRAMEWORK_DIR`:

```bash
export AWF_FRAMEWORK_DIR="/абсолютный/путь/к/agentic-workflow"
```

---

## Быстрый старт

```bash
# 1. Установи awf (один раз)
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

## Команды

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
bin/awf              # тонкий bash-wrapper (36 строк) → python3 -m awf
awf/                 # Python core, 22 модуля, ~2100 строк
  __init__.py        # package init
  __main__.py        # entry point для python3 -m awf
  cli.py             # argparse dispatcher
  paths.py           # разрешение путей (realpath для symlink)
  config.py          # загрузка config.yaml
  yaml_utils.py      # утилиты для YAML
  pipeline.py        # Stage dataclass, load_stages()
  orchestrator.py    # state machine пайплайна
  signals.py         # polling сигналов, prefix-filtering
  transitions.py     # resolve_transition (policy lookup)
  verify.py          # auto-DONE, detect_work_evidence
  git_utils.py       # git-операции (commit, reset, diff)
  todos.py           # list_active_todos, is_closed, has_progress
  cmd_init.py        # команда init
  cmd_start.py       # команды start / continue
  cmd_status.py      # команда status
  cmd_reset.py       # команда reset
  cmd_add_role.py    # команда add-role
  cmd_baseline.py    # команда baseline
  cmd_rollback.py    # команда rollback
  cmd_report.py      # команда report
  opencode_agents.py # управление ~/.config/opencode/opencode.json
templates/           # шаблоны для awf init
  roles/             # supervisor.md, worker.md, reviewer.md, tester.md
  pipelines/         # simple.yaml, full.yaml
  config.default.yaml
  todo-template.md
  done-report.md
  blocked-report.md
protocols/           # спецификации
  communication.md   # спецификация файловой шины
tests/
  e2e/               # 12 тестов, bin/awf как subprocess
  unit/              # 152 теста для awf/*.py
  stubs/opencode     # mock для E2E
docs/history/         # исторические дизайн-документы (до реализации)
BACKLOG.md           # план развития
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

### Auto-DONE (v0.3.2+)

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
├── inbox/                   # TODO от supervisor → worker [gitignored]
├── outbox/                  # DONE/BLOCKED/PROGRESS от worker [gitignored]
├── context/                 # baseline SHA и тест-логи [gitignored]
├── logs/                    # orchestrator.log [gitignored]
└── reports/                 # сводные отчёты [gitignored]
```

Runtime-директории (`inbox/`, `outbox/`, `context/`, `logs/`, `reports/`) не коммитятся в git. Статические файлы (`config.yaml`, `roles/`, `pipelines/`, `phases/`) — коммитятся.

---

## agent-workflow-ui plugin (опционально)

[`agent-workflow-ui`](agent_workflow_ui/) — standalone MCP plugin для opencode,
который даёт supervisor-агенту инструменты визуального взаимодействия с
пользователем: HTML-формы для структурированного ввода и dashboards для
наблюдения за pipeline.

**Status:** v0.1.0 (MVP). См. [`vision/agent-ui-plugin.md`](vision/agent-ui-plugin.md)
для product vision.

### Установка

```bash
pip install -e ./agent_workflow_ui    # в разработке
# или pip install agent-workflow-ui    # после PyPI publish
```

### Настройка

1. **Добавить MCP server** в `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "agent-workflow-ui": {
      "type": "local",
      "command": ["python3", "-m", "agent_workflow_ui"]
    }
  }
}
```

2. **Установить SKILL.md** (LLM policy):

```bash
mkdir -p ~/.config/opencode/skills/agent-workflow-ui
cp agent_workflow_ui/SKILL.md ~/.config/opencode/skills/agent-workflow-ui/SKILL.md
```

После этого supervisor-агент видит 5 tools: `open_form`, `read_submit`,
`cancel_form`, `list_pending_forms`, `list_templates`.

Подробнее: [`agent_workflow_ui/README.md`](agent_workflow_ui/README.md),
[`vision/architecture.md`](vision/architecture.md).

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
# Все тесты (e2e + unit):
python3 -m pytest tests/ -v

# Только E2E (запуск bin/awf как subprocess):
python3 -m pytest tests/e2e/ -v

# Только unit:
python3 -m pytest tests/unit/ -v

# Один конкретный файл:
python3 -m pytest tests/unit/test_signals.py -v

# Coverage (если установлен pytest-cov):
python3 -m pytest tests/ --cov=awf --cov-report=term-missing
```

164 теста: 12 E2E + 152 unit. E2E-тесты используют `tests/stubs/opencode` для mock'а worker'а — реальный opencode не требуется.

**Покрытие кода:** `python3 -m pytest tests/ --cov=awf` формально показывает ~21%, потому что pytest-cov не отслеживает subprocess execution — а именно через subprocess (bin/awf) запускаются все E2E-тесты, покрывающие `cmd_*.py`, `cli.py`, `orchestrator.py`. Реальное покрытие этих модулей обеспечивается E2E. Helper-модули (`signals.py`, `pipeline.py`, `transitions.py`, `verify.py`, `todos.py`, `git_utils.py`, `config.py`, `paths.py`, `yaml_utils.py`) покрыты unit-тестами на 91-100%.

---

## Спецификации и история

- [protocols/communication.md](protocols/communication.md) — спецификация файловой шины (сигналы, naming canonical vs legacy, task lifecycle, safety).
- `docs/history/` — исходные дизайн-документы awf (до реализации, bash era). Историческая справка.
- [BACKLOG.md](BACKLOG.md) — план развития (Tasks 1-7: dashboard, HTTP API, bootstrap, цепочки worker'ов, роли-скиллы, модели, real-time).

---

## Что нового

### v0.4.0 (2026-07-20)

**Миграция bash→Python завершена.** Все 9 команд работают через Python core; `lib/*.sh` (10 файлов, ~1700 строк) удалены.

- **Closes [#1](https://github.com/EnerJizeIT/agentic-workflow/issues/1):** `bin/awf` резолвит симлинки через `os.path.realpath` — install через `ln -s` работает нативно.
- **`awf/` Python package:** 22 модуля, ~2100 строк.
- **164 теста:** 12 E2E (subprocess через `bin/awf`) + 152 unit (все модули `awf/`).
- **`tests/run.sh` удалён** — заменён на pytest.

### Timeline v0.3.x (миграция по волнам)

- **v0.3.4** — weak-spots closure (find_active_todo, status warns, reset --orphans, init model prompt).
- **v0.3.5** — pytest E2E harness + mock opencode stub.
- **v0.3.6** — Wave 4a: `awf status` ported.
- **v0.3.7** — Wave 4b: orchestrator ported (8 модулей).
- **v0.3.8** — Wave 4c: 6 remaining commands ported.
- **v0.3.9** — Wave 4d-part1: 152 unit tests.
- **v0.4.0** — Wave 4d-part2: bash retired.

---

## На что смотреть дальше

Краткий список направлений (детали — в [BACKLOG.md](BACKLOG.md)):

1. **HTML dashboard** — веб-обёртка над CLI.
2. **HTTP API** (`awf serve`) — REST-эндпоинты для dashboard.
3. **Автоинициализация** (`awf bootstrap`) — по файлу требований.
4. **Цепочки worker'ов** — supervisor→worker₁→worker₂→...→supervisor.
5. **Роли-скиллы** — шаблоны для специализированных worker'ов.
6. **Выбор моделей** — привязка LLM к каждой роли.
7. **Real-time статус** — SSE/polling для dashboard.

---

## Лицензия

MIT (см. `pyproject.toml`).
