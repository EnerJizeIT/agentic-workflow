# Готовность: операционная надёжность — потолок стадии, xfail-гейт, doctor и версии

Статус: READY
Ревизия: rd-1
Входы: ops-reliability.md@req-3; ops-reliability.context.md@ctx-1; ops-reliability.requirements-review.md@review-3; ops-reliability.architecture.md@arch-1; ops-reliability.test-spec.md@ts-1
База кода: `962c79dadc11ebdc15885ae261de0a456273ea47`
Проверено: ops-reliability.context.md=ctx-1; ops-reliability.md=req-3; ops-reliability.requirements-review.md=review-3; ops-reliability.architecture.md=arch-1; ops-reliability.test-spec.md=ts-1; ops-reliability.readiness.md=rd-1
Пакет-sha: 45067c5dd4c83b593a1419cb24064fefd84ff269a43f10117c61a10d5c255f61

## Вердикт и способ проверки

READY. Пакет можно отдавать исполнителю. Открытых BLOCKER и MAJOR нет; влияющих на поведение вопросов нет. Пять MINOR и два SUGGESTION ниже разобраны: нормативный ответ на каждый уже есть в R#, K# или T#, поэтому реализаций с разным поведением не остаётся. Ревизии актуальны, база у всех документов одна.

Проверку выполнил отдельный проход стадии `spec-readiness`, не авторы документов. Второго независимого агента не было; это не выдаётся за перекрёстную проверку.

## Проверенные входы и факты

- Все шесть документов прочитаны целиком; `awf spec check docs/spec/ops-reliability` до готовности давал rc 1 только на отсутствующую готовность.
- Рабочее дерево: код на `962c79d` не менялся; в `git status` только каталог пакета.
- Факты сверены вызовами, а не пересказом: `detect_full_suite` (шесть случаев AC6, `pytest ./tests` и `pytest $PWD/tests` — истина, `pytest tests/ -k foo` — ложь), `_is_test_like` (все обёртки из AC25), метаданные (`awf` 2.1.0, плагин-модуль 2.1.0, метаданные плагина 1.6.0), жёсткий кап и `TimeoutError` в `signal_watch`, ветка классификации хвоста в `pipeline_engine`, `flake_off` в `verify_pack`, отсутствие `timeout` в `Stage`/`ALLOWED_STAGE_KEYS`, отсутствие `xfail_allow`/`skip_allow` в `_CONTRACT_KEYS`, мёртвые `retry.*` в живом конфиге.
- Точки расширения из плана существуют: `_write_blocked_note` и `_write_state(last_signal=...)`, `blocked_draft`, `check_model_config`, `stage_to_document`, `StartResult`/`RunNextResult`.

## Матрица покрытия

| ID | Источник | Сценарий/контракт | Проверка |
|---|---|---|---|
| R1 | ops-reliability.md#R1 | C1/K1 | T1/T2/T3/IMP1 |
| R2 | ops-reliability.md#R2 | C1/K2 | T4/T5/IMP2 |
| R3 | ops-reliability.md#R3 | C1/K3 | T6/T7/T8/IMP3 |
| R4 | ops-reliability.md#R4 | C2/K4 | T9/T10/T11/T12/IMP4 |
| R5 | ops-reliability.md#R5 | C3/K5/K6 | T13/T14/T15/T16/IMP5 |
| R6 | ops-reliability.md#R6 | C4/K5/K7 | T17/T18/T19/IMP6 |
| AC1 | ops-reliability.md#R1 | C1/K1 | T1/IMP1 |
| AC2 | ops-reliability.md#R1 | C1/K1 | T1/IMP1 |
| AC3 | ops-reliability.md#R1 | C1/K1 | T2/IMP1 |
| AC4 | ops-reliability.md#R1 | C1/K1 | T3/IMP1 |
| AC5 | ops-reliability.md#R2 | C1/K2 | T4/IMP2 |
| AC6 | ops-reliability.md#R2 | C1/K2 | T4/IMP2 |
| AC7 | ops-reliability.md#R2 | C1/K2 | T4/IMP2 |
| AC8 | ops-reliability.md#R3 | C1/K3 | T6/IMP3 |
| AC9 | ops-reliability.md#R3 | C1/K3 | T7/IMP3 |
| AC10 | ops-reliability.md#R3 | C1/K3 | T8/IMP3 |
| AC11 | ops-reliability.md#R4 | C2/K4 | T9/IMP4 |
| AC12 | ops-reliability.md#R4 | C2/K4 | T9/IMP4 |
| AC13 | ops-reliability.md#R4 | C2/K4 | T10/IMP4 |
| AC14 | ops-reliability.md#R5 | C3/K5/K6 | T13/IMP5 |
| AC15 | ops-reliability.md#R5 | C3/K5/K6 | T14/IMP5 |
| AC16 | ops-reliability.md#R5 | C3/K5/K6 | T15/IMP5 |
| AC17 | ops-reliability.md#R5 | C3/K5/K6 | T15/IMP5 |
| AC18 | ops-reliability.md#R6 | C4/K5/K7 | T17/IMP6 |
| AC19 | ops-reliability.md#R6 | C4/K5/K7 | T17/IMP6 |
| AC20 | ops-reliability.md#R6 | C4/K5/K7 | T17/IMP6 |
| AC21 | ops-reliability.md#R2 | C1/K2 | T5/IMP2 |
| AC22 | ops-reliability.md#R4 | C2/K4 | T11/IMP4 |
| AC23 | ops-reliability.md#R5 | C3/K5/K6 | T16/IMP5 |
| AC25 | ops-reliability.md#R4 | C2/K4 | T12/IMP4 |
| AC26 | ops-reliability.md#R4 | C2/K4 | T11/IMP4 |
| AC27 | ops-reliability.md#R6 | C4/K5/K7 | T19/IMP6 |
| INV1 | ops-reliability.md#Не должно измениться | C1/K3 | T6/T8/IMP3 |
| INV2 | ops-reliability.md#Не должно измениться | C1/K1 | T1/T3/IMP1 |
| INV3 | ops-reliability.md#Не должно измениться | C1/K3 | T6/T8/IMP3 |
| INV4 | ops-reliability.md#Не должно измениться | C1/K2 | T4/IMP2 |
| INV5 | ops-reliability.md#Не должно измениться | C2/K4 | T9/T10/IMP4 |
| INV6 | ops-reliability.md#Не должно измениться | план | T20 |
| INV7 | ops-reliability.md#Не должно измениться | C4/K7 | T13/T16/T17/IMP5/IMP6 |
| INV8 | ops-reliability.md#Не должно измениться | C1/K1 | T2/IMP1 |
| INV9 | ops-reliability.md#Не должно измениться | C1/K3 | T6/T8/IMP3 |

AC24 в требованиях не объявлен — номер снят, поэтому в матрице 41 ID, а не 42. Это закрывает RR18 и QA4: пропуск не маскирует критерий, все объявленные AC на месте.

## Находки и остаточные риски

| ID | Важность | Влияние | Решение |
|---|---|---|---|
| RR14 / QA1 | MINOR | AC8 не называет хвост `429`; поведение R3 задано прямо | Принято: T6 проверяет именно хвост `429` → BLOCKED без backoff. Пробел приёмки, не поведения |
| RR15 / QA2 | MINOR | R4 не говорит про `flake: off`; две реализации проходят AC | Принято: K4 — единственная норма реализации, разбор сводки идёт всегда; T12 это проверяет |
| RR16 / QA3 | MINOR | Строка CLI и условие «раннер ≠ процесс» без отдельного AC | Принято: K7 называет обе поверхности, T18 их проверяет |
| RR17 | SUGGESTION | Дом ключей допуска не был назван в требованиях | Закрыто: K4 делает их ключами верхнего уровня, IMP4 вносит в `_CONTRACT_KEYS` и `docs/unit-contract.md` |
| RR18 / QA4 | SUGGESTION | Нет AC24 | Закрыто здесь: номер снят, матрица содержит 41 объявленный ID |
| FR1 | MINOR | R6 говорит «результат», K7 заполняет поля «после основной работы», T17 проверяет только успешный запуск: у `noop`-отказа две реализации пройдут | Уточнить у архитектора: поля есть у любого результата или только у запущенного. Не блокирует: путь запуска покрыт T17–T19 |
| FR2 | MINOR | В данных T4 стоят `./tests` и `$PWD/tests` без `pytest`; `detect_full_suite` на них ложь (проверено) | Уточнить у QA: писать `pytest ./tests` / `pytest $PWD/tests`. Норма R2 не страдает: там это цели, а не команды |

Остаточные риски, принятые явно:

- A2 и A4 — выбор владельца (уровень наблюдаемости P8; BLOCKED вместо salvage). В требованиях названо, что меняется при другом решении; на текущий пакет это не влияет.
- Дрейф реестра читаемых ключей ловится новым храповиком (D5, T16); сам реестр — ручной список, ошибка правки даст красный тест, не тихое расхождение.
- Doctor и хендшейк проверяются на fixture-проекте, а не на установке пользователя (T13–T19). Это ограничение доказательства, не поведения.

## Условия завершения реализации

1. Шесть IMP-юнитов по плану arch-1; каждый заканчивается своими тестами и зелёным verify. IMP1 → IMP2/IMP3, IMP5 → IMP6, IMP4 независим.
2. Все T1–T20 из ts-1 проходят буквально: ожидания — числа, имена полей, rc, отсутствие второго спавна и лишних файлов.
3. Целевая команда после реализации: `python -m pytest tests/unit/test_stage_timeout.py tests/unit/test_suite_warning_pre_spawn.py tests/unit/test_contract_xfail_gate.py tests/unit/test_doctor.py tests/unit/test_config_keys_registry.py tests/unit/test_launch_handshake.py -q`. Она планируемая: на базе `962c79d` этих файлов нет.
4. Регрессия: `tests/unit/test_signal_watch.py`, `test_pipeline_engine.py`, `test_verify_pack.py`, `test_model_check.py`, `test_process_gates.py`, `tests/negative/test_carry_diag.py`, `test_carry_suite_gate.py` зелёные. Полный прогон — только канонической командой проекта.
5. Обновляемые документы: `docs/unit-contract.md` (ключи `xfail_allow`, `skip_allow`), шаблон конфига (`automation.stage_timeout_seconds`). Выпуск версии не входит.
6. Канарейки INV6 не трогать: `proc.pid > 1` в `awf/_proc.py`, `_forbid_session_kill` в `tests/conftest.py`; их дифф — повод остановить юнит.

## Инструкция исполнителю

Порядок чтения: `ops-reliability.md` → `ops-reliability.architecture.md` → `ops-reliability.test-spec.md`; контекст — для источников и рисков. Держись scope R1–R6 и границ arch-1; T7/P8 не переписывай. Новую существенную неоднозначность возвращай владельцу, а не решай сам. READY разрешает начать реализацию, но не публикацию, не внешние изменения и не коммит без запроса.

## Ограничения проверки

Runtime не запускался: `doctor`, новые поля результата и BLOCKED-ветка проверены по коду и контрактам, не исполнением. Полного сьюта на дереве нет — это состояние зафиксировано в контексте и не создано этой сессией. Тестовая спецификация проектная: T1–T20 не пройдены.
