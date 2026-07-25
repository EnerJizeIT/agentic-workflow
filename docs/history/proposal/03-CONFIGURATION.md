# Конфигурация — Спецификация

---

## `.agentic/config.yaml`

Основной файл настроек проекта. Создаётся один раз при `awf init`.

```yaml
###############################################################################
# PROJECT
###############################################################################
project:
  name: "AI Code Review Agent"           # отображаемое имя
  root: "."                              # корень проекта (относительно .agentic/)

###############################################################################
# FRAMEWORK
###############################################################################
framework:
  # Путь к фреймворку. Автоопределяется из $PATH или ~/.local/bin/awf.
  # Можно переопределить для разработки:
  # path: "/home/user/projects/agentic-workflow"

###############################################################################
# AGENT MODELS
# Каждая роль может использовать свою модель.
###############################################################################
models:
  supervisor:
    # Supervisor = текущая сессия, не отдельный агент.
    # Этот блок используется только для документации.
    description: "Current session model"

  worker:
    agent_name: "worker"                 # имя агента в opencode
    model: "vllm/llm"                    # провайдер/модель
    temperature: 0.1                     # опционально

  reviewer:
    agent_name: "reviewer"
    model: "vllm/llm"
    temperature: 0.1

  tester:
    agent_name: "tester"
    model: "vllm/llm"
    temperature: 0.1

###############################################################################
# VERIFICATION COMMANDS
# Команды, которые используются для проверки качества кода.
# Если команда не нужна — оставь пустым или удали ключ.
###############################################################################
verification:
  test_cmd: "pytest tests/"              # запуск тестов
  lint_cmd: "ruff check ."               # линтер
  typecheck_cmd: "mypy src/"             # типичекинг
  build_cmd:                             # сборка (Docker, npm и т.д.)
  coverage_cmd: "pytest --cov=src tests/"# покрытие

###############################################################################
# PHASES
# План реализации проекта. Файлы с фазами хранятся в .agentic/phases/.
###############################################################################
phases:
  current: ".agentic/phases/MVP-PHASE.md"

###############################################################################
# PIPELINE
# Какой pipeline использовать по умолчанию для `awf start`.
###############################################################################
default_pipeline: "default"

###############################################################################
# RETRY POLICY
# Глобальная политика повторных попыток.
# Может быть переопределена на уровне стадии в pipeline.
###############################################################################
retry:
  max_attempts: 3                        # макс. попыток на одну стадию
  backoff_seconds: 0                     # пауза между попытками
```

---

## `.agentic/pipelines/<name>.yaml`

Описание последовательности стадий. Проект может иметь несколько pipeline'ов для разных сценариев.

### Простой pipeline (supervisor + worker)

```yaml
name: "simple"
description: "Базовый цикл: планирование → реализация → проверка"

stages:
  - name: "plan"
    role: "supervisor"
    action: "create_todo"
    description: "Supervisor изучает план и создаёт TODO"

  - name: "implement"
    role: "worker"
    action: "execute_todo"
    description: "Worker выполняет TODO"
    on_blocked: "escalate"       # при блокере → supervisor
    max_retries: 3

  - name: "verify"
    role: "supervisor"
    action: "verify_result"
    description: "Supervisor проверяет результат"
    on_approved: "commit_and_next"
    on_rejected: "replan"        # создать новый TODO
```

### Полный pipeline (с reviewer и tester'ом)

```yaml
name: "full-review"
description: "Полный цикл с ревью кода и тестированием"

stages:
  - name: "plan"
    role: "supervisor"
    action: "create_todo"

  - name: "implement"
    role: "worker"
    action: "execute_todo"
    on_blocked: "escalate"
    max_retries: 3

  - name: "review"
    role: "reviewer"
    action: "review_code"
    description: "Reviewer проверяет качество реализации"
    on_approved: "next"
    on_rejected: "rollback_to:implement"   # вернуть к worker
    max_retries: 2

  - name: "test"
    role: "tester"
    action: "run_tests"
    description: "Tester запускает тесты и проверяет coverage"
    on_passed: "next"
    on_failed: "rollback_to:implement"     # вернуть к worker
    max_retries: 2

  - name: "finalize"
    role: "supervisor"
    action: "final_verify"
    description: "Финальная проверка и коммит"
    on_approved: "commit_and_report"
    on_rejected: "rollback_to:implement"
```

### Кастомный pipeline (произвольные роли)

```yaml
name: "with-security"
description: "С проверкой безопасности"

stages:
  - name: "plan"
    role: "supervisor"
    action: "create_todo"

  - name: "implement"
    role: "worker"
    action: "execute_todo"
    on_blocked: "escalate"

  - name: "security-audit"
    role: "security-auditor"
    action: "audit_code"
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
```

---

## Схема поля `action`

Действие определяет, что именно делает роль на этой стадии.

| Action | Роль | Описание |
|---|---|---|
| `create_todo` | supervisor | Изучает plan, создаёт TODO в inbox/ |
| `execute_todo` | worker | Выполняет TODO из inbox/ |
| `verify_result` | supervisor | Проверяет DONE отчёты, принимает решение |
| `final_verify` | supervisor | Финальная проверка + генерация отчёта |
| `review_code` | reviewer | Код-ревью изменений worker'а |
| `run_tests` | tester | Запуск тестов, проверка coverage |
| `audit_code` | security-auditor | Проверка безопасности |
| `custom:<cmd>` | любая | Пользовательское действие (bash команда) |

---

## Схема полей `on_*`

Управляют переходом между стадиями.

| Поле | Значение | Описание |
|---|---|---|
| `on_blocked: "escalate"` | эскалировать | Передать управление supervisor'у |
| `on_blocked: "stop"` | остановить | Остановить pipeline, ждать пользователя |
| `on_blocked: "rollback_to:<stage>"` | откат | Вернуться к указанной стадии |
| `on_approved: "next"` | дальше | Перейти к следующей стадии |
| `on_approved: "commit_and_next"` | коммит + дальше | Сделать git commit, затем следующая стадия |
| `on_approved: "commit_and_report"` | коммит + отчёт | Финальный commit + генерация отчёта |
| `on_rejected: "replan"` | перепланировать | Supervisor создаёт новый TODO |
| `on_rejected: "rollback_to:<stage>"` | откат | Вернуться к стадии |
| `on_passed: "next"` | дальше | (для tester) тесты прошли |
| `on_failed: "rollback_to:<stage>"` | откат | (для tester) тесты упали |

---

## `.agentic/phases/<name>.md`

Файлы с планом реализации проекта. НЕ часть фреймворка — это контекст конкретного проекта. Фреймворк просто знает путь к текущему файлу через `phases.current` в конфиге.

Пример структуры:

```
.agentic/phases/
├── MVP-PHASE.md         # шаги MVP: [ ] шаг 1, [x] шаг 2, ...
├── POST-MVP-PHASE.md    # пост-MVP шаги
└── SPRINT-2026-07.md    # спринт
```

Формат файла произвольный — это markdown, который читает supervisor. Фреймворк не парсит его.
