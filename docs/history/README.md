# Historical documents

Эта папка содержит исходные дизайн-документы проекта, написанные **до реализации** awf (когда проект был ещё идеей).

## Что здесь

| Документ | Что описывает | Актуальность |
|---|---|---|
| [`proposal/01-CONCEPT.md`](proposal/01-CONCEPT.md) | Исходная концепция awf: проблема, цели, ключевые принципы. | Историческая справка. Концепция реализована, см. [`README.md`](../../README.md). |
| [`proposal/02-CLI-SPECIFICATION.md`](proposal/02-CLI-SPECIFICATION.md) | Spec CLI команд (bash era). | **Устарел.** CLI переписан на Python в Wave 4 (v0.4.0). Актуальный CLI — в [`awf/cli.py`](../../awf/cli.py). |
| [`proposal/03-CONFIGURATION.md`](proposal/03-CONFIGURATION.md) | Spec config.yaml формата. | **Частично устарел.** Актуальный формат — в [`templates/config.default.yaml`](../../templates/config.default.yaml). |
| [`proposal/04-ROLES.md`](proposal/04-ROLES.md) | Design ролей (supervisor, worker, reviewer, tester). | Историческая справка. Актуальные роли — в [`templates/roles/`](../../templates/roles/). |
| [`proposal/05-ORCHESTRATOR.md`](proposal/05-ORCHESTRATOR.md) | Design bash orchestrator (state machine). | **Устарел.** Orchestrator переписан на Python в Wave 4b. Актуальный — в [`awf/orchestrator.py`](../../awf/orchestrator.py). |
| [`proposal/06-MIGRATION-PLAN.md`](proposal/06-MIGRATION-PLAN.md) | План миграции с «AI Code Review Agent» (предшественник awf). | **Выполнена.** Историческая справка. |

## Зачем хранить

Эти документы объясняют **почему awf стал таким, какой он есть**. Полезно для:
- Понимания изначальных мотиваций и архитектурных решений.
- Исторического контекста при рефакторинге.
- Onboarding новых контрибьюторов, желающих понять эволюцию проекта.

## Что читать вместо этого

- **Текущая архитектура awf** — [`README.md`](../../README.md), [`awf/`](../../awf/) исходники.
- **File bus протокол** — [`protocols/communication.md`](../../protocols/communication.md).
- **Roadmap plugin'а agent-workflow-ui** — [`vision/agent-ui-plugin.md`](../../vision/agent-ui-plugin.md), [`vision/architecture.md`](../../vision/architecture.md).
- **План развития** — [`BACKLOG.md`](../../BACKLOG.md).
