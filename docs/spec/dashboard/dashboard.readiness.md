# Готовность пакета: дашборд awf

Статус: READY
Ревизия: rdy-2
Входы: dashboard.md@req-6, dashboard.context.md@ctx-1, dashboard.requirements-review.md@rrv-6, dashboard.architecture.md@arch-2, dashboard.test-spec.md@ts-2, TODO-0357
База кода: e6878c3
Проверено: dashboard.context.md=ctx-1, dashboard.md=req-6, dashboard.requirements-review.md=rrv-6, dashboard.architecture.md=arch-2, dashboard.test-spec.md=ts-2, dashboard.readiness.md=rdy-2
Пакет-sha: 7bf7df16bda2406c36705ca96561a29be3077bbd154bc8763efcac8379564289

## Вердикт

READY. Ревизии согласованы, база одна (`e6878c3`), требования PASS@rrv-6.
Открытых BLOCKER/MAJOR и влияющих Q# нет: R1–R13, R16 покрыты контрактами;
AC1–AC36 и INV1–INV10 — тестами с точными ожиданиями; план IMP исполним.
Объём 10 998 ≤ 11 000, документы ≤ 2200 (AC36). Режим — финальная проверка
отдельной стадией; чужие документы не правились.

Границы: код и тесты не менялись и не запускались; клиентский JS статичен
(QA2); INV7 — из артефакта базы; GUI и сеть не использовались.

## Находки FR1–FR7 (rdy-1): что стало

| ID | Важность | Локация | Что стало | Состояние |
|---|---|---|---|---|
| FR1 | MAJOR | R8, AC20, T13, K2 | K2 `notification`, K8, `_NOTIFY_MEMORY`, IMP4, T13 | VERIFIED |
| FR2 | MAJOR | R4, AC36, D12 | AC36: шесть документов, ≤ 2200/≤ 11 000, без `finalize`; IMP8, T23 | VERIFIED |
| FR3 | MINOR | R9, AC21, K2 | K2 `stage`/`stages`, K6, IMP6, T14 | VERIFIED |
| FR4 | MINOR | R3, AC6, K2 | `blocked` закреплён за IMP1 | VERIFIED |
| FR5 | MINOR | R11, AC27, K7 | K7 «0 стадий», IMP7, T17 | VERIFIED |
| FR6 | MINOR | R3, AC7, K2 | K2 `engine_exit.path`, K4, T3 | VERIFIED |
| FR7 | MINOR | R1, AC2, D5, K8 | `reset_fact_memory()`, T1/T13 | VERIFIED |

## Находки других отчётов

| ID | Важность | Проверка | Решение |
|---|---|---|---|
| RR48 | MINOR | Счёт полей убран | VERIFIED: AC16 не менялся |
| RR49 | SUGGESTION | K5 считает любое открытие, AC29 — квота запуска | Принято: второй вкладки нет (INV9) |
| RR50/QA7 | MINOR | AC29 и K5 §7а: сервер не стартовал — вкладки нет | VERIFIED: T18-7 совпадает |
| RR51/QA8 | SUGGESTION | В verify показан текущий пак, пометка нужна только коммиту | Принято: AC18 |
| QA2 | MINOR | Заморозка — клиентский JS, T10 статичен | Принято, риск на IMP7 |
| QA4 | MINOR | Порог AC14 флейкает на CI | Принято: медиана 5 запросов, T9 |

## Матрица покрытия

| ID | Источник | Сценарий/контракт | Проверка |
|---|---|---|---|
| R1 | dashboard.md#R1 | C1 | T1/T2 |
| R2 | dashboard.md#R2 | C6 | T4 |
| R3 | dashboard.md#R3 | C1 | T3 |
| R4 | dashboard.md#R4 | C2/C10 | T5/T6/T7 |
| R5 | dashboard.md#R5 | C9 | T7/T8 |
| R6 | dashboard.md#R6 | C12 | T10 |
| R7 | dashboard.md#R7 | C7 | T12 |
| R8 | dashboard.md#R8 | C1 | T13 |
| R9 | dashboard.md#R9 | C5 | T14/T15 |
| R10 | dashboard.md#R10 | K6 | T16 |
| R11 | dashboard.md#R11 | C4 | T17 |
| R12 | dashboard.md#R12 | C8 | T18/T19 |
| R13 | dashboard.md#R13 | план | T20 |
| R14 | dashboard.md#Допущения | план | метод: отложено, AC нет |
| R16 | dashboard.md#R16 | D4 | T21 |
| AC1 | dashboard.md#R1 | C1 | T1 |
| AC2 | dashboard.md#R1 | C1 | T1 |
| AC3 | dashboard.md#R1 | C11 | T2 |
| AC4 | dashboard.md#R2 | C6 | T4 |
| AC5 | dashboard.md#R2 | C6 | T4 |
| AC6 | dashboard.md#R3 | C1 | T3 |
| AC7 | dashboard.md#R3 | C1 | T3 |
| AC8 | dashboard.md#R4 | C2 | T5 |
| AC9 | dashboard.md#R4 | C2 | T5 |
| AC10 | dashboard.md#R4 | C2 | T6 |
| AC11 | dashboard.md#R4 | C10 | T7 |
| AC12 | dashboard.md#R4 | C2 | T5 |
| AC13 | dashboard.md#R5 | C9 | T8 |
| AC14 | dashboard.md#R5 | D11 | T9 |
| AC15 | dashboard.md#R6 | C12 | T10 |
| AC16 | dashboard.md#R6 | C12 | T11 |
| AC17 | dashboard.md#R7 | C7 | T11 |
| AC18 | dashboard.md#R7 | C7 | T12 |
| AC19 | dashboard.md#R7 | C7 | T12 |
| AC20 | dashboard.md#R8 | C1 | T13 |
| AC21 | dashboard.md#R9 | C5 | T14 |
| AC22 | dashboard.md#R9 | C5 | T15 |
| AC23 | dashboard.md#R9 | C5 | T15 |
| AC24 | dashboard.md#R10 | K6 | T16 |
| AC25 | dashboard.md#R10 | K6 | T16 |
| AC26 | dashboard.md#R10 | K6 | T16 |
| AC27 | dashboard.md#R11 | C4 | T17 |
| AC28 | dashboard.md#R11 | C4 | T17 |
| AC29 | dashboard.md#R12 | C8 | T18 |
| AC30 | dashboard.md#R12 | C8 | T19 |
| AC31 | dashboard.md#R12 | C8 | T19 |
| AC32 | dashboard.md#R13 | план | T20 |
| AC33 | dashboard.md#R16 | D4 | T21 |
| AC34 | dashboard.md#R16 | D4 | T21 |
| AC35 | dashboard.md#R5 | C10 | T1/T6/T7 |
| AC36 | dashboard.md#R4 | C2 | T23 |
| INV1 | dashboard.md#Не должно | D9 | T22 |
| INV2 | dashboard.md#Не должно | план | T22 |
| INV3 | dashboard.md#Не должно | D9 | T22 |
| INV4 | dashboard.md#Не должно | D11 | T9 |
| INV5 | dashboard.md#Не должно | K2 | T22 |
| INV6 | dashboard.md#Не должно | D1 | T22 |
| INV7 | dashboard.md#Не должно | план | T22 |
| INV8 | dashboard.md#Не должно | D9 | T22 |
| INV9 | dashboard.md#Не должно | D6 | T18/T19 |
| INV10 | dashboard.md#Не должно | D11 | T9 |

## План IMP

| IMP | R | Файлы | Проверка |
|---|---|---|---|
| IMP1 | R3, R5 | `dashboard.py`, `_results.py`, `lifecycle.py` | T3, T8 |
| IMP2 | R4, R7 | `brief_sections.py`, `dashboard.py`, шаблон | T5–T7, T12 |
| IMP3 | R16 | `dashboard.py`, шаблон | T21 |
| IMP4 | R1, R8 | `_log_reader.py`, `_engine_liveness.py`, `dashboard.py`, шаблон | T1, T13 |
| IMP5 | R12 | `dashboard_open.py`, `dashboard_server.py`, `orchestrator.py`, `cmd_dashboard.py`, `tools/awf.py` | T18, T19 |
| IMP6 | R2, R9, R10 | `dashboard.py`, `worker_tree.py`, `verify.py`, шаблон | T4, T14–T16 |
| IMP7 | R6, R11 | `dashboard.py`, шаблон | T10, T17 |
| IMP8 | R13, AC14, AC36 | `USAGE*.md`, `BACKLOG.md` | T20, T9, T23 |

Порядок: IMP1 → IMP2 → IMP3/IMP4 → IMP5/IMP6/IMP7 → IMP8; IMP5
параллелен, обратных рёбер нет.

## Условия завершения реализации

- T1–T23 падают до кода; INV7 — 84 теста, канон `scripts/suite-canonical.sh`.
- AC14: числа T9 — в IMP8; пустое — T17, XSS/CORS — T22.
- AC36: `awf spec check docs/spec/dashboard` зелёный; `finalize`-файла нет.
- IMP2: `verify_facts` — единственный владелец пака и коммита; второго
  читателя в шаблоне нет (AC35).
- IMP5: `server-failed` — авто не открывает ни `http://`, ни `file://`
  (AC29, T18-7); `file://` — окно 600 с при состоявшемся старте.
- IMP7: снимок до `clear_state`; вкладка замораживает ответ (AC15, D7).

## Остаточные риски и ограничения

QA2 и QA4 приняты с владельцами. GUI, сеть и прогоны не проверялись;
runtime докажут T1–T23. Клиентский JS не исполняется, K8 — серверно.

## Открытые вопросы

Нет. A2, A3, A4, A10, A11 подтверждены архитектурой и тестами; A1, A5–A9,
A12–A14 не меняют норм сверх K1–K8.

## Маршрут исполнителю

Читать: `dashboard.md@req-6` → `dashboard.architecture.md@arch-2` →
`dashboard.test-spec.md@ts-2`; `dashboard.context.md@ctx-1` — источники.
Работать по IMP1–IMP8, scope не расширять (R14/R15 отложены). Новую
неоднозначность — владельцу; READY не разрешает публикацию.
