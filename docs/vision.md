# Product Vision

**Версия:** 2.0 · **Дата:** 2026-09-27 · **Релиз:** 1.4.0

## Что

`agent-workflow-ui` — MCP plugin для opencode. Даёт supervisor-агенту 47 typed MCP tools для управления pipeline без shell-команд.

## Почему

AI-агенты в opencode работают быстрее и надёжнее через typed tools, чем через bash. Формы дают структурированный ввод. Pipeline обеспечивает разделение ролей и quality gate.

## Для кого

Pet-проекты где пользователь хочет делегировать разработку AI-агентам, но контролировать процесс: планировать, проверять, откатывать.

## Tools

**UI (5):** `open_form`, `read_submit`, `cancel_form`, `list_pending_forms`, `list_templates` — HTML-формы в браузере.

**Workflow (42):** полный lifecycle — init, goal, dispatch, start, wait, status, kill, retry, approve, reject, rollback, report, reset, dashboard, model validation, phase management, run (забег: start/status/next/finish/note), restore, prove_red, verify_pack.

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

См. [design.md](design.md) для деталей.

## Dogfood: этот репозиторий

Awf развивает сам себя. Программа стабилизации 2026-09 (31 юнит по
аудитной карте) и волны добора проверок и документации (13 юнитов) прошли
целиком через awf: dispatch → pipeline → verify-гейты → commit; rejects,
salvage и rollback отработаны на реальных падениях.

## Future scenarios

Открытые сценарии и кандидаты — в [BACKLOG-archive](../BACKLOG-archive.md)
(закрытые) и [BACKLOG](../BACKLOG.md) (открытые): decision fork, blockage
recovery, priority planning, onboarding wizard и другие.
