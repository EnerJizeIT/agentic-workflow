# Тестовая спецификация: awf 3.0 — только opencode 2.x

Статус: REVIEW
Ревизия: ts-3
Входы: opencode2-only.md@req-4; opencode2-only.requirements-review.md@reqrev-4; opencode2-only.architecture.md@arch-3; opencode2-only.context.md@ctx-1
База кода: 0384ba35ffc97ce370e6dc94bee098277d775860

## Scope и статус

Проверки **спроектированы, не пройдены**. Scope — R1–R12, AC1–AC38, INV1–INV9 на базе `0384ba3`. Ожидание выведено из R#/AC#; K# даёт только точку входа. Уровни: unit для правил, integration для БД и argv, e2e для сигналов, ручная — файл владельца. TDD применим к T1–T25 и T27–T30; T14 и T26 — измерения.

## Карта покрытия

| Требование | Критерии/инварианты | Сценарий, контракт | T# | IMP |
|---|---|---|---|---|
| R1 | AC1–AC5; INV2 | C1; K1 | T1–T4 | IMP1 |
| R2 | AC6–AC8; INV3 | C2; K2 | T5–T7 | IMP1 |
| R3 | AC9–AC12; INV8 | C3; K3, K4 | T8–T11 | IMP2 |
| R4 | AC14, AC15 | C4; K7 | T12 | IMP3 |
| R5 | AC16–AC20; INV7 | C4; K5, K6 | T13–T16 | IMP3 |
| R6 | AC21–AC23 | C5; K8 | T17, T18 | IMP3 |
| R7 | AC24, AC25; INV5 | C6; K9 | T21, T22 | IMP4 |
| R8 | AC26, AC27 | C7; K9 | T23, T24 | IMP4 |
| R9 | AC28–AC30 | C7; — | T25 | IMP4 |
| R10 | AC13, AC31–AC33 | C7; K10 | T19, T20 | IMP4 |
| R11 | AC34–AC36 | C8; K11 | T26 | IMP5 |
| R12 | AC37, AC38 | C7; K12 | T27 | IMP6 |
| INV1, INV4 | — | C1, C2; K1, K2 | T28, T29 | все |
| INV6, INV9 | — | C7; — | T30 | все |

Пробелов нет; AC13 — страж T19.

## Проверки R1 — версия

### T1 — каждый вход отказывает на 1.19.4
Покрывает: R1, AC1; INV2; C1 шаги 1–2; K1.
Уровень: integration, фейк `opencode` 1.19.4 первым в `PATH`; входы `start`, `continue`, `run-next`, `run-service-start`, foreground, background, MCP-`start`.
Ожидания: каждый — результат, не исключение; текст содержит «требует opencode 2.x» и `1.19.4`; rc 1; спавна нет.
Ловит: вход, забывший gate, и старт, дошедший до спавна.

### T2 — следов отказа нет
Покрывает: R1, AC3; INV2; C1 шаг 2; K1.
Уровень: integration на `tmp_path`, снимок дерева до и после; `.agentic/{inbox,outbox,state,context}` с файлами, один TODO.
Ожидания: `git status --porcelain` пуст; нет `DONE-*.ready`, `current`, `BASELINE-*.sha`, `awf-launch.lease`; TODO-ID цел; PID нет.
Ловит: отказ после взятия lease или списания TODO.

### T3 — неизвестная версия и отсутствие бинаря
Покрывает: R1, AC2; C1 шаг 1; K1.
Уровень: unit `probe_version` + `gate`; фейк с пустым выводом, мусором, rc 1, таймаутом; пустой `PATH`.
Ожидания: четыре первых — `unknown`, текст «версия не определена», «запуск запрещён», rc 0, без исключения; таймаут 5 с; пустой `PATH` — `missing`, «opencode не найден», rc 1.
Ловит: исключение на мусоре, зависание, проход без бинаря.

### T4 — managed и один `--version` на процесс
Покрывает: R1, AC4, AC5; INV3; C1 шаг 1; K1, K2, D8.
Уровень: integration `start` с фейком 1.19.4 и `opencode.managed: true`; счётчик вызовов.
Ожидания: managed на 1.19.4 отказывает текстом R1; на 2.2.3 старт идёт; `--version` вызван один раз на процесс.
Ловит: managed как обход версии, повторную пробу.

## Проверки R2 — флаг

### T5 — `--standalone` во всех argv
Покрывает: R2, AC6, AC7; INV4; C2 шаги 1–2; K2.
Уровень: unit `resolve_standalone`/`apply_standalone` + перехват argv воркера, супервизора, `available_models`.
Данные: ключа нет, `auto`, `on`, `off`, `maybe`, `true`; фейк 2.2.3.
Ожидания: флаг сразу после подкоманды в обоих спавнах и `opencode models`; `maybe` не отказ; прочие аргументы и порядок прежние.
Ловит: ветку по ключу, потерю флага, сдвиг аргументов.

### T6 — managed без флага, включая `opencode models`
Покрывает: R2, AC8; INV3; C2 шаг 3; K2; FR3.
Уровень: integration, `opencode.managed: true`, фейк 2.2.3; перехват argv и `available_models`.
Ожидания: `--standalone` нет ни в одном из трёх argv; `models` отбирает флаг тем же `resolve_standalone`, отдельной ветки нет; печатается прежний `MANAGED_WARNING` дословно; остальные аргументы прежние.
Ловит: managed с флагом и `models` по отдельной ветке.

### T7 — doctor про неизвестный ключ
Покрывает: R2, AC8; C2; K8.
Уровень: unit `run_doctor` на `tmp_path`, `.agentic/config.yaml` с `opencode.standalone: maybe`.
Ожидания: WARN называет `opencode.standalone`; файл и хеш не меняются; запуск не блокируется (старт — T5).
Ловит: тихое принятие мёртвого ключа, правку конфига диагностикой.

## Проверки R3 — данные

### T8 — V2-база даёт прежние числа
Покрывает: R3, AC9; C3 шаг 1; K3.
Уровень: integration на `opencode.db` (`session_v2`+`session_message`); две сессии A, одна B, известные суммы.
Ожидания: `collect_workers` — те же строки, суммы и колонки, что на `0384ba3`; `sessions_na` пуст; rc 0.
Ловит: потерю или удвоение токенов без «both».

### T9 — V1, пустая база, база без таблиц
Покрывает: R3, AC10; INV8; C3 шаг 2; K3, K4.
Уровень: integration `metrics`+`session_store`; (а) `session`+`part`; (б) без таблиц; (в) нет файла.
Ожидания: `sessions_na` — «схема v1 не поддерживается» (а), «таблиц v2 нет» (б, в); суммы `n/a`, не нули; rc 0; `_session_table` → `None`, `SESSION-*` не создан.
Ловит: нули вместо `n/a`, падение на чужой схеме, запись id в v1.

### T10 — смешанная база
Покрывает: R3, AC11; C3 шаг 3; K3, K4.
Уровень: integration, база с обеими схемами; один id с разными суммами.
Ожидания: метрики считают только `session_v2`; захват берёт id из `session_v2`; сессия учтена один раз.
Ловит: выбор `session` первым и двойной счёт.

### T11 — recent models у CLI и плагина
Покрывает: R3, AC12; C3 шаг 4; K3.
Уровень: integration `_load_recent_models`, `recent_models`, плагин `opencode_config.py`; база V2, V1, пустая.
Ожидания: CLI, doctor и плагин дают один список из `session_v2`; на v1 — пустой список и «моделей не найдено (v2: поле не подтверждено)», без падения.
Ловит: два независимых ридера, падение плагина на v1.

## Проверки R4–R6 — конфиг

### T12 — baseURL: только V2
Покрывает: R4, AC14, AC15; C4 шаг 1; K7.
Уровень: unit `_net.model_endpoint_url` на `tmp_path/opencode.json`; данные: (а) `providers.p.settings.baseURL`; (б) `provider.p.options.baseURL`; (в) обе; (г) ни одной.
Ожидания: (а) URL найден, запрос `{base}/models` прежний; (б) `None`, preflight выключен, файл цел побайтно; (в) побеждает V2; (г) `None`, прежнее сообщение.
Ловит: чтение V1-формы, приоритет V1.

### T13 — env в форме V2
Покрывает: R5, AC16; C4 шаг 2; K5.
Уровень: unit `awf_subprocess_env`; JSON из `OPENCODE_CONFIG_CONTENT` парсится, не сравнивается строкой.
Данные: профиль `allow/deny`, `restrict_control_tools=True`, `isolate_mcp=True`.
Ожидания: верхний уровень — только `permissions` (список), `agents`, `mcp.servers`; `bash`/`write`/`task` свёрнуты в `shell`/`edit`/`subagent`.
Ловит: V1-ключи в payload, прежние имена инструментов.

### T14 — порядок правил: стадия и `locked_deny`
Покрывает: R5, AC17, AC18; C4 шаг 2; K5, D1.
Уровень: unit payload + измерение `opencode run --standalone` 2.2.3.
Ожидания: правило стадии после хостового allow; `locked_deny` последним; при конфликте побеждает deny; проба показывает отказ инструмента.
Ловит: профиль, перекрытый хостом; изоляцию только на бумаге.
Протокол: хост разрешает `edit`, env запрещает; проба правит файл в `tmp_path`; отчёт IMP3.

### T15 — `awf init` создаёт V2; бэкап только при существующем файле
Покрывает: R5, AC19; C4 шаг 3; K6, D5; FR1, FR9.
Уровень: e2e `awf init` на `tmp_path`, фейк 2.2.3; случаи: (а) файла нет; (б) V2 с `agents` и `mcp.servers`; (в) V2 без агентов; (г) профиль с `external_directory`.
Ожидания: (а) V2 создан, бэкапа, отчёта и sha256 нет, `doctor` молчит; (б) недостающее дописано, копия снята до записи (sha256 равен исходному); (в) раздел создан, отсутствие `agent` не ошибка; (г) в `agents.<role>.permissions` первый — `{"action":"external_directory","resource":"*","effect":"deny"}`, затем allow `/tmp/opencode/**`, `/tmp/pytest-*`, `/tmp/pytest-*/**` последними: индекс allow больше.
Ловит: бэкап без файла (FR1), V1-запись, бэкап после перезаписи, deny, съевший `/tmp/opencode/**` (FR9).

### T16 — V1-файл не правится
Покрывает: R5, AC20; INV7; C4 шаг 3; K6, D4.
Уровень: e2e `awf init` на файле с `agent`, `permission`, `mcp.foo`.
Ожидания: байты и хеш до/после совпадают; WARN «сконвертируй по R11»; новых ключей нет; rc не аварийный. Строковый `model` V1-файлом не делает — см. T17.
Ловит: нормализацию чужого файла, падение на V1-форме.

### T17 — doctor: формат файла; строковый `model` — не V1
Покрывает: R6, AC21; C5 шаг 1; K8; FR2.
Уровень: unit `run_doctor`, `XDG_CONFIG_HOME` на `tmp_path`; V2; V2 со строковым `model` (корень и `agents.<имя>.model`); V1 (`provider`, `agent`, `permission`, `mcp.<имя>`); смешанный.
Ожидания: оба V2 — `ok`, WARN нет (`model` не входит в `V1_KEYS`); V1 и смешанный — WARN «сконвертируй в V2» с перечнем ключей; sha256 до/после равен.
Ловит: вечный WARN на валидном 2.x-файле (FR2), правку файла диагностикой, молчание про V1.

### T18 — мёртвые строки режима
Покрывает: R6, AC22, AC23; C5 шаг 2; K8.
Уровень: unit `run_doctor` + grep; запуск с `opencode.standalone` в `.agentic/config.yaml`.
Ожидания: строк режима нет; `MODELS_STANDALONE_SUPPORTED` отсутствует; WARN про мёртвый ключ, старт не заблокирован.
Ловит: остаточную строку «standalone» и импорт удалённой константы.

### T19 — страж зелёный, легальные имена не ловятся
Покрывает: R10, AC13, AC31, AC33; C7 шаг 3; K10.
Уровень: новый `test_opencode2_only_guard.py` по `awf/**/*.py`.
Ожидания: зелёный после IMP4 (на `0384ba3` красный по AC31); легальные `session_v2`, `session_message`, `session_store`, `providers`, `providerID`, `--standalone`, `apply_standalone` и комментарии со словом `session` не ловятся.
Ловит: ложные срабатывания, из-за которых страж выключат.

### T20 — страж краснеет от каждого шаблона
Покрывает: R10, AC32; C7 шаг 3; K10.
Уровень: параметризованный unit, копия модуля в `tmp_path` с вставкой; G1 `insert into session`, G2 `["provider"]`, G3 `.get("options")` у provider, G4 `--attach`, G5 `--port`, G6 `opencode.standalone`, G7 `x_v1.py`.
Ожидания: каждый падает с именем шаблона и файла.
Ловит: мёртвый страж, пропускающий возврат 1.x.

## Проверки R7–R8 — сеть и чистка

### T21 — образец 2.2.3 — network
Покрывает: R7, AC24; C6 шаг 1; K9.
Уровень: unit `classify_failure` на выводе 2.2.3 (`ECONNREFUSED: … Unable to connect. …`).
Ожидания: класс `network`; `failure_evidence` содержит строку; rc класс не меняет.
Ловит: потерю рабочих маркеров при чистке.

### T22 — удалённые 1.x-строки не network
Покрывает: R7, AC25; INV5; C6 шаг 2; K9, D3.
Уровень: unit `classify_failure`; `Cannot connect to API`, `Connection reset`, `fetch failed`; прежние `auth`/`transient`.
Ожидания: `fetch failed` — не network (AC25: ни маркер, ни `_NETWORK_RE` не ловят); две словесные строки — network через регулярку (K9); `auth`/`transient` без правок.
Ловит: возврат строки под живой текст и сдвиг классов.

### T23 — grep-ноль по мёртвому коду
Покрывает: R8, AC26; C7 шаг 1; K10.
Уровень: grep по `awf/**/*.py`.
Ожидания: ноль совпадений `--attach`, `--port`, `opencode.standalone`, `provider.*.options`, SQL по v1-таблицам; файлов `*_v1.py`, `*_legacy.py`, `*_compat.py` нет.
Ловит: остаток кода 1.x.

### T24 — чистка не задела чужое
Покрывает: R8, AC27; C7 шаг 1; D5.
Уровень: `git diff` базы `0384ba3` + счёт вхождений `legacy`/`normalize` вне opencode-домена.
Ожидания: изменённых путей state-store и commit-путей нет; счёт совпадает с базой.
Ловит: снос соседнего кода «заодно».

### T25 — документы описывают только 2.x
Покрывает: R9, AC28–AC30; C7 шаг 2.
Уровень: чтение файлов в unit + `spec check docs/spec/opencode2`.
Ожидания: `README.md`, `USAGE.md`, `CHANGELOG.md` требуют opencode 2.x и не обещают 1.19.4; в `docs/design.md` нет объединения схем; в карте нет колонки статуса и раздела отката; `docs/spec/opencode2/assets/historical.md` объявляет пакет историческим, шесть текстов целы, `spec check` → rc 0.
Ловит: обещание поддержки 1.x и сломанный старый пакет.

### T26 — миграция конфига владельца (ручная)
Покрывает: R11, AC34–AC36; C8; K11.
Уровень: ручная процедура, отчёт IMP5.
Шаги: `sha256sum` файла → копия; ключи в V2; чтение файла глазами (не только `debug config`); `awf doctor`; запрос к Jira и Confluence.
Ожидания: в отчёте путь и sha256 бэкапа; doctor `ok` по формату (AC35); сервисы отвечают.
Провал: восстановление из бэкапа, `sha256sum` совпадает с записанным.

### T27 — версия 3.0.0
Покрывает: R12, AC37, AC38; C7 шаг 2; K12.
Уровень: unit `cli` + чтение `pyproject.toml`, `CHANGELOG.md`.
Ожидания: `awf --version` → `3.0.0`, rc 0; в `pyproject.toml` `version = "3.0.0"`; раздел `3.0.0` упоминает «только opencode 2.x»; `2.3.0` нигде нет.
Ловит: расхождение версий и релиз без записи о переходе.

## Инварианты вне перечня IMP

### T28 — kill-группа неприкосновенна
Покрывает: INV1; C1; K1.
Уровень: unit `awf/_proc.py` + `conftest.py::_forbid_session_kill`.
Ожидания: `kill_process_tree` не сигналит при `pid <= 1`; tripwire активен; тесты зелёные.
Ловит: снятый guard и ослабленный tripwire.

### T29 — модели ролей и `argv_agent_flag`
Покрывает: INV4; C2; K2.
Уровень: unit `test_agent_argv.py` и конфиг моделей.
Ожидания: `models.<role>.model` читается как прежде; `argv_agent_flag` даёт прежний argv; `--standalone` рядом и не сдвигает его.
Ловит: сдвиг аргументов при вставке флага.

### T30 — сигналы, lease, состояние
Покрывает: INV6, INV9; C7; —
Уровень: e2e `test_signals.py`, `test_lifecycle.py`, unit lease.
Ожидания: `DONE-<todo>.ready`/`BLOCKED-<todo>.ready` и формат `.agentic/state|inbox|outbox` прежние; второй запуск — `run_mode="noop"` с текстом «another launch is already in progress», без исключения.
Ловит: изменённые сигналы, падение lease.

## Среда и команды

Существующие: `pytest tests/unit/test_opencode_version.py test_preflight_standalone.py test_agent_argv.py test_opencode_db.py test_metrics_v2.py test_doctor.py test_env_validation.py test_opencode_agents_permissions.py -q`; `pytest tests/e2e/test_init.py test_signals.py -q`; `bash scripts/check-contracts.sh`; полный сьют — `scripts/suite-canonical.sh --background`. Планируемые: `test_opencode2_only_guard.py` (T19, T20), кейсы baseURL и формата. Прогонов не было: на `0384ba3` часть ожиданий красная по замыслу TDD. Фикстуры — sqlite в `tmp_path`, фейки из `tests/infra`, `prime_probe`.

## Условия приёмки реализации

Обязательны T1–T30, кроме ручных T14 и T26: зелёные после IMP; T20 доказывает, что страж ловит вставку; `spec check docs/spec/opencode2-only` → rc 0; регрессия — прежние сьюты без v1-кейсов (`test_recent_models_v1`, `test_v1_schema_unchanged`). Предпосылки: opencode 2.2.3 для T14, T21; куки для T26.

## Findings

| ID | Важность | Находка | Владелец | Состояние |
|---|---|---|---|---|
| QA3 | MAJOR | AC17/AC18 без живой пробы; изоляцию доказывает T14 | QA/IMP3 | открыт |
| QA5 | MINOR | AC29 машинно не проверить: T25 плюс ревью IMP4 | ревьюер IMP4 | открыт |

QA1, QA2, QA4 закрыты в req-3. Статус REVIEW: критерии и инварианты покрыты, ожидания выведены из R#/AC#, негативные пути разобраны (V1-файл, чужая база, мусор версии, повтор запуска). READY — за финальным ревью.
