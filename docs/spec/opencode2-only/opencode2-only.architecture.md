# Архитектура: awf 3.0 — только opencode 2.x

Статус: REVIEW
Ревизия: arch-3
Входы: opencode2-only.md@req-4; opencode2-only.requirements-review.md@reqrev-4; opencode2-only.context.md@ctx-1
База кода: 0384ba35ffc97ce370e6dc94bee098277d775860

## 1. Рамка

Требования req-4 приняты (PASS reqrev-4), база — commit 0384ba3. Задача — свести переходный слой 2.3.0 к одной форме на каждом слое, не трогая kill-группы, lease, state-store и модели ролей. Q1–Q3 закрыты фактами (D1–D3, детали в `ctx-1`): `OPENCODE_CONFIG_CONTENT` применяется после глобального и проектного конфигов; маркеры сети — в K9; `permissions` — упорядоченный список `{action, resource, effect}`, побеждает последнее совпадение, действия — `shell`, `edit`, `subagent`, MCP-инструмент `<server>_<tool>`.

## 2. Границы и точки изменения

| Компонент | Файл (символ) | R# |
|---|---|
| Версия и preflight | `awf/opencode_version.py` (`probe_version`, `gate`, `launch_plan`); `_opencode_gate` в `awf/api/pipeline.py`, `awf/api/run.py` | R1 |
| argv | `awf/opencode_version.py` (`resolve_standalone`, `apply_standalone`); `agent_stage.py`, `supervisor.py`, `opencode_db.py` | R2 |
| Схема базы | `awf/opencode_db.py`, `awf/metrics.py`, `awf/session_store.py`, `awf/api/model_check.py`, плагин | R3 |
| baseURL | `awf/_net.py` (`model_endpoint_url`, `_base_url`) | R4 |
| env воркера | `awf/_env.py` (`awf_subprocess_env`, `merge_tool_profile`) | R5 |
| файл агентов | `awf/opencode_agents.py`, `awf/cmd_init.py` | R5 |
| `awf doctor` | `awf/doctor.py` (`_check_config_format`); `awf/config_keys.py` (`V1_KEYS`) | R6 |
| маркеры сети | `awf/_net.py::NETWORK_MARKERS` | R7 |
| чистка | остатки тех же модулей | R8 |
| доки | `README.md`, `USAGE.md`, `CHANGELOG.md`, `docs/design.md`, карта миграции, `assets/historical.md` | R9 |
| страж | `tests/unit/test_opencode2_only_guard.py` (новый) | R10 |
| конфиг владельца | `~/.config/opencode/opencode.json` (ручной шаг) | R11 |
| версия | `awf/__init__.py`, `pyproject.toml`, `CHANGELOG.md`, `awf/cli.py` | R12 |

Новых сервисов и слоёв нет. Поток: K1 (версия) → отказ или K2 (`--standalone`) → спавн с env K5 и файлом K6 → захват K4 (`session_v2`) → метрики K3 (V2 или `n/a`).

## 3. Контракты

### K1 — отказ по версии

`probe_version()` возвращает `Probe(status: ok|missing|unknown, version)`, кэш на процесс. `gate(project_dir)` → `(message, rc, warning)`; пустой `message` — запуск разрешён. Вызов — до TODO, baseline, `.ready`, `current`, dispatch, lease и PID (INV2).

| Условие | `message` (содержит) | rc |
|---|---|---|
| major ≥ 2 | пусто | 0 |
| major < 2 | «awf 3 требует opencode 2.x», версия (`1.19.4`) | 1 |
| `unknown` (rc≠0, мусор, таймаут) | «версия не определена», «запуск запрещён» | 0 |
| `missing` | «opencode не найден» | 1 |

Отказ — результат с текстом, не исключение (доктрина 03). Managed не обходит отказ (D8) и не получает флаг. Ветки `mode == off` и предупреждение «`on` на 1.x» удаляются как недостижимые.

### K2 — argv

`resolve_standalone` возвращает `Plan(add_flag, warning)`; `apply_standalone` вставляет `--standalone` сразу после подкоманды. `managed` → флага нет, печатается `MANAGED_WARNING`; иначе флаг есть всегда, независимо от `.agentic/config.yaml`. Исключение общее для всех вызовов opencode: `opencode models` идёт через тот же `resolve_standalone` (R2, INV3, AC6). Прочие флаги и порядок не меняются. Условие `MODELS_STANDALONE_SUPPORTED` и проверка версии в `available_models` удаляются: отбор флага остаётся в одном месте.

### K3 — чтение базы

`detect_schema` возвращает только `v2` или `none`: `session_v2` + `session_message` — `v2`, остальное — `none`. Константы `SCHEMA_V1`, `SCHEMA_BOTH` и параметр `schema` у `collect_workers`/`collect_supervisor`/`candidate_session_ids`/`matched_session_ids`/`v2_project_session_ids` удаляются; чтение `session`/`part`/`message` и «both» уходят.

| Состояние базы | Метрики | Захват id |
|---|---|---|
| V2 | прежние числа и формат | читает/пишет `session_v2` |
| v1, пустая, без таблиц | `n/a` + причина, rc 0 | молча ничего не пишет |
| смешанная | считает только V2 | берёт `session_v2` |

Причина `n/a` — фиксированные строки: `схема v1 не поддерживается` и `таблиц v2 нет`. `recent_models` читает только `session_v2`; на v1/пустой — сообщение «моделей не найдено (v2: поле не подтверждено)» и пустой список, без падения.

### K4 — захват сессии

`_SESSION_TABLES = ("session_v2",)`; `_session_table` возвращает `session_v2` или `None`. На смешанной базе выбор однозначен. Ошибка чтения — прежний `_DbError`, id не пишется.

### K5 — env воркера (`OPENCODE_CONFIG_CONTENT`)

Payload — V2-документ: `permissions` (список), `agents` (по имени), `mcp.servers`. Ключей `permission`, `agent`, `mcp.<имя>` нет.

```json
{
  "permissions": [
    {"action": "shell", "resource": "*", "effect": "allow"},
    {"action": "edit", "resource": "*", "effect": "allow"},
    {"action": "shell", "resource": "*", "effect": "deny"},
    {"action": "agent-workflow-ui_awf_start", "resource": "*", "effect": "deny"}
  ],
  "agents": {"qa-review": {"permissions": [{"action": "edit", "resource": "*", "effect": "deny"}]}},
  "mcp": {"servers": {"unused-server": {"disabled": true}}}
}
```

Порядок сборки: базовые allow → allow профиля → deny профиля → `locked_deny` последними. Побеждает последнее совпадение: запрет стадии сильнее хостового allow, `locked_deny` сильнее профиля (AC17, AC18). Имена сворачиваются: `bash`→`shell`, `write`/`edit`→`edit`, `task`→`subagent`; при конфликте побеждает deny.

MCP-изоляция: `mcp.servers.<имя>.disabled: true` сливается с хостовым описанием сервера. Если слияние не сохранит `command`, писатель подставляет полное описание из хостового конфига с `disabled: true`. Хост-конфиг только читается.

### K6 — файл агентов: два писателя

Оба писателя — `propose`/`apply` и `ensure_mcp_block` — подчиняются одной матрице. Проверка AC19/AC20 идёт полным `awf init`.

Перечень V1-ключей — один на пакет: `V1_KEYS = ("provider", "agent", "permission")` плюс любой ключ `mcp` верхнего уровня, кроме `servers`. Определение живёт в `awf/config_keys.py` рядом с `READ_KEYS`; читают `opencode_agents.py`, `cmd_init.py` и `doctor.py` (K8). Строковый `model` в список не входит: схема 2.x принимает его в корне и в `agents.<имя>.model`, WARN нет, правке он не мешает (FR2, AC21).

| Состояние файла | Действие | Бэкап | Вывод |
|---|---|---|---|
| нет файла | создать V2: `agents`, `mcp.servers` | нет (D5) | ok |
| есть, V1-ключей нет (в том числе строковый `model`) | дописать недостающее | до записи | ok |
| есть ключ из `V1_KEYS` или `mcp.<имя>` | не менять байты | нет | WARN «сконвертируй по R11» |
| битый JSON | не менять | нет | прежний ERR |

Бэкап — копия существующих байт до записи; без файла шаг пропускается, отчёт и sha256 не создаются, doctor молчит (AC19).

`propose` больше не считает отсутствие `agent` ошибкой: пустой раздел — «создать». `_ensure_worker_permission` пишет в `agents.<role>.permissions` список: сначала `{"action": "external_directory", "resource": "*", "effect": "deny"}`, затем шаблоны (`/tmp/opencode/**`, `/tmp/pytest-*`, `/tmp/pytest-*/**`) — они последние и побеждают. Действие `external_directory` есть в схеме 2.x как ключ правил; что 2.2.3 его исполняет, подтверждает проба T14. Если действие неизвестно, отказ деградирует до хостовой политики. Модель роли — `{"providerID","model"}` либо строка `provider/model`; поле промпта — `system`.

### K7 — baseURL

`model_endpoint_url` читает только `providers.<p>.settings.baseURL` (схема `Config.Provider.Settings`, D2). Ветка `provider.<p>.options.baseURL` и правило «v1 первым» удалены. Ни одной формы — `None`, preflight выключен, файл не меняется. Обе формы — побеждает V2.

### K8 — `awf doctor`

Новая строка `config` читает файл из `opencode_config_file()` через `_load_opencode_root`. V2-ключи (`providers`, `agents`, `mcp.servers`, `permissions`) без V1-ключей → `ok`. Любой ключ из `V1_KEYS` → `WARN «сконвертируй в V2: <перечень>»`; файл не пишется, хеш не меняется. Строковый `model` WARN не даёт (AC21). Смешанный файл трактуется как V1. Строки режима и `MODELS_STANDALONE_SUPPORTED` удаляются; `opencode.standalone` уходит из `READ_KEYS` → попадает в мёртвые ключи, запуск не блокирует. Перечень доктор берёт из `awf/config_keys.py`, а не копирует — иначе K6 и K8 разойдутся (RR21).

### K9 — маркеры сети

| Строка | Решение | Причина |
|---|---|---|
| `ECONNREFUSED`, `ECONNRESET`, `ETIMEDOUT` | оставить | коды libuv, версия-нейтральны, 2.2.3 их печатает |
| `Unable to connect` | оставить | замеренный текст 2.2.3 на мёртвом эндпоинте |
| `Connection reset` | удалить | формулировка 1.x; `_NETWORK_RE` ловит «reset» |
| `Cannot connect to API` | удалить | формулировка 1.x; `_NETWORK_RE` ловит «connect» |
| `fetch failed` | удалить | формулировка undici-эпохи |

Классы и правило «одно совпадение — network» не меняются; `_NETWORK_RE` страхует две словесные строки. `fetch failed` — свидетель AC25; если проба покажет его, строку вернуть (D3).

### K10 — страж от возврата 1.x

Новый тест `tests/unit/test_opencode2_only_guard.py` читает `awf/**/*.py` (кроме тестов) и падает при совпадении шаблона:

| ID | Шаблон | Разрешено |
|---|---|---|
| G1 | `(from\|join\|into\|update)\s+(session\|part\|message)\b` (без регистра) | `session_v2`, `session_message`, `session_store` |
| G2 | `["']provider["']` / `["']agent["']` как корень конфига (`.get(...)`, `root[...]`) | `providers`, `providerID`, `agents`; объявленный `V1_KEYS` в `awf/config_keys.py` и строка перечня в `awf/doctor.py` — исключение по точному пути и объявлению модуля |
| G3 | `.get("options")` / `["options"]` рядом с provider | `settings`, `options` агента |
| G4 | `--attach` | — |
| G5 | `--port` | — |
| G6 | `opencode.standalone`, `STANDALONE_KEY`, `standalone_mode` | `--standalone`, `apply_standalone` |
| G7 | имена файлов `*_v1.py`, `*_legacy.py`, `*_compat.py` | — |

AC32: параметризованный тест копирует модуль во временный каталог, вставляет шаблон и ждёт красный. G1 бьёт по SQL и именам таблиц, не по подстроке (CTX12). G2 намеренно ловит `awf/api/model_check.py::_load_opencode_config`: код в IMP2 переводится на `providers`/`agents`, исключения нет (FR8). Исключение для `V1_KEYS`/`doctor` нужно, потому что доктор обязан называть мёртвые ключи; список живёт в одном объявлении, разъезд K6/K8 ловится отсутствием копии.

Зелёный прогон относится к дереву после IMP4: на `0384ba3` шаблоны G1/G2/G6 находят `SCHEMA_V1`, v1-SQL, `root.get("provider")` и `opencode.standalone` (AC31).

### K11 — миграция конфига владельца

Ручной шаг без кода: бэкап `opencode.json` (путь и sha256 в отчёт IMP), перевод ключей в V2 (`permissions` списком, `agents`, `mcp.servers`), проверка файла (не только `opencode debug config` — он нормализует V1 в памяти, RR15) и `awf doctor` → `ok`; затем запрос к Jira и Confluence. Провал — восстановление из бэкапа.

### K12 — версия 3.0.0

`awf/__init__.py::__version__`, `pyproject.toml`, `CHANGELOG.md` дают `3.0.0`. Команды `awf --version` нет; AC37 требует её, поэтому в argparse добавляется флаг, печатающий `__version__` (единственное «новое», RR19).

## 4. Отказы и совместимость

| Условие | Наблюдаемый результат | Инвариант |
|---|---|---|
| 1.x, неизвестная версия, нет бинаря | noop-результат с текстом; `.agentic/*`, TODO-ID, lease, PID не тронуты | INV2, D1 |
| managed включён | флага нет, предупреждение прежнее; отказ по версии срабатывает | INV3, D8 |
| v1/пустая база | `n/a` с причиной, rc 0; id не пишется | INV8 |
| V1-файл конфига | байты и хеш не меняются, WARN | INV7 |
| файла агентов нет | создаётся V2, бэкапа нет, doctor молчит | INV7, D5 |
| нет `baseURL` | preflight сети выключен, сообщение прежнее | R4 |

Что ломается для 1.x: старт отказывает; `opencode.standalone` не действует; метрики по v1-сессиям дают `n/a`; V1-файл не читается и не правится — это и есть ответ пользователю 1.x.

## 5. Решения

| ID | Решение | Основание и последствие |
|---|---|---|
| D1 | Изоляция env-документом, правила в `permissions` последними | `Config.load` ставит env после глобального и проектного конфигов; временный файл агента дал бы гонки |
| D2 | `permissions` — список `{action,resource,effect}`, действия 2.x | Схема и v2-доки; «последнее совпадение» требует явного порядка |
| D3 | Семь строк маркеров с поимённым решением (K9) | Иначе можно выкинуть рабочие строки; свидетель AC25 — `fetch failed` |
| D4 | V1-файл не трогаем, WARN | Правка чужого конфига дороже предупреждения (A5, AC20) |
| D5 | Бэкап только при перезаписи существующих байт | У отсутствующего файла восстанавливать нечего (AC19) |
| D6 | Отсутствие `agents` — не ошибка, а «создать» | Иначе `awf init` не проходит AC19 |
| D7 | Страж — текстовые шаблоны в тесте, не линтер | Детерминированно, без новой зависимости |
| D8 | Managed — opt-in без флага, отказ версии не обходит | Изоляция — выбор оператора, версия — жёсткое требование |
| D9 | Один `V1_KEYS` в `awf/config_keys.py` для K6 и K8 | Две копии дают файл, который `awf init` считает V2, а doctor — V1 |

## 6. План IMP

| ID | Цель, ссылки | Файлы | Зависит | Проверка |
|---|---|---|---|---|
| IMP1 | Запуск: отказ версии + флаг всегда (R1, R2, K1, K2, C1, C2) | `awf/opencode_version.py`, `awf/api/pipeline.py`, `awf/api/run.py`, `awf/agent_stage.py`, `awf/supervisor.py`, `awf/opencode_db.py`; двойники → 2.2.3 | — | сид (1,19,4) отказывает без следов; флаг при любом конфиге; managed без флага |
| IMP2 | Данные только V2 (R3, K3, K4, C3) | `awf/opencode_db.py`, `awf/metrics.py`, `awf/session_store.py`, `awf/api/model_check.py` (корни конфига, FR8), плагин | IMP1 | V2-числа прежние; v1/пустая → `n/a`; смешанная → `session_v2`; v1-кейсы удалены; `get("provider"` в `model_check` нет |
| IMP3 | Конфиг: env, файл агентов, doctor, baseURL (R4–R6, K5–K8, C4, C5) | `awf/_env.py`, `awf/opencode_agents.py`, `awf/cmd_init.py`, `awf/doctor.py`, `awf/_net.py`, `awf/config_keys.py` | IMP1 | payload-тест (порядок правил, имена, `mcp.servers`); `awf init`: без файла — V2 без бэкапа, на V2 — бэкап, на V1 — байты целы (AC19, AC20); doctor ok со строковым `model`, WARN на V1; baseURL обе формы |
| IMP4 | Чистка, маркеры, доки, страж (R7–R10, K9, K10, C6, C7) | `awf/_net.py`, остатки `awf/opencode_version.py`, доки §2, новый страж | IMP1–IMP3 | страж зелёный после IMP4 и краснеет от вставки (AC32), G2 не бьёт `V1_KEYS` и `session_v2` (AC33); grep-ноль по R8; `awf spec check docs/spec/opencode2` → rc 0 |
| IMP5 | Миграция конфига владельца (R11, K11, C8) | `~/.config/opencode/opencode.json` (ручной шаг) | IMP3 | бэкап + sha256 в отчёте; doctor ok; Jira/Confluence отвечают; откат проверен |
| IMP6 | Релиз 3.0.0 (R12, K12) | `awf/__init__.py`, `pyproject.toml`, `CHANGELOG.md`, `awf/cli.py` | IMP1–IMP5 | `awf --version` = 3.0.0; в трёх местах нет 2.3.0; CHANGELOG содержит раздел 3.0.0 |

## 7. Покрытие

| Требование | Контракты/решения | IMP |
|---|---|---|
| R1, R2 | K1, K2, D1, D8 | IMP1 |
| R3 | K3, K4 | IMP2 |
| R4–R6 | K5–K8, D1–D2, D4–D6, D9 | IMP3 |
| R7–R10 | K9, K10, D3, D7 | IMP4 |
| R11, R12 | K11, K12 | IMP5–IMP6 |
| INV2, INV3, INV7, INV8 | K1–K3, K6, D4, D5, D8 | IMP1–IMP3 |
| INV1, INV4, INV6, INV9 | вне перечня файлов IMP — не затрагиваются | все |

## 8. Открытые вопросы

Нет. Q1–Q3 закрыты в D1–D3. Остаточные условия контракт не меняют: `fetch failed` проверяется пробой в IMP4 (D3), действие `external_directory` — ручной пробой T14 в IMP3 (K6).
