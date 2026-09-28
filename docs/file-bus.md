# File Bus Protocol

**Версия:** 1.4 · **Дата:** 2026-09-27 · **Релиз:** 1.4.0

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
| `context/` | Baseline snapshots (SHA, tests, env, untracked) + `CHECKPOINT-{todo}.json` + `RUN-EVIDENCE-{todo}.md` + `VERIFIED-{todo}.sha` + `PIPELINE-{todo}.yaml` (снимок стадий юнита, ORCH M3.3 — continue возобновляет по снимку) |
| `state/` | `current.yaml` — structured pipeline state; `run.yaml` — автономный забег; `service-run.yaml` — служебный забег создания роли (ORCH M5.2, отдельный файл: основной `run.yaml` от него не страдает); `last-kill.json` — последний kill (orphan-предупреждение); `metrics_models_cache.json` — кэш цен моделей |
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
| `APPROVE-{todo_id}.ready` | Supervisor (awf_approve) | inbox | Коммит разрешён; в забеге сигнал несёт привязку — поколение + `verified_sha` + `files_digest` (M2.1) |
| `REVIEW-{todo_id}.md` | Supervisor (verify) | outbox | Работа отклонена (с фидбеком) |
| `SALVAGE-{todo_id}.md` | Orchestrator | inbox | Worker не просигналил |
| `CHECKPOINT-{todo}.json` | Форма чекпоинта (владелец) | context | One-shot форма плана: решение привязано одноразовым токеном; после принятия файл уходит в `CHECKPOINT-{todo}.json.consumed` (аудит-след) |
| `RUN-EVIDENCE-{todo}.md` | Supervisor (`awf_approve(evidence=...)`) | context | Независимая проверка approve в забеге: команды, которые реально прогнаны + вердикт (AUD11-03) |
| `VERIFIED-{todo}.sha` | Supervisor (`awf_approve(verified_sha=...)`) | context | Fingerprint рабочего дерева на момент verify; approve отказывает, если дерево сдвинулось после проверки |
| `RUN-REPORT-{ts}.md` | Supervisor (`awf_run_finish`) | outbox | Итог забега: очередь, бюджет, rejects, причина стопа |
| `SERVICE-RUN-REPORT-{ts}.md` | Supervisor (`awf_run_service_finish`) | outbox | Итог служебного забега (ORCH M5.2): роль/кандидат, позиция, причина стопа + снимок основного забега («продолжай основной») |

Запуск: `.agentic/logs/awf-launch.lease` — не сигнал, а lease одного
владельца запуска: O_EXCL, stale по живости процесса (не по возрасту файла);
прочие одновременные запуски получают `run_mode="noop"` с текстом.

Удалено из протокола: `BRIEF-*` и `TEST-PASSED`/`TEST-FAILED` (движок
никогда их не читал). Странный сигнал без известного префикса
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
- **V-03 (27.09):** в активном забеге approve дополнительно требует
  `verified_sha` (отпечаток дерева с момента `awf tree-sha`) — без него
  отказ, сигнал APPROVE не публикуется; вне забега параметр опционален.
- **M2.1 (27.09):** в активном забеге коммит авторизует только bound
  APPROVE. Сигнал несёт привязку решения к циклу: поколение забега (в том
  же lock-держании, что и вердикт в журнал), проверенный отпечаток и
  дайджест набора файлов, который применит commit-гейт (тот же
  `plan_files` на baseline-входе гейта). Гейт сверяет привязку: одобрение
  устаревшего поколения (забег рестартанулся/ревизован после approve),
  ACK или APPROVE без привязки (ручной touch, старая версия) — отказ
  с внятным текстом, повторный `awf_approve`; расхождение набора файлов
  или отпечатка — отказ. Вне забега сигнал остаётся пустым маркером,
  поведение прежнее.

## Состояние забега (state/run.yaml)

Один источник правды по забегу; RunPlan (цель, критерии, решения) —
производное от этих полей, второго хранилища нет (ORCH M1.1). Запись
только через `run_state` (контракт
`docs/contracts/run-state-writes.md`); битые поля читаются как пропуск
с предупреждением, файл при этом сохраняется для разбора. Чтение —
единым reader'ом `awf/run_plan_read.py` (ORCH M1.2): `awf_brief` (краткая
карточка: цель/позиция/бюджет/последнее решение/ссылки на источники) и
`awf_load_supervisor_context` (все решения) — два представления этой
записи; битый файл деградирует обе поверхности до «нет забега» с
предупреждением.

| Поле | Смысл |
|---|---|
| `active` | забег активен (False = завершён) |
| `queue` | `[{"todo_id", "pipeline"}, ...]`; legacy-строки нормализуются при чтении |
| `index` / `current` | позиция в очереди / запущенный элемент |
| `completed` | завершённые TODO |
| `rejects` | `{TODO: число}` — счётчик reject'ов (гейт «дважды отвергнут») |
| `outcomes` | `{TODO: вердикт}` — журнал решений verify |
| `stop_flags` | `{TODO: [причина]}` — механический стоп-лист |
| `budget_minutes` / `downtime_seconds` | бюджет в продуктивных минутах и учтённый простой (B2) |
| `started_at` | часы забега; битые часы → стоп, а не elapsed 0 (AUD02-09) |
| `generation` | идентичность забега для CAS-переходов (A-13) |
| `stop_reason` / `report_file` | причина стопа и путь RUN-REPORT |
| `note` / `no_checkpoints` | живое описание (R5) и пропуск чекпоинтов (B4) |
| `goal` / `criteria` | план забега (ORCH M1.1): цель (строка) и критерии (список строк); пишет `awf_run_start`, показаны в `awf_run_status` и RUN-REPORT; в старом run.yaml отсутствуют → читаются как `""` / `[]`, миграции нет |
| `decisions` | append-only причинная память (ORCH M1.1): `[{ts, kind: approve\|reject, todo_id, reason}]`; approve хранит выжимку evidence ≤200 символов (полный текст — в `context/RUN-EVIDENCE-{todo}.md`); повтор того же (kind, todo_id, reason) дубль не добавляет; пишется только в активном забеге |
| `revisions` | применённые ревизии состава очереди (ORCH M3.4): `[{ts, key, kind: "revision", reason, changes, generation, resume_from}]`, `changes` = `[{todo_id, from, to}]` (только НЕ начатые элементы); `resume_from` (ORCH M4.1) — стадия, с которой возобновляется остановленный юнит (пустая, если остановка не нужна / живой стадии нет); пишет `awf_run_revise` в том же CAS-записе, что и смена очереди (условие поколения A-13); повтор с тем же `key` — no-op (идемпотентность); в старом run.yaml отсутствует → читается как `[]`, миграции нет |

**Служебный забег (ORCH M5.2).** Тот же набор полей живёт в отдельном
файле `state/service-run.yaml` (slot `"service"`; все функции `run_state`
принимают `slot="main"|"service"`, дефолт `"main"`). Служебный забег
создаёт роль-кандидата во время основного: его очередь — один юнит, а
основной `run.yaml` не перезаписывается и не завершается (побайтовая
неприкосновенность — инвариант, покрыт тестом). Движок кладёт простой
(`add_downtime`) в слот забега, которому принадлежит текущий TODO
(`run_slot_for_todo`) — salvage/backoff служебного юнита не трогают
основной файл. Вердикты и решения служебного юнита пишутся в слот
`"service"`; approve несёт evidence (`RUN-EVIDENCE-{todo}.md`), но сигнал
`APPROVE-{todo}.ready` пустой — служебный verify не коммитит (кандидат
остаётся в `roles/draft/` до явного adopt, M5.1). Штатный `awf_approve` /
`awf_reject` для юнита служебного слота — вежливый отказ с текстом ДО любых
побочных эффектов (REVIEW-файл не пишется, счётчики и эскалация
двойного-reject основного не трогаются): путь вердикта —
`awf_run_service_approve`, отклонение кандидата — `awf_run_service_finish`
+ повторный запуск.

## Stage prompt injection

`build_prompt(kind, todo_id)` конструирует prompt для каждой стадии:

- **execute:** signal contract + pipeline context
- **plan:** plan snippet + vision injection
- **verify:** verify snippet ("DECIDE YOURSELF")
- **salvage:** salvage snippet ("check git diff, ACK or REVIEW")

Snippets добавляются в КОНЕЦ prompt (recency bias).

## Handoff chain

Каждая стадия получает handoff-файлы предыдущих стадий через `--file` аргументы. `collect_handoff()` собирает PROGRESS + DONE + git diff в `{stage_name}-{todo_id}.md`.

Имя по имени СТАДИИ, а не роли: две стадии с одной ролью (например два QA)
не затирают handoff друг друга. Legacy-файлы `{role}-{todo_id}.md` (до
переименования) продолжают читаться для незавершённых запусков.
