# Контекст: адаптация awf под opencode 2.x (два режима)

Scope: opencode2 (SPEC-ран: только документы; код и конфиги не меняем)
Ревизия: ctx-2
Входы: TODO-0311; `opencode2.md`@req-4; `opencode2.requirements-review.md`@reqrev-4; ТЗ владельца `/home/pklochkov/Desktop/awf-opencode2-adaptation-spec.md`; карта `docs/design/opencode-v2-migration.md`; код awf на базе ниже
База кода: commit `4a15afccea417216d35e547bdeb20ea7eb0e22b2` (не менялся с ctx-1); рабочее дерево: только неотслеживаемый каталог `docs/spec/opencode2/`
Статус: REVIEW

## 1. Итог для аналитика

Круг 2 закрывает три пункта ревью — RR32, RR33, RR34. Все три живут в тексте требований; в коде с ctx-1 ничего не изменилось. Менять нужно: норму про `--standalone` на 1.x (RR32), умолчание при отсутствии ключа (RR33), источник токенов в метриках (RR34).

Два измеренных ограничения задают рамку правки:

- **Доступно два слова.** `count_words('opencode2.md')` = 2198 при пороге 2200. Любая добавка требует сокращения в другом месте, иначе `spec check` упадёт на бюджете.
- **Бинарь 1.19.4 отвергает `--standalone`.** Живая проба: `opencode run --standalone -m zzz/zzz hi` печатает справку и выходит с кодом 1; в `opencode run --help` флага нет. Значит `on` на v1 — не «лишний флаг в argv», а мёртвый старт воркера.

Что уже есть в коде (карта ниже, детали прежние): оба спавна `opencode run`, сборка окружения, предупреждения о сети, чтение метрик, проверка моделей, убийство группы процессов.

Чего по-прежнему нет: определения версии, понятия «эффективный режим», второго ридера метрик, цепочки baseURL для v2-нативного конфига.

## 2. Что изменилось с ctx-1 (круг 2)

| Факт | Как проверено | Итог |
|---|---|---|
| База кода | `git log --oneline -3`, `git status --porcelain` | HEAD `4a15afc` (TODO-0309); грязных файлов нет, только untracked `docs/spec/opencode2/` |
| Версия opencode | `opencode --version` | 1.19.4, без изменений |
| Флаг у v1 | `opencode run --help \| grep -c standalone` | 0 вхождений |
| Поведение v1 с флагом | `timeout 15 opencode run --standalone -m zzz/zzz hi` | справка, rc=1 (подтверждает RR32 независимо от ревьюера) |
| v2 на машине | `ls -d /tmp/opencode-v2 /tmp/oc2` | обоих каталогов нет; артефакты пробы 24.10 удалены |
| Схема БД v1 | python `sqlite3` по `~/.local/share/opencode/opencode.db` | таблицы `session` (4751 строк), `part`, `message`, `session_message` (0 строк), `session_input`, `session_context_epoch`; `session_v2` нет |
| Шим на `--version` | `grep --version tests/infra/opencode_shim/opencode tests/stubs/opencode` | обработчика нет (CTX2 в силе) |
| Бюджет требований | `awf.spec_package.count_words` | 2198 / 2200 |
| Готовность пакета | `python -m awf spec check docs/spec/opencode2` | NOT READY: нет architecture, test-spec, readiness — ожидаемо для стадии 2 из 8 |

Важно про карту миграции: она утверждает, что 2.1.4 проверена лично (`/tmp/oc2/opencode --version`), но сами артефакты были временными и теперь удалены. Для круга 2 это значит: факты о 2.1.4 остаются «по документации», пока v2 не установят заново (CTX1, A1).

## 3. Карта затронутых путей

| Пункт ТЗ | Вход | Компоненты | Данные и эффекты | Источник |
|---|---|---|---|---|
| P0-1 `--standalone` | старт стадии | `agent_stage.run_agent_stage`, `supervisor.run_supervisor_via_subprocess` | argv процесса, env воркера | `awf/agent_stage.py:200-230`; `awf/supervisor.py:1159-1170` |
| P0-2 preflight старта | `awf start` / `awf continue` | `_start_pipeline`, `continue_pipeline`, `_background` | `StartResult` (noop с текстом) до lease/PID | `awf/api/pipeline.py:1874-1960`; `:2146` |
| P0-3 kill-группа | `awf kill`, таймаут, salvage | `kill_pipeline`, `_proc.kill_process_tree` | state `worker_pid`, `last-kill.json` | `awf/api/pipeline.py:2835-2925`; `awf/_proc.py:34-77` |
| P1-4 метрики | `awf metrics` | `metrics.collect_workers`, `collect_supervisor` | opencode.db read-only | `awf/metrics.py:384-706` |
| P1-5 model_check и плагин | doctor, формы | `model_check._load_cli_models`, `_load_recent_models`; плагин | `opencode models`, `session` | `awf/api/model_check.py:54,83`; плагин `opencode_config.py:43,123` |
| P2-6 baseURL | preflight воркера | `_net.model_endpoint_url` | GET `{base}/models` | `awf/_net.py:172-196` |
| P2-7 маркеры сети | классификация смерти | `_net.NETWORK_MARKERS`, `classify_failure` | retry/BLOCKED решение | `awf/_net.py:36-48,100-160` |

## 4. Переиспользуемые компоненты и контракты

- **Конфиг проекта читается свободно.** `awf/config.py:12-40` отдаёт весь YAML без схемы; ключ `opencode.standalone` не требует правок загрузчика.
- **Отказ на старте — `StartResult` с текстом, не исключение.** Так сделаны таймаут стадии, spec-гейт и бюджет ревью (`awf/api/pipeline.py:1884-1915`); доктрина 03 закрепляет «noop с текстом». Preflight версии встаёт в ту же цепочку до lease и первого файла.
- **argv — список.** Оба спавна собирают его и передают в `run_subprocess_until_signal`; тесты перехватывают (`tests/unit/test_agent_stage_prompt.py:16`).
- **Изоляция окружения.** `awf_subprocess_env` (`awf/_env.py:297-467`) — единственная точка сборки `OPENCODE_CONFIG_CONTENT`; вырезает `OPENCODE_SERVER_*` (`:455-467`).
- **Чтение opencode.json.** `model_check._load_opencode_config` (`awf/api/model_check.py:22-52`) отдаёт только корни `provider` и `agent`; для P2-6 нужен доступ к `providers.*.settings.baseURL`.
- **Проба схемы.** CLI `sqlite3` нет; проба только через Python `sqlite3` (как `metrics._open_db`, `:306-315`).
- **Тестовый шим.** `tests/infra/opencode_shim/opencode` не разбирает подкоманду, `--version` не знает — ответит пустым выводом (CTX2).
- **Счётчик бюджета.** `awf/spec_package.py:434-500`: `count_words`, порог 2200 на документ, 11000 на пакет из шести документов; context в бюджет не входит.
- **Гейт пакета.** `awf/spec_package.py:73-90, 723-900`: шесть суффиксов, шапки, PASS/READY, матрица, общая база, `Пакет-sha`, бюджеты.

## 5. Поведение, которое нельзя сломать

- argv на v1 без `--standalone`; `auto` следует версии, `off` — никогда. Ключ отсутствует — ведёт себя как `auto` (RR33).
- Отказ preflight — до любых записей, спавнов и lease.
- Пользовательский `~/.config/opencode/opencode.json` не правится; `awf init` (`awf/cmd_init.py:205-215`) в этой задаче не трогаем.
- Нет схемы или строк в БД — `n/a`, не нули (`awf/metrics.py:306-315`).
- Никаких новых MCP-тулов и глобальных зависимостей.
- Guard `proc.pid > 1` (`awf/_proc.py`) и tripwire `_forbid_session_kill` (`tests/conftest.py`) неприкосновенны.
- Классификация `network/auth/transient` (`awf/_net.py:100-160`) не меняется по смыслу, только дополняется маркерами v2.

## 6. Baseline

| Проверка | Команда | Каталог | Результат | Наблюдение |
|---|---|---|---|---|
| Контракты | `bash scripts/check-contracts.sh` | корень | PASS (ctx-1) | 8 контрактов, 8 гейтов |
| Храповик | `bash scripts/ratchet.sh` | корень | PASS (ctx-1) | счётчики в baseline |
| Бюджет инструкций | `bash scripts/instruction-gates.sh` | корень | PASS (ctx-1) | 948/950 слов, доктрина 250/250 |
| Целевые тесты | `pytest tests/unit/test_metrics.py tests/unit/test_model_check.py tests/unit/test_agent_stage_exit_code.py tests/unit/test_agent_stage_prompt.py -q` | корень | PASS (ctx-1) | 88 passed за 4.64 с |
| Полный сьют | `verification.test_cmd` | корень | NOT_RUN | база 0310 снята с `run_tests=False`; ~2385 тестов дольше 300 с |
| v1 opencode | `opencode --version` | — | 1.19.4 | системный бинарь, не обновляем |
| v2 opencode | `ls -d /tmp/opencode-v2 /tmp/oc2` | — | NOT_RUN | каталогов нет |
| Схема БД v1 | python `sqlite3`, режим read-only | — | OK | `session` 4751, `session_message` 0, `session_v2` нет |
| Схема БД v2 | — | — | NOT_RUN | без установленного v2 не увидеть |
| Бюджет требований | `count_words` | — | 2198/2200 | запас два слова |

`.agentic/context/BASELINE-TODO-0311.tests.log` — тестовый снимок на входе отсутствует (`run_tests=False`), это ограничение измерения, а не дефект.

## 7. Находки в scope

| ID | Важность | Файл / символ | Факт и доказательство | Влияние на фичу | Следующий шаг / владелец | Уверенность |
|---|---|---|---|---|---|---|
| CTX1 | MAJOR | среда; ТЗ §«Что известно» | v2 нет: `/tmp/opencode-v2` и `/tmp/oc2` отсутствуют. Карта миграции ссылается на пробу 24.10, но артефакты удалены. Все утверждения о 2.1.4 (флаги, БД, managed) — по документации. | Ни одну пробу v2 нельзя выполнить до переустановки; план обязан содержать установку и проверку фактов. | Архитектор: шаг установки и пробы в IMP-план; аналитик: пометка «по документации». | Высокая |
| CTX2 | MAJOR | `awf/agent_stage.py:200`; `tests/infra/opencode_shim/opencode` | Определения версии в awf нет; шим не разбирает подкоманду и на `--version` молчит (grep обработчика пуст). | `auto` может тихо уйти в «версия неизвестна»; без правки шима тесты версии невозможны. | Архитектор: место определения версии + контракт шима; QA: тесты argv на фейке. | Высокая |
| CTX3 | MAJOR | `awf/metrics.py:384-445`; БД | В v1-базе есть `session_message` (0 строк) и `part`; `session_v2` нет. Признак «есть `session_message` → v2» ошибочен. | Метрики могут выбрать неверный ридер и дать нули при зелёных тестах. | Архитектор: признак — `session_v2` (или отсутствие `part`); QA: тест на реальной v1-схеме. | Высокая |
| CTX4 | MAJOR | `awf/metrics.py:464-468, 547-553, 665-704` | Воркерские итоги — из колонок `session.*`, супервизор — из `message.data`, компрессии — из `part.data`. Где v2 хранит то же, не проверено. | Второй ридер — не замена имён таблиц; нужен маппинг полей и правило `n/a`. | Архитектор: маппинг; QA: синтетика v2 по подтверждённой схеме. | Средняя |
| CTX5 | MAJOR | `awf/api/pipeline.py:2835-2925`; `awf/_proc.py:34-77` | Убийство идёт по pid пайплайна и группе воркера. В v2 без `--standalone` сессия живёт на общем сервере вне группы. | P0-3 без живой пробы — гипотеза: орфан-сессия = правки в чужой коммит. | Архитектор: e2e со стабом `serve` и критерий «нет живых потомков»; QA: тест дерева процессов. | Средняя |
| CTX6 | MAJOR | `awf/_env.py:297-467`; `awf/api/pipeline.py:1874` | Изоляция держится на env. В v2 общий сервер находится через файл-реестр, managed-режим env игнорирует. Кода про managed нет. | Проверка «major ≥ 2 → standalone» не покрывает managed и запуск через сервер. | Архитектор: предикат эффективного режима и точка отказа; аналитик: AC про managed. | Средняя |
| CTX7 | MINOR | `awf/api/pipeline.py:1874-1915, 2146` | Отказы старта живут в `_start_pipeline`; `continue_pipeline` — отдельный вход; foreground и background идут через один `_start_pipeline`. | Preflight в одном пути обходится вторым (`awf continue`). | Архитектор: обе точки и порядок к lease; QA: тест отсутствия side effects. | Высокая |
| CTX8 | MINOR | `awf/_net.py:172-196`; `awf/api/model_check.py:22-52` | Загрузчик конфига отдаёт корни `provider`/`agent`; baseURL берётся из `provider.<p>.options.baseURL`; `providers.*.settings.baseURL` не читается. | P2-6 молча выключится после перевода конфига на v2-форму. | Архитектор: расширять загрузчик или читать отдельно; QA: тест обеих форм. | Высокая |
| CTX9 | MINOR | `awf/api/model_check.py:54-80`; плагин `opencode_config.py:123-170` | `opencode models` — таймаут 10 с, без `--standalone`; в v2 это серверный вызов, холодный старт может не уложиться. Поддержка флага у `models` не проверена. | Пустой набор даёт ложное «модель недоступна» в doctor и формах. | Архитектор: нужен ли флаг и деградация; QA: тест таймаута и пустого вывода. | Средняя |
| CTX10 | MINOR | `awf/spec_package.py:434-500, 723-900` | Гейт требует шесть документов, шапки, PASS/READY, матрицу, общую базу, `Пакет-sha`, бюджеты 2200/11000. Context в бюджет не входит. | Несогласованная ревизия или sha валит приёмку без обсуждения. | Аналитик/архитектор/QA: ревизии в шапках, sha пересчитать в readiness последним. | Высокая |
| CTX11 | MAJOR | `docs/spec/opencode2/opencode2.md` | Бюджет 2198/2200 слов, измерено `count_words`. Круг 2 обязан добавить норму про версию/пробу (RR32), умолчание (RR33) и источник токенов (RR34). | Без сокращения того же объёма `spec check` упадёт на MAJOR-бюджете — это блокирующая приёмка, а не стилистика. | Аналитик: сократить формулировки на величину добавки; ревьюер: сверять счётчик после правки. | Высокая |
| CTX12 | MAJOR | `awf/agent_stage.py:200`; бинарь 1.19.4 | Живая проба: `opencode run --standalone …` → справка, rc=1; в `run --help` флага нет. То есть argv-тест (AC3 в req-4) зелёный при мёртвом старте. | Норма «`on` всегда добавляет флаг» несовместима с «v1 работает как раньше»; нужен AC, проверяющий запуск, а не только argv. | Аналитик: переписать R1/AC3/INV1/D1 по решению супервизора (флаг только 2.x, режим по версии/пробе); QA: тест «v1 + on стартует». | Высокая |

## 8. Несвязанные находки

- CLI `sqlite3` не установлен — пробы БД только через Python.
- Бюджет инструкций на пределе (948/950, доктрина 250/250): текст в `AGENTS.md` или `docs/contracts/*` потребует удаления.
- Полный сьют дольше 300 с; целевые прогоны укладываются в секунды.

## 9. Вопросы и непроверенные предположения

- Q1 (архитектор, до readiness): как отличать managed-режим сервера, чтобы отказ R2 не срабатывал ложно и AC12 было чем проверить.
- Q2 (архитектор): где v2 хранит итоговые токены и стоимость — колонки `session_v2` или агрегация по `session_message.data` (связано с RR34).
- Q3 (архитектор): поддерживает ли `opencode models` флаг `--standalone` в 2.1.4 — решает проба A7 после переустановки v2.
- Q4 (QA): хватит ли стаба `serve` в группе процессов, чтобы проверить «после kill нет живых потомков» без настоящего v2.
- Допущение A1: установка v2 в `/tmp/opencode-v2` допустима на этапе реализации, но не в этом spec-ране (границы TODO — только `docs/spec/opencode2/`).
- Допущение A2: `opencode --version` в v2 сохраняет формат `X.Y.Z`; на бинаре сейчас не проверить.
