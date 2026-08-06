# Product Vision

**Версия:** 0.7 · **Дата:** 2026-08-06

## Что

`agent-workflow-ui` — MCP plugin для opencode. Даёт supervisor-агенту 24 typed MCP tools для управления pipeline без shell-команд.

## Почему

AI-агенты в opencode работают быстрее и надёжнее через typed tools, чем через bash. Формы дают структурированный ввод. Pipeline обеспечивает разделение ролей и quality gate.

## Для кого

Pet-проекты где пользователь хочет делегировать разработку AI-агентам, но контролировать процесс: планировать, проверять, откатывать.

## Tools

**UI (5):** `open_form`, `read_submit`, `cancel_form`, `list_pending_forms`, `list_templates` — HTML-формы в браузере.

**Workflow (19):** полный lifecycle — init, dispatch, start, wait, status, kill, approve, rollback, report, reset, dashboard, model validation.

## Конкурентные преимущества

- Pipeline с ролями (analyst → architect → implementer → QA → audit)
- Plan checkpoint — preview TODO перед запуском агентов
- Dashboard — live мониторинг pipeline
- TODO lifecycle — архивирование, reconcile, crash recovery
- Salvage path — recovery когда worker не просигналил

## Архитектура

Plugin зависит от `awf` Python-пакета. Все workflow tools — thin async wrappers над `awf.api.*()`. Бизнес-логика в `awf`, не в plugin.

См. [architecture.md](architecture.md) для деталей.

## Future scenarios

| Сценарий | Что добавляет |
|---|---|
| 2 · Decision fork | Runtime ad-hoc forms |
| 3 · Blockage recovery | Multi-step problem→solution flow |
| 5 · Priority planning | Drag-and-drop UI |
| 6 · Onboarding wizard | Multi-form conditional logic |
