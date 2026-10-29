# Архитектура: адаптация awf под opencode 2.x (режимы v1 и v2)

Статус: REVIEW
Ревизия: arch-1
Входы: opencode2.md@req-5; opencode2.requirements-review.md@reqrev-5; opencode2.context.md@ctx-2; TODO-0311
База кода: commit `4a15afccea417216d35e547bdeb20ea7eb0e22b2`; в дереве только untracked `docs/spec/opencode2/`

## Ответ на открытые вопросы

Q1 (managed) закрыт D4: признак — явный opt-in `opencode.managed` в конфиге проекта, env `AWF_OPENCODE_MANAGED` перекрывает; по умолчанию `false`. Автоопределение по реестру сервиса не делаем: файл `~/.local/state/opencode/service.json` говорит лишь «сервер есть», а не «политику задаёт оператор»; ложное срабатывание выключает защиту от орфанов молча.

RR35 (RR35 MINOR из ревью) закрыт D6 и K2: версия неизвестна — `on` ведёт себя как `auto` (без флага), затем отказ K3. Сочетание «`on` + неизвестная версия» больше не зависит от реализации.

A6/A7 (пробы `models --standalone` и поля recent models) — D9: до пробы ридер не угадывает; константы `MODELS_STANDALONE_SUPPORTED` и `V2_MODEL_FIELD` заполняет IMP1, тесты строятся на снятой фикстуре.

## Границы и точки изменения

Новых сервисов, очередей и MCP-тулов нет. Один новый модуль `awf/opencode_version.py` (версия и режим) и один `awf/opencode_db.py` (проба схемы, общий для CLI и плагина). `OPENCODE_CONFIG_CONTENT` и `agent_workflow_ui` не переписываются, кроме импорта общего ридера.

| Компонент | Ответственность | Файл | R# |
|---|---|---|---|
| Версия и режим | `--version`, домен ключа, эффективный argv | новый `awf/opencode_version.py` | R1 |
| Спавн воркера | вставка `--standalone` | `awf/agent_stage.py::run_agent_stage` | R1 |
| Спавн супервизора | то же | `awf/supervisor.py::run_supervisor_via_subprocess` | R1 |
| Preflight | отказ до побочных эффектов | `awf/api/pipeline.py`, `awf/api/run.py` | R2 |
| Managed | признак и предупреждение | `awf/opencode_version.py`, `awf/cmd_doctor.py` | R2 |
| Kill | группа процессов | `awf/api/pipeline.py::kill_pipeline`, `awf/_proc.py` | R3 |
| Метрики | два ридера, `n/a` | `awf/metrics.py`, `awf/opencode_db.py` | R4 |
| Модели | recent models, `opencode models` | `awf/api/model_check.py`, `agent_workflow_ui/.../opencode_config.py` | R5 |
| BaseURL | цепочка v1 → v2 | `awf/_net.py::model_endpoint_url` | R6 |
| Маркеры сети | дополнить закрытый список | `awf/_net.py` | R7 |
| Откат | без миграции | конфиг/бинарь, кода не требует | R8 |

## Контракты

### K1. Версия

`probe_version(binary="opencode", timeout=5.0) -> Probe`. `Probe.status` — `ok` / `missing` / `unknown`; `version` — `(major, minor, patch)` при `ok`. Вызов `[binary, "--version"]`, таймаут 5 с, разбор первого `X.Y.Z` регуляркой `\b(\d+)\.(\d+)\.(\d+)\b` из первой непустой строки. Ненулевой код, пустой или мусорный вывод, таймаут, `OSError` — `unknown`; `FileNotFoundError` — `missing`. Исключений нет.

Счётчик: кэш в памяти процесса по ключу `(binary, resolved_path)`; не более одного вызова на процесс (AC6). Сброс — `reset_version_cache()` для тестов. Background-ребёнок — отдельный процесс, свой кэш. Модуль не импортирует `awf.api`, чтобы не создать цикл.

Двойники: шим `tests/infra/opencode_shim/opencode` и стаб `tests/stubs/opencode` на `--version` печатают `AWF_SHIM_VERSION`, иначе `AWF_TEST_OPENCODE_VERSION`, иначе `1.19.4`; код 0; сигналов не пишут и `invocation.log` не трогают (AC7).

### K2. Режим и argv

`resolve_standalone(config, probe, managed) -> Plan`: поля `mode` (`auto`/`on`/`off`), `add_flag`, `warning`, `refusal`.

Домен ключа после `yaml.safe_load` (D5): ключа нет — `auto`; `True` — `on`, `False` — `off`; строки без учёта регистра `auto`, `on`, `off`, `true`, `false` — соответствующий режим; прочее (`maybe`, числа, списки) — `refusal` с именем `opencode.standalone` и значением (AC5, AC37).

| Версия | Ключ | Флаг | Исход |
|---|---|---|---|
| 1.x | `auto`/`off` | нет | старт (AC1, AC11) |
| 1.x | `on` | нет | старт + предупреждение (AC3, D1) |
| 2.x | `auto`/`on` | `--standalone` | старт (AC2, AC3) |
| 2.x | `off` | нет | отказ K3 (AC9, AC35) |
| неизвестна/нет бинаря | любой, включая `on` | нет | отказ K3 (AC10, AC39, AC40, RR35) |
| любая | managed=`true` | нет | предупреждение вместо отказа (AC12) |

Флаг вставляется сразу после `run`: `["opencode", "run", "--standalone", "--auto", …]`; остальные аргументы и порядок прежние. Оба спавна строят argv одним helper'ом; `off` и 1.x дают побайтно прежний argv (INV1).

### K3. Preflight

`preflight(project_dir) -> Refusal | None` — композиция K1, K2 и D4. Отказ несёт `message`, называющий ключ `opencode.standalone`, версию и режим; для `missing` — «opencode не найден» (AC40).

Точки вызова — до первого побочного эффекта каждого входа:

| Вход | Место | Форма отказа |
|---|---|---|
| `start` foreground/background | `_start_pipeline`, до `_reconcile`, spec-гейта и lease | `StartResult(run_mode="noop")` |
| `continue` | `_continue_pipeline`, до ACK и lease | `StartResult` |
| `run-next` | `_run_next`, до резервирования `current`, baseline и `.ready` | `RunNextResult(action="refused")` |
| `run-start` | `run_start`, до записи `state/run.yaml` | `RunStartResult` |
| `run-service-start` | `run_service_start`, до `_write_service_pipeline` и dispatch | `ServiceRunResult` |

`supervise` и `retry_stage` идут через `continue_pipeline` — отдельной точки не получают (RR31). Отказ — не исключение: состояние-условие (`2.x` + `off`, неизвестная версия) — код 0; ошибка конфига и нет бинаря — код 1. Предупреждение managed/1.x `on` печатается в вывод и stage-log.

### K4. Managed

`is_managed(config) -> bool`: `AWF_OPENCODE_MANAGED` в `1/true/yes` — `true`; иначе `opencode.managed` при `True` — `true`; иначе `false`. Иных источников нет (D4). При `true` K2 не добавляет флаг и K3 не отказывает; `awf doctor` печатает строку `managed: on — worker env может игнорироваться, standalone выключен`. Риск managed — в readiness: изоляция env в managed не гарантирована, это ограничение принимает владелец.

### K5. Метрики: проба схемы и два ридера

`detect_schema(con) -> "v1" | "v2" | "both" | "none"` по `SELECT name FROM sqlite_master WHERE type='table'`:

- v1 — есть `session` и `part`; `message` отсутствует — предупреждение, ридер прежний (AC17, AC20);
- v2 — есть `session_v2` и `session_message`;
- both — оба ридера, объединение по `id` сессии, при совпадении побеждает v2-запись (AC36, AC41);
- none — `n/a` с причиной, не нули (AC21).

Признак v2 — именно `session_v2`, не `session_message`: в текущей v1-базе `session_message` есть и пуста (CTX3, D7). `part` в v2 не читается.

v1-ридер не меняется: итоги воркера из `session.*`, супервизор из `message.data`, компрессии из `part.data`.

v2-ридер: сессии из `session_v2` (`id`, `title`, `time_created`, `time_updated`); проект — вхождение нормализованного пути в `session_message.data` (`LIKE` с escape, как у v1-`part`); `title` из `session_v2`; токены и стоимость — сумма по строкам `session_message` этой сессии из `data`:

```
{"tokens": {"input": N, "output": N, "cache": {"read": N, "write": N}}, "cost": X}
```

Сумма повторяет семантику v1 `message` (строка = сообщение), поэтому вклад сообщений не теряется. `input` берётся как есть, кэш в него не прибавляется; `cost` суммируется отдельно. Окна юнита — по `time_created` сессии. Компрессии в v2 не измеряются: `part` нет, поле остаётся 0 с однократным предупреждением `v2: компрессии не измеряются`. Строки есть, проект не совпал — ноль сессий и `n/a` по суммам (AC22).

### K6. Модели: общий ридер и CLI

`awf/opencode_db.py` экспортирует `detect_schema`, `recent_models(db_path, limit)`, `available_models()`. Плагин импортирует их из `awf` (зависимость уже объявлена), своей копии SQL не держит — тест расхождения потребителей (AC24).

Recent models: v1 — `SELECT DISTINCT model FROM session` (текущее поведение). v2 — источник задаёт `V2_MODEL_FIELD` (D9): `("session_v2", "model")` либо `("session_message", "$.model")`. Разбор значения прежний: JSON `{"providerID","id"}` → `provider/id`; строка без JSON — как есть. Пока IMP1 не подтвердил поле, константа `None`: пустой набор и предупреждение «моделей не найдено (v2: поле не подтверждено)», без догадок (RR25, A7).

`available_models()`: `["opencode", "models"]`; `--standalone` добавляется только при major ≥ 2 и `MODELS_STANDALONE_SUPPORTED=True`; на 1.x — никогда; при `False` — без флага, факт в `awf doctor` (AC27). `--help` в рантайме не вызывается. Таймаут 10 с, пустой вывод, ненулевой код, `OSError` — пустой набор и предупреждение, не исключение (AC25, AC26).

### K7. BaseURL

`model_endpoint_url` сначала читает `provider.<p>.options.baseURL` (v1), затем `providers.<p>.settings.baseURL` (v2). Обе формы — побеждает v1 (AC29); ни одной — `None`, preflight выключен, сообщение прежнее, конфиг не переписывается (AC30). Запрос остаётся `GET {base}/models`.

### K8. Маркеры сети

Список `NETWORK_MARKERS` — закрытый; IMP1 снимает строки v2 на мёртвом эндпоинте, IMP5 добавляет их как константы с комментарием-источником. Старые маркеры и `_NETWORK_RE` не меняются; порядок классификации `network/auth/transient` прежний (AC31, AC32, INV6). Посторонняя строка класса network не получает.

### K9. Kill группы

Код не меняется, если IMP1 подтверждает: при `--standalone` дочерний `serve` остаётся в группе воркера. `kill_process_tree` уже шлёт TERM группе, ждёт grace, затем KILL группе и добивает остаток; `worker_pid` пишется при спавне. Guard `proc.pid > 1` и tripwire не трогаются (INV4). Если проба покажет, что `serve` уходит в новую сессию, расширять kill на чужие pid запрещено — такой случай закрывается отказом K3 (`off` на v2) и фиксируется в IMP-отчёте. v1-дерево и порядок kill прежние (AC15).

### K10. Совместимость и откат

Схемы `opencode.db` не мигрируются, `opencode.json` не переписывается, `.agentic/state|inbox|outbox` и сигналы не меняются (INV2, INV7). Возврат бинаря 1.19.4 при `auto` даёт прежний argv и прежние метрики; v2-сессии в смешанной базе видны (AC33). Записи в окне «режим и отказ» отсутствуют: preflight только читает конфиг и версию (AC34).

## Данные и состояния

| Объект | Владелец записи | Жизненный цикл |
|---|---|---|
| `Probe` версии | `opencode_version` | кэш процесса, сбрасывается тестом |
| Эффективный режим | вычисляется на входе и спавне | не хранится; источник — конфиг + версия |
| `opencode.standalone`, `opencode.managed` | пользователь, `config.yaml` | читаются, не пишутся |
| `awf doctor` строки | `cmd_doctor` | вычисляются при вызове |
| opencode.db | opencode | только чтение, схема не меняется |

## Отказы и совместимость

| Отказ | Поведение |
|---|---|
| 2.x + `off` | типизированный отказ до lease/baseline/TODO, текст с ключом |
| версия неизвестна | то же, включая `on` (RR35) |
| нет бинаря | отказ «opencode не найден», без подсказки про `standalone` |
| `standalone: maybe` | отказ с именем ключа и значением до спавна |
| managed при opt-in | предупреждение, старт без флага |
| БД без схемы/строк | `n/a`, код 0, без исключений |
| `opencode models` молчит/таймаут | пустой набор и предупреждение |
| нет baseURL ни в одной форме | preflight выключен, сообщение прежнее |

Совместимость: на 1.x argv и метрики прежние; ключи необязательны (нет ключа — `auto`, нет `managed` — `false`); откат — смена бинаря, миграции нет.

## Решения

D1 — на 1.x `on` не передаёт флаг, а предупреждает: бинарь 1.19.4 отвергает `--standalone` (rc 1); «включить, где поддержано» (req D1).

D2 — v2: `title` из `session_v2`, токены и `cost` из `session_message.data` суммой; `part` не читается (req D2, RR34).

D3 — версия читается в каждом процессе, кэш на процесс; background-ребёнок считает сам (req D3).

D4 — managed — явный opt-in, не автоопределение: реестр сервиса не различает managed и обычный общий сервер (Q1).

D5 — домен `standalone` — после `yaml.safe_load`; булевы и строки разбираются, прочее — отказ (req D5).

D6 — неизвестная версия fail-closed, нет бинаря — свой текст; `on` при неизвестной версии не добавляет флаг (req D6, RR35).

D7 — схема БД по `session_v2`, а не по `session_message`: в v1-базе вторая таблица есть и пуста (CTX3).

D8 — один ридер БД в `awf/opencode_db.py` для метрик, CLI и плагина: две копии SQL разошлись бы (AC24).

D9 — A6/A7 фиксируются пробами IMP1 в константах; рантайм не угадывает поле и флаг (RR25, A6, A7).

D10 — kill остаётся групповым; расширение на чужие pid запрещено (INV4, K9).

## План

IMP1 — пробы v2, до кода. Установить 2.1.4 в `/tmp/opencode-v2`, HOME/XDG изолированы; снять: формат `--version`; принятие `--standalone`; схему `session_v2`/`session_message` и форму `data`; поле модели (A7); поддержку `models --standalone` (A6); строки сетевой ошибки на мёртвом эндпоинте; kill группы с живым `serve`. Результат: отчёт и фикстуры в `/tmp/opencode/**`, заполненные константы. Критерий: у каждой неизвестности есть команда и вывод; проба не трогает реальные `~/.config` и `~/.local/share`. Зависимостей нет; без неё IMP4–IMP6 не начинать.

IMP2 — версия и режим (K1, K2) в `awf/opencode_version.py`; вставка флага в оба спавна; `--version` в шиме и стабе. Проверка: AC1–AC7, AC37, AC38; целевые тесты argv.

IMP3 — preflight (K3, K4) в пяти входах и строка `awf doctor`; зависит от IMP2. Проверка: AC8–AC13, AC39, AC40; тест отсутствия следов (файлы, `git status`, lease, номер TODO).

IMP4 — метрики (K5, D2, D7): проба схемы, v2-ридер, смешанная база, `n/a`; зависит от IMP1 (фикстура). Проверка: AC17–AC22, AC36, AC41.

IMP5 — модели, baseURL, маркеры (K6–K8): общий ридер в `awf/opencode_db.py`, плагин импортирует; флаг и поле по константам IMP1; зависит от IMP1. Проверка: AC23–AC32.

IMP6 — kill и откат (K9, K10): e2e со стабом `serve`, хеши конфига и базы, сьют на 1.19.4; зависит от IMP1 и IMP3. Проверка: AC14–AC16, AC33–AC35; `scripts/suite-canonical.sh` зелёный.

## Покрытие

| R# / INV# | Контракты | Решения | IMP |
|---|---|---|---|
| R1 / INV1 | K1, K2 | D1, D3, D5, D6 | IMP1–IMP3 |
| R2 | K3, K4 | D4, D6 | IMP3 |
| R3 / INV4 | K9 | D10 | IMP1, IMP6 |
| R4 / INV3 | K5 | D2, D7 | IMP4 |
| R5 | K6 | D8, D9 | IMP1, IMP5 |
| R6 | K7 | — | IMP5 |
| R7 / INV6 | K8 | — | IMP1, IMP5 |
| R8 / INV2, INV7 | K10 | — | IMP6 |
