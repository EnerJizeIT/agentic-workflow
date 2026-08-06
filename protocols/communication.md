# File Bus Protocol

**Версия:** 1.1 · **Дата:** 2026-08-06

## Обзор

Pipeline обменивается сигналами через файлы в `.agentic/`. Каждый сигнал — пустой `.ready` файл (триггер) или `.md` файл (контент).

## Директории

| Директория | Назначение |
|---|---|
| `inbox/` | TODO файлы + supervisor сигналы (ACK, APPROVE) |
| `outbox/` | Worker сигналы (DONE, BLOCKED, REVIEW) + PROGRESS |
| `done/{todo_id}/` | Архив завершённых TODO (после verify approve) |
| `handoff/` | Per-stage handoff файлы (`{role}-{todo_id}.md`) |
| `context/` | Baseline snapshots (SHA, tests, env, untracked) |
| `state/` | `current.yaml` — structured pipeline state |
| `logs/` | orchestrator.log, awf-start.out, worker logs |

## Сигналы

| Сигнал | Кто создаёт | Где | Значение |
|---|---|---|---|
| `TODO-{id}.ready` | Supervisor (plan) | inbox | TODO готов к выполнению |
| `DONE-{id}.ready` | Worker | outbox | Работа завершена |
| `BLOCKED-{id}.ready` | Worker | outbox | Не может продолжать |
| `ACK-{id}.ready` | Supervisor (verify) | inbox | Работа принята |
| `APPROVE-{id}.ready` | Supervisor (awf_approve) | inbox | Коммит разрешён |
| `REVIEW-{id}.md` | Supervisor (verify) | outbox | Работа отклонена (с фидбеком) |
| `SALVAGE-{id}.md` | Orchestrator | inbox | Worker не просигналил |

## TODO lifecycle

```
dispatch_todo → inbox/TODO-{id}.ready
    ↓ pipeline start
worker stages → outbox/DONE-{id}.ready
    ↓ verify approve
archive_todo → done/{id}/ (inbox + outbox очищены)
```

## Stage prompt injection

`build_prompt(kind, todo_id)` конструирует prompt для каждой стадии:

- **execute:** signal contract (DF5-1) + pipeline context (BD-10)
- **plan:** plan snippet + vision injection
- **verify:** verify snippet ("DECIDE YOURSELF")
- **salvage:** salvage snippet ("check git diff, ACK or REVIEW")

Snippets добавляются в КОНЕЦ prompt (recency bias).

## Handoff chain (BD-15)

Каждая стадия получает handoff-файлы предыдущих стадий через `--file` аргументы. `collect_handoff()` собирает PROGRESS + DONE + git diff в `{role}-{todo_id}.md`.
