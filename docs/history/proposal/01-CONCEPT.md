# Agentic Workflow Framework — Концепция

**Версия:** 0.1 (draft)  
**Статус:** на согласовании

---

## Проблема

Сейчас методология supervisor/worker зашита в папку `AGENTIC-WORKFLOW/` конкретного проекта. Все пути, команды verify, инструкции ролей привязаны к «AI Code Review Agent». Чтобы использовать этот подход для другого проекта — нужно копировать и вручную адаптировать. Невозможно развивать методологию независимо от проектов.

## Цель

Отделить **методологию агентной разработки** от **конкретного проекта**, чтобы:

1. Методологию можно было развивать, версионировать и передавать коллегам отдельно
2. Любой проект подключал workflow через простой конфиг
3. Набор ролей и их порядок были гибкими: от простого supervisor/worker до цепочки с reviewer и tester'ом
4. Пользователь запускал всё одной командой и получал отчёт

---

## Ключевые принципы

### 1. Декларативность

Workflow описывается конфигурацией (YAML), а не кодом. Добавить роль — создать markdown-файл с инструкциями и добавить строку в конфиг. Не нужно писать Python.

### 2. Файловая шина

Агенты общаются через файлы на диске (`inbox/`, `outbox/`). Это надёжно, просто отлаживать и не требует дополнительной инфраструктуры (Redis, Kafka). Каждый агент — отдельный процесс, который может упасть и перезапуститься.

### 3. Независимость ролей

Каждая роль — самодостаточный markdown-файл с инструкциями. Роль не знает о других ролях, кроме того, куда она кладёт результат. Оркестратор решает, кто следующий.

### 4. Минимальный порог входа

Пользователь, который впервые видит фреймворк, должен уметь:
- Инициализировать проект за одну команду
- Запустить workflow за одну команду
- Понять статус по одной команде

Всё остальное — опционально.

### 5. Гибкость pipeline

Pipeline — это последовательность стадий. Каждая стадия вызывает роль. Количество ролей и их порядок определяются конфигом проекта:

```
Простой:     Supervisor → Worker → Supervisor
Расширенный: Supervisor → Worker → Reviewer → Tester → Supervisor
Произвольный: [любая комбинация]
```

---

## Архитектура

```
┌─────────────────────────────────────────────────────────────┐
│  Пользователь                                                │
│  - awf init    (один раз)                                   │
│  - awf start   (запуск pipeline)                            │
│  - awf status  (проверка состояния)                         │
│  - awf report  (отчёт)                                     │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  Orchestrator (awf)                                         │
│                                                              │
│  Читает .agentic/config.yaml                                │
│  Читает .agentic/pipelines/<name>.yaml                      │
│  Последовательно запускает стадии:                          │
│    1. Находит инструкцию роли в .agentic/roles/             │
│    2. Генерирует задачу из контекста                         │
│    3. Запускает opencode agent                               │
│    4. Ждёт DONE / BLOCKED в outbox/                         │
│    5. Переходит к следующей стадии или rollback             │
└──────────────────────┬──────────────────────────────────────┘
                       │
          ┌────────────┼────────────┐
          ▼            ▼            ▼
     ┌─────────┐ ┌─────────┐ ┌─────────┐
     │Worker   │ │Reviewer │ │Tester   │  ← каждая роль = отдельный процесс
     │(код)    │ │(ревью)  │ │(тесты)  │     opencode run --agent <role>
     └─────────┘ └─────────┘ └─────────┘
          │            │            │
          └────────────┴────────────┘
                       │
                       ▼
              Файловая шина (.agentic/inbox/, outbox/)
```

---

## Структура файлов

### Фреймворк (отдельный репозиторий)

```
agentic-workflow/
├── bin/awf                        # CLI entry point
├── lib/
│   ├── init.sh                    # логика "awf init"
│   ├── orchestrator.sh            # логика pipeline execution
│   ├── baseline.sh                # создание baseline snapshot
│   ├── verify.sh                  # regression verify
│   ├── rollback.sh                # откат к baseline
│   └── status.sh                  # вывод статуса
├── templates/
│   ├── config.default.yaml        # шаблон конфига проекта
│   ├── pipeline.simple.yaml       # шаблон: supervisor + worker
│   ├── pipeline.full.yaml         # шаблон: supervisor + worker + reviewer + tester
│   ├── roles/
│   │   ├── supervisor.md          # абстрактная инструкция
│   │   ├── worker.md              # абстрактная инструкция
│   │   ├── reviewer.md            # абстрактная инструкция
│   │   └── tester.md              # абстрактная инструкция
│   ├── todo-template.md           # шаблон TODO
│   ├── done-report.md             # шаблон DONE отчёта
│   └── blocked-report.md          # шаблон BLOCKED отчёта
├── protocols/
│   └── communication.md           # протокол файловой шины
└── README.md
```

### Проект после `awf init`

```
my-project/
├── .agentic/
│   ├── config.yaml                # настройки проекта
│   ├── roles/                     # инструкции ролей (копия + локальные правки)
│   │   ├── supervisor.md
│   │   └── worker.md
│   ├── pipelines/                 # определение pipeline'а
│   │   └── default.yaml
│   ├── phases/                    # план реализации проекта
│   │   └── MVP-PHASE.md
│   ├── inbox/                     # runtime: задачи → агентам
│   ├── outbox/                    # runtime: отчёты ← от агентов
│   ├── context/                   # runtime: baseline, снимки
│   ├── logs/                      # runtime: логи итераций
│   └── reports/                   # runtime: суммари для пользователя
├── src/...                        # код проекта
└── .gitignore                     # содержит .agentic/inbox/, outbox/, context/, logs/, reports/
```

---

## Конфигурация

### `.agentic/config.yaml`

```yaml
project:
  name: "My Project"
  root: "."

framework:
  path: "~/projects/agentic-workflow"    # путь к фреймворку (автоопределяется)

worker:
  agent_name: "worker"                   # имя opencode агента
  model: "vllm/llm"                      # модель

verification:
  test_cmd: "pytest tests/"
  lint_cmd: "ruff check ."
  typecheck_cmd: "mypy src/"
  build_cmd:                             # опционально

phases:
  current: ".agentic/phases/MVP-PHASE.md"
```

### `.agentic/pipelines/default.yaml`

```yaml
name: "default"
description: "Supervisor планирует, Worker выполняет, Supervisor проверяет"

stages:
  - name: "plan"
    role: "supervisor"
    action: "create_todo"               # действие: создать TODO

  - name: "implement"
    role: "worker"
    action: "execute_todo"              # действие: выполнить TODO
    on_blocked: "escalate"              # при блокере: эскалировать supervisor'у

  - name: "verify"
    role: "supervisor"
    action: "verify_result"             # действие: проверить результат
    on_approved: "commit_and_next"      # если ок: коммит и следующий шаг
    on_rejected: "replan"              # если нет: supervisor создаёт новый TODO
```

Альтернативный pipeline с reviewer и tester'ом:

```yaml
name: "full-review"
description: "Полный цикл с ревью и тестированием"

stages:
  - name: "plan"
    role: "supervisor"
    action: "create_todo"

  - name: "implement"
    role: "worker"
    action: "execute_todo"
    on_blocked: "escalate"

  - name: "review"
    role: "reviewer"
    action: "review_code"
    on_approved: "next"
    on_rejected: "rollback_to:implement"

  - name: "test"
    role: "tester"
    action: "run_tests"
    on_passed: "next"
    on_failed: "rollback_to:implement"

  - name: "finalize"
    role: "supervisor"
    action: "final_verify"
    on_approved: "commit_and_report"
    on_rejected: "rollback_to:implement"
```

---

## Команды CLI

| Команда | Описание | Когда используется |
|---|---|---|
| `awf init [--template simple\|full]` | Создать `.agentic/` с шаблонами | Один раз при старте проекта |
| `awf start [--pipeline <name>]` | Запустить pipeline от начала | Основной способ работы |
| `awf continue` | Продолжить прерванный pipeline | После interruptions |
| `awf status` | Показать текущее состояние | В любой момент |
| `awf report` | Показать сводный отчёт | По завершении или в процессе |
| `awf add-role <name>` | Сгенерировать шаблон новой роли | При расширении pipeline |
| `awf baseline <task-id>` | Создать baseline snapshot | Ручной вызов (auto в pipeline) |
| `awf rollback <task-id>` | Откатить к baseline | При проблемах |
| `awf reset` | Очистить runtime (inbox/outbox/logs) | Начать сначала |

---

## Роли

### Supervisor (стратег)

**Что делает:** Понимает проект, определяет следующий шаг, создаёт TODO с точными диффами, проверяет результаты, принимает решения.

**Кто запускает:** Текущая сессия (пользователь или сильная модель). Не отдельный агент.

**Ввод:** План проекта (`phases/`), последний отчёт из `outbox/`.

**Вывод:** `inbox/TODO-NNNN.md` + `inbox/TODO-NNNN.ready`.

### Worker (исполнитель)

**Что делает:** Берёт TODO из `inbox/`, применяет Find/Replace через edit tool, запускает verify, пишет DONE/BLOCKED.

**Кто запускает:** Opencode-агент (слабая/локальная модель).

**Ввод:** `inbox/TODO-NNNN.md`.

**Вывод:** `outbox/DONE-NNNN.md` или `outbox/BLOCKED-NNNN.md`.

### Reviewer (ревьюер) — опционально

**Что делает:** Проверяет, что worker реализовал TODO корректно: код соответствует задаче, качество хорошее, ничего не упущено. Может внести мелкие правки.

**Кто запускает:** Opencode-агент.

**Ввод:** TODO + diff изменений worker'а.

**Вывод:** `outbox/REVIEW-APPROVED-NNNN.md` или `outbox/REVIEW-REJECTED-NNNN.md` с комментариями.

### Tester (тестировщик) — опционально

**Что делает:** Запускает тесты, проверяет coverage, ищет regressions. Не читает TODO — проверяет только работающий код.

**Кто запускает:** Opencode-агент или просто скрипт.

**Ввод:** diff изменений + тестовый набор из конфига.

**Вывод:** `outbox/TEST-PASSED-NNNN.md` или `outbox/TEST-FAILED-NNNN.md`.

---

## Жизненный цикл одной итерации

```
Пользователь: awf start
                │
                ▼
        ╔═══════════════╗
        ║ Orchestrator  ║  читает pipeline, находит первую стадию
        ╚═══════╤═══════╝
                │
                ▼
        ╔═══════════════╗
        ║ Supervisor    ║  (текущая сессия)
        ║ - читает план ║
        ║ - создаёт TODO║
        ║ - кладёт в    ║
        ║   inbox/      ║
        ╚═══════╤═══════╝
                │  TODO-N.ready
                ▼
        ╔═══════════════╗
        ║ Orchestrator  ║  видит .ready → запускает следующую стадию
        ╚═══════╤═══════╝
                │
                ▼
        ╔═══════════════╗
        ║ Worker        ║  (opencode agent)
        ║ - читает TODO ║
        ║ - edit tool   ║
        ║ - verify      ║
        ║ - пишет DONE  ║
        ╚═══════╤═══════╝
                │  DONE-N.ready
                ▼
        ╔═══════════════╗
        ║ Orchestrator  ║  видит .ready → следующая стадия?
        ╚═══════╤═══════╝
                │
    ┌───────────┼───────────┐
    ▼           ▼           ▼
╔═══════╗ ╔═══════╗ ╔═══════╗
║Reviewer║ ║Tester ║ ║Superv.║  ← зависит от pipeline
╚═══════╝ ╚═══════╝ ╚═══════╝
    │           │           │
    └───────────┴───────────┘
                │
                ▼
        ╔═══════════════╗
        ║ Orchestrator  ║  все стадии пройдены?
        ║ - генерирует  ║
        ║   report      ║
        ╚═══════╤═══════╝
                │
                ▼
         .agentic/reports/status.md
```

---

## Протокол файловой шины

Описание сигналов, директорий и правил обмена см. в `protocols/communication.md` (переносится из текущего `COMMUNICATION-PROTOCOL.md` без изменений — этот слой уже абстрактный и не зависит от проекта).

---

## Что НЕ входит в фреймворк

- **Бизнес-логика проекта.** Фреймворк не знает, что такое «AI Code Review Agent» или любой другой проект.
- **Архитектура приложения.** Фреймворк не диктует, как устроен код проекта.
- **Выбор моделей.** Фреймворк не навязывает конкретные модели — это настройка в конфиге.
- **Хранение кода.** Фреймворк работает с любым git-репозиторием.
