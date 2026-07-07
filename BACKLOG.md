# BACKLOG: agentic-workflow

> **Для Supervisor:** каждая задача описана на уровне «что построить». Worker самостоятельно определяет реализацию. Опускаться до Find/Replace — только если предыдущие попытки не сработали.

---

## Prohibitions

- DO NOT break existing CLI (`awf init`, `awf start`) without backward compatibility.
- DO NOT introduce new external dependencies (npm packages, Python libs) unless explicitly required by the task.
- DO NOT change the file-bus protocol (`.ready` signals, inbox/outbox structure) without updating `protocols/communication.md` simultaneously.
- DO NOT commit documentation-only changes for tasks that require code implementation.

---

### Task 1 · HTML dashboard как обертка над CLI

**Files:** `dashboard/` (new directory), `bin/awf`

**Description:**

Создать одностраничное HTML-приложение (один файл, без сборщиков и фреймворков), которое служит веб-оберткой над CLI. Пользователь, далекий от терминала, должен через браузер:

- Видеть текущий статус воркфлоу (какая роль активна, что происходит).
- Инициализировать проект (`awf init`).
- Запускать пайплайн (`awf start`).
- Смотреть логи и отчеты из `.agentic/outbox/`.
- Делать rollback и baseline.

Технические требования:
- Один HTML-файл, встроенные CSS + JS (как в скилле `html-presentation`).
- Адаптивная вёрстка.
- Данные получаются через локальный HTTP API (см. Task 2).
- Файл доступен через `awf serve` — запускает простой HTTP-сервер.

**Constraints:**
- Не ломать существующую CLI-функциональность.
- Dashboard — опциональная надстройка; CLI продолжает работать автономно.

**Verify:** `awf serve` запускает сервер, страница открывается, кнопки работают.

**Done when:** Страница отображает статус воркфлоу, позволяет выполнить основные операции (init, start, status, rollback).

---

### Task 2 · Локальный HTTP API для dashboard

**Files:** `lib/server.sh` (new), `bin/awf`

**Description:**

Добавить подкоманду `awf serve` которая запускает минимальный HTTP-сервер на порту (по умолчанию 8080). Сервер предоставляет REST API:

| Endpoint | Method | Описание |
|---|---|---|
| `/api/status` | GET | Текущий статус воркфлоу (роли, сигналы, последний TODO) |
| `/api/init` | POST | Инициализация проекта `{projectDir, pipeline}` |
| `/api/start` | POST | Запуск пайплайна `{taskFile}` |
| `/api/logs` | GET | Лог оркестратора |
| `/api/outbox` | GET | Список DONE/BLOCKED отчетов |
| `/api/report/{id}` | GET | Содержимое конкретного отчета |
| `/api/baseline` | POST | Создание бейзлайна |
| `/api/rollback` | POST | Откат к бейзлайну |
| `/api/models` | GET | Список доступных моделей opencode |

Сервер выполняет соответствующие CLI-команды (`awf status`, `awf init` и т.д.) и возвращает JSON.

**Constraints:**
- Использовать встроенные инструменты (bash `ncat`, `socat`, или Python `http.server` — то, что доступно).
- Не блокировать CLI при работающем сервере (запуск в фоне).
- Кроссплатформенность (Linux/macOS).

**Verify:** Каждый endpoint возвращает корректный JSON.

**Done when:** Все endpoints работают, dashboard может получать данные.

---

### Task 3 · Автоинициализация по файлу требований

**Files:** `lib/init.sh`, `bin/awf`

**Description:**

Пользователь передает файл с требованиями (Markdown/TXT/PDF). Приложение должно:

1. Проверить, есть ли `.agentic/` в текущем каталоге.
2. **Если нет** — автоматически создать структуру:
   - `.agentic/config.yaml` (дефолтный конфиг).
   - `.agentic/pipelines/simple.yaml` (копия из шаблонов).
   - `.agentic/roles/` (копии ролей из шаблонов).
   - `.agentic/inbox/`, `.agentic/outbox/`, `.agentic/logs/`, `.agentic/context/`.
3. Сохранить файл требований как `.agentic/plan.md` — стартовую точку для Supervisor.
4. Supervisor анализирует файл требований и декомпозирует задачи в TODO листы.

Новая подкоманда: `awf bootstrap <requirements-file>` — делает всё вышеописанное.

**Constraints:**
- Если `.agentic/` уже существует — спросить пользователя (перезаписать / продолжить / отмена).
- Поддержать форматы: `.md`, `.txt`, `.pdf`.

**Verify:** `awf bootstrap requirements.md` создает структуру, `plan.md` записан.

**Done when:** Файл требований загружен, структура создана, Supervisor готов к работе.

---

### Task 4 · Выбор сценария пайплайна

**Files:** `lib/orchestrator.sh`, `templates/pipelines/`, `lib/config-loader.sh` (new)

**Description:**

Поддержать выбор сценария выполнения через UI и CLI:

| Сценарий | Описание |
|---|---|
| `supervisor-only` | Только Supervisor генерирует TODO, но не делегирует (ручной режим). |
| `supervisor-worker` | Классический: Supervisor → Worker → Supervisor (ревью). |
| `supervisor-worker*X` | Цепочка: Supervisor → Worker₁ → Worker₂ → ... → Worker_N → Supervisor. Каждый worker применяет свой скилл к одной задаче последовательно. |

В YAML-конфиге:

```yaml
scenario: supervisor-worker*3
workers:
  - role: developer
    skill: backend-developer
  - role: tester
    skill: test-writer
  - role: reviewer
    skill: code-reviewer
```

Оркестратор должен поддерживать передачу задачи между worker'ами: каждый worker работает с одним и тем же TODO, но со своей ролью и контекстом. Результат одного worker'а становится входом для следующего.

**Constraints:**
- Сохранить обратную совместимость: старые конфиги без `scenario` работают как `supervisor-worker`.
- Оркестратор отслеживает прогресс цепочки (worker 1 из 3 → worker 2 из 3 → ...).

**Verify:** Пайплайн с 3 worker'ами выполняется последовательно, каждый видит работу предыдущего.

**Done when:** Все три сценария работают через CLI и отражаются в dashboard.

---

### Task 5 · Роли и скиллы для worker'ов

**Files:** `templates/roles/`, `lib/add-role.sh`, `lib/skill-loader.sh` (new)

**Description:**

Расширить систему ролей для поддержки специализированных worker'ов:

1. Создать шаблонные роли-скиллы:
   - `backend-developer.md` — разработка серверной части.
   - `frontend-developer.md` — фронтенд и UI.
   - `test-writer.md` — написание и исправление тестов.
   - `code-reviewer.md` — ревью кода, поиск багов.
   - `docs-writer.md` — документация.
   - `security-auditor.md` — аудит безопасности.

2. Каждая роль — это Markdown-файл с инструкциями (как существующие `supervisor.md` / `worker.md`).

3. В конфиге пользователь указывает, какие роли участвуют в цепочке и в каком порядке:

```yaml
pipeline:
  - stage: worker
    role: backend-developer
  - stage: worker
    role: test-writer
  - stage: worker
    role: code-reviewer
```

4. Команда `awf add-role <role-name>` копирует шаблон роли в `.agentic/roles/` и добавляет её в конфиг.

5. Supervisor при выборе роли получает соответствующий скилл-файл как инструкцию.

**Constraints:**
- Роли должны быть расширяемыми: пользователь может создавать свои.
- Порядок ролей в пайплайне определяет порядок передачи задач.

**Verify:** `awf add-role test-writer` создает файл, пайплайн использует роль.

**Done when:** 6 шаблонных ролей созданы, можно добавлять и комбинировать их в пайплайне.

---

### Task 6 · Выбор и настройка моделей для участников

**Files:** `lib/model-selector.sh` (new), `bin/awf`, `templates/config.yaml`

**Description:**

Дать возможность выбрать LLM-модель для каждой роли из списка доступных моделей opencode.

Функциональность:

1. Команда `awf models list` — показывает доступные модели (через opencode API или конфигурацию).

2. Конфиг поддерживает привязку модели к роли:

```yaml
models:
  default: "claude-sonnet-4-20250514"
  roles:
    supervisor: "gpt-4.1"
    worker: "claude-sonnet-4-20250514"
    reviewer: "gemini-2.5-pro"
```

3. По умолчанию одна модель для всех, но можно переопределить для конкретной роли.

4. Dashboard показывает, какая модель назначена каждой роли, и позволяет менять.

5. При запуске воркфлоу оркестратор передает модель в соответствующий агент.

**Constraints:**
- Список моделей берется из opencode (проверить, какой API/конфиг используется).
- Если указанная модель недоступна — fallback на default.

**Verify:** `awf models list` выводит список, `awf start` использует правильные модели.

**Done when:** Модели выбираются через UI и CLI, применяются к ролям при запуске.

---

### Task 7 · Real-time обновление статуса на странице

**Files:** `dashboard/index.html`, `lib/server.sh`, `lib/status-watcher.sh` (new)

**Description:**

Dashboard должен обновлять статус в реальном времени, показывая:

1. **Текущая фаза пайплайна:** кто сейчас активен (Supervisor / Worker N / Reviewer).
2. **Передача задач:** визуальное отображение потока (Supervisor → Worker 1 → Worker 2 → ... → Supervisor).
3. **Прогресс:** сколько задач выполнено из общего числа в TODO.
4. **Логи:** последние строки лога оркестратора.
5. **Блокировки:** если задача BLOCKED — показать причину и предложить действия.

Техническая реализация:
- WebSocket или Server-Sent Events (SSE) для real-time обновления.
- Если SSE недоступен — polling каждые 2 секунды через `/api/status`.
- Визуальная схема пайплайна: узлы (роли) соединены стрелками, активный узел подсвечен.

UI-элементы:
- Верхняя панель: текущий этап, прогресс-бар.
- Центральная область: схема пайплайна с анимацией передачи.
- Нижняя панель: лог-вывод (скроллируемый, моноширинный шрифт).
- Боковая панель: список DONE/BLOCKED отчетов с возможностью просмотра.

**Constraints:**
- Не перегружать страницу — обновление только изменившихся данных.
- Грация degradation: если SSE недоступен, polling работает.

**Verify:** При запущенном пайплайне страница обновляется без перезагрузки.

**Done when:** Страница показывает real-time статус, схему пайплайна, логи и отчеты.

---

## Зависимости

```
Task 1 (Dashboard) → Task 2 (API) → Task 7 (Real-time)
Task 3 (Bootstrap)  → независимо
Task 4 (Scenarios)  → Task 5 (Roles)
Task 5 (Roles)      → независимо
Task 6 (Models)     → независимо
```

Рекомендуемый порядок реализации:
1. **Task 2** — API (база для dashboard).
2. **Task 3** — Bootstrap (автоинициализация).
3. **Task 4** — Сценарии (расширение оркестратора).
4. **Task 5** — Роли (шаблоны worker'ов).
5. **Task 1** — Dashboard (сборка UI поверх API).
6. **Task 6** — Модели (выбор LLM).
7. **Task 7** — Real-time (дополнение dashboard).
