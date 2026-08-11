# Product Vision

**Версия:** 1.0 · **Дата:** 2026-08-11

## Что

`agent-workflow-ui` — MCP plugin для opencode. Даёт supervisor-агенту 29 typed MCP tools для управления pipeline без shell-команд.

## Почему

AI-агенты в opencode работают быстрее и надёжнее через typed tools, чем через bash. Формы дают структурированный ввод. Pipeline обеспечивает разделение ролей и quality gate.

## Для кого

Pet-проекты где пользователь хочет делегировать разработку AI-агентам, но контролировать процесс: планировать, проверять, откатывать.

## Tools

**UI (5):** `open_form`, `read_submit`, `cancel_form`, `list_pending_forms`, `list_templates` — HTML-формы в браузере.

**Workflow (24):** полный lifecycle — init, goal, dispatch, start, wait, status, kill, retry, approve, reject, rollback, report, reset, dashboard, model validation, phase management.

Все workflow tools возвращают `next_action` — компактную инструкцию для supervisor.

## Конкурентные преимущества

- **SMO (State-Machine Orchestration):** awf ведёт supervisor по фазам (init→goal→form→normalize→brief→run→verify→done) через compact prompts + next_action. Даже слабые модели (Qwen vllm) проходят полный flow без ошибок.
- **Pipeline с ролями:** любой набор (analyst → architect → implementer → QA → audit, или 1 stage, или 10 — пользователь выбирает).
- **Dashboard v2:** HTTP server с live polling. Chat-style handoffs с chain visualization. TODO content. TODO timeline. Worker status. Browser notifications.
- **Pre-dispatch check:** grep кода перед запуском pipeline — warning если задача уже реализована.
- **Approve/Reject:** симметричная пара tools для verify.
- **TODO lifecycle:** архивирование, reconcile, crash recovery, rollback.

## Архитектура

Plugin зависит от `awf` Python-пакета. Все workflow tools — thin async wrappers над `awf.api.*()`. Бизнес-логика в `awf`, не в plugin.

См. [architecture.md](architecture.md) для деталей.

## Dogfood-результаты (6 сессий)

6 dogfood-сессий на jira-epic-presenter (Qwen vllm):
- Dogfood #5: 4 TODO + 1 reject, ноль polling, 1× approve каждый
- Dogfood #6: 3 TODO, pre-check показал уже выполненные задачи
- Полный SMO flow работает end-to-end на слабой модели

## Future scenarios

| Сценарий | Что добавляет |
|---|---|
| 2 · Decision fork | Runtime ad-hoc forms |
| 3 · Blockage recovery | Multi-step problem→solution flow |
| 5 · Priority planning | Drag-and-drop UI |
| 6 · Onboarding wizard | Multi-form conditional logic |
