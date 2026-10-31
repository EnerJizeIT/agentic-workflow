# Готовность пакета: awf 3.0 — только opencode 2.x

Статус: BLOCKED
Ревизия: ready-2
Входы: opencode2-only.context.md@ctx-1; opencode2-only.md@req-3; opencode2-only.requirements-review.md@reqrev-3; opencode2-only.architecture.md@arch-2; opencode2-only.test-spec.md@ts-2
База кода: 0384ba35ffc97ce370e6dc94bee098277d775860
Проверено: opencode2-only.context.md=ctx-1; opencode2-only.md=req-3; opencode2-only.requirements-review.md=reqrev-3; opencode2-only.architecture.md=arch-2; opencode2-only.test-spec.md=ts-2; opencode2-only.readiness.md=ready-2
Пакет-sha: d419c87addd57d848609c32606a247e695a1975888f0d443d1123e28165da931

## Вердикт

**BLOCKED.** Содержательная часть пакета готова: девять находок ready-1 (FR1–FR9) закрыты, PASS reqrev-3 актуален для req-3, у каждого R#/AC#/INV# есть контракт, проверка и IMP. Готовность держит объём: архитектура 2752 слова при пороге 2200 и пакет 11877 при пороге 11000 — оба пункта `awf spec check` считает MAJOR, поэтому rc 0 недостижим (FR15, FR16). Плюс три текстовых расхождения требований, не меняющих поведение (FR10, FR12, FR13).

Способ: стадия `agent-requirements-reviewer`, последовательная самопроверка, не независимый эксперт. Ревизии сверены машинно, схема 2.x — с `https://opencode.ai/config.json`. Код читался, awf и `opencode` не запускались, тесты не запускались; артефакта `SUITE-*.json` на `0384ba3` нет — состояние проверок остаётся открытым для verify.

## Судьба находок ready-1

| ID | Где закрыто | Состояние |
|---|---|---|
| FR1 | AC19 (бэкап только при существующем файле), K6/D5, T15(а) | VERIFIED |
| FR2 | R6/AC21 (`model` строкой — V2), K6/K8 вне `V1_KEYS`, T17 | VERIFIED |
| FR3 | K2 (managed снимает флаг и у `models`), T6 | VERIFIED |
| FR4 | AC25 (`fetch failed` — единственный свидетель), T22 | VERIFIED |
| FR5 | AC31 и T19 — «зелёный после IMP4» | VERIFIED |
| FR6 | Q1–Q3 с колонкой «Закрытие» | VERIFIED |
| FR7 | R1/AC2 фиксируют rc 0 и rc 1, T3 сверяет | VERIFIED |
| FR8 | K10 называет `model_check`; исключение G2 — по объявленному `V1_KEYS` (K6, D9) | VERIFIED |
| FR9 | K6 (deny первым, allow последними), T15(г) | VERIFIED |

## Находки

| ID | Важность | Локация / R# / AC# | Проблема и доказательство | Последствие | Что должно измениться | Владелец | Состояние |
|---|---|---|---|---|---|---|---|
| FR10 | MINOR | INV7 (`:224`), AC19 (`:81`) | INV7: «запись — только `awf init` с бэкапом (AC19–AC21)»; AC19 требует писать V2 без бэкапа, когда файла нет (было RR20) | Инвариант и критерий читаются как взаимоисключающие; тест на INV7 может потребовать бэкап отсутствующего файла | «с бэкапом, если файл существовал» | аналитик | OPEN |
| FR11 | SUGGESTION | R5 (`:76`), R6 (`:88`) | R5 перечисляет V1-ключи как `agent`, `permission`, `mcp.<имя>`, без `provider`; R6 его включает (было RR21) | Исполнитель разрешает по D9 (`V1_KEYS` с `provider`) — двусмысленности нет, расходится только текст | Дописать `provider` в R5 | аналитик | CLOSED контрактом D9; текст не исправлен |
| FR12 | MINOR | AC19 (`:81`), D5 (`:257`) | AC19 ссылается на «D5» как на политику бэкапа, но D5 требований — чистка `legacy`/`normalize`; политика — арх. D5 (было RR22) | Исполнитель ищет основание не там | Ссылка «арх. D5» | аналитик | OPEN |
| FR13 | SUGGESTION | `ctx-1` стр. 37; R1 | Контекст: «остаётся «major < 2 → отказ»»; в коде ветки нет: `launch_plan(Probe("ok",(1,19,4)))` даёт `refusal=None` (было RR24) | Исполнитель может решить, что ветка есть | «отказ на major < 2 добавляется в R1» | аналитик | OPEN |
| FR14 | SUGGESTION | AC2 (`:34`) | Неизвестная версия даёт rc 0 — обёртка считает запуск успешным (было RR23) | Для исполнителя норма однозначна; остаётся продуктовый риск | Решение владельца, если rc 0 не устраивает | владелец | CLOSED: AC2 однозначен |
| FR15 | MAJOR | `opencode2-only.architecture.md` | 2752 слова при пороге 2200 (`budget_rows`) — гейт помечает MAJOR | `spec check` не даёт rc 0, приёмка TODO не выполняется | Сжать архитектуру до 2200 (кандидаты: §2, K5, IMP-таблица) | архитектор | OPEN |
| FR16 | MAJOR | весь пакет | 11877 слов при пороге 11000; порог считает и отчёты пакета | То же: rc 0 недостижим и после FR15 (11877−552=11325) | Сократить пакет минимум на 740 слов суммарно либо решение владельца о пороге | архитектор + аналитик | OPEN |
| FR17 | MINOR | AC8 (`:47`), R2, INV3 | AC8 описывает doctor, managed-ветку AC не фиксирует (было RR14) | Поведение задано INV3 и проверено T6 — исполнителю хватает | — | ревьюер | CLOSED: INV3 + T6 |

## Открытые вопросы

| ID | Вопрос | Владелец | Влияние |
|---|---|---|---|
| FR15/FR16 | Сжимаем пакет или меняем `spec.budget.package` | архитектор, аналитик | Приёмка TODO и `spec check` |
| FR10/FR12 | Правка INV7 и ссылки на D5 в AC19 — сокращением | аналитик | Ясность инварианта |

## Матрица покрытия

| ID | Источник | Сценарий/контракт | Проверка |
|---|---|---|---|
| R1 | opencode2-only.md#R1 | C1/K1 | T1/T2/T3/T4/IMP1 |
| AC1 | opencode2-only.md#R1 | C1/K1 | T1/IMP1 |
| AC2 | opencode2-only.md#R1 | C1/K1 | T3/IMP1 |
| AC3 | opencode2-only.md#R1 | C1/K1 | T2/IMP1 |
| AC4 | opencode2-only.md#R1 | C1/K1 | T4/IMP1 |
| AC5 | opencode2-only.md#R1 | C1/K1 | T4/IMP1 |
| R2 | opencode2-only.md#R2 | C2/K2 | T5/T6/IMP1 |
| AC6 | opencode2-only.md#R2 | C2/K2 | T5/IMP1 |
| AC7 | opencode2-only.md#R2 | C2/K2 | T5/IMP1 |
| AC8 | opencode2-only.md#R2 | C2/K2 | T7/IMP1 |
| R3 | opencode2-only.md#R3 | C3/K3 | T8/T9/T10/T11/IMP2 |
| AC9 | opencode2-only.md#R3 | C3/K3 | T8/IMP2 |
| AC10 | opencode2-only.md#R3 | C3/K3 | T9/IMP2 |
| AC11 | opencode2-only.md#R3 | C3/K3 | T10/IMP2 |
| AC12 | opencode2-only.md#R3 | C3/K3 | T11/IMP2 |
| AC13 | opencode2-only.md#R3 | C7/K10 | T19/IMP4 |
| R4 | opencode2-only.md#R4 | C4/K7 | T12/IMP3 |
| AC14 | opencode2-only.md#R4 | C4/K7 | T12/IMP3 |
| AC15 | opencode2-only.md#R4 | C4/K7 | T12/IMP3 |
| R5 | opencode2-only.md#R5 | C4/K5 | T13/T14/T15/T16/IMP3 |
| AC16 | opencode2-only.md#R5 | C4/K5 | T13/IMP3 |
| AC17 | opencode2-only.md#R5 | C4/K5 | T14/IMP3 |
| AC18 | opencode2-only.md#R5 | C4/K5 | T14/IMP3 |
| AC19 | opencode2-only.md#R5 | C4/K6 | T15/IMP3 |
| AC20 | opencode2-only.md#R5 | C4/K6 | T16/IMP3 |
| R6 | opencode2-only.md#R6 | C5/K8 | T17/T18/IMP3 |
| AC21 | opencode2-only.md#R6 | C5/K8 | T17/IMP3 |
| AC22 | opencode2-only.md#R6 | C5/K8 | T18/IMP3 |
| AC23 | opencode2-only.md#R6 | C5/K8 | T18/IMP3 |
| R7 | opencode2-only.md#R7 | C6/K9 | T21/T22/IMP4 |
| AC24 | opencode2-only.md#R7 | C6/K9 | T21/IMP4 |
| AC25 | opencode2-only.md#R7 | C6/K9 | T22/IMP4 |
| R8 | opencode2-only.md#R8 | C7/план | T23/T24/IMP4 |
| AC26 | opencode2-only.md#R8 | C7/план | T23/IMP4 |
| AC27 | opencode2-only.md#R8 | C7/план | T24/IMP4 |
| R9 | opencode2-only.md#R9 | C7/план | T25/IMP4 |
| AC28 | opencode2-only.md#R9 | C7/план | T25/IMP4 |
| AC29 | opencode2-only.md#R9 | C7/план | T25/IMP4 |
| AC30 | opencode2-only.md#R9 | C7/план | T25/IMP4 |
| R10 | opencode2-only.md#R10 | C7/K10 | T19/T20/IMP4 |
| AC31 | opencode2-only.md#R10 | C7/K10 | T19/IMP4 |
| AC32 | opencode2-only.md#R10 | C7/K10 | T20/IMP4 |
| AC33 | opencode2-only.md#R10 | C7/K10 | T19/IMP4 |
| R11 | opencode2-only.md#R11 | C8/K11 | T26/IMP5 |
| AC34 | opencode2-only.md#R11 | C8/K11 | T26/IMP5 |
| AC35 | opencode2-only.md#R11 | C8/K11 | T26/IMP5 |
| AC36 | opencode2-only.md#R11 | C8/K11 | T26/IMP5 |
| R12 | opencode2-only.md#R12 | C7/K12 | T27/IMP6 |
| AC37 | opencode2-only.md#R12 | C7/K12 | T27/IMP6 |
| AC38 | opencode2-only.md#R12 | C7/K12 | T27/IMP6 |
| INV1 | opencode2-only.md#Не должно измениться | C1/K1 | T28/IMP1 |
| INV2 | opencode2-only.md#Не должно измениться | C1/K1 | T2/IMP1 |
| INV3 | opencode2-only.md#Не должно измениться | C2/K2 | T6/IMP1 |
| INV4 | opencode2-only.md#Не должно измениться | C2/K2 | T29/IMP1 |
| INV5 | opencode2-only.md#Не должно измениться | C6/K9 | T22/IMP4 |
| INV6 | opencode2-only.md#Не должно измениться | C7/план | T30/IMP4 |
| INV7 | opencode2-only.md#Не должно измениться | C4/K6 | T16/IMP3 |
| INV8 | opencode2-only.md#Не должно измениться | C3/K3 | T9/IMP2 |
| INV9 | opencode2-only.md#Не должно измениться | C1/K1 | T30/IMP1 |

Пробелов нет: 59 объявленных ID — 59 строк. T14 и T26 ручные, результат в отчётах IMP3/IMP5.

## Условия завершения

1. Закрыть FR15 и FR16 (объём) и FR10, FR12 (текст требований).
2. `awf spec check docs/spec/opencode2-only` → rc 0, READY.
3. Зелёные T1–T13, T15–T25, T27–T30; T14 и T26 — с результатом в IMP3/IMP5.
4. Регрессия: сьюты IMP1–IMP3, `scripts/check-contracts.sh`, `scripts/ratchet.sh`.

## Риски

Архитектура превышает порог на 552 слова — сжатие может задеть контракты K5/K6. Требования стоят на 2199 из 2200: любая правка FR10/FR12 требует равного сокращения. `fetch failed` и действие `external_directory` подтверждаются только пробами в IMP3/IMP4. Метрики v1 теряются осознанно; проекты на 1.x получат отказ старта.

## Инструкция исполнителю

Работай по R#/AC#/K#/T#, scope не расширяй, порядок IMP1 → IMP6 обязателен. Новую неоднозначность возвращай владельцу. READY не разрешает публикацию и правку чужого конфига.
