# Готовность пакета: контракт контекста стадии

Статус: READY
Ревизия: rd-3 (req-9, rr-9, arch-4, ts-4, ctx-3; заменяет rd-2)
Входы: `context@ctx-3`; `requirements@req-9`; `requirements-review@rr-9`; `architecture@arch-4`; `test-spec@ts-4`; TODO-0374/0377; `.agentic/pipelines/*.yaml`; `.agentic/roles/`; `.agentic/context/PIPELINE-*.yaml`; `.agentic/done/TODO-0320…0370`; `awf/`.
База кода: `main` @ `c523183`; в дереве правки шести документов пакета; код, роли, пайплайны и конфиг не менялись.
Проверено: stage-context.context.md=ctx-3; stage-context.md=req-9; stage-context.requirements-review.md=rr-9; stage-context.architecture.md=arch-4; stage-context.test-spec.md=ts-4; stage-context.readiness.md=rd-3
Пакет-sha: fce994a33671af4e3191b9766e4bd980d7487fae03f9290f7a555eb161b4a6e6

## Вердикт и границы проверки

READY. Гейт `awf spec check docs/spec/stage-context` проходит: шесть документов на одной базе `c523183`, rr-9 PASS покрывает req-9, готовность покрывает все шесть ревизий, `Пакет-sha` сходится. Открытых BLOCKER/MAJOR нет; остались MINOR и SUGGESTION (FR18–FR23), поведение они не меняют.

Ревью — отдельная стадия и сессия `agent-requirements-reviewer`; rr-9, ts-4 и rd-2 прочитаны как навигация, не как доказательство. Пересчитано по файлам, снимкам и логу: 16 имён стадий в 12 пайплайнах; все 16 пар R14 сходятся; 9 файлов `.agentic/roles`, `*.core.md`/`*.compact.md` нет; 213 снимков, `implementer-close` в 30; P0 TODO-0374 = 1 569 + 2 374 + 400 + 350 + 800 = 5 493 при пределах 5 850; verify TODO-0370 = 8 773 ≤ 9 000 при прежних 11 983; суммы пределов 5 600 / 9 000 / 7 900 / 5 150 / 9 000 / 9 000; ключа `stage_types` в `.agentic/config.yaml` нет; `context_mode`, `legacy_context`, `AWF_CONTEXT*` в коде и конфигах отсутствуют.

Тесты не запускались (политика роли). Артефакта `.agentic/context/SUITE-<tree>-*.json` для текущего дерева нет — находка для verify. `ruff check .` — All checks passed.

## Матрица покрытия

|ID|Источник|Сценарий/контракт|Проверка|
|---|---|---|---|
|R1|stage-context.md#R1|C1/C4/K3/D1|T1/T4/T8/T19/IMP1|
|R2|stage-context.md#R2|C2/K5/K12/D5|T1/T11/T45/T46/IMP2|
|R3|stage-context.md#R3|C1/C6/K8/D4|T13/T14/T15/T16/IMP2|
|R4|stage-context.md#R4|C1/K6/D3|T30/T31/T32/IMP2|
|R5|stage-context.md#R5|C8/K8/D4|T17/T26/T27/T28/IMP2|
|R6|stage-context.md#R6|C2/K5|T12/IMP2|
|R7|stage-context.md#R7|C4/K10|T6/T38/T39/IMP1|
|R8|stage-context.md#R8|C7/K5|T40/IMP2|
|R9|stage-context.md#R9|C7/K5/D7|T41/IMP2|
|R10|stage-context.md#R10|C1/K7/D2|T18/T34/T35/T36/IMP2|
|R11|stage-context.md#R11|C1/K9|T33/T42/T43/IMP3|
|R12|stage-context.md#R12|C1/D9|T47/T48/T49/T50/IMP3|
|R13|stage-context.md#R13|C6/K7/D2|T25/T36/IMP2|
|R14|stage-context.md#R14|C4/K1/D1|T2/T3/IMP1|
|AC1|stage-context.md#R1|C5/K3/D1|T1/T8/T19/IMP1|
|AC2|stage-context.md#R1|C4/K1|T2/T10/IMP1|
|AC3|stage-context.md#R3|C6/K8|T13/T17/IMP2|
|AC4|stage-context.md#R3|C8/K8/D4|T19/T20/T21/IMP2|
|AC5|stage-context.md#R3|C1/K8/D6|T29/IMP2|
|AC6|stage-context.md#R4|C1/K6/D3|T30/T31/T32/IMP2|
|AC7|stage-context.md#R10|C6/K7|T36/IMP2|
|AC8|stage-context.md#R2|C2/K5|T11/IMP2|
|AC9|stage-context.md#R5|C1/K5|T23/T24/IMP2|
|AC10|stage-context.md#R5|C8/K8/D4|T26/T28/IMP2|
|AC11|stage-context.md#R7|C4/K10|T6/T38/IMP1|
|AC12|stage-context.md#R8|C7/K5|T40/IMP2|
|AC13|stage-context.md#R9|C7/K5/D7|T41/IMP2|
|AC14|stage-context.md#R10|C1/K7/D2|T4/T18/T35/T37/IMP2|
|AC15|stage-context.md#R7|C6/K10|T39/IMP1|
|AC16|stage-context.md#R10|C1/K7/D2|T37/IMP2|
|AC17|stage-context.md#R12|C1/D9|T48/IMP4|
|AC18|stage-context.md#R12|C1/D9|T49/IMP4|
|AC19|stage-context.md#R11|C1/K9|T42/T43/IMP3|
|AC20|stage-context.md#R12|C1/D9|T50/IMP1|
|AC21|stage-context.md#R1|C5/K3|T5/T8/IMP1|
|AC22|stage-context.md#R1|C1/K11|T1/T9/IMP2|
|AC23|stage-context.md#R2|C2/K5|T45/IMP3|
|AC24|stage-context.md#R6|C2/K5|T12/IMP2|
|AC25|stage-context.md#R11|C1/K9|T24/T42/IMP3|
|AC26|stage-context.md#R11|C1/K9|T44/IMP3|
|AC27|stage-context.md#R2|C2/K12|T46/IMP2|
|AC28|stage-context.md#R12|C1/D9|T48/IMP4|
|AC29|stage-context.md#R12|C1/K9|T44/T48/IMP4|
|AC30|stage-context.md#R12|C1/D9|T48/IMP4|
|AC31|stage-context.md#R13|C6/K7|T25/T36/IMP2|
|AC32|stage-context.md#R1|C4/K3/D8|T6/T7/T51/IMP1|
|AC33|stage-context.md#R14|C4/K1|T2/T3/IMP1|
|AC34|stage-context.md#R5|C8/K8|T20/T27/T28/IMP2|
|AC35|stage-context.md#R3|C1/K8/D4|T14/IMP2|
|AC36|stage-context.md#R3|C4/K8/D5|T15/T16/IMP2|
|AC37|stage-context.md#R12|C1/D9|T47/IMP3|
|AC38|stage-context.md#R11|C1/K6/D3|T33/IMP3|
|AC39|stage-context.md#R1|C4/K3/D1|T4/T5/IMP1|
|AC40|stage-context.md#R1|C4/K3/D1|T5/IMP1|
|AC41|stage-context.md#R1|C4/K3/D1|T5/IMP1|
|INV1|stage-context.md#Не должно измениться|C1/K9|T51|
|INV2|stage-context.md#Не должно измениться|C1/K9|T52|
|INV3|stage-context.md#Не должно измениться|C1/K5|T52|
|INV4|stage-context.md#Не должно измениться|C6/K10|T39|
|INV5|stage-context.md#Не должно измениться|C1/K9|T52|
|INV6|stage-context.md#Не должно измениться|C1/K9|T37/T53|
|INV7|stage-context.md#Не должно измениться|C1/K9|T37/T53|
|INV8|stage-context.md#Не должно измениться|C1/K5|T54|
|INV9|stage-context.md#Не должно измениться|C6/K10|T38|
|INV10|stage-context.md#Не должно измениться|C6/K7|T37|
|INV11|stage-context.md#Не должно измениться|C4/K3|T51|

## Находки

| ID | Важность | Локация / R# / AC# | Проблема и доказательство | Что должно измениться | Владелец | Состояние |
|---|---|---|---|---|---|---|
| FR7 | BLOCKER | ctx-3, шапка «База кода» | База выровнена на `c523183` у всех шести документов; гейт проходит | — | spec-context | VERIFIED |
| FR8 | MINOR | AC3, T13 | T13 сам перечисляет 18 сборок (пять семейств по три юнита плюс три `checklist`); циклической ссылки нет | — | qa | VERIFIED |
| FR9 | MINOR | AC36, arch §6 | 5 493 в требованиях, §6 и T15 совпадают | — | аналитик | VERIFIED (ярлык — FR19) |
| FR10 | MINOR | arch §6, `reviewer` | `CHECK`/`STATE` вынесены из колонки, сумма `reviewer` 7 900, запас 1 100 | — | архитектор | VERIFIED |
| FR11 | MINOR | arch §12, K9 | Файл назван: `.agentic/logs/orchestrator.log`, отдельного нет | — | архитектор | VERIFIED |
| FR12 | MINOR | AC3, AC25 | «пять семейств и строка `checklist`», а не «каждый тип» | — | аналитик | VERIFIED |
| FR13 | MINOR | arch §14 | R10 внесён в IMP2 и в строку покрытия | — | архитектор | VERIFIED |
| FR14 | MINOR | AC41, C4/1d | Ветка значения вне семейств получила AC41 и шаг C4/1d | — | аналитик | VERIFIED (формулировка — FR20) |
| FR15 | MINOR | Q4 | Q4 закрыт как решённый (A8), в таблице вопросов без «открыт» | — | владелец | VERIFIED |
| FR16 | MINOR | R2, AC27 | Литералы совпали с arch §9: `<redacted: имя>`, `[redacted: файл-секрет]` | — | аналитик | VERIFIED |
| FR17 | MINOR | AC12, T40 | AC12 задаёт `закрыто: N (R-1, …)`, T40 проверяет дословно | — | аналитик | VERIFIED |
| FR18 | MINOR | ts T25, T33 | T25 ссылается на `D16`, T33 на `D13`; в arch-4 только D1–D9. Поведение покрыто A9/AC31 и R11/K6 | Заменить ссылки на A9 и D3 либо переиздать карточки | qa | OPEN |
| FR19 | MINOR | AC36 | 5 493 названы «пределами P0», но это размеры блоков; пределы — 5 850 (arch §6, T15) | Поправить слово в AC36 | аналитик | OPEN |
| FR20 | MINOR | AC41 | «значение вне пяти семейств» буквально захватывает `reviewer/checklist`, который R1, arch §4 и T5 допускают | Исключить `reviewer/checklist` в тексте AC41 | аналитик | OPEN |
| FR21 | MINOR | INV11 | Перечень отказов называет `CTX-FAIL` и `strict`, не называя `CTX-TYPE-MISSING` (AC40/AC41, T51) | Дополнить INV11 | аналитик | OPEN |
| FR22 | SUGGESTION | AC33 | AC33 проверяет имена; arch §4 и RR58 требуют ещё сравнения значений (QA24) | Добавить AC на значение либо снять требование из arch | аналитик | OPEN |
| FR23 | SUGGESTION | R12, AC37 | Пороги стопа строже базы: сумма находок ≥ 4 при максимуме 4, медиана > 14,5 при 12,12 мин; числа вынесены в `QUALITY_STOPS` | Подтвердить числа владельцу | владелец | OPEN |

Ранее закрытое проверено повторно: RR40–RR49, RR50–RR58 (база, потолок, миграция) — VERIFIED по числам и arch-4; RR60 → FR20, RR61 → FR19 (числа сошлись), RR62 → FR21, RR64 закрыт литералами, RR65 → FR23. QA14 закрыт, QA15–QA19 и QA23 = FR9–FR14, FR17; QA20 = Q3/Q5/Q6; QA24 = FR22.

## Решение не создаёт переделок

- **Два пути.** Нет: один сборщик K7; `--file`, `build_prompt` (`awf/supervisor.py:399`), `build_replan_context` (`supervisor.py:1304`, `rejection.py:115`) и `resolve_prev_handoffs` (`awf/agent_stage.py:732`) удаляются в IMP2 (D2, AC14). `context-pack --stdout` (`awf/cmd_context_pack.py:30`) — предпросмотр того же K7 без записи файла, выбор сборщика не появляется.
- **Флаг режима.** Сборки нет: `context_mode`, `legacy_context`, `AWF_CONTEXT*` отсутствуют в коде и конфигах (AC14, T35). Единственный переключатель — `stage_types.strict`, разрешён TODO-0377 п. 2, назван в A10/AC14, меняет только WARN на отказ; исключение № 1.
- **Схема пайплайна и снимок.** Не меняются: 12 пайплайнов грузятся без правок, `<action:>` отвергается, набор ключей тот же (AC15, T39); тип живёт в конфиге, снимок только читается (INV9, T38).
- **Дубли ролей.** Нет: 9 файлов `.agentic/roles`, `*.core.md`/`*.compact.md` отсутствуют; роль — существующий файл с выборкой по `##` (R4, T30–T32).
- **Новый постоянный артефакт.** Не появляется: `STAGE-<todo>-<stage>.md` — существующий класс (`awf/context_pack.py::pack_path`, строка 96), назван в R13/AC31; `CTX` — строка существующего `.agentic/logs/orchestrator.log`; реестр владельцев — раздел `awf report`; других файлов AC7/T36 не допускают. Исключение № 2.
- **Второй носитель типа.** Нет: один ключ `stage_types.<имя>` (R1, A3, AC39, AC40); карты «имя → семейство» в рантайме не остаётся (T4).

## План IMP

| IMP | Что | Требования | Точка замера |
|---|---|---|---|
| IMP1 | тип как данные: `awf/stage_context_types.py`, `stage_types` в `.agentic/config.yaml`, `init` пишет, `doctor` сверяет, `_compute_kind` удалён | R1, R7, R14; AC2, AC11, AC15, AC21, AC32, AC33, AC39, AC40 | 16 пар, повторный `init` байты не меняет, 213 снимков, ноль `StageTypeUndeclared` |
| IMP2 | один сборщик и файл: `awf/stage_context*.py`, `redact.py`, роль, обрезка, `agent_stage.py`, `supervisor.py`, удаление старых бюджетов | R2–R6, R8–R10, R13; AC1, AC3–AC16, AC22–AC24, AC27, AC31, AC34–AC36, AC38 | `total` и `cut` ≤ 9 000 на трёх юнитах; TODO-0374 и verify 0370 без `CTX-FAIL`; файл равен argv |
| IMP3 | отчёт и первая точка: `awf/cmd_report.py`, раздел «Контекст стадий», инциденты | R11, R12 (часть); AC19, AC23, AC25, AC26, AC29, AC37 | отчёт равен строке `CTX`; три юнита, три числа против `QUALITY_STOPS` |
| IMP4 | эффект и вторая точка: окно 10 юнитов, три spec-рана, пересчёт базы | R12; AC17, AC18, AC20, AC28, AC30, AC37 | окно 0338–0347, spec-раны 5/2/2, медиана QA 21,1 |

## Условия завершения реализации

- Гейт `awf spec check docs/spec/stage-context` — READY на актуальном `Пакет-sha` (сейчас ok).
- T1–T46, T49, T51–T54 зелёные; T47, T48, T50, T55 — измерением или ручной сверкой с числами в отчёте.
- Регрессия INV1–INV11: `scripts/check-contracts.sh`, тесты сигналов, гейта, схемы DONE, `scripts/run-all.sh`, `ruff check .`.
- Миграция IMP1 одним коммитом: `init` пишет ключ до того, как K3 начинает из него читать; запасного правила нет.
- Статус READY не даёт разрешения на публикацию и внешние изменения.

## Открытые вопросы

| ID | Вопрос | Владелец | Влияние |
|---|---|---|---|
| Q1 | Что считается «клоном»? В логе их нет | владелец | Вне scope |
| Q2 | Секрет в диффе скрывать (R2/AC27)? | владелец | Решено в требованиях, подтверждение не нужно |
| Q3 | Витки spec-стадии: ≤1 или ≤3? Пока открыт — медиана трёх ранов | владелец | Меняет число AC28 |
| Q5 | Цель: ноль на 10 юнитах или медиана ≤1 на трёх? Пока открыт — второй | владелец | Меняет число AC17 |
| Q6 | QA: каждая ≤25 минут или медиана ≤25? Пока открыт — медиана | владелец | Меняет число AC30 |

Остаточные риски: FR18–FR21 — текстовые неточности без влияния на поведение; пороги качества в `QUALITY_STOPS` — цель, а не доказанный эффект (FR23); покрытие роли считается от ядра (D3) — при смене знаменателя WARN появится на каждой стадии; удаление `supervisor.build_prompt` названо только в arch §8; артефакта `SUITE-<tree>-*.json` для текущего дерева нет.
