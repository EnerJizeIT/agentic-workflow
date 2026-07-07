# Роли — Спецификация

Каждая роль описывается markdown-файлом в `.agentic/roles/<role-name>.md`. Фреймворк не парсит эти файлы — они передаются как инструкции агенту через `opencode run --file`.

---

## Supervisor

**Файл:** `.agentic/roles/supervisor.md`  
**Запуск:** текущая сессия (не отдельный процесс)  
**Модель:** любая, обычно самая сильная доступная

### Ответственность

Supervisor — стратег и архитектор. Он не пишет код. Его работа:

1. **Понимание состояния.** Читаet план проекта (`phases.current`), последний отчёт из `outbox/`, git status.
2. **Планирование.** Определяет следующий минимальный шаг к цели.
3. **Создание TODO.** Формирует чёткий TODO-лист с exact-match Find/Replace блоками.
4. **Верификация.** Проверяет результаты worker'а самостоятельно (тесты, diff, архитектура).
5. **Принятие решений.** Continue / fix / rollback / ask_user.

### Входные данные

Orchestrator передаёт supervisor'у контекст:
- Путь к файлу фаз: `{phases.current}`
- Последний отчёт worker'а: `{last_outbox_file}` (если есть)
- Текущий git status

### Выходные данные

- `inbox/TODO-{NNNN}.md` — задача для worker'а
- `inbox/TODO-{NNNN}.ready` — сигнал готовности
- При верификации: `inbox/ACK-{NNNN}.ready` — подтверждение

### Шаблон инструкции

```markdown
# SUPERVISOR: Инструкция

**Роль:** стратег, архитектор, quality gate
**Проект:** {project.name}

## 1. Что читать перед стартом

1. `{phases.current}` — план реализации
2. Последний отчёт из `.agentic/outbox/` (если есть)

## 2. Workflow

### Создание TODO

1. Определи текущее состояние проекта.
2. Выбери ближайший незавершённый шаг из плана.
3. Создай baseline: `awf baseline TODO-{NNNN}`
4. Подготовь TODO по шаблону `.agentic/templates/todo-template.md`
5. Положи в `.agentic/inbox/TODO-{NNNN}.md` + `.ready`

### Верификация результата

1. Прочитай отчёт из `.agentic/outbox/`
2. Запусти verify команды из конфига:
   - Test: `{verification.test_cmd}`
   - Lint: `{verification.lint_cmd}`
   - Typecheck: `{verification.typecheck_cmd}`
3. Проверь git diff — изменения должны быть в исходных файлах
4. Приняты решение: continue / fix / rollback

## 3. Правила

- НЕ пиши код сам. Ты создаёшь инструкции для worker'а.
- Все Find блоки — дословно из файлов.
- Один TODO — один законченный инкремент.
- Baseline обязателен перед каждой задачей.
- Regression verify обязателен перед коммитом.
```

---

## Worker

**Файл:** `.agentic/roles/worker.md`  
**Запуск:** `opencode run --agent worker`  
**Модель:** локальная / cheap model (например, Qwen через vLLM)

### Ответственность

Worker — исполнитель. Он не принимает архитектурных решений. Его работа:

1. **Чтение TODO.** Берёт задачу из `inbox/`.
2. **Реализация.** Применяет Find/Replace через edit tool.
3. **Верификация.** Запускает verify после каждой задачи.
4. **Отчёт.** Пишет DONE или BLOCKED в `outbox/`.

### Входные данные

- `inbox/TODO-{NNNN}.md` — задача
- `context/CONTEXT-{NNNN}.md` — дополнительный контекст (если указан)

### Выходные данные

- `outbox/DONE-{NNNN}.md` + `.ready` — успешно
- `outbox/BLOCKED-{NNNN}.md` + `.ready` — заблокирован

### Шаблон инструкции

```markdown
# WORKER: Инструкция

**Роль:** исполнитель
**Проект:** {project.name}

## 1. Workflow

### Шаг 1 · Найти активный TODO

Проверить `.agentic/inbox/` на наличие `TODO-{NNNN}.ready`.

### Шаг 2 · Создать baseline

```bash
awf baseline TODO-{NNNN}
```

### Шаг 3 · Выполнить задачи

Для каждой Task в TODO:
1. Применить Find → Replace через edit tool
2. Запустить Verify команду
3. Если verify провалился — STOP, написать BLOCKED

### Шаг 4 · Regression verify

```bash
{verification.test_cmd}
{verification.lint_cmd}
{verification.typecheck_cmd}
```

### Шаг 5 · Написать отчёт

- Успех: `.agentic/outbox/DONE-{NNNN}.md` + `.ready`
- Проблема: `.agentic/outbox/BLOCKED-{NNNN}.md` + `.ready`

## 2. Запреты

- НЕ делай git commit без явного указания в TODO.
- НЕ фиксай pre-existing ошибки.
- НЕ используй sed/awk/cat для правок кода.
- НЕ меняй архитектуру без указания в TODO.
- Если Find не находится дословно — STOP, напиши BLOCKED.
```

---

## Reviewer (опционально)

**Файл:** `.agentic/roles/reviewer.md`  
**Запуск:** `opencode run --agent reviewer`  
**Модель:** может быть той же, что worker, или сильнее

### Ответственность

Reviewer проверяет качество работы worker'а:

1. **Соответствие TODO.** Реализовано ли то, что было задумано?
2. **Качество кода.** Читаемость, стиль, отсутствие obvious bugs.
3. **Архитектура.** Не нарушены ли ограничения проекта?
4. **Мелкие правки.** Может внести небольшие исправления сам.

### Входные данные

- `inbox/TODO-{NNNN}.md` — оригинальная задача (для сравнения)
- `git diff` изменений worker'а
- Изменённые файлы

### Выходные данные

- `outbox/REVIEW-APPROVED-{NNNN}.md` + `.ready` — код принят
- `outbox/REVIEW-REJECTED-{NNNN}.md` + `.ready` — нужны доработки
- `outbox/BLOCKED-{NNNN}.md` + `.ready` — критические проблемы

### Шаблон инструкции

```markdown
# REVIEWER: Инструкция

**Роль:** код-ревьюер
**Проект:** {project.name}

## 1. Что делать

1. Прочитай TODO-{NNNN}.md — что должно было быть сделано.
2. Посмотри git diff — что реально изменилось.
3. Прочитай изменённые файлы полностью.
4. Проверь:
   - Все Task из TODO реализованы
   - Код соответствует Find/Replace из TODO
   - Нет случайных изменений вне задач
   - Стиль кода согласован с проектом
   - Нет obvious bugs

## 2. Мелкие правки

Если найдёшь мелкие проблемы (опечатки, форматирование, очевидные баги) —
исправь их через edit tool. Заметь в отчёте.

## 3. Отчёт

### APPROVED
Все задачи выполнены корректно. Мелкие правки внесены.

### REJECTED
Есть проблемы, которые worker должен исправить:
- Конкретное описание проблемы
- Что нужно изменить

### BLOCKED
Критическая проблема (архитектура сломана, данные потеряны):
- Описание проблемы
- Требуется вмешательство supervisor
```

---

## Tester (опционально)

**Файл:** `.agentic/roles/tester.md`  
**Запуск:** `opencode run --agent tester` (или просто скрипт)  
**Модель:** может быть любой; часто достаточно bash-скрипта

### Ответственность

Tester проверяет, что код работает:

1. **Запуск тестов.** Полный набор unit + integration.
2. **Regression check.** Сравнение с baseline — нет новых падений.
3. **Coverage.** Проверка покрытия нового кода тестами.
4. **Build.** Проверка сборки (Docker, npm и т.д.).

### Входные данные

- Команды из конфига: `test_cmd`, `lint_cmd`, `typecheck_cmd`, `build_cmd`
- Baseline: `context/BASELINE-{NNNN}.tests.log`

### Выходные данные

- `outbox/TEST-PASSED-{NNNN}.md` + `.ready` — всё зелёное
- `outbox/TEST-FAILED-{NNNN}.md` + `.ready` — есть падения

### Шаблон инструкции

```markdown
# TESTER: Инструкция

**Роль:** тестировщик
**Проект:** {project.name}

## 1. Что делать

1. Запустить полный набор проверок:

```bash
# Тесты
{verification.test_cmd} > .agentic/outbox/TEST-RESULTS-{NNNN}.log 2>&1

# Линтер
{verification.lint_cmd} >> .agentic/outbox/TEST-RESULTS-{NNNN}.log 2>&1

# Типичекинг
{verification.typecheck_cmd} >> .agentic/outbox/TEST-RESULTS-{NNNN}.log 2>&1

# Сборка (если настроена)
{verification.build_cmd} >> .agentic/outbox/TEST-RESULTS-{NNNN}.log 2>&1
```

2. Сравнить с baseline:
   - Новые падения тестов = regression
   - Новые lint/typecheck ошибки = regression

3. Проверить coverage (если настроено):
```bash
{verification.coverage_cmd}
```

## 2. Отчёт

### PASSED
Все проверки зелёные. Regression: нет новых ошибок.

### FAILED
Описание упавших проверок:
- Какие тесты упали
- Это regression или pre-existing
- Лог ошибок
```

---

## Добавление новой роли

Чтобы добавить новую роль в pipeline:

1. Создать файл `.agentic/roles/<name>.md` со спецификацией
2. Добавить модель в `config.yaml` → `models:`
3. Добавить стадию в `pipelines/<name>.yaml`

Команда `awf add-role <name>` автоматизирует шаги 1-3.
