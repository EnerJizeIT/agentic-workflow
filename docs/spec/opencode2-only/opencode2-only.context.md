# Контекст: awf 3.0 — только opencode 2.x

Статус: REVIEW
Ревизия: ctx-1
Входы: TODO-0315; docs/spec/opencode2/opencode2.context.md@ctx-2; docs/design/opencode-v2-migration.md; код и тесты awf на базе ниже
База кода: commit 0384ba35ffc97ce370e6dc94bee098277d775860; снапшот `.agentic/context/BASELINE-TODO-0315.sha` = 9e44995 (TODO-0313); рабочее дерево чистое

## 1. Итог для аналитика

Решение владельца: awf 3.0 работает только на opencode ≥ 2, обратной
совместимости нет. Сейчас в коде живёт переходный адаптер 2.3.0: он
выбирает режим по версии (`auto`/`on`/`off`), читает две схемы БД и две
формы baseURL. Вырезать нужно ровно это: выбор режима, v1-ридеры, v1-ветки
конфига и v1-тесты.

Три измеренных факта задают рамку:

- **На машине уже opencode 2.2.3.** `opencode --version` → `opencode v2.2.3`.
  Прошлый контекст (ctx-2) писал 1.19.4 — он устарел. Пробы v2 теперь
  доступны локально, ссылаться на «по документации» больше не нужно.
- **v2 всё ещё принимает V1-ключи конфига и нормализует их в памяти.**
  Файл владельца (`~/.config/opencode/opencode.json`) содержит `permission`
  (мапа), `mcp.<имя>`, `agent`, `model` строкой; `opencode debug config`
  отдаёт `permissions` (список), `mcp.servers.<имя>`, `agents`,
  `model: {providerID, model}`. Файл при этом не переписывается.
- **Перевод конфига в V2 — не переименование ключей, а смена формы.**
  `permissions` — упорядоченный список правил `{action, resource, effect}`,
  а не мапа «ключ → allow/deny»; в v2 действует «последнее совпадение
  побеждает», в awf сейчас — «deny сильнее allow».

## 2. Карта затронутых путей

| Слой | Вход | Компоненты | Что удаляется | Источник |
|---|---|---|---|---|
| Запуск | стадия, супервизор | `opencode_version.resolve_standalone`, `apply_standalone` | ветки `auto`/`off`, предупреждение «`on` на 1.x», домен `opencode.standalone` | `awf/opencode_version.py:150-232`; `awf/agent_stage.py:250-258`; `awf/supervisor.py:1199-1208` |
| Preflight | `awf start`, `awf continue`, `awf run` | `pipeline._opencode_gate`, `run._opencode_gate`, `opencode_version.gate/launch_plan` | правило «2.x + off → отказ»; остаётся «major < 2 → отказ» + «версия неизвестна → отказ» | `awf/api/pipeline.py:1922-1945`; `awf/api/run.py:401-413`; `awf/opencode_version.py:236-292` |
| Метрики | `awf metrics` | `metrics.collect_workers`, `collect_supervisor`, `candidate_session_ids`, `matched_session_ids`, `schema`-ветки | чтение `session`/`part`/`message`, объединение `both`, «v2 в форме v1» | `awf/metrics.py:392-455, 496-560, 579-700, 758-850, 1862-1890` |
| Схема БД | CLI и плагин | `opencode_db.detect_schema`, `_rows_v1`, `recent_models` | `SCHEMA_V1`, `SCHEMA_BOTH`, `_rows_v1` | `awf/opencode_db.py:36-39, 57-68, 96-106, 148-171` |
| Захват сессии | reuse (IMP5) | `session_store._SESSION_TABLES`, `_session_table` | таблица `session` и её приоритет | `awf/session_store.py:48, 246-260` |
| Env воркера | спавн | `_env.awf_subprocess_env`, `merge_tool_profile` | V1-ключи `permission`, `agent`, `mcp.<n>.enabled` | `awf/_env.py:265-294, 371-446` |
| Файл агентов | `awf init`, setup | `opencode_agents.propose`, `_ensure_worker_permission` | чтение/запись `agent.*`, `permission.external_directory` | `awf/opencode_agents.py:63-97, 108-160` |
| baseURL | preflight сети | `_net.model_endpoint_url`, `_base_url` | «v1 первым, `options.baseURL`» | `awf/_net.py:172-212` |
| Маркеры сети | классификация смерти | `_net.NETWORK_MARKERS` | v1-строки (какие именно — вопрос Q3) | `awf/_net.py:36-48` |
| Doctor | `awf doctor` | `_check_opencode` | строка режима, предупреждение 1.x, `MODELS_STANDALONE_SUPPORTED` | `awf/doctor.py:293-325`; `awf/opencode_db.py:29` |
| Ключи конфига | doctor dead keys | `config_keys.READ_KEYS` | `opencode.standalone` | `awf/config_keys.py:88-89` |
| Чтение конфига | model_check | `_load_opencode_config` | корни `provider`, `agent` | `awf/api/model_check.py:53-58` |

## 3. Переиспользуемые компоненты и контракты

- **Один владелец схемы.** `awf/opencode_db.py` уже отдаёт решение схемы и SQL
  recent models CLI и плагину (`agent_workflow_ui/.../opencode_config.py:41-61`).
  После вырезания v1 он остаётся единственной точкой: `session_v2` +
  `session_message`, `n/a` для всего остального.
- **Один владелец argv.** `apply_standalone` (`awf/opencode_version.py:222-227`)
  правят оба спавна; при «всегда» он становится простой вставкой флага, но
  остаётся одним местом (тесты перехватывают argv, `tests/unit/test_agent_stage_prompt.py`).
- **Отказ старта — `StartResult` с текстом, не исключение** (`awf/api/pipeline.py:1922-1945`).
  Доктрина 03 закрепляет «noop с текстом»; новый отказ «awf 3 требует opencode 2.x»
  встаёт в ту же цепочку до lease и первого файла.
- **Проба версии кэшируется на процесс** (`opencode_version.probe_version`,
  сиды `prime_probe` / `reset_version_cache`) — тестам не нужен живой бинарь.
- **Гейт пакета.** `awf/spec_package.py`: шесть документов, шапки
  `Статус/Ревизия/Входы/База кода`, бюджеты 2200 слов на документ и 11000 на пакет.
  Context входит в бюджет — писать экономно.

## 4. Поведение, которое нельзя сломать

- Отказ до побочных эффектов: TODO, baseline, `.ready`, `current`, dispatch, lease, PID.
- Guard `proc.pid > 1` и tripwire `_forbid_session_kill` — неприкосновенны.
- Классы `network/auth/transient` меняются только списком строк, не смыслом.
- Managed-режим (`opencode.managed`, env `AWF_OPENCODE_MANAGED`) остаётся opt-in.
- Модели ролей (`models.<role>.model`) и `argv_agent_flag` (A13) не трогаем.
- Пустой/чужой БД — `n/a` с причиной, не нули.
- Пользовательский `opencode.json` не переписывается кодом аудита и не мигрируется автоматически.

## 5. Baseline

| Проверка | Команда | Результат | Наблюдение |
|---|---|---|---|
| Целевые тесты затронутых путей | `/home/pklochkov/.local/share/awf-venv/bin/python -m pytest tests/unit/test_opencode_version.py tests/unit/test_preflight_standalone.py tests/unit/test_opencode_db.py tests/unit/test_metrics.py tests/unit/test_metrics_v2.py tests/unit/test_doctor.py -q` | PASS | 150 passed за 20.9 с |
| Контракты | `bash scripts/check-contracts.sh` с venv в PATH | PASS | 8 contracts, 8 gates |
| Контракты без venv | `bash scripts/check-contracts.sh` (системный python3) | FAIL | `pytest: unrecognized arguments: --timeout=120` — нет плагина, ограничение среды, не дефект кода |
| Храповик | `bash scripts/ratchet.sh` | PASS | counts within baseline |
| Старый пакет спеков | `python -m awf spec check docs/spec/opencode2` | PASS | READY, rc 0 |
| Версия opencode | `opencode --version` | 2.2.3 | строка `opencode v2.2.3` |
| Нормализация конфига | `opencode debug config` | OK | V1-файл → V2-форма в памяти |
| Схема БД | python `sqlite3` read-only, `~/.local/share/opencode/opencode.db` | OK | обе генерации: `session` 4976, `part` 504314, `message` 111626, `session_v2` 5028, `session_message` 121388, `session_part` 0 |
| Полный сьют | `scripts/suite-canonical.sh` | NOT_RUN | дольше 300 с, дубль канонического прогона |
| Гейты инструкций | `bash scripts/instruction-gates.sh` | NOT_RUN | не запускался в этом круге |

## 6. Находки в scope

| ID | Важность | Файл / символ | Факт и доказательство | Влияние на фичу | Следующий шаг | Уверенность |
|---|---|---|---|---|---|---|
| CTX1 | MAJOR | `tests/infra/opencode_version_stub/opencode:8`, `tests/infra/opencode_shim/opencode:64`, `tests/stubs/opencode:31` | Все три тестовых двойника по умолчанию отвечают `1.19.4` (`AWF_TEST_OPENCODE_VERSION`). После «v1 → отказ» каждый неподготовленный тест упрётся в отказ preflight. | Это не хвост, а предпосылка: без смены умолчания на 2.x красным станет весь сьют, а не v1-кейсы. | Архитектор/QA: смена умолчания в том же IMP-юните, что и отказ. | Высокая |
| CTX2 | MAJOR | `awf/_env.py:265-294, 371-446`; нормализованный вывод `opencode debug config` | V2 `permissions` — упорядоченный список `{action, resource, effect}`, «последнее совпадение побеждает»; в awf `merge_tool_profile` кодирует «deny сильнее allow» на мапе. Плюс имена инструментов: в v2 `bash`→`shell`, `write`→`edit`. | Прямой перенос мапы в список меняет приоритеты: профиль стадии и locked_deny перестанут быть сильнее хоста. | Архитектор: правило порядка при сборке списка + маппинг имён; QA: тест «locked deny последним». | Высокая |
| CTX3 | MAJOR | `awf/_env.py:420-467` | `OPENCODE_CONFIG_CONTENT` в бинаре 2.2.3 присутствует (строка в бинаре). Но `opencode debug config` показывает только файлы и не отражает env-источник: с `OPENCODE_CONFIG` и с `OPENCODE_CONFIG_CONTENT` вывод не меняется. Применяет ли env сам `run` 2.2.3 — не проверено. | Если env игнорируется, тихо отключаются permission-изоляция, скрытие контрольных MCP и readonly-роли. Это MAJOR независимо от перевода ключей. | Архитектор: назвать пробу (реальный `opencode run --standalone` с deny) и что делать при отрицательном ответе. | Средняя (гипотеза с путём проверки) |
| CTX4 | MAJOR | `awf/session_store.py:48, 246-260` | `_SESSION_TABLES = ("session", "session_v2")` — первой идёт v1-таблица. На смешанной БД (обе генерации заполнены) захват сессии берёт v1-строку. | Reuse сессии после удаления v1 обязан читать только `session_v2`, иначе тест «v1 удалён» зелёный, а поведение осталось двойным. | Архитектор: явно включить `session_store` в карту правок; QA: тест на смешанной БД. | Высокая |
| CTX5 | MAJOR | `awf/metrics.py` (2064 строки), `tests/unit/test_metrics_v2.py:200,215` | Двойное чтение пронизывает модуль: `schema`-параметр, ветки `V1`/`BOTH` в шести местах, «v2 в форме v1-кортежа». В тестах есть кейсы «both» и «v2 побеждает по id». | Удаление v1 — не удаление файла, а распутывание веток; тесты `both` перестают иметь смысл и должны быть заменены на `n/a` для v1-схемы. | Архитектор: список функций на упрощение; QA: тест «v1-схема → n/a с причиной». | Высокая |
| CTX6 | MINOR | `awf/opencode_db.py:29`; `awf/doctor.py:313-321` | `MODELS_STANDALONE_SUPPORTED = True` — замер IMP1 под 2.2.3. После «флаг всегда» константа и её строка в doctor мертвы. | Мёртвый флаг останется в отчёте doctor и в контракте модуля. | Аналитик: пункт «удалить вместе с ветвлением». | Высокая |
| CTX7 | MINOR | `awf/_net.py:36-48` | Список маркеров смешивает формулировки без атрибуции: комментарий помечает только пару «Unable to connect»/«ECONNRESET» как v2-ную, остальные пять — без метки. | «Удалить v1-маркеры» без поимённого решения может выкинуть рабочие строки (`fetch failed`, `Cannot connect to API`). | Архитектор: решение по каждому маркеру + проба на мёртвом endpoint. | Средняя |
| CTX8 | MINOR | `awf/opencode_agents.py:63-160` | `propose` требует корень `agent`, `_ensure_worker_permission` правит `permission.external_directory` мапой. V2-форма — `agents`, `permissions` списком. | Если перевести только `_env.py`, `awf init` продолжит писать V1-файлы, и doctor будет предупреждать о файлах, созданных самим awf. | Аналитик: требование согласованности «пишем и читаем одну форму». | Высокая |
| CTX9 | MINOR | `awf/config_keys.py:88`; `.agentic/config.yaml` | Ключ `opencode.standalone` исчезает из кода → попадает в dead keys. В конфиге этого проекта его нет (проверено). | Пользователь с `standalone: off` получит WARN и всё равно `--standalone`: это и есть заявленный разрыв с 1.x. | Аналитик: записать как явное «что ломается». | Высокая |
| CTX10 | MINOR | `awf/doctor.py:293-325` | Строки doctor описывают режим и поддержку флага; строки «формат конфига» нет вообще. | Требование «V2 → ok, V1-ключи → WARN» — новая проверка, а не правка существующей. | Архитектор: где читать ключи (`model_check._load_opencode_root` уже отдаёт весь корень). | Высокая |
| CTX11 | MINOR | `README.md:47`; `CHANGELOG.md:12-18`; `docs/design.md:104-105`; `docs/design/opencode-v2-migration.md` | Доки описывают «any recent version», объединение двух схем и откат на 1.19.4. Карта миграции — план перехода с колонкой риска. | Приёмка требует «requires opencode 2.x» и карту «как устроено на 2.x» без колонки статуса. | Аналитик: перечень файлов и что именно переписать. | Высокая |
| CTX12 | SUGGESTION | `TODO-0315` п.7 (страж от возврата) | `--attach` и `--port` в `awf/` уже отсутствуют (0 вхождений). При этом `session`/`part` как подстроки встречаются законно: `session_v2`, `session_message`, `session_store`, комментарии. | Наивный grep-страж либо ничего не ловит, либо падает на легальном коде. | QA: страж по SQL-строкам/именам таблиц, а не по подстроке. | Высокая |
| CTX13 | SUGGESTION | `awf/` целиком | `grep -c legacy` = 290 попаданий, почти все — state-store и commit-пути (`infra/state_store/_migration.py`, `commit_delta.py`), не opencode 1.x. `normalize` — 99. | Пункт ТЗ «пройти grep по legacy/normalize и вычистить» нельзя выполнять буквально. | Аналитик: сузить формулировку до opencode-домена. | Высокая |

## 7. Несвязанные находки

- `bash scripts/check-contracts.sh` падает на системном `python3` (нет `pytest-timeout`); с venv в PATH — PASS. Это среда, не код.
- Полный сьют дольше 300 с; целевые прогоны укладываются в 20 с.
- `part` в БД v1 содержит 504 314 строк: это история, которая после вырезания v1 станет нечитаемой для метрик (владелец согласие дал).

## 8. Вопросы и непроверенные предположения

- Q1 (архитектор): какую входную форму `permissions` принимает 2.2.3 — только список правил или и мапу? От этого зависит, писать в `OPENCODE_CONFIG_CONTENT` список или оставить мапу и использовать нормализацию.
- Q2 (архитектор): применяет ли `opencode run` 2.2.3 переменную `OPENCODE_CONFIG_CONTENT` (CTX3). Нужна одна живая проба с deny и безвредным промптом; без неё permission-изоляция остаётся неподтверждённой.
- Q3 (архитектор): поимённое решение по `NETWORK_MARKERS` (CTX7) — какие строки принадлежат только 1.x.
- Q4 (архитектор): managed-режим в 3.0. Ключ остаётся, но при managed флаг не передаётся — тогда на 2.x kill снова оставляет сессию на сервере. Нужно решить: это осознанный opt-in с предупреждением или в 3.0 managed тоже требует `--standalone`.
- Q5 (QA): чем проверять отказ «awf 3 требует opencode 2.x» без живого 1.x бинаря — сид `prime_probe` с версией (1, 19, 4) достаточен, живой бинарь не нужен (допущение A1).
- Допущение A1: `opencode debug config` не является доказательством поведения `run`; все выводы о применении env помечены как гипотеза.
- Допущение A2: v1-база на машине останется как есть; миграции БД в этой работе нет.
