# Команды CLI — Спецификация

---

## `awf init`

Инициализирует агентный workflow в текущем проекте. Создаёт папку `.agentic/` с конфигурацией и шаблонами ролей.

```
awf init [опции]
```

### Опции

| Флаг | Описание | По умолчанию |
|---|---|---|
| `--template <name>` | Шаблон pipeline: `simple` (supervisor+worker) или `full` (с reviewer+tester) | `simple` |
| `--pipeline-name <name>` | Имя pipeline | `default` |
| `--force` | Пересоздать, если `.agentic/` уже существует | нет |
| `--dry-run` | Показать, что будет создано, не создавая | нет |

### Что делает

1. Проверяет, что текущая директория — git-репозиторий
2. Задаёт вопросы о проекте (интерактивно):
   - Название проекта
   - Команда тестирования (`pytest tests/`)
   - Команда линтинга (`ruff check .`)
   - Команда type checking (`mypy src/`)
   - Команда сборки (опционально)
3. Копирует шаблоны из фреймворка в `.agentic/`:
   - `config.yaml` — заполненный ответами пользователя
   - `roles/supervisor.md` и `roles/worker.md` (+ reviewer/tester для `full`)
   - `pipelines/default.yaml` — по выбранному шаблону
4. Создаёт пустые runtime-папки: `inbox/`, `outbox/`, `context/`, `logs/`, `reports/`
5. Добавляет в `.gitignore` игнорирование runtime-папок

### Результат

```
Создано .agentic/
  ├── config.yaml
  ├── roles/
  │   ├── supervisor.md
  │   └── worker.md
  ├── pipelines/
  │   └── default.yaml
  ├── inbox/
  ├── outbox/
  ├── context/
  ├── logs/
  └── reports/

Добавлено в .gitignore:
  .agentic/inbox/
  .agentic/outbox/
  .agentic/context/
  .agentic/logs/
  .agentic/reports/

Следующий шаг: awf start
```

---

## `awf start`

Запускает pipeline от начала. Это основная команда, которую пользователь вызывает для старта агентной разработки.

```
awf start [опции]
```

### Опции

| Флаг | Описание | По умолчанию |
|---|---|---|
| `--pipeline <name>` | Какой pipeline запустить | `default` |
| `--from-stage <name>` | Начать с конкретной стадии | первая |
| `--auto` | Автоматический режим (без подтверждения между стадиями) | интерактивный |
| `--phase-file <path>` | Путь к файлу с планом | из config.yaml |

### Что делает

1. Читает `.agentic/config.yaml` и `.agentic/pipelines/<name>.yaml`
2. Находит первую стадию (или ту, что указана в `--from-stage`)
3. Для каждой стадии:
   a. Определяет роль и действие
   b. Если роль = supervisor → работает в текущей сессии
   c. Если роль = agent → запускает `opencode run --agent <role> ...`
   d. Ждёт появления сигнала в `outbox/` (DONE/BLOCKED/APPROVED/REJECTED)
   e. По сигналу решает: следующая стадия, rollback, или остановка
4. По завершении всех стадий генерирует отчёт в `.agentic/reports/status.md`

### Поведение при блокере

Если стадия вернула BLOCKED:
- Если в конфиге `on_blocked: "escalate"` → передаёт supervisor'у
- Если `on_blocked: "rollback_to:<stage>"` → возвращается к указанной стадии
- Если `on_blocked: "stop"` → останавливается, ждёт решения пользователя

### Лимит повторных попыток

По умолчанию: 3 попытки на одну стадию перед остановкой. Настраивается в pipeline:

```yaml
stages:
  - name: "implement"
    role: "worker"
    max_retries: 3                     # сколько раз перепробовать
    retry_on: ["blocked", "rejected"]  # при каких сигналах
```

---

## `awf continue`

Продолжает прерванный pipeline. Используется, когда `awf start` был прерван (Ctrl+C, ошибка, перезапуск).

```
awf continue [опции]
```

### Опции

| Флаг | Описание |
|---|---|
| `--pipeline <name>` | Какой pipeline продолжать |
| `--skip-current` | Пропустить текущую стадию |

### Что делает

1. Сканирует `inbox/` и `outbox/` на наличие незавершённых задач
2. Определяет, на какой стадии остановился pipeline
3. Продолжает с этой стадии

---

## `awf status`

Показывает текущее состояние workflow без запуска чего-либо.

```
awf status
```

### Вывод

```
Pipeline: default
Stage: implement (worker)
Task: TODO-0003
Status: ⏳ in progress

Active tasks:
  TODO-0001 ✅ DONE  (approved by supervisor)
  TODO-0002 ✅ DONE  (approved by supervisor)
  TODO-0003 ⏳ WORKER RUNNING

Completed this session: 2
Blocked: 0
Total iterations: 3
```

---

## `awf report`

Показывает сводный отчёт о проделанной работе. Предназначен для пользователя, который хочет понять, что сделали агенты.

```
awf report [опции]
```

### Опции

| Флаг | Описание | По умолчанию |
|---|---|---|
| `--format <text\|markdown>` | Формат вывода | `text` |
| `--since <date>` | Отчёты начиная с даты | начало сессии |
| `--task <id>` | Отчёт по конкретной задаче | все |

### Вывод (консоль)

```
═══════════════════════════════════════════
  Agentic Workflow Report
  Project: AI Code Review Agent
  Generated: 2026-07-07 15:30
═══════════════════════════════════════════

Progress: ████████░░ 8/10 steps completed

Tasks:
  TODO-0001  ✅  Project skeleton        (worker → approved)
  TODO-0002  ✅  Health endpoint         (worker → approved)
  TODO-0003  🔄  API routes              (worker running)
  TODO-0004  ⬜  GitLab integration      (pending)
  TODO-0005  ⬜  OCR skill               (pending)

Blockers: none
Rollbacks: 1 (TODO-0002-fix1 → resolved)

Files changed: 12
Tests passing: 23/23
Coverage: 78%
```

---

## `awf add-role`

Генерирует шаблон новой роли. Используется, когда нужно добавить новую функцию в pipeline (например, «security auditor» или «docs writer»).

```
awf add-role <role-name> [опции]
```

### Опции

| Флаг | Описание |
|---|---|
| `--description <text>` | Описание роли |
| `--model <model>` | Модель для этой роли |

### Что делает

1. Создаёт `.agentic/roles/<role-name>.md` по шаблону
2. Предлагает добавить роль в существующий pipeline или создать новый
3. Генерирует оповещение о том, что нужно заполнить инструкцию

### Сгенерированный шаблон роли

```markdown
# ROLE: <role-name>

**Роль:** <описание>
**Агент opencode:** `<role-name>`
**Модель:** <model>

## 1. Кто ты

<заполни: описание ответственности роли>

## 2. Что ты делаешь

<заполни: входные данные → действия → выходные данные>

## 3. Вход

Эта роль получает на вход:
- <что приходит из предыдущей стадии>

## 4. Действия

<заполни: что делать с входными данными>

## 5. Выход

По завершении создай один из файлов:
- `.agentic/outbox/APPROVED-{NNNN}.md` + `.ready` — успешно
- `.agentic/outbox/REJECTED-{NNNN}.md` + `.ready` — проблемы
- `.agentic/outbox/BLOCKED-{NNNN}.md` + `.ready` — нужен supervisor

## 6. Запреты

<заполни: что НЕЛЬЗЯ делать>
```

---

## `awf baseline`

Создаёт снимок текущего состояния проекта перед выполнением задачи.

```
awf baseline <task-id>
```

### Что сохраняет

- Git SHA HEAD
- Git status (изменённые файлы)
- Результат тестов (если команда настроена)
- Список установленных пакетов (если Python-проект)

### Где хранится

`.agentic/context/BASELINE-{task-id}.sha`, `.status`, `.tests.log`, `.env.log`

---

## `awf rollback`

Откатывает изменения задачи к baseline.

```
awf rollback <task-id> [опции]
```

### Опции

| Флаг | Описание |
|---|---|
| `--hard` | `git reset --hard` к baseline SHA |
| `--soft` | `git reset` к baseline SHA (сохраняет changes) |
| `--dry-run` | Показать, что будет откатено |

---

## `awf reset`

Полная очистка runtime-данных. Используется, чтобы начать pipeline заново.

```
awf reset [опции]
```

### Опции

| Флаг | Описание |
|---|---|
| `--tasks-only` | Очистить только inbox/outbox |
| `--full` | Очистить всё, включая context и logs |
| `--keep-phases` | Не трогать phases/ (по умолчанию) |

### Что очищает

- `inbox/*` — все TODO и ACK
- `outbox/*` — все DONE, BLOCKED, REVIEW, TEST
- `context/*` — все baseline'ы
- `logs/*` — все логи
- `reports/*` — все отчёты
