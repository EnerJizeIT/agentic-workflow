# Готовность пакета: carry batch C+D

Статус: READY
Ревизия: readiness-4
Scope: carry-cd
Входы: `carry-cd.context.md@ctx-2`; `carry-cd.md@req-5`; `carry-cd.requirements-review.md@review-5`; `carry-cd.architecture.md@arch-4`; `carry-cd.test-spec.md@qa-2`; TODO-0266; retired `done/TODO-0243/TODO.md`; протокол `specification-pipeline.md`
База кода: commit `bdd1074`; в дереве untracked `docs/spec/carry-cd/`; дельта `b6c4258..bdd1074` — 12 файлов spec-пайплайна (включая `awf/pipeline_engine.py`), нормативные якоря R#/K# перепроверены на `bdd1074`
Проверено: carry-cd.context.md=ctx-2; carry-cd.md=req-5; carry-cd.requirements-review.md=review-5; carry-cd.architecture.md=arch-4; carry-cd.test-spec.md=qa-2; carry-cd.readiness.md=readiness-4
Пакет-sha: 2077a1196cba0f32369c19b2d3d05af507c19f6f30c0c5a99621ff271c5b1727

## Вердикт

READY. FR11 закрыт: `carry-cd.architecture.md@arch-4` — 2081 слово при пороге 2200; пакет — 9032 слова по пяти документам, с этой готовностью укладывается в 11000; `awf spec check docs/spec/carry-cd` даёт rc 0.

Сжатие не меняло норм: сравнение с сохранённой копией `arch-2` показывает только новый заголовок, правки FR9 (K9, §3, D10, IMP3, A-RR23) и удалённый навигационный §0. Ни один K#/D#/IMP# не потерян. Порядок FR9 один на всех поверхностях: R9/AC24/INV1 (`req-5`) = K9/§3/D10 (`arch-4`) = T6 (`qa-2`): preflight → `RUN-EVIDENCE` → `VERIFIED` → `managed_mkdir` → сигнал; отказ по симлинку не оставляет evidence, отпечатка и сигнала. Preflight достижим первым: `refuse_unsafe_surfaces` проходит по ещё не созданному `inbox`, а stale `CANDIDATE` только удаляется и новых файлов не создаёт.

Способ ревью: отдельная стадия `spec-readiness` (`agent-requirements-reviewer`); прочитаны шесть документов, `approve_commit` и `refuse_unsafe_surfaces` на `bdd1074`, матрица и бюджеты проверены инструментом `awf.spec_package`. Runtime не запускался; выводы о коде — чтением. Авторские файлы не менял.

## Проверка маршрута

- FR1 — VERIFIED: `arch-4` несёт `Статус: REVIEW`.
- FR2 — VERIFIED: `ctx-2` на `bdd1074`, названа дельта `b6c4258..bdd1074`; C10 (`setup.py:488`) и C13 (`:856,937`) на месте.
- FR3 — ACCEPTED MINOR: AC30 говорит «stage-log», поведение задают K12/T2 (лог воркера от `log_start_offset`); A-RR23 это фиксирует.
- FR4 — ACCEPTED MINOR: у R13/A5 нет отдельного AC на легаси-фолбэк; его проверяют T3/T4 и K14.
- FR5 — VERIFIED: T8 держит копию front matter, не путь к живому TODO.
- FR6 — ACCEPTED MINOR: `tests/unit/test_worker_exit.py` существует с TODO-0226; IMP1 его обновляет.
- FR7 — SUGGESTION: IMP2 приписывает `collect_handoff(engine_facts=…)` файлу `unit_contract.py`; функция в `agent_stage.py:328`, и `agent_stage.py` уже в IMP2. Опечатка плана, поведение не меняет.
- FR8 — MINOR, вне пакета: `inbox/TODO-0264.md` дублирует scope; супервизор отзывает или сужает его до первого dev-юнита.
- FR9 — VERIFIED: R9/AC24/C5/INV1 = K9/§3/D10 = T6, порядок и отсутствие файлов при отказе совпадают.
- FR10 — MINOR: навигационные номера строк `ctx-2` (C3, C5, C7, C13, P7) устарели; нормативные якоря R#/K# верны.
- FR11 — VERIFIED: `arch-4` — 2081 слово, порог 2200; бюджет пакета в норме.
- FR12 — SUGGESTION: `qa-2` в шапке называет вход `context.md@ctx-1`, хотя актуален `ctx-2`; проверки и база `bdd1074` от этого не устарели.

## Находки

| ID | Важность | Находка и решение | Владелец |
|---|---|---|---|
| FR1 | BLOCKER, VERIFIED | `arch-4` — REVIEW, содержание прежнее. | архитектор |
| FR2 | BLOCKER, VERIFIED | `ctx-2` на `bdd1074` с названной дельтой. | аудитор |
| FR3 | MINOR, ACCEPTED | RR23: AC30 «stage-log» против K12/T2; поведение задано, текст — после READY. | аналитик |
| FR4 | MINOR, ACCEPTED | RR24: нет отдельного AC на легаси-фолбэк; T3/T4 и K14 покрывают. | аналитик |
| FR5 | MINOR, VERIFIED | RR25: T8 — копия front matter. | тест-спека |
| FR6 | MINOR, ACCEPTED | `test_worker_exit.py` не новый; IMP1 обновляет существующий. | QA |
| FR7 | SUGGESTION | IMP2: `collect_handoff` живёт в `agent_stage.py:328`, файл уже в списке. | архитектор |
| FR8 | MINOR | `inbox/TODO-0264.md` — тот же scope; отозвать или сузить до первого dev-юнита. | супервизор |
| FR9 | MAJOR, VERIFIED | R9/AC24/C5/INV1 = K9/§3/D10 = T6: preflight до записей; отказ без новых файлов. | архитектор, аналитик, QA |
| FR10 | MINOR | `ctx-2`: устаревшие номера строк в навигационных ссылках. | аудитор |
| FR11 | MAJOR, VERIFIED | Архитектура 2081 ≤ 2200; `awf spec check` проходит шаг бюджета. | архитектор |
| FR12 | SUGGESTION | `qa-2`: в шапке `context.md@ctx-1` вместо `ctx-2`; смысл проверок не устарел. | тест-спека |

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
| R12 | carry-cd.md#R12 | C1/K12 | T2/IMP1 |
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
| AC31 | carry-cd.md#R12 | C1/K12 | T2/IMP1 |
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

Блокирующих нет. Q1 (бюджет архитектуры) закрыт `arch-4`. FR8 (`inbox/TODO-0264.md`) не влияет на пакет: супервизор закрывает дубль до первого dev-юнита.

## Остаточные риски и ограничения

- Runtime не запускался: T1–T14 спроектированы, не пройдены. Baseline — `scripts/check-contracts.sh` ok и 103 целевых теста (данные `ctx-2`); полный сьют в spec-ране не гонялся.
- AC30 говорит «stage-log», а проверка идёт по логу воркера (RR23). Исполнитель обязан следовать K12/T2; текст требований можно уточнить после READY.
- Навигационные номера строк в `ctx-2` и вход `ctx-1` в `qa-2` — устаревшие ссылки без влияния на нормы.
- FR8: параллельный `TODO-0264` с тем же scope может привести к двойной работе, если его не отозвать.

## Порядок чтения

Требования → архитектура (K1-K14, D1-D10, IMP1-IMP6) → тест-спек (T1-T14); контекст — источники и риски; `review-5` и этот файл — история находок.

## Условия завершения реализации

- T1-T13 зелёные, T14 пройден; у каждого IMP `prove_red` красный на `bdd1074` и зелёный после.
- `bash scripts/run-all.sh` — rc 0; `check-contracts.sh` — ok; бюджеты не выросли; канарейки `proc.pid > 1` и `_forbid_session_kill` целы.
- Отказы не оставляют лишних файлов, каталогов, `.ready`, baseline и `.bak`; старые проекты идут прежним путём; handoff без «Engine facts» читается.
- Перед первым dev-юнитом закрыть FR8 (TODO-0264).

## Инструкция исполнителю

Работай строго по R#/AC#/INV# и K1-K14, в порядке IMP1 → IMP6. Где AC30 говорит «stage-log», действует K12/T2: лог воркера от `log_start_offset`. Легаси-фолбэк R13 задают A5, K14 и T3/T4. Порядок R9 — preflight до `RUN-EVIDENCE`/`VERIFIED` (K9, D10, T6). Новую существенную неоднозначность верни владельцу; READY не разрешает публикацию и внешние изменения.
