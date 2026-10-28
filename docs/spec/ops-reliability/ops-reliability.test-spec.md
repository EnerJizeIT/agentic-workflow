# Тестовая спецификация: операционная надёжность — потолок стадии, xfail-гейт, doctor и версии

Статус: REVIEW
Ревизия: ts-1
Входы: ops-reliability.md@req-3; ops-reliability.architecture.md@arch-1; ops-reliability.requirements-review.md@review-3; ops-reliability.context.md@ctx-1
База кода: `962c79dadc11ebdc15885ae261de0a456273ea47`

## Scope и статус

Проверки **спроектированы, не пройдены**. Scope — R1–R6, AC1–AC23, AC25–AC27, INV1–INV9 на базе `962c79d`; код, тесты и окружение не менялись. RR14–RR16 (review-3) закрываются проверками T6, T12, T18, хотя отдельных AC у них нет: ожидания выведены из R3, R4, R6 и контрактов K3, K4, K7. Это findings QA1–QA3, не блокеры.

## Карта покрытия

| Требование | Критерии и инварианты | Сценарий/контракт | T# | IMP |
|---|---|---|---|---|
| R1 | AC1–AC4 | C1; K1 | T1–T3 | IMP1 |
| R2 | AC5–AC7, AC21 | C1; K2 | T4–T5 | IMP2 |
| R3 | AC8–AC10 | C1; K3 | T6–T8 | IMP3 |
| R4 | AC11–AC13, AC22, AC25–AC26 | C2; K4 | T9–T12 | IMP4 |
| R5 | AC14–AC17, AC23 | C3; K5, K6 | T13–T16 | IMP5 |
| R6 | AC18–AC20, AC27 | C4; K5, K7 | T17–T19 | IMP6 |
| INV1, INV3, INV9 | — | C1; K3 | T6, T8 | IMP3 |
| INV2, INV4, INV5, INV7, INV8 | — | C1, C2; K1, K2, K4, K7 | T1, T2, T4, T9, T17 | IMP1/2/4/6 |
| INV6 | — | канарейки | T20 | план |

## Проверки

### T1 — Приоритет потолка и источник в логе
- Покрывает: R1, AC1, AC2, INV2; K1; C1.
- Уровень/вход: unit `stage_timeout.resolve`; CLI-парсер `awf start` без `--timeout`; MCP `timeout=None`.
- Данные: `resolve(None,None,{})`; `resolve(None,None,{"automation":{"stage_timeout_seconds":7200}})`; `resolve(None,600,{7200})`; `resolve(120,600,{7200})`; пайплайн без ключа.
- Ожидания: `(3600,"default")`, `(7200,"project")`, `(600,"stage")`, `(120,"caller")`; CLI и MCP передают `None`, не 3600; лог `stage timeout: 600s (source: stage)` до спавна; пайплайн без ключа грузится, кап 3600.
- Ловит: перепутанный приоритет; дефолт 3600 выдан за явное значение.

### T2 — Невалидное значение отвергается до спавна
- Покрывает: R1, AC3, INV8; K1.
- Уровень/вход: unit resolver + `start_pipeline`/`run_next` на fixture-проекте; загрузчик `validate_pipeline_stages`.
- Данные: caller `0`, `-5`, `"1h"`, `True`; стадия `timeout: "1h"`; проект `stage_timeout_seconds: 0`.
- Ожидания: caller/project — `AwfApiError` или `noop` rc 1 с именем входа/ключа; стадия — ошибка загрузки с именем стадии и `timeout`; спавнов 0; нет `awf-launch.lease`, pid-файла, новых файлов inbox/outbox.
- Ловит: тихий откат в 3600 (`if cli_timeout`) и спавн после отказа.

### T3 — Ключ timeout в обоих снапшотах
- Покрывает: R1, AC4; K1.
- Уровень/вход: unit `stage_to_document`, снапшот service-run.
- Данные: `Stage(timeout=600)`, `Stage(timeout=None)`, старый снапшот без ключа.
- Ожидания: `timeout: 600` в per-TODO и service-run снапшотах из одного сериализатора; при `None` ключа нет; старый снапшот читается.
- Ловит: два сериализатора расходятся; service-run теряет ключ.

### T4 — Предупреждение о полном прогоне до спавна
- Покрывает: R2, AC5, AC6, AC7, INV4; K2; C1.
- Уровень/вход: unit `declares_full_suite` + integration на фейковом воркере (`_mock_pipeline` из `tests/negative/test_carry_diag.py`).
- Данные: `pytest tests/ -q`, `pytest .`, `./tests`, `$PWD/tests`, `scripts/run-all.sh`, `uv run pytest tests/`, голый `pytest -k foo` при `testpaths=True` → полный; `pytest tests/unit/test_x.py`, `pytest tests/ -k foo`, голый `pytest -k foo` без `testpaths` → нет.
- Ожидания: `STAGE-WARN: full suite in verify:` с командой и капом 3600 (или 7200 из проекта), ровно одна строка; стадия стартует, rc и BLOCKED не меняются; нет `verify` или полного прогона — строки нет; неразбираемый контракт — строки нет, ошибка остаётся verify-паку.
- Ловит: второй разборщик и список вместо целой функции; предупреждение, блокирующее стадию.

### T5 — Явный timeout подавляет предупреждение
- Покрывает: R2, AC21; K2.
- Данные: полный прогон; caller 7200 при стадии 600 и проекте 7200.
- Ожидания: строки `STAGE-WARN` нет; кап 7200, `source: caller`.
- Ловит: подавление по ключам стадии/проекта вместо caller.

### T6 — Жёсткий кап даёт BLOCKED без backoff
- Покрывает: R3, AC8, INV1, INV3, INV9; K3; C1; RR14.
- Уровень/вход: integration, фейковый воркер, `_install_fake_time` из `tests/unit/test_signal_watch.py`.
- Данные: кап 1 с; воркер пишет `429 overloaded` и не пишет сигнал.
- Ожидания: ровно один спавн; `outbox/BLOCKED-TODO-0001.md` и `.ready`; `## ВЕРДИКТ` называет стадию и `1s`; `## НАХОДКИ` цитирует `429`; `last_signal=BLOCKED-TODO-0001`; rc 1; повторов и backoff 0.
- Ловит: старая классификация хвоста уводит в сеть и жжёт `net_retry_limit` вместо BLOCKED.

### T7 — `blocked-draft` читает заметку
- Покрывает: R3, AC9; K3.
- Данные: заметка из T6; отсутствующая заметка.
- Ожидания: `awf blocked-draft TODO-0001` и MCP `awf_blocked_draft` возвращают НАХОДКИ и НАПРАВЛЕНИЯ-ФИКСА, оставляя пустые слоты «Скоуп»/«Переносы»; без заметки — отказ с сообщением.
- Ловит: форма BLOCKED не разбирается читателем.

### T8 — Поздний DONE, сторож тишины, preflight
- Покрывает: R3, AC10, INV1, INV3, INV9; K3.
- Данные: (а) DONE в окне kill; (б) сторож тишины без сигнала; (в) preflight-таймаут с `is_preflight_timeout`.
- Ожидания: (а) стадия завершена штатно, BLOCKED нет; (б) прежняя ветка аварии с классификацией хвоста, BLOCKED нет; (в) класс `network`, backoff, BLOCKED нет.
- Ловит: жёсткая ветка перехватывает сторож/preflight; поздний DONE теряется.

### T9 — Допуски xfail/skip по команде
- Покрывает: R4, AC11, AC12, INV5; K4; C2.
- Уровень/вход: unit `_contract_tests_section` и `_pytest_counts` с поддельным pytest.
- Данные: `1 xfailed` без допуска; `skip_allow: 2` при `2 skipped`; две команды по `1 skipped` с `skip_allow: 1`; `xfail_allow: 1` при `2 xfailed`.
- Ожидания: FAIL с командой и `1 xfailed`; pass и строка `allowed 0 xfailed, 2 skipped`; pass у обеих команд; FAIL.
- Ловит: суммарный допуск; молчаливый зелёный.

### T10 — rc, не-тест-команда, кривой допуск
- Покрывает: R4, AC13, INV5; K4.
- Данные: rc 1 при `xfail_allow: 5`; `echo "1 skipped"`; `xfail_allow: -1`; `xfail_allow: "2"`.
- Ожидания: FAIL по rc при любом допуске; вывод не-тест-команды не разбирается; `-1` и `"2"` — ошибка контракта, не WARN.
- Ловит: допуск прощает красное; слово «skipped» в чужом выводе.

### T11 — Первая сводка и xpassed
- Покрывает: R4, AC22, AC26; K4; A12.
- Данные: первая `1 xfailed`, вторая `0`; первая `0 xfailed`, вторая `1`; `1 xpassed` при `xfail_allow: 1`.
- Ожидания: FAIL; pass с пометкой нестабильности; pass и `<n> xpassed (stale xfail)`, допуск на xpassed не действует.
- Ловит: вторая сводка меняет вердикт; xpassed судится как xfailed.

### T12 — Обёртки и `flake: off`
- Покрывает: R4, AC25; K4; RR15.
- Данные: `uv run pytest`, pytest из пути venv, `env python -m pytest` с `1 xfailed`; `flake: off` с `1 xfailed`; `flake: off` с чистой сводкой.
- Ожидания: FAIL у всех обёрток; при `flake: off` повторного прогона нет, xfail-вердикт тот же FAIL; чистая сводка — pass.
- Ловит: признак уже флейк-пробы; выключатель `flake` глушит гейт.

### T13 — Doctor: здоровый проект и дрейф раннера
- Покрывает: R5, AC14, INV7; K5, K6; C3.
- Уровень/вход: unit с fixture-проектом; `versions.*` подменены (awf, модуль и метаданные плагина 2.1.0; `runner_dir` нет).
- Данные: здоровый проект; `<runner>/awf/__init__.py` с `__version__ = "2.0.0"`.
- Ожидания: rc 0, все строки `ok`; rc 1, WARN с `2.0.0` и `2.1.0`; запуск не запрещён.
- Ловит: doctor отказывает в запуске; WARN без второй версии.

### T14 — Doctor: нет проекта и нечитаемый конфиг
- Покрывает: R5, AC15; K6.
- Данные: каталог без `.agentic`; нечитаемый `opencode.json`.
- Ожидания: rc 2, одна строка, без трассировки; WARN «unknown», секции версий и ключей на месте.
- Ловит: падение доктора вместо отчёта.

### T15 — Мёртвые ключи и секретность
- Покрывает: R5, AC16, AC17; K6.
- Данные: конфиг с `retry.max_attempts`, `retry.backoff_seconds`, `api_key: "sk-secret"`; конфиг без мёртвых ключей.
- Ожидания: оба имени названы; второй конфиг WARN не даёт; в выводе нет `sk-secret`, значений и токенов.
- Ловит: печать значений; мёртвый ключ не найден.

### T16 — Плагин, метаданные и реестр ключей
- Покрывает: R5, AC23; K5, K6, D5.
- Данные: плагин 2.0.0 при awf 2.1.0; метаданные плагина 1.6.0 при модуле 2.0.0; литерал чтения вне `READ_KEYS`.
- Ожидания: два отдельных WARN с обеими версиями; строка «устарели, переустановите пакет»; храповик краснеет на литерале вне реестра.
- Ловит: один WARN на всё; реестр разошёлся с кодом.

### T17 — Хендшейк запуска
- Покрывает: R6, AC18, AC19, AC20, INV7; K5, K7; C4.
- Уровень/вход: unit `start_pipeline`, `continue_pipeline`, `run_next` на fixture-проекте.
- Данные: все версии 2.1.0; раннер 2.0.0 при процессе и плагине 2.1.0; `runner_dir` нет.
- Ожидания: поля `runner_version`/`plugin_version` заполнены; совпадение — `warning` пуст; дрейф — `warning` с обеими версиями, запуск выполнен, rc прежний; без `runner_dir` WARN о раннере нет, `runner_version` = версия процесса.
- Ловит: warning блокирует запуск; поле пустое.

### T18 — Строка CLI и «раннер ≠ процесс»
- Покрывает: R6, RR16; K5, K7.
- Данные: раннер = плагин = 2.0.0, процесс = 2.1.0; `awf start --background`.
- Ожидания: stderr и лог запуска содержат `runner: 2.0.0, plugin: 2.0.0` и WARN с `2.0.0` и `2.1.0`; MCP-ответ несёт тот же `warning`.
- Ловит: CLI молчит; одно условие WARN вместо двух.

### T19 — Неимпортируемый плагин
- Покрывает: R6, AC27; K5, K7.
- Данные: импорт `agent_workflow_ui` поднимает ошибку.
- Ожидания: `plugin_version = "unknown"`, парного WARN нет, `runner_version` названа, запуск идёт.
- Ловит: `unknown` считается расхождением и блокирует запуск.

### T20 — Канарейки безопасности
- Покрывает: INV6.
- Уровень/вход: статическая проверка и существующий `tests/unit/test_process_gates.py`.
- Данные: `awf/_proc.py`, `tests/conftest.py`.
- Ожидания: `grep` находит `pid > 1` и `_forbid_session_kill`; diff задачи не трогает эти файлы; гейт зелёный.
- Ловит: ослабление guard при правках таймаута.

## Среда и выполнение

Фикстуры: `tmp_path`, `tmp_git_repo`, `awf_project` (`tests/unit/test_model_check.py`), `project` и `_mock_pipeline` (`tests/negative/test_carry_diag.py`), `_install_fake_time` (`tests/unit/test_signal_watch.py`). Время — поддельные часы; внешние сети и провайдеры не вызываются; состояние — во временных каталогах.

Существующие файлы, на которые опираются проверки: `tests/unit/test_signal_watch.py`, `tests/unit/test_pipeline_engine.py`, `tests/unit/test_verify_pack.py`, `tests/unit/test_model_check.py`, `tests/unit/test_process_gates.py`, `tests/negative/test_carry_diag.py`. Они в этой сессии не запускались; полного сьюта на дереве нет (контекст §5). Новые файлы — по плану IMP: `test_stage_timeout.py`, `test_suite_warning_pre_spawn.py`, `test_contract_xfail_gate.py`, `test_doctor.py`, `test_config_keys_registry.py`, `test_launch_handshake.py` — пока не созданы.

Команда после реализации: `python -m pytest tests/unit/test_stage_timeout.py tests/unit/test_suite_warning_pre_spawn.py tests/unit/test_contract_xfail_gate.py tests/unit/test_doctor.py tests/unit/test_config_keys_registry.py tests/unit/test_launch_handshake.py -q` — планируемая, при базе 962c79d не выполнялась. Регрессия: существующие файлы из списка выше плюс `tests/negative/test_carry_diag.py` и `tests/negative/test_carry_suite_gate.py` зелёные.

## Условия приёмки реализации

Обязательны все T1–T20; ожидания буквальны (числа, имена полей, rc, отсутствие вторых файлов и спавнов). T4, T6, T8, T9–T12, T17–T19 исполняются на поддельных воркере/раннере, поэтому доказывают ветку движка, а не реальный провайдер. T13–T16 доказывают отчёт doctor на fixture-проекте, а не установку пользователя. T20 — статическая проверка, не запуск.

## Findings

| ID | Важность | Источник | Влияние | Владелец | Состояние |
|---|---|---|---|---|---|
| QA1 | MINOR | review-3 RR14; R3, AC8 | Хвост `429`/`overloaded` не назван в AC; T6 берёт ожидание из K3 | аналитик | OPEN |
| QA2 | MINOR | review-3 RR15; R4 | `flake: off` не назван в AC; T12 берёт ожидание из K4 | аналитик | OPEN |
| QA3 | MINOR | review-3 RR16; R6 | Строка CLI и условие «раннер ≠ процесс» без AC; T18 берёт их из K7 | аналитик | OPEN |
| QA4 | SUGGESTION | review-3 RR18 | AC24 отсутствует; матрица готовности соберёт 26 критериев | аналитик | OPEN |
