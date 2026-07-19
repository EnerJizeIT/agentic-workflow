# Agentic Workflow Framework

Declarative multi-agent pipeline: **Supervisor plans → Worker implements → Supervisor verifies**. Communication via files on disk. Pure bash core (YAML config + Markdown instructions); YAML parsing uses `yq` when available, otherwise falls back to `python3 + PyYAML`.

---

## Requirements

- **bash** 4+ (arrays, `mapfile`-free parsing).
- **git** (the target project must be a git repo).
- **opencode** CLI on `$PATH` (only needed for `awf start` / `awf continue`).
- A YAML backend (one of):
  - `yq` (preferred) — single static binary, install via your package manager, **or**
  - `python3` with `PyYAML` (`pip install pyyaml`) — already present on most dev machines.

`awf` auto-detects which backend is available. Without one, YAML parsing
errors out loudly instead of silently mis-parsing with regex.

---

## Install

```bash
# Clone the framework (if not already)
git clone git@github.com:EnerJizeIT/agentic-workflow.git
cd agentic-workflow

# Make CLI available system-wide
cp bin/awf ~/.local/bin/awf
chmod +x ~/.local/bin/awf

# Verify
awf help
```

**Примечание:** если `~/.local/bin/` нет в `$PATH`, добавь в `~/.bashrc`:
```bash
export PATH="$HOME/.local/bin:$PATH"
```

---

## First run in a project

### 1. Go to your project

```bash
cd /path/to/your-project   # must be a git repository
```

### 2. Initialize

```bash
awf init --template simple   # supervisor + worker
# or
awf init --template full     # + reviewer + tester
```

Ответь на вопросы (название проекта, команды тестов/линтинга). Будет создана структура:

```
.agentic/
├── config.yaml           # настройки проекта
├── roles/                # инструкции для ролей
│   ├── supervisor.md
│   └── worker.md
├── pipelines/            # определение пайплайна
│   └── default.yaml
├── inbox/                # задачи от supervisor → worker (gitignored)
├── outbox/               # отчеты от worker → supervisor (gitignored)
├── context/              # baseline'ы (gitignored)
├── logs/                 # логи оркестратора (gitignored)
└── reports/              # итоговые отчеты (gitignored)
```

### 3. Write your plan

Создай файл с планом реализации. По умолчанию путь: `.agentic/phases/plan.md`.

Пример:

```markdown
# Implementation Plan

- [ ] Step 1: Create project skeleton
- [ ] Step 2: Implement health endpoint
- [ ] Step 3: Add API routes
- [ ] Step 4: GitLab integration
```

Этот файл читает Supervisor, чтобы определить следующий шаг.

### 4. Start the pipeline

```bash
awf start
```

Оркестратор читает стадии из `.agentic/pipelines/default.yaml` и выполняет их последовательно.

Что произойдет:

1. **Supervisor stage (ты):** orchestrator покажет инструкцию. Ты читаешь `.agentic/roles/supervisor.md`, изучаешь проект, создаёшь TODO и кладёшь его в `.agentic/inbox/TODO-0001.md` + `.agentic/inbox/TODO-0001.ready`. Нажми Enter.

2. **Worker stage (автоматически):** orchestrator запускает отдельный агент opencode. Worker читает TODO, реализует задачи, пишет DONE или BLOCKED в `.agentic/outbox/`.

3. **Если worker вернул BLOCKED:** оркестратор эскалирует на тебя. Ты анализируешь проблему, создаёшь исправленный TODO. Worker перезапускается (до `max_retries` попыток).

4. **Supervisor verify (ты):** orchestrator просит проверить результат. Ты проверяешь код, запускаешь тесты, решаешь: approve / fix / rollback.

**Опции запуска:**

```bash
awf start                          # пайплайн по умолчанию из config.yaml
awf start --pipeline full-review   # конкретный пайплайн
awf start --from-stage review      # начать с указанной стадии
awf start --auto                   # пропустить интерактивные паузы supervisor'а
awf start --timeout 7200           # таймаут агента (сек, по умолчанию 3600)
```

После завершения итерации запусти `awf start` снова для следующего шага.

---

## How it works

```
Supervisor → [TODO в inbox/] → Worker → [DONE/BLOCKED в outbox/] → Supervisor
                                                          ↓
                                                    Если BLOCKED:
                                                    Supervisor replan → новый TODO
                                                    Worker перезапускается (до max_retries)
```

Оркестратор читает стадии из YAML файла пайплайна и выполняет их последовательно. Переходы между стадиями определяются правилами `on_*` в конфиге (`next`, `rollback_to`, `escalate`, `stop`).

Все общение между ролями идет через файлы. **Каноничный формат сигналов** — `{PREFIX}-TODO-{NNNN}` (например `DONE-TODO-0001`, `BLOCKED-TODO-0002`). Legacy-форма `{PREFIX}-{NNNN}` (без `TODO-`) тоже принимается для обратной совместимости.

| Signal | Файл | Кто пишет |
|---|---|---|
| `TASK_READY`     | `inbox/TODO-{NNNN}.md` + `inbox/TODO-{NNNN}.ready` | Supervisor |
| `TASK_DONE`      | `outbox/DONE-TODO-{NNNN}.md` + `.ready`           | Worker/Reviewer/Tester |
| `TASK_BLOCKED`   | `outbox/BLOCKED-TODO-{NNNN}.md` + `.ready`        | Worker/Reviewer/Tester |
| `TASK_ACK`       | `inbox/ACK-TODO-{NNNN}.ready`                     | Supervisor |
| `TASK_PROGRESS`  | `outbox/PROGRESS-TODO-{NNNN}.md` (append-only)    | Worker |
| `REVIEW_APPROVED`/`REVIEW_REJECTED` | `outbox/REVIEW-{APPROVED\|REJECTED}-TODO-{NNNN}.md` | Reviewer |
| `TEST_PASSED`/`TEST_FAILED`         | `outbox/TEST-{PASSED\|FAILED}-TODO-{NNNN}.md`      | Tester |

- **inbox/** — задачи от supervisor к worker'у.
- **outbox/** — отчеты от worker'а к supervisor'у.
- **.ready** сигнал — файл-триггер, сообщающий о готовности (создаётся ПОСЛЕ `.md`).

Подробнее: `protocols/communication.md` (включая §3.6 «Signal naming — canonical vs legacy»).

---

## Commands

| Command | Описание |
|---|---|
| `awf init [--template simple\|full]` | Создать `.agentic/` в проекте |
| `awf start [опции]` | Запустить пайплайн из YAML конфига |
| `awf start --pipeline <name>` | Конкретный пайплайн |
| `awf start --from-stage <name>` | Начать с указанной стадии |
| `awf start --auto` | Без интерактивных пауз supervisor'а |
| `awf start --timeout <sec>` | Таймаут агента (по умолчанию 3600) |
| `awf continue` | Продолжить прерванный пайплайн |
| `awf status` | Текущее состояние воркфлоу |
| `awf report` | Сводный отчет о работе |
| `awf baseline <id>` | Снимок состояния перед задачей |
| `awf rollback <id>` | Откат к baseline |
| `awf reset` | Очистить runtime-данные |
| `awf reset --tasks-only` | Очистить только inbox/outbox |
| `awf reset --orphans [--force]` | Удалить TODO без прогресс-лога (безопасно: только никогда не запускавшиеся) |
| `awf add-role <name>` | Добавить новую роль |

---

## Key files

| File | Что это |
|---|---|
| `.agentic/config.yaml` | Настройки: модели, команды верификации, путь к плану |
| `.agentic/pipelines/default.yaml` | Порядок стадий (plan → implement → verify) |
| `.agentic/roles/supervisor.md` | Инструкция для тебя (supervisor) |
| `.agentic/roles/worker.md` | Инструкция для агента-worker'а |
| `.agentic/phases/plan.md` | Твой план реализации (чеклист шагов) |

---

## Typical session

```bash
# Первый запуск
cd my-project
awf init --template simple
# ответь на вопросы → создан .agentic/

# Напиши план
vim .agentic/phases/plan.md

# Запусти пайплайн
awf start
# → Supervisor stage: создай TODO, нажми Enter
# → Worker stage: агент работает автоматически (может занять время)
# → Если BLOCKED: supervisor создает исправленный TODO, worker перезапускается
# → Verify stage: проверь результат, нажми Enter

# Следующая итерация
awf start

# Конкретный пайплайн
awf start --pipeline full-review

# Начать с конкретной стадии
awf start --from-stage review

# Без пауз (для автоматизации)
awf start --auto

# Посмотри статус
awf status

# Если нужно откатиться
awf rollback TODO-0001

# Полный сброс
awf reset
```

---

## Architecture

```
bin/awf              # CLI entry point
lib/
  yaml.sh            # YAML backend (yq preferred, python3+PyYAML fallback)
  init.sh            # создание .agentic/
  orchestrator.sh    # ядро: выполнение пайплайна
  baseline.sh        # снимки состояния
  rollback.sh        # откат к baseline
  status.sh          # текущий статус
  report.sh          # генерация отчетов
  add-role.sh        # добавление ролей
  reset.sh           # очистка runtime
templates/
  roles/             # шаблоны инструкций (supervisor, worker, reviewer, tester)
  pipelines/         # шаблоны пайплайнов (simple, full)
  todo-template.md   # шаблон TODO
protocols/
  communication.md   # спецификация файловой шины
proposal/            # детальная спецификация (архитектура, конфиги, роли)
tests/
  run.sh             # bash test runner (zero-dep)
BACKLOG.md           # план развития фреймворка
```

---

## Roles

### Supervisor (ты)

Работает в текущей сессии. Не отдельный процесс. Твои задачи:
- Изучить состояние проекта и план.
- Определить следующий шаг.
- Создать TODO с описанием задачи (не Find/Replace, а "что построить").
- Проверить результат работы worker'а.

Инструкция: `.agentic/roles/supervisor.md`.

### Worker (агент)

Запускается как отдельный процесс opencode. Получает TODO, самостоятельно проектирует и реализует решение. Возвращает DONE или BLOCKED.

Worker — capable developer model. Он получает **"what to build"** и сам решает **"how"**. Supervisor опускается до точных Find/Replace только при повторных неудачах.

Инструкция: `.agentic/roles/worker.md`.

### Reviewer / Tester (опционально, `--template full`)

Reviewer проверяет качество кода. Tester запускает тесты и сравнивает с baseline.

---

## Task modes (Supervisor → Worker)

| Mode | Когда использовать | Что получает Worker |
|---|---|---|
| **A: High-level** (по умолчанию) | Обычные задачи | Описание "что построить", ограничения, verify |
| **B: Detailed** | Сложные задачи, несколько файлов | Архитектурные заметки, референсы, паттерны |
| **C: Find/Replace** (fallback) | Предыдущие попытки не сработали | Точные блоки кода для замены |

## Progress tracking

Worker пишет `.agentic/outbox/PROGRESS-{NNNN}.md` после каждого выполненного Task. Файл append-only, Supervisor может читать его в любое время.

```bash
awf status          # показывает прогресс для активных задач
cat .agentic/outbox/PROGRESS-TODO-0001.md   # полный лог
```

## 3-Strike Error Protocol

Worker не эскалирует на первую ошибку. Протокол:

```
Attempt 1: Diagnose & Fix → Attempt 2: Alternative Approach → Attempt 3: Broader Rethink → Escalate
```

Каждая попытка логируется. Supervisor видит историю в BLOCKED-отчёте.

## Session Recovery

Если worker-процесс упал, он восстанавливается с `PROGRESS-{NNNN}.md` при перезапуске — пропускает выполненные задачи, продолжает с первого незавершённого.

---

## Testing

The framework ships a zero-dependency bash test runner for its core logic
(YAML parsing, stage parsing, signal classification, transition resolution,
stale-signal filtering):

```bash
./tests/run.sh
```

No `bats` or other test framework required — just bash + a YAML backend.
The run is hermetic (uses a `mktemp` dir) and safe to invoke from anywhere.

---

## Backlog

План развития фреймворка: `BACKLOG.md`.

Текущие направления:
1. HTML dashboard — веб-обертка над CLI.
2. Автоинициализация по файлу требований (`awf bootstrap`).
3. Цепочки worker'ов (supervisor→worker₁→worker₂→...→supervisor).
4. Выбор моделей opencode для каждой роли.
5. Real-time обновление статуса.
