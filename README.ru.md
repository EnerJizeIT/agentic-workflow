# Agentic Workflow (awf)

[![license: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![tests](https://github.com/EnerJizeIT/agentic-workflow/actions/workflows/test.yml/badge.svg)](https://github.com/EnerJizeIT/agentic-workflow/actions/workflows/test.yml)

> **Оркестратор мульти-агентных пайплайнов для opencode. План → разработка → проверка → коммит — через типизированные MCP tools, не через bash.**

## Какую проблему решает

AI-агенты для кода мощные, но хаотичные. Прыгают сразу к коду без планирования, пропускают ревью, оставляют баги. Токены горят, а ты смотришь беспомощно.

**awf** добавляет структуру: supervisor-агент планирует работу, worker-агенты выполняют через пайплайн, который вы проектируете (любые роли, любая глубина — от одного воркера до многостадийной цепочки), и вы одобряете каждый результат перед коммитом. Всё через естественный язык — *"начни работу по бэклогу"*, *"проверь и одобри"*, *"отклони — DOMParser не починен"*.

Ты контролируешь процесс. Агент на рельсах.

## Возможности

- 🎯 **State-Machine Orchestration (SMO)** — awf ведёт supervisor по фазам: `init → goal → form → normalize → brief → run → verify → done`. Каждый tool возвращает `next_action` — даже слабые модели (Qwen vLLM) проходят полный flow без ошибок.
- 🔧 **29 MCP tools** — типизированное управление пайплайном: init, dispatch, start, approve, reject, rollback, dashboard, валидация моделей. Без bash, без ручного редактирования файлов.
- 📊 **Живой Dashboard** — HTTP server с real-time опросом. Chat-стиль handoffs, содержимое TODO, timeline, статус воркера, браузерные уведомления. Без перезагрузки страницы.
- 🧱 **Кастомные пайплайны** — любые роли, любая глубина. 1 стадия или 10. Аналитик → архитектор → разработчик → QA → аудит, или один воркер. Выбираешь в форме настройки.
- ✅ **Approve / Reject** — симметричные tools для verify. Approve коммитит и архивирует. Reject убивает пайплайн и запрашивает исправления.
- 🔍 **Pre-dispatch проверка** — перед запуском пайплайна awf грепает код на ключевые слова из TODO. Предупреждение если задача уже может быть реализована.
- 🔄 **Crash recovery** — salvage-путь когда воркер не просигналил, очистка orphan TODO, реконсиляция состояния при старте.
- 📋 **Increment planning** — варианты декомпозиции (vertical, horizontal, risk-first) в HTML-форме для выбора пользователем.

## Установка

```bash
pip install -e .
pip install -e ./agent_workflow_ui
```

Добавь в `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "agent-workflow-ui": {
      "type": "local",
      "command": ["python3", "-m", "agent_workflow_ui"],
      "enabled": true
    }
  }
}
```

Перезапусти opencode.

## Использование

Говори естественным языком — supervisor-агент вызывает нужные tools:

| Ты говоришь | Что происходит |
|---|---|
| *"Инициализируй awf в проекте"* | Создаёт `.agentic/`, определяет стек, спрашивает цель |
| *"Разработай MVP"* | Открывает форму → настраивает пайплайн → планирует первый TODO |
| *"Verify"* | Supervisor читает handoffs, проверяет git diff, одобряет или отклоняет |
| *"Отклони — кэш не добавлен"* | Пайплайн остановлен, новый TODO с инструкцией по исправлению |

### SMO Flow

```
init → goal → form → normalize → brief → run → verify → done
  │       │       │         │         │       │       │
  awf     user    форма     роли      TODO    агенты  approve/
 setup    цель    pipeline  analyze+  dispatch+ work   reject
                           confirm    start
```

Каждая фаза: компактный промт (~50 строк) + `next_action` в каждом tool result.

## Архитектура

```
opencode (supervisor LLM)
  ↕ MCP stdio (29 типизированных tools)
agent-workflow-ui plugin
  ↕ Python import
awf orchestrator
  ↕ subprocess
opencode run (worker agents)
  ↕ HTTP daemon thread
Dashboard (live /api/state polling)
```

Два пакета:
- **`awf`** — Python core. Pipeline engine, phase state machine, signals, commit gate, dashboard server.
- **`agent_workflow_ui`** — MCP plugin. Thin async wrappers + `next_action` guidance + HTML forms.

## Dashboard

Живой HTTP-дашборд открывается автоматически при запуске пайплайна:

- **Двухпанельный layout** — sidebar с пайплайном (стадии, прогресс, воркер) + табы с контентом
- **💬 Agent Chat** — handoffs в виде чата с chain visualization (`↓ передал → 🔧 Implementer`)
- **📝 Задача** — полное содержимое TODO в отрендеренном markdown
- **📊 События** — значимые события, новые сверху
- **TODO timeline** — `[✅ TODO-0001] ─ [✅ TODO-0002] ─ [🔄 TODO-0003]`
- **Браузерное уведомление** когда пайплайн достиг verify

## Документация

- [USAGE.ru.md](USAGE.ru.md) — сценарии использования и справочник tools
- [Архитектура](vision/architecture.md) — компоненты, потоки данных, design decisions
- [Product Vision](vision/agent-ui-plugin.md) — конкурентные преимущества, dogfood-результаты
- [Supervisor Flow (SMO)](vision/supervisor-flow.md) — фазовая система, паттерн next_action
- [BACKLOG](BACKLOG.md) — открытые задачи
- [README.md](README.md) — English README

## Real-world результаты

6 dogfood-сессий на jira-epic-presenter (Qwen vLLM):
- Полный SMO flow: init → goal → form → normalize → brief → run → verify
- До 4 TODO за сессию, 1× approve (без циклов), ноль polling
- Reject flow протестирован: supervisor нашёл пропущенную работу, отклонил, перенаправил с фиксом
- Pre-dispatch check обнаружил уже реализованные задачи

## Лицензия

MIT — см. [LICENSE](LICENSE).
