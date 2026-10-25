# Готовность пакета: пайплайн спецификации и передача нормы

Статус: READY
Ревизия: readiness-2
Scope: spec-pipeline
Входы: `spec-pipeline.context.md@ctx-1`, `spec-pipeline.md@req-3`, `spec-pipeline.requirements-review.md@reqrev-4`, `spec-pipeline.architecture.md@arch-3`, `spec-pipeline.test-spec.md@qa-2`, TODO-0252, протокол `specification-pipeline.md`
База кода: commit `3b7cb07`; в дереве — untracked `docs/spec/**`
Проверено: spec-pipeline.context.md=ctx-1, spec-pipeline.md=req-3, spec-pipeline.requirements-review.md=reqrev-4, spec-pipeline.architecture.md=arch-3, spec-pipeline.test-spec.md=qa-2, spec-pipeline.readiness.md=readiness-2
Пакет-sha: `111f63629826beae8f32da13b51c63d7a01f86b667fc785b08e0e6212b52555e` — пять документов (готовность исключена), пути от каталога пакета

## Вердикт

**READY.** Пакет полон, ревизии актуальны, FR1-FR7 маршрута `readiness-1` закрыты; открытых BLOCKER/MAJOR и влияющих Q# нет. К реализации допустимы IMP1-IMP8; код не написан и проверок реализации нет.

Способ ревью: отдельная стадия `agent-requirements-reviewer`, та же модель; независимость ограничена. Runtime не запускался; факты о коде — чтением. Проверены R1-R8, AC1-AC31, INV1-INV5, C1-C6, IMP1-IMP8, T1-T17, K1-K7, D1-D12, A1-A6. Гейты: `check-contracts` — ok; `ratchet` — в baseline; `instruction-gates` — 948/950 и 250/250. Бюджет: пакет 10966/11000, документы ≤2200. Вектор отпечатка `820b644e...` пересчитан и совпал; `Пакет-sha` посчитан по K1 (порядок байтов UTF-8, `путь\0sha256\n`, без нормализации).

## Проверка маршрута readiness-1

- FR1 — закрыт: `reqrev-4` несёт `Статус: PASS`, `Проверено: spec-pipeline.md=req-3`, `База кода`; содержание не менялось.
- FR2 — закрыт: матрица K1 в `arch-3` четырёхколоночная (`ID | Источник | Сценарий/контракт | Проверка`) и вмещает цепочку `ID → C#/K#/D# → T#/IMP#`; T9/T12 проверяют форму и токены; AC13/AC31 не менялись.
- FR3 — закрыт: гейт назван ровно в трёх точках R3 — создание, старт, продолжение; verify не перепроверяет готовность (D6), `verify-pack` печатает справку без стопа и коммита; T6/T15 закрепляют.
- FR4 — закрыт: форма отпечатка задана (путь от каталога пакета, `/`, порядок байтов, префикс `путь\0sha256\n`, без нормализации), вектор T4 воспроизведён.
- FR5-FR7 — приняты с основанием: AC30 — четыре ключа (A1); источник строки и пять документов — K1/T9/T4; перевыпуск готовности после нового PASS — AC5/AC6/T4; C2 историчен.

## Находки

| ID | Важность | Состояние | Проверено |
|---|---|---|---|
| FR1-FR4 | MAJOR | VERIFIED | закрыты `reqrev-4`, `arch-3`, `qa-2` по маршруту выше |
| FR5-FR7 | MINOR | ACCEPTED | безопасное чтение задано A1, K1/T9/T4, AC5/AC6; поведение не меняется |
| RR25 | MINOR | ACCEPTED | AC30 читается как четыре ключа; «назначения» нет (A1, K1) |
| RR26-RR28 | MINOR | ACCEPTED | источник обязателен (K1/T9), пять документов без готовности (K1/T4), новые отказы — только в пути пакета (§10/T16) |
| RR29 | MINOR | ACCEPTED | перевыпуск готовности требуют AC5/AC6 и T4; C2 — история сценария |

Новых находок нет. Открытых вопросов нет: Q1-Q4 сведены к R4, A5, A4, A6.

## Матрица покрытия

| ID | Источник | Сценарий/контракт | Проверка |
|---|---|---|---|
| R1 | spec-pipeline.md#R1 | C1/K7/D12 | T1/IMP7 |
| R2 | spec-pipeline.md#R2 | C1/C2/K1/D1/D5 | T2/T3/T4/IMP1 |
| R3 | spec-pipeline.md#R3 | C1/C2/C3/K2/K3/D2 | T5/T6/IMP2/IMP3 |
| R4 | spec-pipeline.md#R4 | C1/C3/K4/D3/D8 | T7/T8/IMP4 |
| R5 | spec-pipeline.md#R5 | C4/K5/D4/D9 | T10/T11/IMP5 |
| R6 | spec-pipeline.md#R6 | C5/K6/D10 | T12/T13/IMP6 |
| R7 | spec-pipeline.md#R7 | C1/C3/план | T14/T15/IMP8 |
| R8 | spec-pipeline.md#R8 | C6/K3 | T16/IMP8 |
| AC1 | spec-pipeline.md#R1 | C1/K7 | T1/IMP7 |
| AC2 | spec-pipeline.md#R1 | C1/K7 | T1/IMP7 |
| AC3 | spec-pipeline.md#R1 | C1/K7 | T1/IMP7 |
| AC4 | spec-pipeline.md#R2 | C1/K1 | T2/IMP1 |
| AC5 | spec-pipeline.md#R2 | C1/C2/K2 | T3/IMP1 |
| AC6 | spec-pipeline.md#R2 | C1/K1/D5 | T4/IMP1 |
| AC7 | spec-pipeline.md#R2 | C4/K5 | T10/IMP5 |
| AC8 | spec-pipeline.md#R3 | C3/K3 | T5/IMP2 |
| AC9 | spec-pipeline.md#R3 | C1/K3 | T5/IMP2 |
| AC10 | spec-pipeline.md#R3 | C1/K3/D5 | T4/IMP2 |
| AC11 | spec-pipeline.md#R4 | C1/K4/D8 | T7/IMP4 |
| AC12 | spec-pipeline.md#R4 | C3/K4 | T8/IMP4 |
| AC13 | spec-pipeline.md#R4 | C1/K1/K4 | T9/IMP1 |
| AC14 | spec-pipeline.md#R4 | C1/план | T13 |
| AC15 | spec-pipeline.md#R5 | C4/K5/D4 | T10/IMP5 |
| AC16 | spec-pipeline.md#R5 | C4/K5/D4 | T10/IMP5 |
| AC17 | spec-pipeline.md#R5 | C4/K5/D9 | T11/IMP5 |
| AC18 | spec-pipeline.md#R5 | C1/K5/K7 | T1/T10/IMP5/IMP7 |
| AC19 | spec-pipeline.md#R6 | C5/K6/D10 | T12/IMP6 |
| AC20 | spec-pipeline.md#R6 | C5/K6 | T13 |
| AC21 | spec-pipeline.md#R6 | C5/K6 | T12/IMP6 |
| AC22 | spec-pipeline.md#R6 | C5/K6 | T13 |
| AC23 | spec-pipeline.md#R7 | C1/план | T14/IMP8 |
| AC24 | spec-pipeline.md#R7 | C1/план | T14/IMP8 |
| AC25 | spec-pipeline.md#R7 | C1/C3/K3/K4 | T15/IMP8 |
| AC26 | spec-pipeline.md#R7 | C1/план | T14/IMP8 |
| AC27 | spec-pipeline.md#R8 | C6/K3 | T16/IMP8 |
| AC28 | spec-pipeline.md#R8 | C6/K3 | T16/IMP8 |
| AC29 | spec-pipeline.md#R3 | C2/C3/K3 | T6/IMP3 |
| AC30 | spec-pipeline.md#R2 | C1/K1 | T2/IMP1 |
| AC31 | spec-pipeline.md#R6 | C5/K1 | T12/IMP6 |
| INV1 | spec-pipeline.md#Не должно измениться | C6/K3 | T16 |
| INV2 | spec-pipeline.md#Не должно измениться | C1/K4 | T16/IMP4 |
| INV3 | spec-pipeline.md#Не должно измениться | C6/K5 | T17 |
| INV4 | spec-pipeline.md#Не должно измениться | C4/K5 | T17/IMP5 |
| INV5 | spec-pipeline.md#Не должно измениться | C5/K6 | T14 |

Исключения: AC14/AC20/AC22 — ручная рубрика T13 без IMP; INV1/INV3/INV5 — регрессия и измерение. Прочих нет.

## Ограничения и остаточные риски

- Baseline `CTX10`: `verification.baseline_cmd` не уложилась в 300 с; целевые тесты зелёные. Полный сьют — на dev-ране.
- T13 выполняется по DONE dev-рана; здесь не выполнялась — это условие завершения, не дефект спецификации.
- RSK1 и RSK3 приняты (A2/A3); RSK2/RSK4 закрыты R6 и A6.
- Формулировки RR25-RR29 приняты как MINOR: поведение задано однозначно, правка текста не требуется.
- Контекст хранит полный sha, остальные — 7 знаков; K1 сравнивает базу по префиксу от 7.
- Разделитель списка в `Входы`/`Проверено` K1 не назван: вердикт пишет `,`, авторские шапки — `;`. Разбор обязан принимать оба, иначе действительный пакет отвергается; поведение от этого не меняется.

## Порядок чтения и условия завершения

Порядок: `spec-pipeline.md` → `spec-pipeline.architecture.md` (план) → `spec-pipeline.test-spec.md`; контекст — источники и ограничения; ревью — основания PASS.

Условия завершения реализации:
- T1-T12, T15-T17 зелёные; T13 записана; T14: шесть `prove_red` красный на `3b7cb07` → зелёный, `bash scripts/run-all.sh` зелёный, бюджет инструкций не вырос.
- Отказы T5, T6, T8 не оставляют TODO, baseline, lease и стадии; сообщение называет документ, обе ревизии, для `BLOCKED` — вопрос и владельца.
- `git diff` dev-рана — только IMP1-IMP8; `.agentic/` не коммитится; `default` и `code-health` не меняются; `pid > 1` и `_forbid_session_kill` не тронуты.
- Команды: проверенные — гейты выше и 153+133 целевых теста; не запускались — `run-all.sh`, baseline; планируемые — файлы T1-T12, T15-T17.
- READY не даёт разрешения на публикацию, внешние изменения или новый scope; новая существенная неоднозначность возвращается владельцу.
