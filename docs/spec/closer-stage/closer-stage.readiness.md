# Готовность: closer-стадия Definition-of-done

Статус: READY
Ревизия: ready-1
Входы: closer-stage.context.md@ctx-1; closer-stage.md@req-4; closer-stage.requirements-review.md@review-4; closer-stage.architecture.md@arch-2; closer-stage.test-spec.md@qa-1
База кода: commit 258f28a43f91663011c3a88967f15a2dd894d4b8; рабочее дерево: untracked `docs/spec/closer-stage/`, `M awf/budget.py`, `M tests/negative/test_unit_budget.py` (RR24)
Проверено: closer-stage.context.md=ctx-1; closer-stage.md=req-4; closer-stage.requirements-review.md=review-4; closer-stage.architecture.md=arch-2; closer-stage.test-spec.md=qa-1; closer-stage.readiness.md=ready-1
Пакет-sha: 2168474a3ce4f5f9f2ef79b7ceaaa2f312bc6c395b28b48b9c643f0a2c075c2e

## Вердикт

READY. Все R1-R9, AC1-AC28 и INV1-INV6 проверяемы и покрыты; K1-K7 согласованы с требованиями; план IMP исполним. Способ — отдельная сессия `agent-requirements-reviewer` (`spec-readiness`). Прочитаны все шесть документов, `arch-2` (`arch-1` устарел), `review-4` и код базы. Авторские документы и код не менялись; runtime не проверялся — closer-кода нет.

## Порядок чтения

Требования → архитектура (K1-K7, D1-D8, IMP1-IMP7) → тест-спек (T1-T10); контекст — источники и риски; `review-4` и этот файл — история находок.

## Находки

| ID | Важность | Решение |
|---|---|---|
| RR24 | OPEN, супервизор | Грязные `awf/budget.py` и `tests/negative/test_unit_budget.py` (`review_passes`) вне scope; супервизор закрывает отдельным юнитом или исключением. |
| RR25 | MINOR, принято | AC7 называет последним писателем implementer, хотя `agent-architect-review` пишет. Позиция closer и порядок T4 однозначны, `arch-2` D8 верен; текст AC7 — в req-5 (аналитик). |
| RR26 | MINOR, принято | INV3 говорит «ретраи не тратят», AC11 — «ретрай после падения». Поведение задают AC11, K4 и T5: при пределе 2 ровно два прогона, затем BLOCKED; текст INV3 — в req-5. |
| RR27 | MINOR, принято | «Открытые вопросы» ссылаются на `arch-1`; Q1-Q6 закрыты A1-A5, актуальна `arch-2`. |
| QA1 | MINOR, принято | Имена ключей `--json` задаёт T7 (`item`, `caught`, `runs`); архитектор подтверждает при реализации. |
| QA2 | MINOR, принято | AC8 против K1: service-run снимает `write_allow` при замене `output`; closer его не объявляет, T1 проверяет исключение. |
| QA3 | MINOR, принято | AC27 называет closer, K2 обобщает на любую стадию с `skip_if`; в итерации ключ ставит только closer, T3 проверяет его. |
| QA4 | SUGGESTION, принято | Формат `dod:` для нечитаемого блока не задан; T2 требует `blocked` и маркер в `missing`. |

Открытых BLOCKER и MAJOR нет; влияющих на поведение Q# нет. REVIEW авторов и PASS ревью актуальны для названных ревизий.

## Матрица покрытия

| ID | Источник | Сценарий/контракт | Проверка |
|---|---|---|---|
| R1 | closer-stage.md#R1 | C6/K1 | T1/IMP1 |
| R2 | closer-stage.md#R2 | C1/C2/K2 | T2/T3/IMP2 |
| R3 | closer-stage.md#R3 | C1/C2/K6 | T3/T4/IMP3 |
| R4 | closer-stage.md#R4 | C3/K4 | T5/IMP4 |
| R5 | closer-stage.md#R5 | C4/K5 | T6/IMP5 |
| R6 | closer-stage.md#R6 | C5/K3/K7 | T7/IMP6 |
| R7 | closer-stage.md#R7 | C6/K6 | T4/T8/IMP7 |
| R8 | closer-stage.md#R8 | C1/C2/C3/C4/C5/C6/K1/K2/K3/K4/K5/K6/K7 | T9/IMP7 |
| R9 | closer-stage.md#R9 | план | T10 |
| AC1 | closer-stage.md#R1 | C6/K1 | T1/IMP1 |
| AC2 | closer-stage.md#R1 | C6/K1 | T1/IMP1 |
| AC3 | closer-stage.md#R1 | C6/K1 | T1/T8/IMP1 |
| AC4 | closer-stage.md#R2 | C1/C2/K2 | T2/IMP2 |
| AC5 | closer-stage.md#R2 | C1/C2/K2 | T2/T3/IMP2 |
| AC6 | closer-stage.md#R2 | C1/C2/K2 | T2/T3/IMP2 |
| AC7 | closer-stage.md#R3 | C1/C2/K6 | T4/IMP3 |
| AC8 | closer-stage.md#R3 | C1/C2/K1/K6 | T4/IMP3 |
| AC9 | closer-stage.md#R3 | C1/C2/K6 | T4/IMP3 |
| AC10 | closer-stage.md#R4 | C3/K4 | T5/IMP4 |
| AC11 | closer-stage.md#R4 | C3/K4 | T5/IMP4 |
| AC12 | closer-stage.md#R5 | C4/K5 | T6/IMP5 |
| AC13 | closer-stage.md#R5 | C4/K5 | T6/IMP5 |
| AC14 | closer-stage.md#R5 | C4/K5 | T6/IMP5 |
| AC15 | closer-stage.md#R6 | C5/K3/K7 | T7/IMP6 |
| AC16 | closer-stage.md#R6 | C5/K7 | T7/IMP6 |
| AC17 | closer-stage.md#R6 | C5/K7 | T7/IMP6 |
| AC18 | closer-stage.md#R7 | C6/K6 | T8/IMP7 |
| AC19 | closer-stage.md#R7 | C6/K6 | T8/IMP7 |
| AC20 | closer-stage.md#R8 | C1/C2/C3/C4/C5/C6 | T9/IMP7 |
| AC21 | closer-stage.md#R8 | план | T9/IMP7 |
| AC22 | closer-stage.md#R9 | план | T10 |
| AC23 | closer-stage.md#R9 | план | T10 |
| AC24 | closer-stage.md#R4 | C3/K4 | T5/IMP4 |
| AC25 | closer-stage.md#R2 | C1/C2/K3 | T2/IMP2 |
| AC26 | closer-stage.md#R2 | C2/K2 | T2/T3/IMP2 |
| AC27 | closer-stage.md#R3 | C1/C2/K2 | T3/T4/IMP3 |
| AC28 | closer-stage.md#R6 | C5/K7 | T7/IMP6 |
| INV1 | closer-stage.md#Не должно измениться | C1/C2/K2 | T2/T9/IMP2 |
| INV2 | closer-stage.md#Не должно измениться | план | T9/IMP7 |
| INV3 | closer-stage.md#Не должно измениться | C3/K4 | T5/T9/IMP4 |
| INV4 | closer-stage.md#Не должно измениться | план | T9/IMP7 |
| INV5 | closer-stage.md#Не должно измениться | план | T9/IMP7 |
| INV6 | closer-stage.md#Не должно измениться | C6/K6 | T8/T9/IMP7 |

## Условия завершения реализации

- T1-T9 зелёные, T10 записан; у каждого IMP `prove_red` красный на `258f28a` и зелёный после (новые символы — `new-api` с примечанием).
- `bash scripts/run-all.sh` — rc 0; бюджеты инструкций 948/950 и 250/250; канарейки целы.
- Старые пайплайны и юниты без блока идут прежним путём; позиции closer в `default`/`full`/`code-health` сохранены; `docs/unit-contract.md` обновлён.
- Коммит содержит только diff юнита, `.agentic/` не коммитится. На базе прошли 177 и 133 теста; T1-T9 планируемые; `awf spec check` — rc 0.

## Инструкция исполнителю

Работай строго по R#/AC#/INV# и K1-K7. Новую существенную неоднозначность возвращай владельцу, не решай сам. READY не разрешает публикацию и внешние изменения. RR24 закрывается отдельно. Семантическая правка требований делает этот READY устаревшим.
