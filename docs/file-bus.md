# File Bus Protocol

**Версия:** 1.3 · **Дата:** 2026-09-27 · **Релиз:** 1.4.0

## Обзор

Pipeline обменивается сигналами через файлы в `.agentic/`. Основной формат — пустой `.ready` файл (триггер) и `.md` файл (контент); кроме того, в `context/` живут машиночитаемые артефакты — `CHECKPOINT-{todo}.json`, `RUN-EVIDENCE-{todo}.md`, `VERIFIED-{todo}.sha`.

Канонический формат id — `TODO-NNNN` (например `DONE-TODO-0001.ready`). Legacy-короткая форма `NNNN` (`DONE-0001.ready`) принимается на чтение для совместимости со старыми запусками. Typo-форма `.md.ready` (LLM иногда дописывает `.ready` к имени `.md`) тоже принимается и чистится.

## Директории

| Директория | Назначение |
|---|---|
| `inbox/` | TODO файлы + supervisor сигналы (ACK, APPROVE, SALVAGE) |
| `outbox/` | Worker сигналы (DONE, BLOCKED, REVIEW) + PROGRESS |
| `done/{todo_id}/` | Архив завершённых TODO (после verify approve) |
| `handoff/` | Per-stage handoff файлы (`{stage_name}-{todo_id}.md`; legacy `{role}-{todo_id}.md` принимается) |
| `context/` | Baseline snapshots (SHA, tests, env, untracked) + `CHECKPOINT-{todo}.json` + `RUN-EVIDENCE-{todo}.md` + `VERIFIED-{todo}.sha` |
| `state/` | `current.yaml` — structured pipeline state; `run.yaml` — автономный забег; `last-kill.json` — последний kill (orphan-предупреждение); `metrics_models_cache.json` — кэш цен моделей |
| `logs/` | orchestrator.log, awf-start.out, worker logs; `awf-launch.lease` — lease одного владельца запуска |

## Сигналы

| Сигнал | Кто создаёт | Где | Значение |
|---|---|---|---|
| `TODO-{todo_id}.ready` | Supervisor (plan) | inbox | TODO готов к выполнению |
| `PROGRESS-{todo_id}.md` | Worker | outbox | Заметки прогресса (входит в handoff) |
| `DONE-{todo_id}.ready` (+`.md`) | Worker | outbox | Работа завершена (`.md` — резюме) |
| `DONE-{todo_id}.json` | Worker | outbox | Machine facts (опционально, unit contract) |
| `BLOCKED-{todo_id}.ready` (+`.md`) | Worker | outbox | Не может продолжать |
| `REVIEW-APPROVED-{todo_id}.ready` | Worker | outbox | Работа одобрена (классификация: approved) |
| `REVIEW-REJECTED-{todo_id}.ready` | Worker | outbox | Работа отклонена (классификация: rejected) |
| `ACK-{todo_id}.ready` | Supervisor (verify) | inbox | Работа принята |
| `APPROVE-{todo_id}.ready` | Supervisor (awf_approve) | inbox | Коммит разрешён |
| `REVIEW-{todo_id}.md` | Supervisor (verify) | outbox | Работа отклонена (с фидбеком) |
| `SALVAGE-{todo_id}.md` | Orchestrator | inbox | Worker не просигналил |
| `CHECKPOINT-{todo}.json` | Orchestrator (plan checkpoint, BD-36) | context | One-shot форма плана: решение привязано одноразовым токеном; после принятия файл уходит в `CHECKPOINT-{todo}.json.consumed` (аудит-след) |
| `RUN-EVIDENCE-{todo}.md` | Supervisor (`awf_approve(evidence=...)`) | context | Независимая проверка approve в забеге: команды, которые реально прогнаны + вердикт (AUD11-03) |
| `VERIFIED-{todo}.sha` | Supervisor (`awf_approve(verified_sha=...)`) | context | Fingerprint рабочего дерева на момент verify; approve отказывает, если дерево сдвинулось после проверки |
| `RUN-REPORT-{ts}.md` | Orchestrator (`awf_run_finish`) | outbox | Итог забега: очередь, бюджет, rejects, причина стопа |

Запуск: `.agentic/logs/awf-launch.lease` — не сигнал, а lease одного
владельца запуска: O_EXCL, stale по живости процесса (не по возрасту файла);
прочие одновременные запуски получают `run_mode="noop"` с текстом.

Удалено из протокола: `BRIEF-*` (R5, двухфазный бриф) и `TEST-PASSED`/`TEST-FAILED`
(движок никогда их не читал — AUD16-03). Странный сигнал без известного префикса
классифицируется как `unknown` и эскалируется, как и любой мусор.

## TODO lifecycle

```
dispatch_todo → inbox/TODO-{todo_id}.ready
    ↓ pipeline start
worker stages → outbox/DONE-{todo_id}.ready (+ PROGRESS/{stage} handoff)
    ↓ verify
supervisor → inbox/ACK-{todo_id}.ready → approve → inbox/APPROVE-{todo_id}.ready
    ↓ commit gate
archive_todo → done/{todo_id}/ (inbox + outbox очищены)
```

При отклонении: supervisor пишет `outbox/REVIEW-{todo_id}.md` — пайплайн
останавливается, новый TODO с инструкцией диспетчируется отдельно.

## ACK vs APPROVE (забег)

- **ACK** — «работа принята» (решение verify-стадии, интерактивный поток).
- **APPROVE** — «коммит разрешён» (сигнал commit-гейта; в `--auto` создаёт
  `awf_approve`, в забеге — тот же путь).
- **Evidence-гейт (AUD11-03):** в активном забеге file-based ACK и APPROVE
  без `context/RUN-EVIDENCE-{todo}.md` **игнорируются** движком. Approve в
  забеге идёт только через `awf_approve(todo_id, evidence=...)` — evidence
  (команды, которые реально прогнаны, + вердикт) пишется в RUN-EVIDENCE до
  сигнала. Вне забега bare ACK — обычный поток, гейт его не трогает. REVIEW
  гейтом не ограничивается — отклонению проверка не нужна.

## Stage prompt injection

`build_prompt(kind, todo_id)` конструирует prompt для каждой стадии:

- **execute:** signal contract (DF5-1) + pipeline context (BD-10)
- **plan:** plan snippet + vision injection
- **verify:** verify snippet ("DECIDE YOURSELF")
- **salvage:** salvage-stage snippet (`_SNIPPET_SALVAGE` — "check git diff, ACK or REVIEW")

Snippets добавляются в КОНЕЦ prompt (recency bias).

## Handoff chain (BD-15)

Каждая стадия получает handoff-файлы предыдущих стадий через `--file` аргументы. `collect_handoff()` собирает PROGRESS + DONE + git diff в `{stage_name}-{todo_id}.md`.

Имя по имени СТАДИИ, а не роли: две стадии с одной ролью (например два QA)
не затирают handoff друг друга. Legacy-файлы `{role}-{todo_id}.md` (до
переименования) продолжают читаться для незавершённых запусков.
