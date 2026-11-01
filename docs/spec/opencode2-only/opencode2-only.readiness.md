# Готовность пакета: awf 3.0 — только opencode 2.x

Статус: READY
Ревизия: ready-3
Входы: opencode2-only.context.md@ctx-1; opencode2-only.md@req-4; opencode2-only.requirements-review.md@reqrev-4; opencode2-only.architecture.md@arch-3; opencode2-only.test-spec.md@ts-3
База кода: 0384ba35ffc97ce370e6dc94bee098277d775860; в дереве только `docs/spec/opencode2-only/`
Проверено: opencode2-only.context.md=ctx-1; opencode2-only.md=req-4; opencode2-only.requirements-review.md=reqrev-4; opencode2-only.architecture.md=arch-3; opencode2-only.test-spec.md=ts-3; opencode2-only.readiness.md=ready-3
Пакет-sha: 729a83c948caf1438b3ae9ef88c6c7593352807b17282d13525913ff898f02d5

## Вердикт

READY для реализации. Открытых BLOCKER/MAJOR нет: находки ready-2 закрыты, объём пакета приведён к порогам (архитектура 2167, пакет 10110 слов). Пакет описывает awf 3.0 — только opencode ≥ 2, без обратной совместимости.

Способ: стадия `agent-requirements-reviewer`, последовательная самопроверка, не независимый эксперт. Код читался, awf и `opencode` не запускались, тесты не запускались; артефакта `SUITE-*.json` на `0384ba3` нет — состояние проверок остаётся открытым для verify.

## Судьба находок

| ID | Где закрыто | Состояние |
|---|---|---|
| FR1 / RR20 | AC19, K6, D5: бэкап только при существующем файле | VERIFIED |
| FR2 / RR21 | R6/AC21, K6/K8 вне `V1_KEYS`, T17 | VERIFIED |
| FR3 | K2 (managed снимает флаг и у `models`), T6 | VERIFIED |
| FR4 | AC25 (`fetch failed` — единственный свидетель), T22 | VERIFIED |
| FR5 | AC31 и T19 — «зелёный после IMP4» | VERIFIED |
| FR6 | Q1–Q3 с колонкой «Закрытие» | VERIFIED |
| FR7 | R1/AC2 фиксируют rc 0 и rc 1, T3 сверяет | VERIFIED |
| FR8 | K10 называет `model_check`; исключение G2 — по объявленному `V1_KEYS` | VERIFIED |
| FR9 | K6 (deny первым, allow последними), T15 | VERIFIED |
| FR10 | INV7 согласован с AC19: «с бэкапом, если файл существовал» | VERIFIED |
| FR12 | AC19 ссылается на «арх. D5» | VERIFIED |
| FR13 | R1 прямо называет отсутствующую ветку отказа на major < 2 | VERIFIED |
| FR15 | Архитектура сжата до 2167 слов при пороге 2200 | VERIFIED |
| FR16 | Пакет 10110 слов при пороге 11000 | VERIFIED |
| FR11, FR14, FR17 | Текст R5, AC2 и INV3/T6 | CLOSED контрактом |
| QA3 | AC17/AC18 без живой пробы; изоляцию доказывает T14, результат — в отчёте IMP3 | CONDITION |
| QA5 | AC29 машинно не проверить: T25 плюс ревью IMP4 | CONDITION |

## Матрица покрытия

| ID | Источник | Сценарий/контракт | Проверка |
|---|---|---|---|
| R1 | opencode2-only.md#R1 | C1/K1 | T1/IMP1 |
| AC1 | opencode2-only.md#R1 | C1/K1 | T1/IMP1 |
| AC2 | opencode2-only.md#R1 | C1/K1 | T3/IMP1 |
| AC3 | opencode2-only.md#R1 | C1/K1 | T2/IMP1 |
| AC4 | opencode2-only.md#R1 | C1/K1 | T4/IMP1 |
| AC5 | opencode2-only.md#R1 | C1/K1 | T4/IMP1 |
| R2 | opencode2-only.md#R2 | C2/K2 | T5/IMP1 |
| AC6 | opencode2-only.md#R2 | C2/K2 | T5/IMP1 |
| AC7 | opencode2-only.md#R2 | C2/K2 | T5/IMP1 |
| AC8 | opencode2-only.md#R2 | C2/K2 | T7/IMP1 |
| R3 | opencode2-only.md#R3 | C3/K3 | T8/IMP2 |
| AC9 | opencode2-only.md#R3 | C3/K3 | T8/IMP2 |
| AC10 | opencode2-only.md#R3 | C3/K3 | T9/IMP2 |
| AC11 | opencode2-only.md#R3 | C3/K3 | T10/IMP2 |
| AC12 | opencode2-only.md#R3 | C3/K3 | T11/IMP2 |
| AC13 | opencode2-only.md#R3 | C7/K10 | T19/IMP4 |
| R4 | opencode2-only.md#R4 | C4/K7 | T12/IMP3 |
| AC14 | opencode2-only.md#R4 | C4/K7 | T12/IMP3 |
| AC15 | opencode2-only.md#R4 | C4/K7 | T12/IMP3 |
| R5 | opencode2-only.md#R5 | C4/K5 | T13/IMP3 |
| AC16 | opencode2-only.md#R5 | C4/K5 | T13/IMP3 |
| AC17 | opencode2-only.md#R5 | C4/K5 | T14/IMP3 |
| AC18 | opencode2-only.md#R5 | C4/K5 | T14/IMP3 |
| AC19 | opencode2-only.md#R5 | C4/K6 | T15/IMP3 |
| AC20 | opencode2-only.md#R5 | C4/K6 | T16/IMP3 |
| R6 | opencode2-only.md#R6 | C5/K8 | T17/IMP3 |
| AC21 | opencode2-only.md#R6 | C5/K8 | T17/IMP3 |
| AC22 | opencode2-only.md#R6 | C5/K8 | T18/IMP3 |
| AC23 | opencode2-only.md#R6 | C5/K8 | T18/IMP3 |
| R7 | opencode2-only.md#R7 | C6/K9 | T21/IMP4 |
| AC24 | opencode2-only.md#R7 | C6/K9 | T21/IMP4 |
| AC25 | opencode2-only.md#R7 | C6/K9 | T22/IMP4 |
| R8 | opencode2-only.md#R8 | C7/K10 | T23/IMP4 |
| AC26 | opencode2-only.md#R8 | C7/K10 | T23/IMP4 |
| AC27 | opencode2-only.md#R8 | C7/K10 | T24/IMP4 |
| R9 | opencode2-only.md#R9 | C7/план | T25/IMP4 |
| AC28 | opencode2-only.md#R9 | C7/план | T25/IMP4 |
| AC29 | opencode2-only.md#R9 | C7/план | T25/IMP4 |
| AC30 | opencode2-only.md#R9 | C7/план | T25/IMP4 |
| R10 | opencode2-only.md#R10 | C7/K10 | T19/IMP4 |
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
| INV9 | opencode2-only.md#Не должно измениться | C7/план | T30/IMP4 |

Пробелов нет: 59 объявленных ID — 59 строк. T14 и T26 ручные, результат в отчётах IMP3/IMP5.

## Условия завершения

1. Зелёные T1–T13, T15–T25, T27–T30; T14 и T26 — с результатом в IMP3/IMP5.
2. `awf spec check docs/spec/opencode2-only` → rc 0, READY.
3. Регрессия: сьюты IMP1–IMP3, `scripts/check-contracts.sh`, `scripts/ratchet.sh`.
4. Порядок IMP1 → IMP6 обязателен: IMP2–IMP4 зависят от версии и флага.

## Риски

Метрики v1 теряются осознанно; проекты на 1.x получат отказ старта — это и есть ответ. V1-конфиг владельца читается как WARN, не правится; перевод — ручной шаг R11 с бэкапом. `fetch failed` и действие `external_directory` подтверждаются только пробами IMP3/IMP4. Сжатие пакета задело прозу, не контракты: K1–K12 и D1–D9 сохранены.

## Инструкция исполнителю

Работай по R#/AC#/K#/T#, scope не расширяй, порядок IMP1 → IMP6 обязателен. Не ослабляй guard `proc.pid > 1` и tripwire. Новую неоднозначность возвращай владельцу, а не решай сам. READY не разрешает публикацию и правку чужого конфига без бэкапа.
