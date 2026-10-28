# Архитектура: операционная надёжность — потолок стадии, xfail-гейт, doctor и версии

Статус: REVIEW
Ревизия: arch-1
Входы: ops-reliability.md@req-3; ops-reliability.context.md@ctx-1; ops-reliability.requirements-review.md@review-3
База кода: `962c79dadc11ebdc15885ae261de0a456273ea47`

## 1. Границы и точки изменения

| Компонент | Ответственность | Файл (существующий → дельта) | R# |
|---|---|---|---|
| Разрешение потолка | один расчёт «явный → стадия → проект → 3600» | новый `awf/stage_timeout.py` | R1, R2 |
| Схема стадии | ключ `timeout` в загрузке, валидации и снапшоте | `awf/pipeline.py` (`Stage`, `ALLOWED_STAGE_KEYS`, `validate_pipeline_stages`, `stage_to_document`) | R1 |
| Входы запуска | дефолт «не задано», отказ по невалидному значению | `awf/cli.py`, `awf/orchestrator.py`, `awf/api/run.py`, `awf/api/pipeline.py`, `awf/cmd_start.py` | R1 |
| Предупреждение о полном прогоне | признак по `verify:` до спавна | `awf/suite_guard.py` (новый helper над `detect_full_suite`) + `awf/pipeline_engine.py` | R2 |
| Жёсткий таймаут | типизированное исключение и BLOCKED-заметка | `awf/signal_watch.py`, `awf/pipeline_engine.py` | R3 |
| xfail/skip-гейт | разбор первой сводки pytest и допуски контракта | `awf/verify_pack.py`, `awf/unit_contract.py`, `docs/unit-contract.md` | R4 |
| Версии | источники версий и текст WARN для doctor и запуска | новый `awf/versions.py` | R5, R6 |
| Doctor | отчёт ok/WARN, rc 0/1/2 | новые `awf/doctor.py`, `awf/cmd_doctor.py`, `awf/config_keys.py`; `awf/cli.py`; `agent_workflow_ui/src/agent_workflow_ui/tools/awf.py` | R5 |
| Хендшейк запуска | поля результата и строка CLI | `awf/api/_results.py`, `awf/api/pipeline.py`, `awf/api/run.py`, `awf/cmd_start.py` | R6 |

Новых сервисов, очередей и файлов состояния нет: все точки уже существуют, кроме четырёх перечисленных модулей.

## 2. Контракты

### K1 — Потолок стадии

`awf/stage_timeout.py` — единственный расчёт. Значения — положительные целые секунды; `bool` отвергается (в Python `True` — целое).

```
resolve(caller: int | None, stage_timeout: int | None, config: dict)
    -> Resolved(seconds: int, source: str)
```

Приоритет: `caller` → `stage_timeout` → `automation.stage_timeout_seconds` → 3600. `source` ∈ `caller`, `stage`, `project`, `default`; строка лога стадии до спавна: `stage timeout: <N>s (source: <source>)`.

- `caller` приходит из CLI `--timeout` (дефолт `None`) и MCP `timeout` (дефолт `None`). `None` — «не задано»; иначе целое > 0, иначе отказ `AwfApiError` с именем входа.
- `stage_timeout` — ключ стадии `timeout`; валидация в `validate_pipeline_stages` (ошибка загрузки с именем стадии и ключа).
- проект — `automation.stage_timeout_seconds`; проверяется до lease, PID-файла и первого спавна. Невалидное значение даёт `noop`-отказ с rc 1 и именем ключа (`StartResult` / `RunNextResult`), файлы не пишутся (INV8).
- `stage_to_document` пишет `timeout`, когда он задан; снапшот per-TODO и service-run получают ключ из одного сериализатора (AC4).
- шаблон `awf/api/_templates.py` перечисляет `automation.stage_timeout_seconds`.

### K2 — Предупреждение о полном прогоне

`suite_guard.declares_full_suite(verify: list[str], project_dir: Path) -> str | None` возвращает первую команду, для которой `detect_full_suite(command, testpaths=suite_command.testpaths_configured(project_dir))` истинна. Второго разборщика нет; `testpaths` читается тем же `suite_command`, что и в гейте.

Движок перед спавном воркера читает контракт TODO (`unit_contract.parse_todo_contract`). Если команда найдена и `caller is None`, в лог стадии идёт:

```
STAGE-WARN: full suite in verify: `<cmd>`; cap <N>s (source: <source>) — raise timeout or split the unit
```

Предупреждение не меняет rc, не пишет BLOCKED, стадия стартует. Явный `caller` подавляет строку при любых ключах стадии и проекта (AC21). Если контракт не разбирается, предупреждение не строится — ошибка контракта остаётся делом verify-пака.

### K3 — Жёсткий таймаут

`awf/signal_watch.py` получает `class HardTimeoutError(TimeoutError)`; им заменяется текущий `TimeoutError` на жёстком капе, текст «did not produce signal within Ns» не меняется. Preflight-таймаут и сторож тишины это исключение не поднимают: их классы прежние (INV3, INV9).

В `pipeline_engine` ветка `except (RuntimeError, TimeoutError)` сначала проверяет `isinstance(e, HardTimeoutError)` **и** отсутствие сигнала на диске:

- сигнал есть → прежний путь завершения стадии (AC10, INV1);
- сигнала нет → `_stop_on_hard_timeout(project_dir, todo_id, stage_name, logs_dir, seconds, tail)`.

Заметка — стандартная форма `docs/unit-contract.md` (`_write_blocked_note`, тот же писатель, что у auth): вердикт называет стадию и кап; `## НАХОДКИ` — строка `кап: <N>s` и хвост лога; направления — «подними timeout (`--timeout` / ключ стадии / `automation.stage_timeout_seconds`)» и «дели юнит». Затем `last_signal=BLOCKED-<id>`, лог `hard timeout on <stage> — BLOCKED, stage stopped`, rc 1, второго спавна нет. `awf blocked-draft` читает файл без изменений.

### K4 — xfail и skip в контрактных командах

`verify_pack._pytest_counts(output) -> dict` разбирает каноническую сводку pytest (последняя строка с парами «число + слово»: `passed`, `failed`, `error`, `xfailed`, `xpassed`, `skipped`, `deselected`, `warning`). Отсутствующее слово — 0. Разбирается сводка первого (канонического) прогона; вторая сводка флейк-пробы только добавляет пометку нестабильности, вердикт не меняет (AC22).

Секция `contract_tests` для команды с `_is_test_like(cmd) == True`:

| Условие | Итог |
|---|---|
| rc ≠ 0 | FAIL (прежний, INV5) |
| rc 0, `xfailed > xfail_allow` или `skipped > skip_allow` | FAIL: команда и числа |
| rc 0, в пределах допуска | pass; строка `allowed <n> xfailed, <m> skipped` при ненулевом допуске |
| rc 0, `xpassed > 0` | pass; строка `<n> xpassed (stale xfail)` |
| не тест-команда | сводка не разбирается |

Допуски `xfail_allow` / `skip_allow` — ключи верхнего уровня контракта TODO, неотрицательные целые, по умолчанию 0, на каждую команду отдельно. Оба входят в `_CONTRACT_KEYS` и в `docs/unit-contract.md`; `-1` и `"2"` — ошибка контракта (как неизвестный тип), не предупреждение. `flake: off` выключает только повторный прогон: разбор сводки выполняется всегда (RR15).

### K5 — Версии

`awf/versions.py` — единственный дом:

| Функция | Значение |
|---|---|
| `process_version()` | `awf.__version__` процесса |
| `pinned_version(project_dir, config)` | `__version__` из `<runner_dir>/awf/__init__.py` разбором текста, без импорта чужого чекаута; `""` — ключа нет, `"unknown"` — файл не читается |
| `runner_version(project_dir, config)` | `pinned_version`, иначе `process_version` |
| `plugin_version()` | `agent_workflow_ui.__version__`; ошибка импорта → `"unknown"` |
| `install_metadata(name)` | `importlib.metadata.version(name)`; ошибка → `"unknown"` |
| `version_warning(...)` | `""` или строка расхождения |

Условия WARN: версия модуля плагина ≠ `awf`-версии раннера (с обеими); метаданные ≠ версии модуля (для `awf` и `agent-workflow-ui`); `runner_dir` задан и его версия ≠ версии процесса (с обеими и путём). Плагин `"unknown"` пары не образует и WARN о паре не даёт (AC27). Текст: `version drift: runner <r> (pinned: <dir>), process <p>, plugin <v> — run awf doctor; restart the opencode session` — перечисляются только разошедшиеся участники.

### K6 — Doctor

`awf/doctor.py::run_doctor(project_dir) -> DoctorReport` — чистая сборка отчёта из уже существующих поверхностей:

| Проверка | Источник | WARN |
|---|---|---|
| нет `.agentic` | `paths.agentic_dir` | rc 2, отчёт одной строкой, без трассировки |
| `awf` модуль | `versions.process_version` | — |
| закреплённый раннер | `versions.pinned_version` | ≠ процессу (с обеими) |
| плагин модуль | `versions.plugin_version` | ≠ версии раннера (с обеими) |
| метаданные `awf`, `agent-workflow-ui` | `versions.install_metadata` | ≠ модулю: «устарели, переустановите пакет» |
| модели | `check_model_config` (импорт внутри функции) | исключение → строка `unknown`, прочие секции остаются |
| мёртвые ключи | `awf/config_keys.dead_keys` | ключ назван |

Формат строки: `<ok|WARN>  <проверка>: <факт>`; значения ключей и токены не печатаются, только имена (AC17). `rc` 0 — WARN нет, 1 — есть, 2 — проект не оценивается. `DoctorReport` = `{rc, lines, warnings}`.

`awf/config_keys.py`: `READ_KEYS: frozenset[str]` — точки, которые читает код (`automation.runner_dir`, `automation.net_retry_limit`, `automation.preflight_timeout_seconds`, `automation.no_output_timeout_seconds`, `automation.stage_timeout_seconds`, `models`, `limits.max_review_passes`, `verification.*` и т.д.). `dead_keys(config)` разворачивает конфиг в точечные листья и возвращает листья вне `READ_KEYS`; `retry.max_attempts` и `retry.backoff_seconds` попадают туда по факту отсутствия читателя. Отдельный тест-храповик сверяет реестр с литералами `cfg_mod.get(...)`/`config.get(...)` в `awf/**/*.py` и краснеет на расхождении.

`awf/cmd_doctor.py::run(args)` печатает `lines`, возвращает `rc`. MCP-инструмент `awf_doctor(project_dir)` возвращает `{status: "ok", rc, lines, warnings}`; rc 2 — не ошибка инструмента.

### K7 — Хендшейк запуска

`StartResult` и `RunNextResult` получают поля `runner_version: str = ""`, `plugin_version: str = ""`, `warning: str = ""`. Их заполняет один helper `versions.handshake(project_dir, config)` в `start_pipeline`, `continue_pipeline` и `run_next` после основной работы: запуск не отказывает, rc не меняется (INV7), `warning` пуст при совпадении. MCP-инструменты отдают поля через `as_dict`, без отдельного кода. CLI `awf start` / `awf continue` печатают `runner: <r>, plugin: <v>` и непустой WARN в stderr и в лог запуска, когда он известен (`result.log_file`).

## 3. Состояния и отказы

| Событие | Наблюдаемый результат |
|---|---|
| невалидный `stage.timeout` | ошибка загрузки пайплайна, стадия не создаётся |
| невалидный `automation.stage_timeout_seconds` | `noop`, rc 1, имя ключа, ни одного файла |
| невалидный явный `timeout` | `noop`, rc 1, имя входа |
| полный прогон в `verify:` без явного `timeout` | `STAGE-WARN`, стадия стартует |
| жёсткий кап | BLOCKED-заметка + `.ready`, `last_signal=BLOCKED-<id>`, rc 1, один спавн |
| DONE в окне kill | стадия завершается штатно, BLOCKED нет |
| preflight-таймаут | класс `network`, backoff (INV3) |
| сторож тишины | прежняя ветка аварии, BLOCKED не пишет (INV9) |
| `xfailed` сверх допуска | секция FAIL, комиссия не проходит |
| `xpassed` | pass со строкой о числе |
| doctor: WARN | rc 1, отчёт, запуск не запрещён |
| расхождение версий при запуске | `warning`, запуск выполнен |

## 4. Совместимость

- Пайплайн без `timeout` грузится как раньше; эффективный кап остаётся 3600 (INV2).
- Дефолт `--timeout`/`timeout` становится `None`; прежнее поведение сохраняется через источник `default`. Явные `0` и отрицательные раньше молча игнорировались (`if cli_timeout`), теперь это отказ — изменение названо в требованиях (AC3).
- Снапшот стадии получает необязательный ключ; старые снапшоты читаются тем же загрузчиком.
- Новые поля результата аддитивны: старые потребители их игнорируют.
- Новые ключи контракта необязательны; TODO без них судятся как раньше, кроме нового правила «xfail/skip при rc 0».
- `HardTimeoutError` — подкласс `TimeoutError`: существующие перехваты и тесты продолжают работать.
- Мёртвые ключи не удаляются из живого конфига автоматически: doctor их только называет.

## 5. Решения

| ID | Решение | Альтернатива и почему нет | Последствия |
|---|---|---|---|
| D1 | типизированный `HardTimeoutError`, не разбор текста | сравнение строки хрупко и смешает preflight | смена класса в signal_watch; тесты ловят `TimeoutError` |
| D2 | один resolver с `None` = «не задано» | оставить дефолт 3600 — R2 неотличим от явного значения (RR6) | правка пяти входов |
| D3 | WARN о полном прогоне только при `caller is None` | учитывать ключи стадии/проекта — противоречит AC21 | ключи не подавляют предупреждение |
| D4 | вердикт по первой сводке, вторая только метка | судить обе — две реализации разойдутся (A5) | стабильный вердикт |
| D5 | реестр читаемых ключей + тест-храповик | рантайм-интроспекция не видит литералы чтения | новый файл и тест |
| D6 | версия закреплённого чекаута — разбор файла | импорт исполнил бы чужой код в процессе MCP | `"unknown"` на нечитаемом файле |
| D7 | `"unknown"` для плагина и метаданных | пустая строка смешалась бы с «ключа нет» | AC27 без пары WARN |
| D8 | doctor возвращает данные, CLI печатает | только текст — MCP не сможет решать по rc | `DoctorReport` |
| D9 | источник в логе — английские токены | русские подписи разъедутся с кодом | AC1/AC2 сверяют токен |

Допущения: `agent-workflow-ui` установлен как распределение `agent-workflow-ui` (метаданные могут отсутствовать → `unknown` + WARN); имя ключа `automation.stage_timeout_seconds` новое и в живом конфиге отсутствует; `awf blocked-draft` уже читает стандартную форму BLOCKED без правок.

Открытых блокирующих вопросов нет. Вопросы вне scope: OPS8 (общий дом версии opencode) и OPS5 (классификация провайдера) не проектируются.

## 6. План IMP#

| IMP | Цель и ссылки | Файлы | Зависит от | Проверка |
|---|---|---|---|---|
| IMP1 | K1: resolver, ключ стадии, дефолт «не задано», отказ, лог (R1, AC1–AC4, INV2, INV8) | `awf/stage_timeout.py` (новый), `awf/pipeline.py`, `awf/cli.py`, `awf/orchestrator.py`, `awf/api/run.py`, `awf/api/pipeline.py`, `awf/cmd_start.py`, `awf/api/_templates.py` | — | новые `tests/unit/test_stage_timeout.py` (приоритет, типы, отказ, снапшот); целевые прогоны загрузчика пайплайна |
| IMP2 | K2: предупреждение о полном прогоне (R2, AC5–AC7, AC21, INV4) | `awf/suite_guard.py`, `awf/pipeline_engine.py` | IMP1 | `tests/unit/test_suite_warning_pre_spawn.py`: шесть случаев AC6, подавление явным `timeout`, rc не меняется |
| IMP3 | K3: `HardTimeoutError` и BLOCKED-заметка (R3, AC8–AC10, INV1, INV9) | `awf/signal_watch.py`, `awf/pipeline_engine.py` | IMP1 | тест на фейковом воркере: один спавн, заметка с капом и хвостом, `blocked-draft`; preflight и поздний DONE |
| IMP4 | K4: xfail/skip-гейт и допуски (R4, AC11–AC13, AC22, AC25, AC26, INV5) | `awf/verify_pack.py`, `awf/unit_contract.py`, `docs/unit-contract.md` | — | `tests/unit/test_contract_xfail_gate.py`: первая/вторая сводка, допуск на команду, `flake: off`, не-тест-команда |
| IMP5 | K5+K6: `versions`, реестр ключей, doctor CLI и MCP (R5, AC14–AC17, AC23, INV7) | `awf/versions.py`, `awf/config_keys.py`, `awf/doctor.py`, `awf/cmd_doctor.py` (новые), `awf/cli.py`, `agent_workflow_ui/src/agent_workflow_ui/tools/awf.py` | — | `tests/unit/test_doctor.py` на fixture-проекте, `tests/unit/test_config_keys_registry.py` |
| IMP6 | K7: версии в результате запуска и CLI (R6, AC18–AC20, AC27, INV7) | `awf/api/_results.py`, `awf/api/pipeline.py`, `awf/api/run.py`, `awf/cmd_start.py` | IMP5 | `tests/unit/test_launch_handshake.py`: пара версий, `runner_dir`, `unknown`, WARN не меняет rc |

Покрытие инвариантов: INV1, INV9 — IMP3; INV2, INV8 — IMP1; INV3 — IMP3 (preflight не трогаем) и IMP4 (вердикт по rc); INV4 — IMP2; INV5 — IMP4; INV6 — без изменений, канарейки не входят в дифф; INV7 — IMP5, IMP6.

Порядок: IMP1 → IMP2 → IMP3, IMP4 независим, IMP5 → IMP6. Один IMP — один проверяемый результат; IMP4 и IMP5 можно вести параллельно с IMP1.
