# Supervisor Flow (SMO)

**Версия:** 1.0 (Implemented) · **Дата:** 2026-08-11

## Что

State-Machine Orchestration: awf ведёт supervisor-агента через детерминированные
фазы сессии — от init до verify. Supervisor — диалоговый координатор, активный в
ключевых точках и пассивный между ними.

## Статус: РЕАЛИЗОВАН

SMO полностью реализован и протестирован в 6 dogfood-сессиях. Compact prompts +
next_action работают на слабых моделях (Qwen vllm).

## Фазы

```
init → goal → form → normalize → brief → run → verify → done
```

| Фаза | Что делает supervisor | Что делает awf |
|---|---|---|
| **init** | — | Создаёт `.agentic/`, возвращает compact prompt + next_action |
| **goal** | Спрашивает пользователя о цели | `awf_set_goal` → сохраняет goal, advances phase |
| **form** | Рекомендует роли, открывает форму | `apply_project_setup` → materializes pipeline.yaml + config + roles |
| **normalize** | `awf_analyze_roles`, проверяет перекрытия | `awf_confirm_normalized` → advances to brief |
| **brief** | Изучает проект + BACKLOG, pre-check кода, батчит задачи, dispatch | `awf_dispatch_todo` (pre-check grep) → `awf_start` |
| **run** | **IDLE** — ждёт пользователя | Pipeline: agent stages, signal detection, dashboard live |
| **verify** | Читает handoffs + git diff, решает | `awf_approve` / `awf_reject` → commit/replan |
| **done** | Спрашивает пользователя "что дальше?" | State cleared, goal preserved |

## Ключевой паттерн: next_action

Каждый MCP tool возвращает `next_action` — компактную инструкцию:

```
awf_init → "Спроси о цели → awf_set_goal"
awf_set_goal → "Открой форму → awf_open_project_setup_form"
awf_confirm_normalized → "Изучи BACKLOG, вызови awf_dispatch_todo"
awf_dispatch_todo → "Call awf_start(background=True)"
awf_start → "GO IDLE. Do NOT poll. Wait for user."
awf_approve → "Pipeline EXIT. Wait for user. DO NOT dispatch without asking."
awf_reject → "Fix issues → dispatch_todo → awf_start."
```

Слабые модели (Qwen) следуют next_action, игнорируя длинные промты. Проверено
в 6 dogfood-сессиях.

## Принципы

- **Детерминизм + LLM.** Awf = рельсы (state machine, signals, handoffs, git,
  templates, tools). LLM = поезд. Больше детерминизма в рутине → меньше
  LLM-ошибок → выше качество.
- **Compact prompts.** Phase-specific ~50-100 строк вместо 600-строчного
  supervisor.md. `awf_init` и `load_supervisor_context` возвращают compact prompt
  для текущей фазы.
- **Batch guidance.** Supervisor батчит BACKLOG задачи в крупные TODO, проверяет
  код до dispatch. Pre-check grep предупреждает если паттерн уже в коде.

## Архитектурное решение

SMO распространяет existing pipeline-state-machine pattern на setup-flow:

```
Было: execute-стадии уже автоматизированы (plan → agent → agent → verify)
Стало: setup-стадии тоже автоматизированы (init → goal → form → normalize → brief)
```

Awf = state-machine на весь цикл сессии, не только на execute-часть.

## Связанные документы

- [Product Vision](agent-ui-plugin.md) — что делают tools
- [Архитектура](architecture.md) — как устроено
- [BACKLOG](../BACKLOG.md) — открытые задачи
