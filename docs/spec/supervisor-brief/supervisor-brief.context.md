# Контекст: тулы супервизора и единая точка входа

Статус: REVIEW
Ревизия: ctx-2 (переснято после решения владельца «дубли удаляем»)
Входы: TODO-0326 (REVIEW-фиксы витков 1–4); `.agentic/pipelines/spec-supervisor-brief.yaml@spec-context`
База кода: HEAD `40bb6cc`; дерево грязное — `?? docs/spec/supervisor-brief/`. Артефакт бейзлайна держит `85ec91a` (01.11, на 10 коммитов позади); все факты ниже сняты с текущего дерева.
Ограничения: полный сьют не запускался; проверки — §8.

## 1. Итог для аналитика

- «Что сейчас и что делать» закрывают четыре MCP-тула: `awf_brief`, `awf_status`, `awf_current_step`, `awf_load_supervisor_context`. Каждый называет себя точкой входа (CTX1). Решение витка 4 — оставить `awf_brief`, два дубля удалить.
- Удаление упирается в три факта, которых не было в ctx-1:
  1. **Свой текст отказа после снятия имени невозможен**: MCP регистрирует только `registry.TOOLS`, вызов снятого имени даёт `ToolError("Unknown tool: …")` из `mcp` (CTX12).
  2. **`awf/api/context.py` переживёт удаление тула**: `get_status` импортирует оттуда три helper'а (CTX13).
  3. **Полный промпт не влезает в карточку**: промпт 775 слов, карточка 954 при потолке 900, карта 407 (CTX14).
- Порядок советов имеет владельца — `next_action_from_status`; им пользуются `awf_status` и `awf_brief`, а `awf_load_supervisor_context` подменяет ответ словарём `_PHASE_NEXT` (CTX2).
- Карта тулов статическая: 8 групп, 66 записей (56 MCP + 40 CLI), проверяются только имена (CTX7).
- Правда о verify раздвоена: `GATES-<id>.md` пишется движком до пробуждения супервизора и в одиночном старте, и в забеге, но карточка и статус его не читают; отпечатка дерева в отчёте нет (CTX6, CTX15). Вердикт коммит-гейта лежит в `COMMIT-AUDIT-<id>.json` и `publication_facts`, карточка их не читает (CTX16).

## 2. Карта затронутых путей

| Пункт | Вход | Код | Ответ | Потребители |
|---|---|---|---|---|
| `awf_brief` | MCP, CLI `awf brief` | `tools/awf.py:3406` → `api/brief.py:13` → `brief.py:810` → `render_brief:576` | `BriefResult`, 40 полей + `text` | MCP, CLI, `agents_md.py`, skill, `test_brief.py` |
| `awf_status` | MCP, CLI `awf status` | `tools/awf.py:243` → `get_status` | `StatusResult`, 36 полей + `next_action` | MCP, CLI, дашборд, brief, context |
| `awf_current_step` | MCP | `tools/awf.py:3368` → `phase.py:33,197` | `phase`, `prompt`, `goal`; совета нет | MCP, `_core.md`, `phase-init.md`, `phase-normalize.md`, `api/context.py` |
| `awf_load_supervisor_context` | MCP | `tools/awf.py:2521` → `api/context.py:314` | `SupervisorContextResult`, 38 полей | MCP, `agents_md.py`, `docs/design.md`, тесты |
| Карта тулов | файл | `tool_map.yaml` → `load_tool_map:194` | 8 групп | brief, `map_coverage` |
| Verify-пак | MCP, CLI, движок | `verify_pack.py:1922`; вызов — `pipeline_engine.py:1590-1592` | `GATES-<id>.md` + JSON | `budget.py:434`, `publish.py:1474` |
| Коммит-гейт | движок | `commit_gate.py`; `publish.py:1637` | `COMMIT-AUDIT-<id>.json`, receipt | `api/run.py:761,975` |

## 3. Что делает каждый тул

**`awf_brief`** собирает карточку в `build_brief`: шапка, `next_action`, выдержка промпта, State, Defaults, Scenarios, Tool map, Rituals, Recovery, Doctrine, What's new, Feedback. Рендер — чистая `render_brief`. Без `.agentic/` не падает. Живой ответ — 954 слова.

**`awf_status`** отдаёт состояние и добавляет `next_action`, если движок его не положил (`tools/awf.py:258-264`). Поля включают `pipeline_running`, `log_tail`, `current_stage_kind`, `expected_action`, `checkpoint_pending`, `stale_stage`, `engine_dead`, `zombie_state`, `run_active`.

**`awf_current_step`** — обёртка над `detect_phase` + `get_phase_prompt`. Промпт фазы `run`: 5076 символов, 112 строк, 775 слов. Совета не даёт, хотя `docs/design.md:44` обещает `next_action` у каждого тула.

**`awf_load_supervisor_context`** агрегирует vision, `plan.md`, промпт, статус, стадию, запреты следующей роли, `git diff --stat`, запись забега. `supervisor_md` байт-в-байт равен `phase_prompt` (проверено вызовом).

## 4. Пересечения и расхождения

### 4.1 Четыре заявки на одну точку входа

| Источник | Текст | Файл |
|---|---|---|
| `awf_brief` | «Call this at the START of a new session (or when context is lost)» | `tools/awf.py:3409` |
| `awf_current_step` | «Call this at the START of every interaction» | `tools/awf.py:3377` |
| `awf_load_supervisor_context` | «at session start… Replaces 5-6 separate tool calls» | `tools/awf.py:2526` |
| `AGENTS.md` | «start with `awf_brief`… `awf_status` is for mid-cycle checks» | `agents_md.py:11,36-37` |

Инъекция уже выбрала `awf_brief`, но совет звать `awf_load_supervisor_context` остался в рецепте и таблице (`agents_md.py:48,94`), хотя тул требует `.agentic/` (`context.py:333`).

### 4.2 `next_action`: один владелец, несколько ответов

Одно состояние (стадия `spec-context`, процесс жив) даёт два совета: brief/status — «Pipeline running… go idle; in a run wait with `awf_wait_for_event`», context — «Pipeline running. GO IDLE — wait for user». Второй берётся из `_PHASE_NEXT` (`tools/awf.py:2559-2574`) при пустом `result.next_action`; в забеге он ошибочен. Расхождение закреплено `tests/negative/test_context_single_source.py`.

### 4.3 Ожидание событий

Один docstring (`tools/awf.py:3008-3017`) держит оба правила: «Do NOT call in a loop after `awf_start`» и «inside an active run the supervisor DOES wait in a loop». Признака режима в ответе нет; `phase-run.md` различает режимы через `awf_run_status`.

### 4.4 Карта тулов

Восемь групп, подсказки 2–5 слов, группа `helpers` скрыта. Спорные тулы стоят рядом в `check` с подсказками «state: units, pipeline», «phase + prompt», «full context, one call». Выбор по ним не сделать. `map_coverage` проверяет только имя.

### 4.5 Промпт фазы и бюджет карточки

| Что | Слов | Символов |
|---|---|---|
| карточка целиком | 954 | — |
| Tool map / Recovery / State | 407 / 163 / 94 | — |
| полный промпт `run` | 775 | 5076 |
| `_core.md` + `phase-run.md` | 333 + 414 | 2189 + 2731 |

Потолок `MAX_WORDS=900` (`brief.py:29`); карточка показывает 45-словную выдержку (`:49`) и строку «(… full version: `awf_current_step`)» (`:601`). Полный промпт требует 775 слов — больше карты и recovery вместе. Выдержка рендерится только без записи забега (`r.run is None`, `:594-601`); здесь запись есть, поле посчитано (45), в текст не попало.

### 4.6 Правда о verify

Пак пишет `GATES-<id>.md`: вердикт `ok|failed|nothing-measured`, exit 0/1/2, 13 секций, JSON-блок `todo_id`, `verdict`, `exit_code`, `measured`, `sections`, `details`, `report` (`verify_pack.py:1881-1905`). Файлы 0337–0341 лежат в `.agentic/context/`.

Движок зовёт пак до ожидания супервизора в обоих режимах (`s_kind == "verify"`, `pipeline_engine.py:1590-1592`), без `run_record.active`; сбой даёт отчёт с вердиктом `error` (`:396-411`).

Чего нет: отпечатка текущего дерева. `Generated` — момент прогона; секция `diff` считает от `BASELINE-<id>.sha`, но SHA в JSON не сохраняется. Правило «вердикт текущего прохода» (R7/AC28) артефактом не проверяется. Единственный парсер отчёта — частный `budget._gate_statuses` (`budget.py:424`).

### 4.7 Коммит-гейт

Артефакт `COMMIT-AUDIT-<id>.json`: `status` (`committed|skipped|refused|error`), `excluded`, `worker_staged`, `skip_reason` (`commit_gate.py:425-461`). Публикация отдаёт `commit`/`receipt`/`archived` (`publish.py:1637`). В `BriefResult` и `StatusResult` полей коммита нет.

### 4.8 Docstring'и и поля

- `awf_status` в Returns — 9 полей из 36 (`tools/awf.py:250-256`); нет `pipeline_running`, `log_tail`, `expected_action`, `stale_stage`, `engine_dead`.
- `awf_brief` — часть из 40: нет `standalone_todos`, `run_goal`, `stale_stage`, `actionable_fact`.
- `awf_load_supervisor_context` обещает `last_signal` «from log», фактически — из state (`context.py:62`).
- `awf_current_step` обещает «~50-100 lines»; фактически 112.

## 5. Поверхности, которые придётся править при удалении

| Файл:строка | Что | Тип |
|---|---|---|
| `tools/awf.py:3368-3403`, `:2521-2591` | определения тулов + `_PHASE_NEXT` | код |
| `tools/awf.py:2273,2573,2649` | советы и docstring | `next_action` |
| `tools/registry.py:78,94` | регистрация | реестр |
| `brief.py:45-49,601,806` | выдержка, строка «full version», fallback-совет | карточка |
| `tool_map.yaml:37-40` | обе записи в `check` | карта |
| `phase.py:276` | текст отказа `advance_phase` | сообщение |
| `api/lifecycle.py:780,1158` | комментарий и `suggestion` | совет |
| `api/brief.py:31`; `run_plan_read.py:3` | docstring «два представления» | докстринг |
| `_core.md:48`; `phase-init.md:9`; `phase-normalize.md:6` | «зови `awf_current_step`» | промпт фазы |
| `agents_md.py:48,94` | рецепт и таблица | инъекция |
| `SKILL.awf-supervisor.md:156` | группа `check` | skill |
| `USAGE.md:669,1006,1012,1070,1079` | справка и таблица | доки |
| `USAGE.ru.md:759,765,824,833` | то же | доки |
| `docs/design.md:44,100`; `docs/file-bus.md:108` | описание и таблица модулей | доки |
| `agent_workflow_ui/README.md:19` | список имён | доки пакета |
| `tests/.../test_server_smoke.py:109,122`; `test_awf_tools.py:2063-2100`; `test_coverage_gaps.py:132-136` | ожидаемые имена и тесты тулов | тесты |
| `test_context_single_source.py`; `test_todo0307...`; `test_run_evidence_plan.py:9`; `test_phase.py:207`; `test_orchestrator.py:1736`; `test_phase_live.py:6` | пинящие тесты и упоминания | тесты |

Числа для синхронизации: `USAGE.md:687`, `USAGE.ru.md:373`, `README.md:61,95` (56 = 51 `awf_*` + 5 UI); сверяет `test_audit25_w6_docs_ci.py`. После удаления двух тулов станет 54 = 49 + 5.

CLI: подкоманд `current-step` и `supervisor-context` нет (`awf --help`, `invalid choice`); «удалить из CLI» — no-op, CLI трогает только `awf brief`.

## 6. Переиспользуемые компоненты

- **Советы.** `next_action_from_status` (`brief.py:706`): `expected_action` → foreign_commit → salvage → blocked → budget → engine_dead → stale → zombie → running → active → suggestion.
- **Гейт setup-цепочки.** `_actionable_fact:689` + `_ACTIONABLE_FIELDS:673`.
- **Карточка.** `BriefResult` + `render_brief`; `as_dict()` для `awf brief --json`.
- **Карта.** `load_tool_map:194`, `tool_registry:255` (читает `registry.tool_names()`), `map_coverage:287`.
- **Запись забега.** Один читатель `run_plan_read.py`.
- **Фазы.** `detect_phase`/`get_phase_prompt`; шаблоны `awf/templates/roles/supervisor/*.md`, проектные переопределяют.
- **Verify и коммит.** `verify_pack.verify_pack:1922`; `budget._gate_statuses:424`; `commit_gate` audit; `publish.publication_facts:1637`.
- **Хелперы статуса.** `api/context.py:31,125,204` — `_extract_stage_info`, `_compute_expected_action`, `_compute_stage_kind`.

## 7. Поведение, которое нельзя сломать

- `awf_brief` не падает без `.agentic/`.
- Детерминизм: два вызова в одном состоянии совпадают, кроме даты.
- Карта и реестр синхронны (`map_coverage`).
- `awf_wait_for_event` отдаёт `done` только для подтверждённого цикла; потолок `TRANSPORT_CAP=55`, поднимается `wait.cap_seconds`/`AWF_WAIT_CAP`.
- `AGENTS.md` и skill байт-в-байт равны источникам.
- Числа тулов в доках = реестр.
- `awf_status` read-only.

## 8. Baseline и проверки

| Проверка | Результат |
|---|---|
| `git rev-parse HEAD`, `git status --porcelain` | `40bb6cc`; `?? docs/spec/supervisor-brief/` |
| `pytest test_brief.py test_phase.py test_phase_live.py test_context_single_source.py test_server_smoke.py -q` | PASS: 124 passed, 5.57 с |
| `pytest test_audit25_w6_docs_ci.py test_tool_descriptions.py test_agents_md.py test_supervisor_skill.py test_audit25_r06_registry.py -q` | PASS: 61 passed, 2.23 с |
| `api.brief(Path('.'))` | 954 слова (потолок 900); 40 полей |
| `map_coverage()` | оба списка пусты |
| `get_phase_prompt('run', '.')` | 5076 символов, 112 строк, 775 слов |
| `api.load_supervisor_context(Path('.'))` | `supervisor_md == phase_prompt`; warning про запреты; `next_action=""` |
| `python3 -m awf status` | `log_tail` от TODO-0340 при активном TODO-0326 |
| Реестр | 56 MCP: 51 `awf_*` + 5 UI |
| `awf --help` | 40 подкоманд; `brief`, `status` есть; `current-step` нет |
| Полный сьют | NOT_RUN |

## 9. Findings

| ID | Важность | Файл / символ | Факт | Влияние | Следующий шаг |
|---|---|---|---|---|---|
| CTX1 | MAJOR | `tools/awf.py:3377,3409,2526`; `agents_md.py:11` | Четыре источника зовут себя точкой входа | 2–3 вызова на один вопрос | Аналитик: правило одного входа |
| CTX2 | MAJOR | `tools/awf.py:2559-2574` vs `brief.py:706` | Разные советы из одного состояния | В забеге совет context ошибочен | Один владелец совета |
| CTX3 | MAJOR | `tools/awf.py:3008-3017` | «Не в цикле» и «в цикле» в одном docstring | Непонятно, ждать или реагировать | Одно правило с признаком режима |
| CTX4 | MAJOR | `brief.py:29`; `test_brief.py:602-608` | Живая карточка 954 слова > 900; тест меряет фикстуру | Потолок не держится | Бюджет по секциям |
| CTX5 | MAJOR | `_background.py:274`; `lifecycle.py:1204` | `log_tail` от предыдущего юнита | Супервизор читает не тот юнит | Лог текущего прогона |
| CTX6 | MAJOR | `verify_pack.py:1881-1905`; `brief.py:88-97` | `GATES` есть, карточка молчит | Ручной дубль машинного отчёта | Вердикт verify — в `awf_brief` |
| CTX7 | MINOR | `tool_map.yaml`; `brief.py:287` | 66 записей, подсказки 2–5 слов, проверяется имя | Карта не отвечает «что делать» | Ситуационная карта |
| CTX8 | MINOR | `context.py:363` | `supervisor_md == phase_prompt` | Дубль нагрузки | Решить с судьбой тула |
| CTX9 | MINOR | `context.py:393-396` | Warning про отсутствие секции запретов | Шум в bootstrap | Сверить маркеры секций |
| CTX10 | MINOR | `tools/awf.py:250-256,3419-3440` | Docstring'и перечисляют часть полей | Модель не видит остальные | Синхронизировать |
| CTX11 | SUGGESTION | `tool_map.yaml`; `agents_md.py`; skill | Три ручных источника текста | Правило меняется трижды | Проверка согласованности |
| CTX12 | BLOCKER | `server.py:27-28`; `mcp/.../tool_manager.py:78,91` | После снятия имени MCP отдаёт `Unknown tool`; своего диспетчера нет | Требование «внятный отказ» невыполнимо без сохранения имени | Владельцу: заглушка или стандартная ошибка |
| CTX13 | MAJOR | `lifecycle.py:1189,1228`; `context.py:31,125,204` | `get_status` импортирует три helper'а | Модуль нельзя удалить целиком | Удалять только `load_supervisor_context` |
| CTX14 | MAJOR | `brief.py:29,49,594-601`; `phase.py:197` | Промпт 775 слов при потолке 900 и карточке 954 | Полный промпт не помещается | Бюджет по секциям |
| CTX15 | MAJOR | `verify_pack.py:1881-1905`; `pipeline_engine.py:1590-1592` | Пак идёт до пробуждения в обоих режимах; отпечатка дерева нет | R7/AC28 артефактом не проверяется | Назвать признак свежести |
| CTX16 | MINOR | `commit_gate.py:425-461`; `publish.py:1637` | Вердикт коммита в audit, бриф его не читает | Не видно, чем кончился гейт | Читать audit в брифе |
| CTX17 | MINOR | `USAGE.md:687`; `USAGE.ru.md:373`; `README.md:61,95` | Числа сверяются тестом | Забытое число роняет CI | Включить в план |
| CTX18 | MINOR | `awf --help` | Подкоманд для двух тулов нет | Ложная строка в плане | Убрать из требований |

## 10. Вне scope и вопросы

Вне спецификации: очередь inbox и `done/`, сервисный забег, движок и `DONE.json`, `build/lib/**` (игнорируемые копии).

- **Q1 (владелец, блокирует).** Отказ удалённого тула: заглушка с тем же именем (набор не сокращается) или стандартная ошибка MCP (требование снимается)?
- **Q2 (аналитик).** Где живёт полный промпт: в карточке, отдельным полем JSON или по запросу?
- **Q3 (архитектор).** Как уложить 775 слов в бюджет 900 при карте 407: сжимать карту, выносить промпт по требованию, поднимать `MAX_WORDS`?
- **Q4 (архитектор).** Признак свежести вердикта verify: отпечаток дерева в отчёте, mtime против правок или стадия verify в статусе?
- **Q5 (архитектор).** «Чего ждёт движок»: `expected_action` и `checkpoint_pending` есть в статусе, не в `BriefResult`.
- **Q6 (архитектор).** Python-потребители `load_supervisor_context` (`api/__init__.py:95,282`, `planning.py:4`): оставить API или снять с тулом?
- **Непроверено:** `wait_for_event` в реактивном режиме; `run.yaml` при активном забеге; полный сьют.
