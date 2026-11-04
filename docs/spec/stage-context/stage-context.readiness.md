# Готовность пакета: контракт контекста стадии

Статус: READY
Ревизия: rd-1
Входы: `stage-context.context.md@ctx-2`; `stage-context.md@req-5`; `stage-context.requirements-review.md@rr-5`; `stage-context.architecture.md@arch-2`; `stage-context.test-spec.md@ts-2`; TODO-0374; `.agentic/pipelines/*.yaml`; `.agentic/roles/`; `awf/`.
База кода: `main` @ `99e98ed`; в дереве только untracked `docs/spec/stage-context/`.
Проверено: stage-context.context.md=ctx-2; stage-context.md=req-5; stage-context.requirements-review.md=rr-5; stage-context.architecture.md=arch-2; stage-context.test-spec.md=ts-2; stage-context.readiness.md=rd-1
Пакет-sha: af6c74eb72238c725a4886b7866d795d60ca970947dfa6acf17d5fa3d35032ac

## Вердикт и границы проверки

READY: пакет можно отдавать исполнителю. R1–R12, AC1–AC30, INV1–INV9, C1–C7, T1–T46 и IMP1–IMP4 связаны, открытых BLOCKER/MAJOR нет.

Ревью — отдельная стадия и сессия `agent-requirements-reviewer`; входы прочитаны целиком, rr-5 и ts-2 — навигация. Пересчитано: 15 имён в 12 пайплайнах (семь `spec-*`); 212 снимков, `implementer-close` в 30; девять ролей 134 276 байт = 94 896 символов; доктрина 2 958 байт = 1 871 символ; потолки 12 000 и 6 000 символов; 42 юнита — 25 блоков `## REVIEW-фикс` у 12, максимум 5, серия нулей 9, минимум окна из 10 — 1; QA 72–73 запуска, медиана 19,2 минуты, максимум 90,4, 19 дольше 25; 3 файла `STAGE-DIFF`, 1 отказ `COMMIT-AUDIT`.

Проверки не запускались (политика роли), артефакта `SUITE-<tree>-*.json` на дереве `d16ed2e` нет — находка для verify.

Порядок чтения: `stage-context.md@req-5` → `stage-context.architecture.md@arch-2` с планом IMP → `stage-context.test-spec.md@ts-2`; `stage-context.context.md@ctx-2` — источники и риски; `stage-context.requirements-review.md@rr-5` — история находок.

## Матрица покрытия

|ID|Источник|Сценарий/контракт|Проверка|
|---|---|---|---|
|R1|stage-context.md#R1|C1/C5/K1/K2|T1/T2/T3/T4/T5/T6/IMP1|
|R2|stage-context.md#R2|C2/K4/K11|T7/T41/T42/IMP2|
|R3|stage-context.md#R3|C1/C6/K6|T9/T10/T11/T12/T21/IMP2|
|R4|stage-context.md#R4|C1/C2/K5|T22/T23/T24/T25/IMP2|
|R5|stage-context.md#R5|C6/K4/K6|T16/T17/T18/T19/T20/IMP2|
|R6|stage-context.md#R6|C1/C2/K3|T30/IMP2|
|R7|stage-context.md#R7|C6/K9|T31/T32/T33/IMP1/IMP3|
|R8|stage-context.md#R8|C7/K4|T34/IMP3|
|R9|stage-context.md#R9|C7/K10|T35/IMP3|
|R10|stage-context.md#R10|C1/C2/C5/K3/K9|T26/T27/T28/T29/IMP3|
|R11|stage-context.md#R11|C1/C4/K7/K8|T36/T37/T38/T39/T40/IMP4|
|R12|stage-context.md#R12|C4/K8|T43/T44/T45/IMP4|
|AC1|stage-context.md#R1|C1/C5/K1|T1/T3/T4/IMP1|
|AC2|stage-context.md#R1|C1/K1|T6/IMP1|
|AC3|stage-context.md#R3|C1/C4/K6|T9/T10/T36/IMP2|
|AC4|stage-context.md#R3|C1/K3/K6|T13/T15/IMP2|
|AC5|stage-context.md#R3|C1/K6|T21/IMP2|
|AC6|stage-context.md#R4|C1/C2/K5|T22/T23/T24/T25/IMP2|
|AC7|stage-context.md#R10|C1/K3|T28/T29/IMP3|
|AC8|stage-context.md#R2|C2/K4|T7/IMP2|
|AC9|stage-context.md#R5|C6/K4|T16/T17/IMP2|
|AC10|stage-context.md#R5|C6/K6|T19/T20/IMP2|
|AC11|stage-context.md#R7|C6/K9|T31/T32/T33/IMP1/IMP3|
|AC12|stage-context.md#R8|C7/K4|T34/IMP3|
|AC13|stage-context.md#R9|C7/K10|T35/IMP3|
|AC14|stage-context.md#R10|C1/K3/K6|T26/T27/T29/IMP3|
|AC15|stage-context.md#R10|C6/K9|T8/T33/IMP1|
|AC16|stage-context.md#R10|C5/K1/K9|T2/T27/T29/IMP3|
|AC17|stage-context.md#R12|C4/K8|T43/IMP4|
|AC18|stage-context.md#R12|C1/C4/K8|T14/T44/T46/IMP4|
|AC19|stage-context.md#R11|C1/K7/K8|T38/T40/IMP4|
|AC20|stage-context.md#R12|C1/K8|T45/IMP4|
|AC21|stage-context.md#R1|C5/K2|T3/T4/IMP1|
|AC22|stage-context.md#R1|C5/K10|T5/IMP1|
|AC23|stage-context.md#R2|C2/K4/K8|T41/IMP4|
|AC24|stage-context.md#R6|C2/K3|T30/IMP2|
|AC25|stage-context.md#R11|C1/K7|T36/T37/IMP4|
|AC26|stage-context.md#R11|C1/C4/K8|T39/IMP4|
|AC27|stage-context.md#R2|C2/K11|T42/IMP2|
|AC28|stage-context.md#R12|C5/K8|T43/IMP4|
|AC29|stage-context.md#R12|C4/K8|T39/T43/IMP4|
|AC30|stage-context.md#R12|C4/K8|T43/IMP4|
|INV1|stage-context.md#Не должно измениться|C1/K10|T46/IMP3|
|INV2|stage-context.md#Не должно измениться|C2/K10|T46/IMP3|
|INV3|stage-context.md#Не должно измениться|C1/K10|T46/IMP3|
|INV4|stage-context.md#Не должно измениться|C6/K9|T8/IMP1|
|INV5|stage-context.md#Не должно измениться|C2/K9|T46/IMP3|
|INV6|stage-context.md#Не должно измениться|C1/K10|T46/IMP3|
|INV7|stage-context.md#Не должно измениться|C1/K10|T29/IMP3|
|INV8|stage-context.md#Не должно измениться|C1/K3|T29/IMP3|
|INV9|stage-context.md#Не должно измениться|C6/K9|T31/IMP1/IMP3|
## Находки

| ID | Важность | Локация | Проблема и решение | Состояние |
|---|---|---|---|---|
| RR1–RR5, RR7, RR8 | — | req-2…req-4 | Проверены, текст не менялся с rr-4 | VERIFIED |
| RR9–RR11 | — | R1, R4, R2/AC27 | Один носитель типа; маркер — заголовок `##`; правило секрета в R2 | VERIFIED |
| RR12, RR21, RR22 | MAJOR/MINOR | AC17, AC29 | Окно 10, один порог, нулевые инциденты; числа совпали с пересчётом | VERIFIED |
| RR13–RR16, RR18, RR19 | — | AC2, AC6, AC23, AC11 | Закрыты в rr-4, текст не менялся | VERIFIED |
| RR6 | SUGGESTION | arch §12, K8 | Команда одна — `awf report --context`; второго пути нет | VERIFIED |
| RR17 | MINOR | R2/AC27, arch §9 | Правило секрета: имя и файл-секрет, владелец `awf/redact.py` | VERIFIED |
| FR1 | MINOR | R2/AC27, arch §9, T42 | Литерал пометки: требования «секрет скрыт», архитектура `<redacted: имя>`; норматив — arch и T42, AC27 читаем по смыслу | OPEN (аналитик, QA) |
| FR2 | MINOR | T3, arch §4 | T3 берёт роль вместо имени стадии: `agent-project-auditor` — стадия `default`, тип `reviewer`, не `execute`; норматив — arch §4 | OPEN (QA) |
| FR3, RR23, QA11 | MINOR | AC29, T43 | Baseline инцидентов на 42 юнитах, AC29 говорит про окно 10; сравнивать на равном объёме: 42 юнита и доля окон | OPEN (аналитик) |
| FR5, QA6 | MINOR | AC9, arch §3 | `COMMIT-AUDIT` не блок текста, влияет только на отчёт; читаем AC9 как «стадия стартует» | OPEN (аналитик) |
| QA8 | MINOR | R8, T34 | Пометка о закрытых — слово «закрыт» и число, как ждёт T34 | OPEN (аналитик) |
| RR25, QA13 | SUGGESTION | AC30 | Порог «каждая QA ≤25 мин» не оформлен решением владельца | OPEN (владелец) |
| QA10 | SUGGESTION | AC28 | Порог spec-рана ≤1 — Q3 | OPEN (владелец) |
| RR20 | SUGGESTION | ctx-2 §3 | Счётчики ролей и «305 стадий» не пересчитаны; на требования не влияют | OPEN (аудитор) |
| FR4, RR24, QA12 | MINOR | AC17, arch §12 | Порядок и начало окна заданы в arch §12; в AC17 норму не дублируем | REJECTED (дубль нормы) |
| FR6 | SUGGESTION | R3, A-arch4 | Потолок `plan` 6 000 выведен из семейства `supervisor` | REJECTED (вывод верен) |

Открытые MINOR не меняют поведение: норматив назван, правка — текст следующей ревизии. Ни один не требует продуктового решения.

## Решение не создаёт переделок

- Один путь: D2; `--file`, `resolve_prev_handoffs`, ветки `build_prompt` и бюджеты verify/replan удаляются одним коммитом (IMP3). `context-pack --stdout` — предпросмотр того же `build_stage_context` (K3), не второй сборщик.
- Флага режима нет: AC14, T27 ищет `context_mode`, `legacy_context`, `AWF_CONTEXT*`.
- Схема пайплайна и форма снимка не меняются: AC15, T8, T33; снимок только читается (INV9).
- Дублей ролей нет: R4, T24; девять файлов, `*.core.md` и `*.compact.md` отсутствуют.
- Нового постоянного артефакта нет: AC7, T28; `STAGE-*.md` не пишется, `CTX` — строка существующего лога, реестр владельцев — раздел `awf report` (AC23).
- Носитель типа один: `config.yaml stage_types.<имя>` (R1, A3, D1); 15 имён и `implementer-close` одной таблицей, уточняющего ключа нет.
- Протокола самоотчёта и цепочки запасных правил нет: R1, R7, AC16.

## План IMP

| IMP | Что | Требования | Точка замера |
|---|---|---|---|
| IMP1 | тип как данные: `stage_context_types.py`, `stage_types` в конфиге, убрать `_compute_kind` | R1, R7, AC2, AC11, AC15, AC16, AC21, AC22 | ноль `StageTypeUndeclared`; 12 пайплайнов, 15 имён, 212 снимков |
| IMP2 | сборщик: `stage_context*.py`, `redact.py`, блоки, роль, обрезка | R2–R6, AC3–AC10, AC23, AC24, AC27 | `total` и `cut` ≤ потолка на трёх юнитах |
| IMP3 | один путь: `agent_stage.py`, `supervisor.py`, удалить `--file` и `resolve_prev_handoffs` | R7–R10, AC7, AC8, AC11–AC16, AC22 | три юнита; verify ≤6 000 без среза TODO |
| IMP4 | отчёт: `cmd_report.py`, `--context`, инциденты, минуты QA | R11, R12, AC19, AC25, AC26, AC28–AC30 | отчёт равен строке `CTX`, окно из 10 |

## Условия завершения реализации

- Зелёные T1–T42 и T46 по карточкам; красная фаза падает по существу.
- Регрессия INV1–INV9: `scripts/check-contracts.sh`, тесты сигналов, гейта и схемы DONE, полный `scripts/run-all.sh`.
- Замеры IMP1–IMP4 из `awf report --context`; числа отчёта равны строке `CTX`.
- T43–T45 — измерение после внедрения: окно — первые 10 юнитов после внедрения по номеру (arch §12); spec-ран ≤1 блок (AC28, Q3); инциденты на 42 юнитах и долей окон (AC29); каждая QA ≤25 минут (AC30).
- Стадии не правят чужие файлы; коммитит движок после verify.

## Открытые вопросы и остаточные риски

Q1 (клон) вне scope. Q2 (секрет в диффе) закрыт правилом R2/AC27. Q3 — порог spec-рана ≤1 (AC28): меняет число приёмки, не поведение. Q4 закрыт A8 — счёт по блокам `## REVIEW-фикс`.

Остаточные риски: размеры контекста по стадиям не перезамерены (handoff перезаписаны); байты против символов (CTX17); пороги R12 — цели после внедрения, не доказанный эффект.

## Инструкция исполнителю

Работать в scope пакета: код `awf/`, `stage_types` в `.agentic/config.yaml`; роли, пайплайны и доктрину не менять текстом. Новую существенную неоднозначность вернуть владельцу. READY не разрешает публикацию и внешние изменения.
