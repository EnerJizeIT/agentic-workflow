# Контекст: closer-стадия Definition-of-done

Scope: closer-stage (SPEC-ран: только документы, код не меняется)
Ревизия: ctx-1
Входы: TODO-0265; retired `done/TODO-0245/TODO.md` (8 требований); протокол `agent-system-analyst/references/specification-pipeline.md`; код awf на базе ниже
База кода: commit `258f28a43f91663011c3a88967f15a2dd894d4b8`; в рабочем дереве — новый пакет `docs/spec/closer-stage/` (untracked) и правка `M awf/budget.py`, `M tests/negative/test_unit_budget.py`: `review_passes` не считает заголовки со словом `spec`; правка внесена стадией spec-requirements req-3 вне scope, решение — за супервизором (RR24)
Статус: REVIEW

## 1. Итог для аналитика

Что уже есть и можно не изобретать:

- DoD-гейт: `awf/checklist_gate.py:493` (`run`) исполняет пробы блока, пишет вердикт в `context/CHECKLIST-<todo>.json` (`_cache_path`, `:384`). Вердикт привязан к отпечатку дерева, блока и целей проб; тот же вход переиспользует запись, другой — пересчитывает (`:554`).
- Форма вердикта: `status=none|checked`, `ok`, `items[]` (`ok|missing|manual`), `missing[]`, `warnings[]`, `proposals`, `tree_sha`, `block_sha`, `probe_sha`, `cached`.
- verify-pack уже вызывает гейт отдельной секцией `checklist` (`awf/verify_pack.py:1084`): провал — `fail` секции, отсутствие блока — `none`, битый блок — `fail`, а не падение пакета.
- CLI: `awf checklist show|check|apply|hash <todo>` (`awf/cmd_checklist.py`, `awf/cli.py:537`). `hash` (`:99`) возвращает хэш блока; `apply` пересобирает блок только у нестартовавшего юнита.
- Пакет-гейт SPEC-рана: `spec_gate` (`awf/spec_package.py:705`) читает шесть документов, требует REVIEW у авторов, PASS у ревью, READY у готовности и совпадение `Пакет-sha`; бюджет — 2200 слов на документ и 11000 на пакет (`docs/unit-contract.md:240-280`). Файл требований — top-level `*.md` с именем каталога, то есть `closer-stage.md`.
- Блок `## Чек-лист завершения` — единственный источник пунктов для гейта (`docs/unit-contract.md:155`); композиция base/classes/wave/acceptance замораживается при диспатче.
- Схема стадии: `Stage` (`awf/pipeline.py:43`), допустимые ключи (`:150`), загрузка (`:514`), объявленный выход с проверкой свежести (`awf/pipeline_engine.py:950`), write-allowlist стадии (`:1387`), BLOCKED-схема (`:1488`).
- Цикл стадий и решения о переходе: `awf/orchestrator.py:300`; запуск исполнителя — `awf/pipeline_engine.py:1630`; сборка промпта стадии — `awf/agent_stage.py:34,72`.

Чего нет — это придётся спроектировать:

- `skip_if` нет нигде: ни в `ALLOWED_STAGE_KEYS`, ни в `Stage`, ни в снапшоте, ни в шаблонах (grep по `awf/`, `tests/`, `docs/` — ноль).
- closer-стадии нет ни в одном рабочем пайплайне: `.agentic/pipelines/default.yaml`, `awf/templates/pipelines/full.yaml`, `code-health.yaml`, `spec.yaml` её не содержат. Файл `.agentic/pipelines/closer-stage.yaml` — сам SPEC-ран для этого пакета, а не closer-стадия.
- Бюджета `checklist.closer_attempts` нет; ключ никто не читает и не валидирует.
- Неизменяемость блока: хэш получить можно, но никто не снимает его до closer и не сравнивает после; нет места хранения «до».
- `awf checklist stats` нет: действия — только `show|check|apply|hash`.
- Теста `tests/negative/test_dod_closer_stage.py` нет, хотя контракт retired TODO-0245 ссылался на него в `prove_red`.

Главные ограничения, которые сужают решения:

- `kind` стадии вычисляется по позиции (`awf/pipeline.py:177`): первая стадия — `plan`, последняя — `verify`. Значит, воркер на позиции 0 получает супервизорский промпт (`awf/supervisor.py:265`). В прошлой попытке вышло `Stage 0: spec-context (agent-project-auditor :: plan)`; теперь первая стадия `closer-stage.yaml` — `plan`.
- Коммит делает движок на verify: у стадии в середине нет commit-инструментов, но `write_allow`/`output` надо объявить, иначе запись нарушит allowlist.
- Кэш гейта хранит только последний вердикт на юнит и перезаписывается при смене дерева. Истории «сколько раз пункт был missing на первом проходе» в `CHECKLIST-*.json` нет; для `caught` нужен отдельный носитель или новое поле.
- `status=none` (блока нет) — не провал: старые юниты остаются зелёными. `skip_if: checklist_green` должен трактовать `none` как «пропустить», а не как «красный».

## 2. Карта затронутых путей

| Требование | Вход | Компоненты | Данные и эффекты | Источник |
|---|---|---|---|---|
| 0. Пакет-гейт | `docs/spec/closer-stage/*` | `spec_gate`, `cmd_spec_check` | `awf spec check` → rc 0 при READY; бюджет слов | `awf/spec_package.py:705`; `docs/unit-contract.md:240-280` |
| 1. `skip_if` в схеме | `pipeline.yaml` | валидатор стадий, `Stage`, загрузчик, снапшот | ошибка загрузки на неизвестном значении; поле едет в per-TODO снапшот | `awf/pipeline.py:150,235,514`; `pipeline_snapshot_text` `:628`; `awf/api/run.py:3525` |
| 2. Гейт после implementer | TODO юнита | вызов `checklist_gate.run` | `context/CHECKLIST-<todo>.json`; строка `dod: N/M` в логе стадии | `awf/checklist_gate.py:493`; `awf/orchestrator.py:300` |
| 3. Closer-стадия | шаблоны пайплайнов | пара `Stage`: `checklist-close` (`role=agent-implementer`, `session_key`, `skip_if`) и `checklist-check` (`role=agent-qa-review`, `skip_if: checklist_close_skipped`) | пропуск без спавна воркера; иначе обычная стадия | `awf/templates/pipelines/*`; `.agentic/pipelines/default.yaml` |
| 4. Бюджет closer | `config.yaml` | новый валидируемый ключ, счётчик попыток | `BLOCKED-<id>.md` со списком missing | образец: `awf/budget.py:78`; шаблон `awf/api/_templates.py:58` |
| 5. Неизменяемость | TODO + контракт | `block_hash`, снимок «до», сравнение «после» | BLOCKED «closer edited the checklist/contract» | `awf/checklists/_render.py:197`; `awf/cmd_checklist.py:99` |
| 6. Статистика | `context/CHECKLIST-*.json` | новое действие CLI | markdown-таблица + `--json`; пустые данные — пустая таблица | `awf/checklist_gate.py:384,493`; `awf/cmd_checklist.py` |
| 7. Шаблоны и поставка | генератор/шаблоны | `build_pipeline_stages`, `BUILTIN_PIPELINES`, YAML-стартеры | closer в новых проектах; совместимость старых | `awf/api/setup.py:76,275`; `awf/templates/pipelines/` |
| 8. Тесты | новый negative-файл | сценарии green/red/бюджет/hash/stats/старый пайплайн | красный до, зелёный после | `tests/negative/test_dod_closer_stage.py` (новый) |

## 3. Переиспользуемые компоненты и контракты

- `checklist_gate.run(project, todo_id)` — готовый вердикт; его поля (`ok`, `missing`, `manual`, `block_sha`, `cached`) описывают и решение о пропуске, и данные статистики.
- `block_hash(todo_text)` (`awf/checklists/_render.py:197`) — хэш только блока, не всего TODO. Подходит для «блок не изменился»; для «контракт не изменился» нужен отдельный хэш (см. CTX8).
- Схема стадий: добавление ключа требует пройти все писатели — `ALLOWED_STAGE_KEYS`, `validate_pipeline_stages`, `load_stages`, `Stage`, `pipeline_snapshot_text` и сборщик service-run (`awf/api/run.py:3535`). Один пропущенный писатель молча теряет ключ: снапшот — это то, что реально исполняется.
- Один рендер YAML — `dump_pipeline_document` (`awf/api/pipelines.py:80`); формы и `awf pipeline-write` уже идут через него.
- Валидируемый целочисленный конфиг уже есть: `max_review_passes` с потолком и `BudgetConfigError` (`awf/budget.py:78`). Для `closer_attempts` разумно повторить этот приём, а не читать сырое значение.
- `readonly_roles` в этом проекте — `agent-qa-review`, `agent-project-auditor` (`.agentic/config.yaml`). `agent-implementer` не readonly, поэтому closer может писать файлы и запускать `awf checklist check` через bash.
- CLI-действия — словарь `_ACTIONS` в `awf/cmd_checklist.py`; новое действие встраивается туда же, без второго диспетчера.
- Пакет нормы как контракт: `spec_package` + `source_refs` в TODO дев-юнита (`docs/unit-contract.md:240-270`) запускают `spec_gate` в трёх точках — создание, старт, продолжение. Готовый пакет `docs/spec/closer-stage` станет нормой для IMP-юнитов только через это объявление.

## 4. Поведение, которое нельзя сломать

- Кэш гейта: тот же `tree_sha` + `block_sha` + `probe_sha` → запись переиспользуется; смена дерева или целей проб → пересчёт. DONE-заявления не истина: расхождение — предупреждение.
- Пробы остаются дешёвыми и читающими: закрытый allowlist флагов, очищенное окружение, таймаут не больше 120 секунд, путь внутри проекта.
- Блок в TODO — единственный источник пунктов; исполнитель не правит чек-лист, только предлагает пункты в DONE (предложение — предупреждение, не автоприменение).
- Пустой блок — провал, отсутствие блока — `none`. Это разные состояния, их нельзя схлопнуть в «зелено».
- Переходы, ретраи, salvage, BLOCKED-схема и write-манифест не меняются. Старый пайплайн без `skip_if` грузится и идёт как раньше.
- Канарейки безопасности: `proc.pid > 1` в `awf/_proc.py` и tripwire в `tests/conftest.py` не трогать.
- Бюджет инструкций: 948/950 слов и 250/250 доктрины. Новые строки в `AGENTS.md`, `docs/contracts/*`, доктрине требуют удаления других.

## 5. Baseline и тестовые средства

| Проверка | Команда | Результат | Наблюдение |
|---|---|---|---|
| Отпечаток дерева | `awf tree-sha` | — | `827d6718…`; `SUITE-`-артефакта на этот отпечаток нет |
| Целевые тесты | `pytest tests/negative/test_dod_checklist_core.py tests/unit/test_pipeline.py tests/unit/test_pipelines_api.py -q` | PASS | 177 passed, 19.01 с (свежий прогон на этом дереве) |
| Тесты движка | `pytest tests/unit/test_pipeline_engine.py tests/unit/test_run.py -q` | PASS | 133 passed, 9.41 с (свежий прогон) |
| Полный сьют | `verification.test_cmd` | NOT_RUN на текущем дереве | предыдущий артефакт `SUITE-faa6…json` снят на другом дереве; счётчик в `run-all.sh` |
| Ворота | `bash scripts/run-all.sh` | NOT_RUN | контракты, храповик, инструкции, проектные команды |

Тестовые средства: `tests/negative/test_dod_checklist_core.py` (ядро 0244: композиция, пробы, кэш, CLI, verify-pack), `tests/unit/test_pipeline.py` и `test_pipelines_api.py` (схема и запись пайплайна), `tests/unit/test_pipeline_engine.py` и `test_run.py` (переходы, объявленный выход, verify). Новый файл closer-тестов пока не существует.

## 6. Находки в scope

| ID | Важность | Файл / символ | Факт и доказательство | Влияние на фичу | Следующий шаг / владелец | Уверенность |
|---|---|---|---|---|---|---|
| CTX1 | MAJOR | `awf/pipeline.py:150,235,514`; `Stage:43`; `pipeline_snapshot_text:628`; `awf/api/run.py:3525` | Ключа `skip_if` нет ни в схеме, ни в загрузчике, ни в снапшоте (grep — ноль). Снапшот перечисляет поля вручную и при пропуске молча теряет ключ. | Без правки всех писателей стадия «пропустить» не доедет до исполняемого снапшота или упадёт на неизвестном ключе. | Архитектор: перечислить все поверхности, добавить enum-валидацию и тест снапшота. | Высокая |
| CTX2 | MAJOR | `awf/pipeline.py:177`; `awf/supervisor.py:265`; лог забега | `kind` считается по позиции: стадия 0 — `plan`. Воркер получает супервизорский промпт «создай TODO». Наблюдено: `Stage 0: spec-context (agent-project-auditor :: plan)`. | Если closer поставить первой (или единственной рабочей), он получит не тот промпт; для spec-рана это уже сработало. | Аналитик/архитектор: не ставить closer первой либо ввести явный kind; проверить, что положение closer — середина. | Высокая |
| CTX3 | MAJOR | `awf/checklist_gate.py:384,493`; `awf/cmd_checklist.py` | На юнит хранится один `CHECKLIST-<todo>.json`; при смене дерева он перезаписывается. Истории первого прохода нет; `manual`-пункты не имеют пробы. | `stats` в формулировке 0245 («caught — сколько раз missing на pass 1») из текущих файлов не выводится; нужно определить носитель истории. | Аналитик: уточнить смысл `caught/runs`; архитектор: выбрать поле или append-only файл. | Высокая |
| CTX4 | MAJOR | `awf/api/_templates.py:58`; `awf/budget.py:78` | Ключа `checklist.closer_attempts` нет в шаблоне конфига; читателя и валидатора тоже нет. | Без валидации опечатка молча отключит или увеличит бюджет; поведение «красный после closer» не определено кодом. | Аналитик: семантика попыток; архитектор: валидатор по образцу `max_review_passes`. | Высокая |
| CTX5 | MINOR | `awf/api/lifecycle.py:384`; `awf/api/setup.py:76,275` | «Генератор init» в 0245 назван неточно: `init_project` не пишет `.agentic/pipelines/*` вовсе; пайплайн создаёт форма через `build_pipeline_stages` или берётся готовый стартер. | Непонятно, какие поверхности считать нормативными для «closer в новых проектах». | Аналитик: назвать поставку (setup-генератор, `full.yaml`, `code-health.yaml`, локальный `default.yaml`); архитектор: план правок. | Высокая |
| CTX6 | MINOR | `awf/pipeline.py:514`; `awf/pipeline.py:235` | Неизвестный ключ стадии — ошибка валидатора; неизвестное значение нового enum тоже должно быть ошибкой загрузки. Сейчас загрузчик на ошибке YAML возвращает пустой список. | Иначе опечатка `skip_if: checklist-grenn` тихо оставит стадию всегда включённой. | Архитектор: явная ошибка значения + тест; сверить с поведением `write_check`. | Высокая |
| CTX7 | MINOR | `awf/pipeline.py:629`; `awf/api/run.py:3510` | Два независимых сборщика словаря стадии: per-TODO снапшот и service-run. Оба перечисляют поля вручную. | Новый ключ легко добавить в один и забыть в другом; снапшот — исполняемая копия. | Архитектор: один сериализатор полей стадии или тест на равенство. | Высокая |
| CTX8 | MINOR | `awf/checklists/_render.py:197`; `awf/unit_contract.py:127` | `block_hash` покрывает только блок чек-листа; контракт (`verify/gates/prove_red/files`) хэшируется отдельно или не хэшируется. | «Closer изменил чек-лист/контракт» — два разных инварианта; один хэш их не покрывает. | Аналитик: что именно неизменяемо; архитектор: где хранить «до» и что сравнивать. | Средняя |
| CTX9 | SUGGESTION | `awf/cmd_checklist.py`; `context/CHECKLIST-*.json` | Статистика может быть markdown и `--json`; пустые данные обязаны давать пустую таблицу, а не выдуманные нули. | Дешёвая наблюдаемость без изменения гейта. | Аналитик: формат отчёта; QA: тест пустого набора. | Высокая |
| CTX10 | MINOR | `awf/spec_package.py:705`; `docs/unit-contract.md:240-280` | Новый пакет обязан пройти `spec check`: шесть документов, статусы, покрытие, `Пакет-sha`; бюджет 2200/11000; файл требований — `closer-stage.md`. | Формат пакета задан гейтом, а не волей автора; отступление даёт rc≠0 в приёмке 0265. | Аналитик: держать имена и бюджет; архитектор: объявить `spec_package` у первого IMP-юнита. | Высокая |

## 7. Несвязанные находки

- Предыдущая попытка 0265 была заблокирована устаревшим снимком `PIPELINE-TODO-0265.yaml` от `spec.yaml`: он требовал чужой пакет `docs/spec/spec-pipeline/`. Теперь снимок переснят с `pipelines/closer-stage.yaml` и указывает на `docs/spec/closer-stage/`. К closer-требованиям отношения не имеет.
- `.agentic/` в `.gitignore`: правки `.agentic/pipelines/default.yaml` живут только на этой машине. Переносимая поставка — `awf/templates/pipelines/*` и генератор формы.

## 8. Вопросы и допущения

- Q1 (аналитик): требование 2 («гейт после implementer») и требование 3 («closer-стадия») — это один механизм или два? Нужен один нормативный триггер, иначе непонятно, когда именно запускается closer.
- Q2 (архитектор): где вычисляется `skip_if` — в цикле `orchestrator.py:300` до спавна или в начале `execute_agent_stage`? Как пропуск виден в логе, state и verify?
- Q3 (архитектор): где хранится история для `stats` — новое поле в `CHECKLIST-<todo>.json` (перезаписывается) или отдельный append-only файл? Что считать `runs` при `cached=true`?
- Q4 (архитектор): неизменяемость — хэш блока, хэш контракта или оба? Где хранится снимок «до» (state, context, TODO) и переживает ли он рестарт?
- Q5 (аналитик): `closer_attempts` — попытки на юнит или на вход стадии? После второго красного — BLOCKED без третьего круга; повторный запуск после BLOCKED бюджет не увеличивает?
- Q6 (аналитик): какие поверхности поставки нормативны — генератор формы, `full.yaml`, `code-health.yaml`, локальный `default.yaml`, пример волны?
- A1: `agent-implementer` не входит в `readonly_roles`, поэтому closer может писать файлы и запускать `awf checklist check`. Проверено конфигом и набором инструментов роли.
- A2: `kind` в этом дереве не настраивается явно; единственный способ получить execute-промпт — поставить стадию в середину пайплайна.
