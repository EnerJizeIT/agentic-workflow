# Миграция с текущего AGENTIC-WORKFLOW

Как перенести существующий проект «AI Code Review Agent» на новую схему.

---

## Что меняется

| Сейчас | После миграции |
|---|---|
| `AGENTIC-WORKFLOW/` (в репозитории) | `.agentic/` (частично в gitignore) |
| Пути захардкожены в скриптах | Все пути из `config.yaml` |
| SUPERVISOR.md ссылается на `../MVP/`, `../STRATEGY/` | Supervisor читает пути из конфига |
| PHASES/ внутри AGENTIC-WORKFLOW | `.agentic/phases/` |
| MODELS-GUIDE.md про Kimi/Qwen | `models:` секция в config.yaml |
| Скрипты знают только этот проект | Скрипты работают с любым проектом |

---

## План миграции

### Шаг 1: Создать фреймворк

Создать репозиторий `agentic-workflow` с CLI (`awf`) и шаблонами.

### Шаг 2: Инициализировать текущий проект

```bash
cd "/AI Code Review & Analysis Agent"
awf init --template full
```

Это создаст `.agentic/` параллельно со старым `AGENTIC-WORKFLOW/`.

### Шаг 3: Заполнить конфиг

Копировать из текущего состояния:

- Verify-команды → `verification:` в config.yaml
- Модели → `models:` в config.yaml
- Путь к MVP-PHASE.md → `phases.current`

### Шаг 4: Перенести фазы

```bash
cp "AGENTIC-WORKFLOW/PHASES/MVP-PHASE.md" .agentic/phases/
cp "AGENTIC-WORKFLOW/PHASES/POST-MVP-PHASE.md" .agentic/phases/
```

### Шаг 5: Адаптировать роли

Текущие `SUPERVISOR.md` и `WORKER.md` содержат привязки к проекту (`../MVP/`, `pytest tests/`). Заменить на переменные из конфига:

| Было | Стало |
|---|---|
| `../MVP/4. PROJECT PLAN MVP.md` | `{phases.current}` |
| `pytest tests/` | `{verification.test_cmd}` |
| `AGENTIC-WORKFLOW/inbox/` | `.agentic/inbox/` |
| Kimi K2.7 Code, Qwen3.6-27B | `{models.supervisor}`, `{models.worker}` |

### Шаг 6: Удалить старый AGENTIC-WORKFLOW

После проверки, что всё работает через `.agentic/`:

```bash
rm -rf AGENTIC-WORKFLOW/
git add -A
git commit -m "Migrate to agentic-workflow framework"
```

---

## Runtime-файлы: что куда

| Файл | Сейчас | После | В git? |
|---|---|---|---|
| Инструкции ролей | `AGENTIC-WORKFLOW/SUPERVISOR.md` | `.agentic/roles/supervisor.md` | Да |
| Pipeline конфиг | нет | `.agentic/pipelines/default.yaml` | Да |
| Config | нет | `.agentic/config.yaml` | Да |
| Фазы проекта | `AGENTIC-WORKFLOW/PHASES/` | `.agentic/phases/` | Да |
| TODO задачи | `AGENTIC-WORKFLOW/inbox/` | `.agentic/inbox/` | Нет |
| Отчёты | `AGENTIC-WORKFLOW/outbox/` | `.agentic/outbox/` | Нет |
| Baseline'ы | `AGENTIC-WORKFLOW/context/` | `.agentic/context/` | Нет |
| Логи | `AGENTIC-WORKFLOW/logs/` | `.agentic/logs/` | Нет |
| Отчёты для пользователя | нет | `.agentic/reports/` | Нет |
