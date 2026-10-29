# Готовность пакета: адаптация awf под opencode 2.x

Статус: READY
Ревизия: ready-1
Входы: opencode2.context.md@ctx-2; opencode2.md@req-5; opencode2.requirements-review.md@reqrev-5; opencode2.architecture.md@arch-1; opencode2.test-spec.md@ts-1
База кода: commit 4a15afccea417216d35e547bdeb20ea7eb0e22b2; в дереве только untracked docs/spec/opencode2/
Проверено: opencode2.context.md=ctx-2; opencode2.md=req-5; opencode2.requirements-review.md=reqrev-5; opencode2.architecture.md=arch-1; opencode2.test-spec.md=ts-1; opencode2.readiness.md=ready-1
Пакет-sha: cd5ce79bc6693e22dd8888a38f29c430c023b0ed1e52b8309063a984f60f06f6

## Вердикт

READY для реализации. Открытых BLOCKER/MAJOR нет; два MINOR и два SUGGESTION не меняют поведение и закрываются по ходу IMP-плана. Q1 (managed) закрыт архитектором D4: признак — явный opt-in `opencode.managed`, по умолчанию `false`, поэтому текущее поведение не меняется. RR32–RR35 закрыты и проверены.

Режим: финальная проверка пакета (этап 5 протокола), после `arch-1` и `ts-1`.
Способ ревью: отдельная стадия пайплайна (`agent-requirements-reviewer`); это не второй независимый эксперт.

Границы: прочитаны все шесть документов на указанных ревизиях. Runtime awf не запускался; `opencode` 1.19.4 проверен только на разбор аргументов. Машинные проверки — ниже.

## Машинные проверки

- `awf spec check` до записи этого файла: rc 1, причина — нет документа readiness (ожидаемо для стадии 7 из 8).
- 56 объявленных ID: R1–R8, AC1–AC41 без пропусков, INV1–INV7.
- Бюджеты до readiness: context 1903, requirements 2200, review 827, architecture 2043, test-spec 2036; порог документа 2200.
- Все шесть шапок разбираются; `База кода` совпадает (`4a15afc`); ревизии входов совпадают с заявленными.
- `agent_workflow_ui` объявляет зависимость `awf>=2.1.0` — импорт общего ридера K6 выполним.
- Функции, на которые ссылается K3, существуют: `_start_pipeline`, `_continue_pipeline`, `_run_next`, `run_start`, `run_service_start`.

## Порядок чтения

`opencode2.md` (нормы R1–R8, AC1–AC41, INV1–INV7) → `opencode2.architecture.md` (контракты K1–K10, решения D1–D10, план IMP1–IMP6) → `opencode2.test-spec.md` (T1–T21 и условия приёмки) → `opencode2.context.md` (факты и риски) → `opencode2.requirements-review.md` (история находок RR1–RR36).

## Сводка проверки

Требования: каждая R# имеет источник, AC и сценарий C#; основной путь и исключения задают результат; повторного счёта, нулей вместо `n/a` и «молчаливых» отказов нет. Архитектура: каждый R#/AC#/INV# сопоставлен контракту и точке изменения; отказ fail-closed стоит до lease, baseline, TODO и `current` на всех входах; kill остаётся групповым, guard и tripwire не трогаются. Тесты: у каждого AC есть T# с конкретным входом, ожиданием и наблюдаемой точкой; исключение — AC6 (FR2).

Риски ниже уровня MAJOR приняты явно:

- Managed (D4): opt-in не подтверждён владельцем отдельным сообщением; default `false` сохраняет текущее поведение. В managed-режиме изоляция env не гарантирована — это ограничение принимается владельцем при включении.
- AC39: на 1.x с нечитаемой версией старт теперь отказывает. Это осознанное изменение поведения (fail-closed), его нужно назвать в заметках к релизу.
- A6/A7 и реальные фикстуры 2.1.4 ждут IMP1; до пробы T9, T11, T16, T18 идут `skip` с причиной, а не зелёным.

## Находки

| ID | Важность | Локация / R# / AC# | Проблема и доказательство | Последствие | Что должно измениться | Владелец | Состояние |
|---|---|---|---|---|---|---|---|
| FR1 | MINOR | R2 (`:56-58`), AC12 (`:66`), D4 (`:227`); arch D4/K4 | Требования говорят «managed при открытом Q1», но Q1 закрыт решением архитектора D4. Норма managed теперь живёт в K4, а не в R2. Поведение совпадает: opt-in → предупреждение, без opt-in → отказ K3. | Читатель требований видит мёртвую ветку и ищет ответ в архитектуре; две редакции одной нормы разойдутся при следующей правке. | Переписать R2/AC12/D4 на `opencode.managed` и `AWF_OPENCODE_MANAGED`, убрать «при открытом Q1». | аналитик | OPEN |
| FR2 | MINOR | AC6 (`:45`), K1 (`:40`), T4 (`:49-54`) | T4 ожидает «`--version` вызван один раз на процесс», но шим и стаб по K1 не пишут счётчик и `invocation.log`. Наблюдаемой точки нет; AC6 пока недоказуем. | Проверка счётчика выродится в доверие к коду или исчезнет из прогона. | В T4 добавить тестовый фейк, который дописывает строку в файл-счётчик, и ожидание «одна строка на процесс, две у background-ребёнка». | QA | OPEN |
| FR3 | SUGGESTION | `opencode2.md` (`:36`), бюджет | Требования стоят ровно 2200/2200. Любая правка FR1 упрётся в порог `spec check`. | Исправление wording превратится в перепечатку требований. | Сжать раздел «Текущее поведение» на 21+ слово вместе с правкой FR1. | аналитик | OPEN |
| FR4 | SUGGESTION | R2 (`:56`), AC13 (`:67`), K3 (`:72`), T6 (`:63-68`) | K3 добавляет пятый вход `run-start`, в R2 и AC13 его нет; T6 вместо `start` берёт `run_start` (сам `start` — в T5). Поведение безопаснее, но состав входов описан в двух местах по-разному. | При следующем входе в API легко забыть preflight: список в требовании неполон. | Назвать `run-start` в R2 и AC13 либо явно записать «список входов неполон, см. K3». | аналитик | OPEN |

## Матрица покрытия

| ID | Источник | Сценарий/контракт | Проверка |
|---|---|---|---|
| R1 | opencode2.md#R1 | C1/K1/K2 | T1 |
| R2 | opencode2.md#R2 | C2/K3/K4 | T5 |
| R3 | opencode2.md#R3 | C3/K9 | T9 |
| R4 | opencode2.md#R4 | C4/K5 | T10 |
| R5 | opencode2.md#R5 | C5/K6 | T14 |
| R6 | opencode2.md#R6 | C6/K7 | T17 |
| R7 | opencode2.md#R7 | C7/K8 | T18 |
| R8 | opencode2.md#R8 | C8/K10 | T19 |
| AC1 | opencode2.md#R1 | C1/K2 | T1 |
| AC2 | opencode2.md#R1 | C1/K2 | T1 |
| AC3 | opencode2.md#R1 | C1/K2 | T3 |
| AC4 | opencode2.md#R1 | C1/K1 | T4 |
| AC5 | opencode2.md#R1 | C1/K2 | T2 |
| AC6 | opencode2.md#R1 | C1/K1 | T4 |
| AC7 | opencode2.md#R1 | C1/K1 | T4 |
| AC8 | opencode2.md#R2 | C2/K3 | T5 |
| AC9 | opencode2.md#R2 | C2/K3 | T5 |
| AC10 | opencode2.md#R2 | C2/K3 | T3 |
| AC11 | opencode2.md#R2 | C2/K2 | T1 |
| AC12 | opencode2.md#R2 | C2/K4 | T7 |
| AC13 | opencode2.md#R2 | C2/K3 | T6 |
| AC14 | opencode2.md#R3 | C3/K9 | T9 |
| AC15 | opencode2.md#R3 | C3/K9 | T9 |
| AC16 | opencode2.md#R3 | C3/K9 | T9 |
| AC17 | opencode2.md#R4 | C4/K5 | T10 |
| AC18 | opencode2.md#R4 | C4/K5 | T11 |
| AC19 | opencode2.md#R4 | C4/K5 | T11 |
| AC20 | opencode2.md#R4 | C4/K5 | T10 |
| AC21 | opencode2.md#R4 | C4/K5 | T12 |
| AC22 | opencode2.md#R4 | C4/K5 | T12 |
| AC23 | opencode2.md#R5 | C5/K6 | T14 |
| AC24 | opencode2.md#R5 | C5/K6 | T14 |
| AC25 | opencode2.md#R5 | C5/K6 | T15 |
| AC26 | opencode2.md#R5 | C5/K6 | T15 |
| AC27 | opencode2.md#R5 | C5/K6 | T16 |
| AC28 | opencode2.md#R6 | C6/K7 | T17 |
| AC29 | opencode2.md#R6 | C6/K7 | T17 |
| AC30 | opencode2.md#R6 | C6/K7 | T17 |
| AC31 | opencode2.md#R7 | C7/K8 | T18 |
| AC32 | opencode2.md#R7 | C7/K8 | T18 |
| AC33 | opencode2.md#R8 | C8/K10 | T19 |
| AC34 | opencode2.md#R8 | C8/K10 | T19 |
| AC35 | opencode2.md#R8 | C8/K3 | T5 |
| AC36 | opencode2.md#R4 | C4/K5 | T13 |
| AC37 | opencode2.md#R1 | C1/K2 | T2 |
| AC38 | opencode2.md#R1 | C1/K1 | T4 |
| AC39 | opencode2.md#R2 | C2/K3 | T3 |
| AC40 | opencode2.md#R2 | C2/K3 | T8 |
| AC41 | opencode2.md#R4 | C4/K5 | T13 |
| INV1 | opencode2.md#R1 | C1/K2 | T1/T21 |
| INV2 | opencode2.md#R8 | C8/K10 | T19/T21 |
| INV3 | opencode2.md#R4 | C4/K5 | T10/T12 |
| INV4 | opencode2.md#R3 | C3/K9 | T9/T20 |
| INV5 | opencode2.md#Не должно измениться | план | T20 |
| INV6 | opencode2.md#R7 | C7/K8 | T18 |
| INV7 | opencode2.md#R8 | C8/K10 | T21 |

## Условия завершения реализации

- Обязательны T1–T21 по разбивке IMP2–IMP6; тесты без фикстуры IMP1 — `skip` с причиной, не зелёный результат.
- `scripts/suite-canonical.sh` зелёный на итоговом дереве; canary kill и tripwire на месте (INV4); `awf/opencode_agents.py` не меняется (INV5).
- IMP1 выполняется до IMP4–IMP6; отчёт пробы и фикстуры лежат в `/tmp/opencode/**`, реальные `~/.config` и `~/.local/share` не тронуты.
- Перед IMP3 FR1 и FR4 закрывает аналитик; FR2 закрывает QA вместе с T4. FR3 — при первой правке требований.

## Инструкция исполнителю

Работай только по R1–R8 и K1–K10; не добавляй MCP-тулы, зависимости и миграции. Не ослабляй guard `proc.pid > 1` и tripwire. Новую существенную неоднозначность возвращай владельцу, а не решай сам. READY не даёт разрешения на публикацию, установку 2.1.4 вне IMP1 или изменение системного opencode.
