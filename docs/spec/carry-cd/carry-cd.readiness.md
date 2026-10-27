# Готовность пакета: carry batch C+D

Статус: READY
Ревизия: readiness-6
Scope: carry-cd
Входы: `carry-cd.context.md@ctx-4`; `carry-cd.md@req-7`; `carry-cd.requirements-review.md@review-7`; `carry-cd.architecture.md@arch-5`; `carry-cd.test-spec.md@qa-3`; TODO-0279; TODO-0266; протокол `specification-pipeline.md`
База кода: HEAD `13fb5e4` + WIP TODO-0276 в рабочем дереве; пакет `docs/spec/carry-cd/` untracked
Проверено: carry-cd.context.md=ctx-4; carry-cd.md=req-7; carry-cd.requirements-review.md=review-7; carry-cd.architecture.md=arch-5; carry-cd.test-spec.md=qa-3; carry-cd.readiness.md=readiness-6
Пакет-sha: 9233514185900d60d1662cfbfd89b9d9045248e28d359bf4535e85afbff7be94

## Вердикт

READY. Блокеров и MAJOR нет: `review-7` PASS по `req-7`, контракты K1–K14 согласованы с R1–R13, матрица покрытия полная (54 ID), `Пакет-sha` сходится, K6 в норме — контекст 1962/2200, пакет 10983/11000. F1 закрыт на всех поверхностях: R12/AC30–AC32 = K12/D6/D11/D12 = T2 = контекст §0.

Остаточные MINOR/SUGGESTION не меняют реализуемого поведения: единица близости D6 названа в D12 (`весь хвост`), расхождение «network/transient» — только слово при одном backoff.

Способ ревью: стадия `spec-readiness` (`agent-requirements-reviewer`), отдельная сессия. Прочитаны шесть документов целиком, `budget_rows`, `spec check`, разбор матрицы и проба `awf._net.classify_failure` на строках §0 на текущем дереве. Runtime не запускался.

## Сверка входа ctx-4 после сжатия

Контекст сжат с `ctx-3` до `ctx-4` ради K6; копии `ctx-3` в дереве нет, поэтому проверял по фактам: строки провайдера на текущем дереве воспроизведены пробой (F1 `Invalid credential` → `network`; F2 `statusCode: 401` → `""`; F3 JSON-код → `""`; F6 `rc=137` → `oom-like`). Нормы `req-7` не менялись, PASS `review-7` остаётся в силе.

Сжатие потеряло навигацию, не нормы: подразделы §0.1–§0.5 исчезли, ссылки на них остались (FR17); счётчики строк и матрица замеров ушли — факты живут в §0 и `DONE-TODO-0276`, T2 закрепляет ожидания.

## Проверка маршрута

| ID | Важность | Локация | Проблема и доказательство | Последствие | Что должно измениться | Владелец | Состояние |
|---|---|---|---|---|---|---|---|
| FR1 | MAJOR | `arch-5` | Статус `REVIEW`, входы `req-7`; DRAFT заблокировал бы | — | — | архитектор | VERIFIED |
| FR2 | MAJOR | `ctx-4` §0 | Реальные строки провайдера сохранены, F1 воспроизведён пробой | Без них F1 не доказуем | — | аудитор | VERIFIED |
| FR4 | MINOR | R13/A5 | Отдельного AC на легаси-фолбэк нет; приёмка по T3/T4/K14 | — | Уточнить после READY | аналитик | ACCEPTED |
| FR7 | SUGGESTION | IMP2 | `collect_handoff` приписан `unit_contract.py`; функция в `agent_stage.py:343` | Имя файла, не контракт | Исправить имя при правке IMP2 | архитектор | OPEN |
| FR9 | MAJOR | R9/AC24/INV1 = K9/D10 = T6 | Preflight → `RUN-EVIDENCE` → `VERIFIED` → `managed_mkdir` → сигнал | — | — | архитектор/аналитик/QA | VERIFIED |
| FR10 | MINOR | `ctx-4` §2 | Навигационные номера строк не перепроверены; якоря R#/K# верны | Ссылки, не нормы | Перепроверить при следующей правке | аудитор | OPEN |
| FR11 | MAJOR | `arch-5` | 2199 ≤ 2200 | — | — | архитектор | VERIFIED |
| FR13 | BLOCKER | `ctx-4`; пакет | K6 (RR26) закрыт: 1962/2200, пакет 10983/11000 | — | — | аудитор (ctx-4) | VERIFIED |
| FR14 | MINOR | R12/AC31 | RR30: тексты R12/AC31 не называют единицу близости D6 | Решение задано: D12/A-RR30 — весь хвост; T2 закрепляет | Назвать единицу в R12/AC31 | аналитик (req-8) | ACCEPTED |
| FR15 | MINOR | AC31/K12 | QA5: AC31 говорит «network», K12 — `network`/`transient` при одном backoff | Поведение одно (`is_network_failure`) | Привести слово | аналитик | ACCEPTED |
| FR16 | MINOR | WIP 0276 | QA6: `test_carry_diag.py` (`:393-396`, `:412`, `:717-732`) пинит старое правило | IMP1 обязан перевернуть | Обновить пины в IMP1 | реализатор 0276 | OPEN |
| FR17 | MINOR | `ctx-4`; `arch-5`; `qa-3` | Ссылки на §0.1/§0.4/§0.5 после сжатия: `context.md:6,141,178`; `architecture.md:5,7`; `test-spec.md:5,51`; `requirements-review.md:5,31` | Навигация, содержание §0 на месте | Заменить на «§0» | аудитор, архитектор, QA | OPEN |
| FR18 | MINOR | `arch-5`; `qa-3` | Входы называют `ctx-3`, документ на `ctx-4` | Нормы от `req-7`, не менялись | Обновить строку входов | архитектор, QA | OPEN |
| FR19 | MINOR | `ctx-4` §0 | «Факты»: `Endpoint is unavailable` и `[unsupported_request]` записаны как «сеть»/«не сеть», а дерево даёт `""`/`network` — это цель, не замер | Читатель примет цель за измерение | Пометить колонку «текущий класс» либо «цель» | аудитор | OPEN |

Закрытые: FR3 (AC30 — лог воркера), FR5 (T8 — копия front matter), FR6 (файл есть), FR8 (TODO-0264 отозван), FR12 (входы актуальны).

## Матрица покрытия

| ID | Источник | Сценарий/контракт | Проверка |
|---|---|---|---|
| R1 | carry-cd.md#R1 | C1/K1 | T1/IMP1 |
| R2 | carry-cd.md#R2 | C2/K2 | T5/IMP3 |
| R3 | carry-cd.md#R3 | K3 | T13/IMP5 |
| R4 | carry-cd.md#R4 | K4 | T13/IMP5 |
| R5 | carry-cd.md#R5 | K5 | T8/IMP4 |
| R6 | carry-cd.md#R6 | C3/K6 | T9/IMP4 |
| R7 | carry-cd.md#R7 | K7 | T10/IMP4 |
| R8 | carry-cd.md#R8 | C4/K8 | T11/IMP5 |
| R9 | carry-cd.md#R9 | C5/K9 | T6/IMP3 |
| R10 | carry-cd.md#R10 | K10 | T12/IMP6 |
| R11 | carry-cd.md#R11 | C6/K11 | T7/IMP3 |
| R12 | carry-cd.md#R12 | C1/K12/D6/D11/D12 | T2/IMP1 |
| R13 | carry-cd.md#R13 | C7/K13/K14 | T3/T4/IMP2 |
| AC1 | carry-cd.md#R1 | C1/K1 | T1/IMP1 |
| AC2 | carry-cd.md#R1 | C1/K1 | T1/IMP1 |
| AC3 | carry-cd.md#R1 | C1/K1 | T1/IMP1 |
| AC4 | carry-cd.md#R2 | C2/K2 | T5/IMP3 |
| AC5 | carry-cd.md#R2 | C2/K2 | T5/IMP3 |
| AC6 | carry-cd.md#R3 | K3 | T13/IMP5 |
| AC7 | carry-cd.md#R3 | K3 | T13/IMP5 |
| AC8 | carry-cd.md#R4 | K4 | T13/IMP5 |
| AC9 | carry-cd.md#R4 | K4 | T13/IMP5 |
| AC10 | carry-cd.md#R4 | K4 | T13/IMP5 |
| AC11 | carry-cd.md#R4 | K4 | T13/IMP5 |
| AC12 | carry-cd.md#R5 | K5 | T8/IMP4 |
| AC13 | carry-cd.md#R5 | K5 | T8/IMP4 |
| AC14 | carry-cd.md#R5 | K5 | T8/IMP4 |
| AC15 | carry-cd.md#R5 | K5 | T8/IMP4 |
| AC16 | carry-cd.md#R6 | C3/K6 | T9/IMP4 |
| AC17 | carry-cd.md#R6 | C3/K6 | T9/IMP4 |
| AC18 | carry-cd.md#R6 | C3/K6 | T9/IMP4 |
| AC19 | carry-cd.md#R6 | C3/K6 | T9/IMP4 |
| AC20 | carry-cd.md#R7 | K7 | T10/IMP4 |
| AC21 | carry-cd.md#R7 | K7 | T10/IMP4 |
| AC22 | carry-cd.md#R8 | C4/K8 | T11/IMP5 |
| AC23 | carry-cd.md#R8 | C4/K8 | T11/IMP5 |
| AC24 | carry-cd.md#R9 | C5/K9 | T6/IMP3 |
| AC25 | carry-cd.md#R9 | C5/K9 | T6/IMP3 |
| AC26 | carry-cd.md#R10 | K10 | T12/IMP6 |
| AC27 | carry-cd.md#R10 | K10 | T12/IMP6 |
| AC28 | carry-cd.md#R11 | C6/K11 | T7/IMP3 |
| AC29 | carry-cd.md#R11 | C6/K11 | T7/IMP3 |
| AC30 | carry-cd.md#R12 | C1/K12 | T2/IMP1 |
| AC31 | carry-cd.md#R12 | C1/K12/D6 | T2/IMP1 |
| AC32 | carry-cd.md#R12 | C1/K12 | T2/IMP1 |
| AC33 | carry-cd.md#R13 | C7/K13/K14 | T3/T4/IMP2 |
| AC34 | carry-cd.md#R13 | C7/K13/K14 | T3/T4/IMP2 |
| INV1 | carry-cd.md#Не должно измениться | C2/C5/C6/C3/C4/K2/K9/K11/K6/K8/K10 | T5/T6/T7/T9/T11/T12/IMP3 |
| INV2 | carry-cd.md#Не должно измениться | C5/K9 | T6/IMP3 |
| INV3 | carry-cd.md#Не должно измениться | C4/K8 | T11/IMP5 |
| INV4 | carry-cd.md#Не должно измениться | C2/K2 | T5/T14/IMP3 |
| INV5 | carry-cd.md#Не должно измениться | план | T14 |
| INV6 | carry-cd.md#Не должно измениться | C1/K12 | T2/T14/IMP1 |
| INV7 | carry-cd.md#Не должно измениться | план | T14 |

## Открытые вопросы

| ID | Вопрос | Владелец | Влияние |
|---|---|---|---|
| RR30 | Назвать единицу близости D6 в R12/AC31 (решение — D12: весь хвост) | аналитик (req-8) | Не блокирует |
| QA5 | Слово «network» в AC31 привести к K12 | аналитик | Не блокирует |
| FR17 | Ссылки на §0.1–§0.5 после сжатия контекста | аудитор, архитектор, QA | Не блокирует |
| FR18 | Входы `arch-5`/`qa-3` называют `ctx-3` | архитектор, QA | Не блокирует |
| FR19 | Пометки «замер/цель» в §0 контекста | аудитор | Не блокирует |
| FR7 | Имя файла `collect_handoff` в IMP2 | архитектор | Не блокирует |

## Остаточные риски и ограничения

- Runtime не запускался: T1–T14 спроектированы, не пройдены; база — 103 теста и 96 целевых IMP1 (`ctx-4` §6).
- QA6: без переворота пинов WIP 0276 новые тесты дадут ложный красный; IMP1 обязан перевернуть их вместе с `_net.py`.
- FR10: номера строк §2 в `ctx-4` не перепроверены; нормы верны.
- FR17/FR18/FR19 — навигация и формулировки; реализацию не меняют.

## Порядок чтения

Требования → архитектура (K1–K14, D1–D12, IMP1–IMP6) → тест-спека (T1–T14); контекст — факты §0; `review-7` и этот файл — история.

## Условия завершения реализации

- T1–T13 зелёные, T14 пройден; `prove_red` красный на `13fb5e4`, зелёный после.
- `bash scripts/run-all.sh` rc 0; канарейки `proc.pid > 1` и `_forbid_session_kill` целы.
- Отказы не оставляют файлов, каталогов, `.ready`, baseline и `.bak`.
- Пакет в READY: `readiness-6`, `spec check` rc 0.

## Инструкция исполнителю

Работай по R#/AC#/INV# и K1–K14, порядок IMP1 → IMP6. Классы — по R12/K12: OOM, D6, auth, сеть; auth в контексте провайдера — код ИЛИ фраза, вне — код и фраза; D6 — весь хвост лога. Пины `test_carry_diag.py` перевернуть вместе с `_net.py` (QA6). R9 — preflight до `RUN-EVIDENCE`/`VERIFIED` (K9, D10, T6). Легаси-фолбэк R13 — A5, K14, T3/T4. Неоднозначность верни владельцу; READY не разрешает публикацию.
