# Готовность пакета: экономия контекста стадии и продолжение сессии

Статус: READY
Ревизия: readiness-4
Входы: `context-efficiency.context.md@ctx-2`; `context-efficiency.md@req-7`; `context-efficiency.requirements-review.md@rev-7`; `context-efficiency.architecture.md@arch-3`; `context-efficiency.test-spec.md@ts-4`; TODO-0302; протокол `specification-pipeline.md`
База кода: commit `401680bbfd568ea03793a6fcbd107180fcc49da4`; в дереве — untracked `docs/spec/context-efficiency/`
Проверено: context-efficiency.context.md=ctx-2; context-efficiency.md=req-7; context-efficiency.requirements-review.md=rev-7; context-efficiency.architecture.md=arch-3; context-efficiency.test-spec.md=ts-4; context-efficiency.readiness.md=readiness-4
Пакет-sha: 5a4f9dbabaf96b978c19f88ea6dfeacef10f88d7dadf2cc60fd76780c95c3bab

## Вердикт

**READY.** Ревизии актуальны: требования req-7, ревью rev-7 PASS, архитектура arch-3, тесты ts-4. Бюджеты проходят: документы 2198/2200/1180/2118/2185 слов, пакет 10767 при пороге 11 000. FR4–FR6 закрыты по существу: архитектура 2118/2200; R6/AC37 совпадают с K6/D13 (ключ — точный `title` и окно `time_created`, `directory` только тай-брейк); AC8 называет stage-log. У каждого R#, AC# и INV# есть контракт и проверка T#, план укладывается в IMP1–IMP6. Открытых BLOCKER/MAJOR нет.

Способ ревью: стадия `spec-readiness`, attempt 1, последовательная самопроверка в одной сессии; независимость ограничена моделью. Прочитаны требования, ревью, архитектура, тест-спека, контекст и прошлая готовность; код и база читались как факт о текущем поведении.

## Что проверено независимо

- Бюджет: `check_budgets` не даёт находок; `awf spec check` после этой ревизии проходит.
- Захват сессии: в реальной базе `session` есть `title`, `directory`, `time_created`, `time_updated` в миллисекундах; у 1661 из 1742 сессий `awf-*` `directory=/home/pklochkov` — замер D13 подтверждён. Таблицы `session_v2` в базе нет; проба K6 её допускает, ветка покрыта T14.
- Флаг: `opencode run --help` 1.19.4 показывает `-s, --session` рядом с `--agent`, `--model`, `--file`, `--title`; совместимость по смыслу остаётся за IMP5 шаг 0 и T26.
- Метрики: `_WORKER_TITLE_RE` по-прежнему с `$` (`awf/metrics.py:120`) — AC38 требует правки кода, T17 её фиксирует.
- `review_headings` отдаёт только заголовки (`awf/todos.py:22-24`); тело секций берёт новый helper (R2/K2).

## Находки

| ID | Важность | Где | Как закрыто или почему не блокирует | Владелец | Состояние |
|---|---|---|---|---|---|
| FR4 | MAJOR | архитектура; пакет | Архитектура сведена к 2118 словам, пакет к 10767; шаг бюджета проходит. | архитектор | VERIFIED |
| FR5 | MAJOR | R6/AC37; K6/D13 | R6 и AC37 переписаны под `title` и окно `time_created`; тай-брейк `directory` в T14 набор 2. | аналитик | VERIFIED |
| FR6 | MINOR | AC8; AC9 | AC8 называет stage-log в источниках, AC9 берёт из него хвост. | аналитик | VERIFIED |
| RR50 | MINOR | R8/AC31; K8/D14 | AC31 задаёт результат `reuse: k/n`; журнал `SESSIONS-<todo>.jsonl` — решение архитектуры K8/D14, закреплённое T22. Имя файла не меняет поведения, поэтому норма R8 не нужна. | архитектор | ACCEPTED |
| RR51 | MINOR | R2; AC7; AC41 | Норма — AC7: сумма секций 1/3/4/6/7/9 ≤150; K2 добавляет потолок 15 для девятой; T5 проверяет оба счёта. Скобка AC41 описывает фикстуру без девятой секции и поведение не меняет. | аналитик | ACCEPTED |
| RR52 | SUGGESTION | R7 | Возраст ≤240 минут проверяется и для `always` (K7, T20); формулировка «без порога» читается как «без порога попыток». | аналитик | ACCEPTED |
| RR53 | SUGGESTION | C3 | T13 задаёт `fix-only` и вход 1 без `--session`; сценарий не расходится с R6/AC39 в проверяемой части. | QA | ACCEPTED |
| RR54 | SUGGESTION | R6; AC37 | Пара записей одного `title` в окне и тай-брейк по `directory` покрыты T14 набор 2. | QA | VERIFIED |
| Q3 | SUGGESTION | R7 | Флаг `--session` есть в справке; семантика — IMP5 шаг 0 и T26. Негативный исход (fresh, причина в логе) описан; при несовместимости требования возвращаются аналитику. | архитектор | ACCEPTED |
| Q4 | SUGGESTION | R1/R2 | Порог 150 строк для секций 1/3/4/6/7 решён супервизором (TODO-0302, RR33); восемь юнитов — из 0250 и AC1. | владелец | ACCEPTED |

FR1–FR3 из `readiness-2` остаются VERIFIED: ключ `title`+окно в K6/D13, журнал в K8/D14, у каждого IMP1–IMP6 есть pytest-команда. RR32–RR38 сверены ревью rev-7 и остаются на месте (AC5 «не заголовки», AC6, AC17, AC43, AC1); исходные тексты этих находок утрачены с прошлой ревизией отчёта, сверка шла по `ctx-2` и отчёту rev-7.

## Матрица покрытия

| ID | Источник | Сценарий/контракт | Проверка |
|---|---|---|---|
|R1|context-efficiency.md#R1|C1/K1|T1/IMP1|
|R2|context-efficiency.md#R2|C1/C2/K2|T3/T4/IMP2|
|R3|context-efficiency.md#R3|C2/K4|T9/IMP3|
|R4|context-efficiency.md#R4|C1/C2/K5|T10/IMP4|
|R5|context-efficiency.md#R5|C1/K3|T7/IMP2|
|R6|context-efficiency.md#R6|C3/C4/K6/D13|T13/T14/IMP5|
|R7|context-efficiency.md#R7|C3/C4/K7/D12|T18/T26/IMP5|
|R8|context-efficiency.md#R8|C3/C4/K8/D14|T21/T22/IMP6|
|R9|context-efficiency.md#R9|C2/K9/D9|T8/IMP2|
|AC1|context-efficiency.md#R1|C1/K1|T1/IMP1|
|AC2|context-efficiency.md#R1|C1/K1|T1/IMP1|
|AC3|context-efficiency.md#R1|C1/K1|T2/IMP1|
|AC4|context-efficiency.md#R1|C1/K1|T1/IMP1|
|AC5|context-efficiency.md#R2|C1/C2/K2/D9|T3/T4/IMP2|
|AC6|context-efficiency.md#R2|C1/K2|T3/IMP2|
|AC7|context-efficiency.md#R2|C2/K2/D10|T5/IMP2|
|AC8|context-efficiency.md#R2|C1/K2|T6/IMP2|
|AC9|context-efficiency.md#R2|C1/K2|T6/IMP2|
|AC10|context-efficiency.md#R3|C2/K4|T9/IMP3|
|AC11|context-efficiency.md#R3|C2/K4|T9/IMP3|
|AC12|context-efficiency.md#R3|C2/K4/D8|T9/IMP3|
|AC13|context-efficiency.md#R3|C2/K4|T9/IMP3|
|AC14|context-efficiency.md#R4|C1/C2/K5|T10/IMP4|
|AC15|context-efficiency.md#R4|C1/K5|T10/IMP4|
|AC16|context-efficiency.md#R4|C1/K5/D8|T11/IMP4|
|AC17|context-efficiency.md#R4|C1/K5|T12/IMP4|
|AC18|context-efficiency.md#R5|C1/K3|T7/IMP2|
|AC19|context-efficiency.md#R5|C1/K3|T7/IMP2|
|AC20|context-efficiency.md#R5|C1/K3|T7/IMP2|
|AC21|context-efficiency.md#R6|C3/K6/D13|T13/IMP5|
|AC22|context-efficiency.md#R6|C3/K6/D6|T15/IMP5|
|AC23|context-efficiency.md#R6|C4/K6|T15/IMP5|
|AC24|context-efficiency.md#R7|C1/K7|T18/IMP5|
|AC25|context-efficiency.md#R7|C3/K7/D12|T18/T26/IMP5|
|AC26|context-efficiency.md#R7|C3/K7/D7|T19/IMP5|
|AC27|context-efficiency.md#R7|C3/C4/K7/D6|T20/IMP5|
|AC28|context-efficiency.md#R7|C3/K7/D12|T20/T26/IMP5|
|AC29|context-efficiency.md#R8|C2/C4/K8|T21/IMP6|
|AC30|context-efficiency.md#R8|C3/K8|T22/IMP6|
|AC31|context-efficiency.md#R8|C3/K8/D14|T22/IMP6|
|AC32|context-efficiency.md#R8|C4/K8|T23/IMP6|
|AC33|context-efficiency.md#R9|C2/K9|T8/IMP2|
|AC34|context-efficiency.md#R9|C2/K9|T8/IMP2|
|AC35|context-efficiency.md#R9|C2/K9|T8/IMP2|
|AC36|context-efficiency.md#R3|C1/K2|T3/IMP2|
|AC37|context-efficiency.md#R6|C3/K6/D13|T14/IMP5|
|AC38|context-efficiency.md#R6|C3/K10|T17/IMP5|
|AC39|context-efficiency.md#R6|C3/K6|T13/IMP5|
|AC40|context-efficiency.md#R6|C3/K6|T16/IMP5|
|AC41|context-efficiency.md#R2|C2/K2/D9|T4/T5/IMP2|
|AC42|context-efficiency.md#R7|C3/K7|T19/IMP5|
|AC43|context-efficiency.md#R9|C2/K9/D9|T8/IMP2|
|INV1|context-efficiency.md#Не должно измениться|C1/K5|T12/IMP4|
|INV2|context-efficiency.md#Не должно измениться|C1/C2/C3/C4/K5|T24/IMP4|
|INV3|context-efficiency.md#Не должно измениться|C1/C2/C3/C4/K5|T24/IMP4|
|INV4|context-efficiency.md#Не должно измениться|C1/C2/C3/C4/K5|T25/IMP4|
|INV5|context-efficiency.md#Не должно измениться|C1/C2/C3/C4/K8|T25/IMP4|
|INV6|context-efficiency.md#Не должно измениться|C3/K10|T17/IMP5|
|INV7|context-efficiency.md#Не должно измениться|C1/C2/C3/C4/K5|T25/IMP4|

## Ограничения проверки

- T1–T26 не выполнялись: это условия реализации. Целевые тесты базы зелёные (21 и 85), полный сьют не запускался.
- Захват сессии на живой базе не прогонялся: T14–T16 стоят на фейковой sqlite, живой путь проверяет IMP5.
- Семантика `--session` (тот же id, вложения видны) — только T26; справка подтверждает наличие флага.
- Документ требований — 2200 из 2200 слов: новая норма требует сначала освободить слова.
- Отчёты ревью и готовности перезаписываются: история находок живёт в журналах стадий.

## Условия завершения реализации

- IMP1 `test_agent_stage_prompt.py`; IMP2 `test_context_pack.py`; IMP3 `test_verify_pack_stage_context.py`; IMP4 `test_stage_context_pack.py`; IMP5 `test_session_reuse.py` (плюс T26 вручную); IMP6 `test_session_reuse_status.py` — команды планируемые, не выполнялись.
- T1–T25 зелёные с буквальными ожиданиями, T26 — ручной замер IMP5; регрессия: `test_pipeline_context.py`, `test_verify_pack.py`, `test_metrics.py`, `test_agent_stage_exit_code.py`, `test_signal_watch.py`, `test_process_gates.py`.
- `pid > 1` в `awf/_proc.py` и `_forbid_session_kill` в `tests/conftest.py` не тронуты; `bash scripts/run-all.sh` зелёный.

## Порядок чтения и инструкция исполнителю

Порядок: `context-efficiency.md` → `context-efficiency.architecture.md` (контракты K1–K10, решения D1–D14, план IMP1–IMP6) → `context-efficiency.test-spec.md`. Контекст `ctx-2` и ревью `rev-7` — источники и история. READY не даёт разрешения на новый scope: новая существенная неоднозначность возвращается владельцу, внешние изменения и публикация не следуют из этой готовности.
