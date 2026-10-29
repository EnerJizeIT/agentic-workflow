# Архитектура: экономия контекста стадии и продолжение сессии

Статус: REVIEW
Ревизия: arch-3
Входы: `context-efficiency.md@req-7`; `context-efficiency.requirements-review.md@rev-7`; `context-efficiency.context.md@ctx-2`; `context-efficiency.readiness.md@readiness-2`; TODO-0302
База кода: commit `401680bbfd568ea03793a6fcbd107180fcc49da4`; в дереве только untracked `docs/spec/context-efficiency/`
Изменения: arch-1 — первая редакция; arch-2 — FR1–FR3; arch-3 — req-7, FR4 (бюджет), RR50/RR51.

## Ответ на находки

FR4 закрыт сокращением; FR5/FR6 — в req-7 (R6/AC37 — `title`+окно, `directory` — тай-брейк; AC8 — stage-log). RR50: `k/n` из `SESSIONS-<todo>.jsonl` (K8, D14). RR51: секция 9 входит в AC7, AC41 без 9 — за аналитиком.

## Границы и точки изменения

Сборка идёт до спавна, `opencode.db` читается только на чтение, новых сервисов нет. Сигнал остаётся единственным признаком завершения (INV2).

| Компонент | Ответственность | Файл | R# |
|---|---|---|---|
| Промпт | восемь юнитов и подсказка | `awf/supervisor.py::_build_pipeline_context` | R1 |
| Пакет | секции, sha входов, обрезка | новый `awf/context_pack.py` | R2, R9 |
| Тело `## REVIEW-фикс` | текст секций до `## ` | `awf/todos.py` рядом с `review_headings` | R2 |
| CLI | `awf context-pack` | `awf/cli.py`, `awf/context_pack.py` | R5 |
| Спавн | `--file`, `--session`, заголовок | `awf/agent_stage.py::run_agent_stage` | R4, R6 |
| Свежесть | `stage_context` отчёта | `awf/verify_pack.py` | R3 |
| Сессия | reused/fresh, счётчики | новый `awf/session_store.py`, `awf/pipeline_state.py` | R6–R8 |
| Наблюдаемость | `reuse: k/n` | `awf/api/lifecycle.py`, `awf/cmd_status.py`, `awf/api/run.py` | R8 |
| Метрики | разбор `-a<N>` | `awf/metrics.py` | R6 |

## Контракты

### K1. Промпт: список завершённых юнитов

`_build_pipeline_context` читает `.agentic/done/`, отбрасывает текущий TODO, сортирует по убыванию номера, берёт восемь (меньше — все). Строка `Prior completed TODOs: TODO-0300, …`, затем `Full list: awf report, .agentic/done/`. Блоки «Pipeline context», «Stage assignment», «Stage identity» не меняются (AC4); фрагмент не зависит от архива, при восьми id ≤300 символов (AC3).

### K2. Файл пакета

Путь `.agentic/context/STAGE-<todo>-<stage>.md`; UTF-8, LF, один финальный перевод строки, без времени и случайных значений. Секции: 1 — задача и роль (todo, стадия, роль, задание из `PIPELINE-<todo>.yaml`); 2 — контракт (шапка TODO, «Что сделать», «Границы», «Приёмка», «Чек-лист завершения» дословно); 3 — состояние (baseline sha, `git status --porcelain`, `git diff --stat`, untracked); 4 — прошлая стадия (handoff до 20 строк и путь); 5 — все `## REVIEW-фикс` дословно до `## `, затем проваленные секции `GATES-<todo>.md` и хвост 20 строк stage-log; 6 — пути из TODO и `source_ref`, до 10; 7 — как работать (читай TODO и пакет, проверяй утверждения пробами, пиши сигнал, AC36); 8 — `- <label>: <path> sha256:<hex>` для TODO, baseline, роли, доктрины, handoff, нет — `sha256:absent`; 9 — дельта при продолжении (K9).

Нет входа — `n/a`, стадия не падает (AC9). Источники: TODO, git, handoff, `GATES`, stage-log, `context/`; окружение и секреты не читаются (AC8). Потолки строк: 1 — 10, 3 — 20, 4 — 20, 6 — 10, 7 — 12, 9 — 15; переполнение — `- … сокращено: <ссылка>`; секции 2, 5, 8 не режутся; сумма 1/3/4/6/7/9 ≤150 (AC7). Тело секций отдаёт helper в `awf/todos.py` на границе `_apply_section_update`; `review_headings` — только заголовки. Доктрина в sha — текст `assemble_doctrine(...)`; baseline — байты `BASELINE-<todo>.sha`; TODO — байты inbox; роль — `resolve_role_file`.

### K3. Команда

`awf context-pack <todo> --stage <name> [--stdout]`. Без `--stdout` пишет K2 атомарно, код 0, повтор даёт те же байты. С `--stdout` печатает текст, файл не создаёт и не меняет. Неизвестный TODO, стадия или битый снимок — код 2, сообщение с значением, файл не создаётся. Команда не резервирует номер и не пишет состояние.

### K4. Секция `stage_context` в `awf verify-pack`

Секция входит в `verify_pack` и в `GATES-<todo>.md`. При `engine.context_pack: off` — `pass`, `выключен (engine.context_pack: off)`, вердикт прежний (AC12). При `on`: файла нет — `fail`, `устарел: пакет не найден`; sha входов разошёлся — `fail`, `устарел: изменились входы: <список>`; совпал — `pass`, `актуален`. В `fail` — команда `regenerate with awf context-pack`.

### K5. Спавн

Порядок argv: `run --auto --print-logs --agent <agent> --title <title> --file <роль> [--file <доктрина>] --file <TODO> [--file <пакет>] [--model <model>] [--session <id>] <handoffs…> -- <prompt>`. Handoffs и порядок не меняются (INV7); пакет — после TODO и до handoffs, отсутствующий при `on` собирается до спавна. Ошибка сборки — строка в stage-log, спавн без пакета, код возврата прежний (AC14–AC16).

Заголовок: при `reuse_session != off` — `awf-<role>-<stage>-<todo>-a<N>`, при `off` — прежний `awf-<role>-<todo>` (AC39). Стадия — `stage.id or stage.name`, чтобы две стадии одной роли различались (AC37). `--session` — после `--model`; при выключенных ключах argv совпадает с текущим, кроме промпта R1 (AC17, INV1).

### K6. Файл сессии и счётчик попыток

`.agentic/context/SESSION-<todo>-<stage>.json` — ровно шесть ключей: `session_id`, `title`, `agent`, `model`, `attempt`, `ts`. `ts` — ISO-8601 UTC из базы (`time_updated`, иначе `time_created`), не время записи. Запись атомарная, после спавна, только при найденном id.

Счётчик пары — `state/current.yaml`: `stage_attempts: {"<todo>:<stage>": N}`. `pipeline_state.bump_stage_attempt(...)` делает чтение-изменение-запись под замком `write_state` и возвращает номер входа. Ошибка записи — журнал, возврат 1, стадия не падает. Номер резервируется до спавна, не откатывается и переживает `awf continue` (AC21, AC40).

Захват id: проба `sqlite_master` — `session`, затем `session_v2`. Ключ свежего спавна — точное равенство `title` плюс окно `time_created` от `spawn_started_ms = time.time() * 1000`, снятого перед `Popen`:

```
WHERE title = ? AND time_created >= spawn_started_ms - 2000
  AND time_created <= now_ms + 120000
ORDER BY time_created DESC, id DESC LIMIT 1
```

`directory` фильтром не является (замер: `$HOME` у 1652 сессий `awf-*`) — только тай-брейк при равных `time_created`. `agent` не фильтр: нужен для поля файла. Запас 2 с покрывает задержку строки в базе, 120 с — защита от немонотонных часов; пустой результат — id не пишется.

Продолжение ищет `session_id` из файла по `id`; `time_created` не сравнивается. Открытие read-only, `timeout=1.0`, `busy_timeout=1000`, проверка монотонных часов (AC23). Нет базы, схемы, колонок или записи — id не пишется, причина в stage-log, стадия не падает (AC22).

### K7. Выбор сессии

`session_store.decide(...)` идёт после резервирования номера, до сборки пакета; возвращает `reuse`, `session_id`, `reason`.

| Условие | Исход |
|---|---|
| `engine.reuse_session: off` | fresh, `reuse_session off` |
| роль не `agent-implementer` и не `agent-refactoring-specialist` | fresh, `role not allowed` |
| `session_fail_count` юнита ≥ 2 | fresh, `reuse disabled after 2 failures` |
| файла пары нет или он битый | fresh, `no session file` |
| роль или модель не совпали | fresh, `role/model mismatch` |
| `ts` не читается, в будущем или возраст > 240 мин | fresh, `session too old` |
| база недоступна или id не подтверждён | fresh, `session not found` |
| `fix-only`, номер входа 1 | fresh, `attempt 1` |
| `fix-only`, номер > 1, файл валиден | reused |
| `always`, файл валиден | reused, в том числе без `attempt` |

Возраст — от `ts` до текущего момента; лимит — `engine.session_max_age_minutes`, по умолчанию 240. При reused остаются `--agent`, `--model` и все прежние `--file` (AC28). Мёртвая база на продолжении даёт fresh, файл пары сохраняется (D6).

### K8. Наблюдаемость и счётчик неудач

Попытка пишет в stage-log ровно `session: reused <id>` или `session: fresh (<причина>)`. Счётчик неудач: ключ `todo_id`, число `session_fail_count`. Reused с сигналом сбрасывает в 0; reused без сигнала и с кодом ≠ 0 прибавляет 1; fresh не меняет. Другой юнит начинает с 0; `awf continue` сохраняет (AC29, AC32).

`reuse: k/n` по стадиям считается из журнала `.agentic/context/SESSIONS-<todo>.jsonl` — строка на попытку спавна под `O_APPEND`: `{"stage":…,"attempt":1,"decision":"fresh","reason":…,"ts":…}`.

`n` — число строк пары (юнит, стадия), `k` — строки `"decision":"reused"` этой пары; стадия берётся из поля: `awf-<role>-<todo>.out` её не несёт, две стадии одной роли пишут в один файл. Запись делает `agent_stage` после `decide` для каждой попытки, включая `off` (`fresh`, `reuse_session off`), что даёт `0/n`. Журнал переживает очистку `state/current.yaml` и слияние логов. `awf status` печатает `Stage <stage>: reuse k/n`; RUN-REPORT — `**Session reuse:** TODO-NNNN <stage> k/n`; нет журнала — поля нет, при `off` — `0/n` (AC30, AC31). Формат строки stage-log журнал не меняет.

### K9. Секция «С прошлого хода»

Только при reuse: `- виток: N` (старший номер `## REVIEW-фикс`, иначе число секций); `- изменённые файлы: a, b … +N` (`git diff --name-only <baseline>`, до 10); `- новая секция: ## REVIEW-фикс (…); тело — в секции 5`; `- красные тесты: …` из `GATES`; `- прошлая сессия не является источником фактов; проверяй дерево`. Пустой дифф и нет секций — `- нет изменений`; при свежей сессии секции нет (AC33–AC35, AC43).

### K10. Метрики

`metrics` принимает старый `awf-<role>-<todo>` и новый `awf-<role>-<stage>-<todo>-a<N>`. Общий `parse_worker_title(title, known_roles)` возвращает `(role, stage, todo, attempt)`; известные роли — из `.agentic/roles/*.md`, при неоднозначности побеждает длинное совпадение. `workers_by_role` группирует по `role`: стадия и суффикс статистику не делят (AC38, INV6).

## Данные и состояния

| Объект | Владелец записи | Жизненный цикл |
|---|---|---|
| `context/STAGE-<todo>-<stage>.md` | `context_pack` | пересборка при спавне и по команде; `off` не создаёт |
| `context/SESSION-<todo>-<stage>.json` | `session_store` | после спавна; продолжение обновляет `attempt` и `ts` |
| `context/SESSIONS-<todo>.jsonl` | `agent_stage` | строка на попытку; источник `k/n`; откат не чистит |
| `state/current.yaml` → `stage_attempts` | `pipeline_state` | до конца юнита; откат и continue сохраняют |
| `state/current.yaml` → `session_fail_*` | `session_store` | сброс на reused-успехе; другой юнит с 0 |

## Отказы и совместимость

| Отказ | Поведение |
|---|---|
| Сборка упала | stage-log, спавн без пакета; при `on` verify провалит юнит по AC12 (D8) |
| База недоступна или схема незнакома | fresh, причина в логе, файл не пишется, стадия не падает |
| В окне нет строки с нужным `title` | fresh, `session not found`; id не пишется |
| `directory` не каталог проекта | не отказ: ключ — `title` и окно, `directory` — тай-брейк |
| Запись состояния не удалась | номер 1, причина в логе, стадия идёт |
| `--session` отклонён | код ≠ 0 без сигнала — неудача reused; после второй — fresh до конца юнита |
| Пакет устарел | `stage_context` fail и команда пересборки |
| `context_pack: off` | пакет не собирается, «выключен», вердикт прежний |
| `reuse_session: off` | заголовок прежний, база не читается, файл не создаётся (AC39) |

Совместимость: handoff, коммит-гейт и изоляция `.agentic/` не меняются; новый модуль входит в wheel через `include = ["awf*"]`; метрики читают оба заголовка; откат — два ключа `engine.*`. Миграции нет: файлы появляются при первом спавне.

## Решения

D1 — пакет файлом через `--file`, не секцией промпта: AC14 и `--stdout`, `off` откатывает без правок роли (Q2).
D2 — свежесть по sha входов, живое дерево входом не является: стадия сама его меняет (A1).
D3 — id в файле пары, заголовок несёт стадию и номер; поиск по заголовку — только свежий спавн (Q1, CTX9).
D4 — счётчик попыток в `current.yaml` через один мутатор под `write_state`.
D5 — `fix-only` решает по номеру входа, не по `## REVIEW-фикс` (A3, A6).
D6 — мёртвая база на продолжении — fresh, файл пары сохраняется; иначе непроверенный id даёт две неудачи (RR44).
D7 — `always` только в паре и для двух ролей: иначе контекст `implementer` попадёт в `implementer-close` (A8).
D8 — нет пакета при `on` — провал verify, не исключение (RR47).
D9 — секция 5 — все `## REVIEW-фикс`, секция 9 — номер последнего витка (RR48, AC41).
D10 — потолки 1/7/9 явные, переполнение — ссылка (RR49).
D11 — `reuse: k/n` из журнала, не из stage-log (FR2).
D12 — Q3: справка 1.19.4 показывает `-s/--session` рядом с остальными флагами; семантику проверяет IMP5 шаг 0 (живой прогон в scratch): сообщение в ту же сессию, вложения видны. Негатив — `--session` не добавляется, политика деградирует до fresh.
D13 — id по точному `title` и окну `time_created`; `directory` — тай-брейк (у 1652 сессий `$HOME`), фильтр по каталогу отброшен (FR1).
D14 — журнал `SESSIONS-<todo>.jsonl`: stage-log закреплён AC30 без стадии, а AC31 требует счёт по стадиям; пишется и при `off`.

Допущения: Q4 (пороги 8 и 150) — за владельцем; id — только из базы и файла пары; `time_created` — миллисекунды эпохи, схема с секундами даёт fresh.

## План

| IMP | Цель и ссылки | Файлы | Зависит | Проверка |
|---|---|---|---|---|
| IMP1 | R1: восемь юнитов | `awf/supervisor.py`, `tests/unit/test_agent_stage_prompt.py` | — | `pytest tests/unit/test_agent_stage_prompt.py -q` → 8 id, фрагмент ≤300 |
| IMP2 | R2/R5/R9: пакет, тело секций, CLI | `awf/context_pack.py` (новый), `awf/todos.py`, `awf/cli.py`, `tests/unit/test_context_pack.py` | — | `pytest tests/unit/test_context_pack.py -q` → 8 секций, две сборки равны, `--stdout`, AC41 |
| IMP3 | R3: `stage_context` | `awf/verify_pack.py`, `tests/unit/test_verify_pack_stage_context.py` | IMP2 | `pytest tests/unit/test_verify_pack_stage_context.py -q` → актуален/устарел/нет файла/off |
| IMP4 | R4: пакет в спавне | `awf/agent_stage.py`, `tests/negative/test_stage_context_pack.py` | IMP2 | `pytest tests/negative/test_stage_context_pack.py -q` → argv-порядок, сбой не меняет код |
| IMP5 | R6/R7/K10: сессия, журнал, метрики | `awf/session_store.py` (новый), `awf/pipeline_state.py`, `awf/agent_stage.py`, `awf/metrics.py`, `tests/negative/test_session_reuse.py` | IMP1 | шаг 0 — живой `--session` (Q3); `pytest tests/negative/test_session_reuse.py -q` → fake v1/v2, `title`+окно при `$HOME`, 6 полей, `-a2` |
| IMP6 | R8: неудачи и `reuse: k/n` | `awf/agent_stage.py`, `awf/session_store.py`, `awf/api/lifecycle.py`, `awf/cmd_status.py`, `awf/api/run.py`, `tests/unit/test_session_reuse_status.py` | IMP5 | `pytest tests/unit/test_session_reuse_status.py -q` → две неудачи → fresh, k/n по стадиям |

Порядок: IMP1 и IMP2 независимы; IMP3, IMP4 после IMP2; IMP5 после IMP1; IMP6 после IMP5. Интерпретатор — venv `/home/pklochkov/.local/share/awf-venv/bin/python`; полный сьют — `bash scripts/run-all.sh`.

## Покрытие

- R1 — K1/IMP1; R4 — K5/IMP4; R2, R5, R9 — K2, K3, K9/D9, D10/IMP2.
- R3 — K4, D2, D8/IMP3; R6–R8 — K6–K8, K10, D3–D7, D11–D14/IMP5–IMP6.
- INV1–INV7 — K5, K8, K10; сигнал не меняется, IMP4–IMP6.
