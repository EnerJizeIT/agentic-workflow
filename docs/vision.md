# Product Vision

**Версия:** 2.1 · **Дата:** 2026-09-27 · **Релиз:** 1.4.0

## Что

`agent-workflow-ui` — MCP plugin для opencode. Даёт supervisor-агенту 52 typed MCP tools для управления pipeline без shell-команд. Supervisor всегда планирует работу, проверяет результат и ведёт следующий забег; состав исполнительных ролей меняется под цель.

## Почему

AI-агенты в opencode работают быстрее и надёжнее через typed tools, чем через bash. Формы дают структурированный ввод. Сильный supervisor держит контекст проекта и решения, а pipeline поручает узкие действия специализированным агентам, в том числе более слабым моделям.

## Для кого

Pet-проекты, где пользователь хочет поручать AI-агентам последовательную работу над кодом, документацией, тестами или анализом и сохранять контроль над целью, проверкой и результатом через supervisor.

## Tools

**UI (5):** `open_form`, `read_submit`, `cancel_form`, `list_pending_forms`, `list_templates` — HTML-формы в браузере.

**Workflow (47):** полный lifecycle — init, goal, dispatch, start, wait, status, kill, retry, approve, reject, rollback, report, reset, dashboard, model validation, phase management, run (забег: start/status/next/finish/note/revise), service run (создание роли: start/status/finish/approve), restore, prove_red, verify_pack.

Все workflow tools возвращают `next_action` — компактную инструкцию для supervisor.

## Конкурентные преимущества

- **SMO (State-Machine Orchestration):** awf ведёт supervisor по фазам (init→goal→form→normalize→brief→run→verify→done) через compact prompts + next_action. Исполнительные роли получают свои узкие поручения; supervisor сохраняет общую цель и право решения.
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

Supervisor сможет пересобирать состав и порядок рабочих стадий внутри
активного забега, а при нехватке компетенции — запускать связанный служебный
забег для создания новой проектной роли. После проверки роли и её
инкрементальной нормализации он продолжит исходный забег с обновлённым
планом. Текущая фаза `normalize` обслуживает первичную настройку;
повторный сброс настройки для новой роли не потребуется. План реализации —
в [стратегии развития](audit-2026-09-27-gpt6-sol-xhigh/IMPLEMENTATION-STRATEGY.md).

Сценарии и кандидаты — в [BACKLOG-archive](../BACKLOG-archive.md) (история
решений, включая decision fork, blockage recovery, priority planning,
onboarding wizard) и [BACKLOG](../BACKLOG.md) (открытая работа).
