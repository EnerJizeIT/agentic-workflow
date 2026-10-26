# Ревью требований: carry batch C+D

Статус: PASS
Ревизия: review-5
Входы: carry-cd.md@req-5; carry-cd.context.md@ctx-2; carry-cd.requirements-review.md@review-4; carry-cd.readiness.md@readiness-2; carry-cd.architecture.md@arch-2 (K9, §3 — вне scope правки); carry-cd.test-spec.md@qa-1 (T6); TODO-0266; retired `.agentic/done/TODO-0243/TODO.md`; `BACKLOG-NEXT.md` — `/home/pklochkov/Desktop/awf report/roadmap/BACKLOG-NEXT.md`
База кода: HEAD `bdd1074`; рабочее дерево: untracked `docs/spec/carry-cd/`
Проверено: carry-cd.md=req-5

## Режим и границы

Ревью требований до проектирования. Способ — отдельная сессия роли `agent-requirements-reviewer` (стадия `spec-requirements-review`); автор требований — `spec-requirements`. Прочитал req-5 целиком, readiness-2 (FR9), arch-2 (K9 и §3), qa-1 (T6) и `approve_commit` на HEAD. Требования, архитектуру и тест-спеку не менял. Runtime не запускал: выводы о достижимости порядка — по коду. `awf spec check` даёт rc 1 только на readiness-2, которая называет `req-4`; до readiness-3 это ожидаемо. Бюджет требований 2200/2200.

## Вердикт

**PASS по req-5.** FR9 readiness-2 на стороне требований закрыт: R9 задаёт порядок «preflight `inbox` и сигнала → `RUN-EVIDENCE`/`VERIFIED-<id>.sha` → сигнал», preflight до `managed_mkdir` и любых записей. AC24, C5 и INV1 говорят то же. Порядок выполним: `refuse_unsafe_surfaces` пропускает ещё не созданный `inbox` (`managed_fs.py:126`), а `approve_commit` сейчас пишет evidence (`pipeline.py:786`), отпечаток (`:930`), затем создаёт `inbox` (`:938`) — preflight в начале функции оставляет отказ без файлов. Обратный порядок (K9 arch-2) AC24 нарушает, поэтому приёмку проходит ровно одна трактовка. Блокеров, MAJOR и блокирующих Q# нет. PASS относится только к req-5.

## Что проверено в изменении

| Место | Проверка | Итог |
|---|---|---|
| R9 (`:120-128`) | порядок, поверхности preflight, no-follow сигнала | согласовано |
| AC24 (`:126`) | отказ до `RUN-EVIDENCE`, `VERIFIED`, сигнала | совпадает с R9 |
| C5 (`:214-221`) | preflight первым, до записей | совпадает |
| INV1 (`:241`) | «ни файла» + оговорка R9 про preflight | совпадает |
| AC25/INV2 (`:127,242`) | отпечаток до сигнала, поля бинда | не тронуто, держится |
| Остальные R# | не менялись; ссылки и нумерация AC целы | расхождений нет |

A7 (подмена сигнала на обычный файл вне scope) остаётся допустимым ограничением и AC24 не противоречит. Широкая фраза INV1 «ни файла» привязана оговоркой «R9: preflight до `RUN-EVIDENCE`/`VERIFIED`» к отказу preflight; ветка уже учтённого reject не менялась и в FR9 не входила.

## Перенесённые находки (не блокируют PASS)

| ID | Важность | Где | Суть и маршрут | Владелец |
|---|---|---|---|---|
| RR23 | MINOR | R12, AC30 (`:156`) | AC30 всё ещё говорит «stage-log», тогда как K12/T2 задают лог воркера от `log_start_offset`; поведение однозначно, текст правится после READY | аналитик |
| RR24 | MINOR | R13/A5 (`:165,255`) | отдельного AC на легаси-фолбэк нет; T3/T4 и K14 поведение задают; AC — после READY | аналитик/QA |
| FR10 | MINOR | `ctx-2` | навигационные номера строк части требований устарели; нормативные якоря R#/K# верны | аудитор |
| FR8 | MINOR | `inbox/TODO-0264.md` | implementer-юнит с тем же scope; закрыть до первого dev-юнита | супервизор |

RR25 закрыт: T8 держит копию front matter (readiness-2 FR5). RR1–RR22 не переоткрываю.

## Следующему этапу

- **Архитектор (arch-3):** K9 (`architecture.md:51`) всё ещё пишет «`VERIFIED` → `refuse_unsafe_surfaces` → `managed_mkdir`»; привести к «preflight → `RUN-EVIDENCE` → `VERIFIED` → `managed_mkdir` → сигнал». Строка §3 (`:87`) «K9 — до записи сигнала» должна назвать evidence и отпечаток. D4 не меняется. Существующая очистка stale `CANDIDATE-<id>.json` на входе артефакта не создаёт, но T6 проверяет состав каталогов — её место назвать явно.
- **QA (qa-2):** T6 (`test-spec.md:81-86`) должен после отказа по симлинку проверять отсутствие `RUN-EVIDENCE`, `VERIFIED` и сигнала — это условие FR9 «T6 явно проверяет состав каталогов». Обычный путь по-прежнему «отпечаток → сигнал».
- **Readiness-3:** бюджет пакета 10881/11000; правки только сжатием.

## Маршрут

1. arch-3 закрывает K9/§3. 2. qa-2 дополняет T6. 3. readiness-3 выставляет READY. 4. После READY — RR23, RR24, FR10, FR8. Требования req-5 не меняются.
