# Ревью требований: closer-стадия Definition-of-done

Статус: PASS
Ревизия: review-4
Входы: closer-stage.md@req-4; closer-stage.context.md@ctx-1; closer-stage.requirements-review.md@review-3; closer-stage.architecture.md@arch-1 (устарел, не норма); TODO-0245 (retired); TODO-0265; протокол specification-pipeline
База кода: commit 258f28a43f91663011c3a88967f15a2dd894d4b8; рабочее дерево: `?? docs/spec/closer-stage/`, `M awf/budget.py`, `M tests/negative/test_unit_budget.py` (RR24)
Проверено: closer-stage.md=req-4

## Режим и границы

Режим — ревью требований до проектирования. Способ — отдельная сессия роли `agent-requirements-reviewer` (стадия `spec-requirements-review`); автор требований — стадия `spec-requirements`, другая сессия. Прочитал `closer-stage.md` целиком, контекст, review-3, `arch-1`, retired TODO-0245, TODO-0265 и код на базе. Требования и код не менял. Полный сьют не запускал: для ревью требований он не даёт доказательств. Запускал `awf spec check` (rc 1 — нет test-spec и readiness, для этой стадии ожидаемо) и `scripts/instruction-gates.sh` (ok, 948/950 и 250/250). Runtime-поведение по чтению кода не подтверждаю.

## Вердикт

**PASS.** RR18–RR24 закрыты; новых BLOCKER/MAJOR нет. Остались три MINOR: RR25 — AC7 называет последним писателем implementer, хотя `agent-architect-review` не readonly и стоит между implementer и qa-review; позицию closer это не меняет, но обоснование ложное. RR26 — INV3 говорит «ретраи … не тратят», а R4/AC11 тратят завершённый прогон с вердиктом и называют только «ретрай после падения». RR27 — раздел «Открытые вопросы» ссылается на `arch-1`, который сам BLOCKED и основан на req-1. MINOR не блокируют проектирование; их можно закрыть в req-5 (тогда PASS станет устаревшим) или принять с обоснованием. Документ требований стоит 2199/2200 слов — любая правка требует сокращения; пакет 7382/11000, на test-spec и readiness остаётся 3618 слов.

## Проверка review-3

| ID | Состояние | Проверка |
|---|---|---|
| RR18 | VERIFIED | R4, AC11, INV3 и C3-3c говорят одно: бюджет тратит завершённый прогон closer с вердиктом; падение, salvage, ретрай после падения и `skip_if`-пропуск — нет. Цикл падений держат `max_retries` стадии и salvage-счётчик. Формулировка INV3 — RR26. |
| RR19 | VERIFIED | R2 переносит единственную точку решения на вход closer и прямо говорит: после `implementer` строки `dod:` нет. «Изменения» req-4 это фиксируют. |
| RR20 | VERIFIED | AC7 больше не утверждает, что `qa-review` пишет тесты; назван `readonly_roles`. Новый фактический дефект того же абзаца — RR25. |
| RR21 | VERIFIED | R6 исключает `manual` из статистики: у них нет пробы; AC15-AC17 это не перечат. |
| RR22 | VERIFIED | Текст задачи R3 говорит «только пункты с пробами (`missing`); `manual` вне scope, блок не править»; AC5 и AC9 согласованы. |
| RR23 | VERIFIED | AC7 задаёт якорь: сразу после `qa-review`, до `agent-project-auditor`; arch-1 K6 устарел и должен быть заменён в arch-2. |
| RR24 | VERIFIED (шапки) / OPEN (супервизор) | Шапка требований и контекста называют `M awf/budget.py`, `M tests/negative/test_unit_budget.py` и происхождение — правка `review_passes` из spec-requirements req-3. Решение «отдельный юнит или исключение» остаётся за супервизором; DONE-0265 «код не менялся» по-прежнему неверен, но шапка его исправляет. |

## Новые находки

| ID | Важность | Локация / R# / AC# | Проблема и доказательство | Последствие | Что должно измениться | Владелец | Состояние |
|---|---|---|---|---|---|---|---|
| RR25 | MINOR | AC7 (`closer-stage.md:63`), R7 (`:111`); `.agentic/config.yaml:41`; `.agentic/pipelines/default.yaml` | AC7: «`qa-review` и `project-auditor` в `readonly_roles`, последний писатель — implementer». В конфиге readonly только `agent-qa-review` и `agent-project-auditor`; `agent-architect-review` не readonly и в `default.yaml` имеет задачу «fix within the unit's scope by refactoring», то есть пишет. Порядок: implementer → architect-review → qa-review → auditor. Последний писатель — architect-review, не implementer. | Позиция closer от этого не меняется: «сразу после qa-review» и так позже architect-review. Но обоснование в AC7 ложно; тест или читатель, выводящий позицию из «последнего писателя», может поставить closer до qa-review и разойтись с AC7. | Назвать `agent-architect-review` писателем (последний писатель — он) и сохранить «сразу после qa-review»; либо включить его в `readonly_roles`, если он не должен писать. Синхронизировать arch-2 и тест. | аналитик (текст); супервизор (конфиг) | OPEN |
| RR26 | MINOR | INV3 (`closer-stage.md:212`), AC11 (`:77`), R4 (`:74`) | INV3: «бюджет closer тратят только завершённые прогоны с вердиктом, а падения, salvage, ретраи и пропуски — нет». AC11 уточняет: не тратит «ретрай после падения». Ретрай после завершённого closer (второй запуск при бюджете 2) — это и ретрай, и завершённый прогон с вердиктом; INV3 в буквальном чтении освобождает его от бюджета и противоречит «при бюджете 2 — ровно второй запуск, после него BLOCKED». | Две реализации: (а) все ретраи бесплатны — второй closer не тратит бюджет, возможен третий; (б) бесплатны только ретраи после падения — совпадает с AC11. AC11 конкретнее, но INV3 — инвариант, и тест может пойти за ним. | В INV3 заменить «ретраи» на «ретраи после падения» (или «salvage-ретраи»), как в AC11. Поведение не меняется, если следовать AC11. | аналитик | OPEN |
| RR27 | MINOR | «Открытые вопросы» (`closer-stage.md:227`), шапка (`:5`); arch-1 (`architecture.md:3-5`) | Требования говорят: «Q1-Q6 закрыты (A1-A5, `arch-1`)». `arch-1` — BLOCKED и основан на req-1; его K6 ставит closer после `agent-architect-review`, что расходится с AC7/R7. Ссылка на него как на закрытие вопросов вводит в заблуждение. | Архитектор или readiness может принять решения arch-1 за актуальные; особенно позицию closer. | Сослаться только на A1-A5; актуальная архитектура — arch-2. | аналитик | OPEN |

## Что подтвердилось независимо

- `skip_if` отсутствует в `ALLOWED_STAGE_KEYS`, `Stage`, загрузчике и снапшотах; `checklist.closer_attempts` и `awf checklist stats` в коде отсутствуют — это новые поверхности, не выданные за существующие.
- `readonly_roles` в `.agentic/config.yaml:41` — ровно `[agent-qa-review, agent-project-auditor]`; `agent-architect-review` не входит и имеет пишущую задачу (RR25).
- Порядок `default`/`full`: plan → implementer → architect-review → qa-review → project-auditor → verify; `code-health`: plan → architect-review → refactoring-specialist → verify.
- Загрузчик `write_check` принимает только строки `"on"`/`"off"`; голый YAML `off` читается как bool и отвергается (`awf/pipeline.py:342-350`). В R3/AC8 `off` — схема, в YAML нужны кавычки.
- Гейт: `status=none|checked`, `ok` без `missing`, `manual` не влияет на `ok`; пустой блок — `status=checked`, `ok=false`, `missing=["(empty block)"]`; нечитаемый — `["(unreadable block)"]` (`awf/checklist_gate.py:474-490,520-548`).
- `awf spec check docs/spec/closer-stage` → rc 1 только из-за отсутствия test-spec и readiness; требования, статус и бюджет проходят.
- Бюджеты: требования 2199/2200 слов; пакет 7382/11000 (context 2039, architecture 1612, review 1532); инструкции 948/950, доктрина 250/250.
- `review_passes` действительно изменён в рабочем дереве: заголовки со словом `spec` больше не считаются витком; тест `test_spec_review_heading_is_not_a_pass` это фиксирует.

## Покрытие требований и источники

| R# | Источник | AC# | Сценарий | Замечание |
|---|---|---|---|---|
| R1 | TODO-0245 п.1; CTX1, CTX6, CTX7; RR1, RR9 | AC1-AC3 | C6 | RR16 проверен; ключ общий, в итерации только closer |
| R2 | TODO-0245 п.2; CTX2, CTX3; RR1, RR2, RR4, RR7 | AC4-AC6, AC25, AC26 | C1, C2 | RR19 закрыт; AC5 — manual; AC26 — статический дефект |
| R3 | TODO-0245 п.3; CTX2; RR2, RR3 | AC7-AC9, AC27 | C1, C2 | RR20 закрыт, RR25 — обоснование позиции; AC27 — статический дефект |
| R4 | TODO-0245 п.4; CTX4 | AC10, AC11, AC24 | C3 | RR18 закрыт; RR26 — формулировка INV3 |
| R5 | TODO-0245 п.5; CTX8; RR5 | AC12-AC14 | C4 | противоречий нет |
| R6 | TODO-0245 п.6; CTX3, CTX9; RR4, RR6 | AC15-AC17, AC28 | C5 | RR21 закрыт |
| R7 | TODO-0245 п.7; CTX5; RR2 | AC18, AC19 | C6 | RR25 — позиция и обоснование; локальный файл gitignored |
| R8 | TODO-0245 п.8; AGENTS.md | AC20, AC21 | C1-C6 | RR15 проверен; перечень включает AC18, AC24, AC27 |
| R9 | Приёмка TODO-0265 | AC22, AC23 | — | план, поведенческого сценария нет |

Все восемь пунктов retired TODO-0245 сведены в R1-R8; п.7 (пример волны) вынесен в A3, п.2 (гейт после implementer) переехал в точку решения closer (RR19). INV1, INV2, INV4, INV5, INV6 подтверждены; INV3 по поведению согласован с R4/AC11, но формулировка требует правки RR26.

## Заметки для arch-2

- K6 в arch-1 устарел: там `default` — после `agent-architect-review`, в req-4 — после `qa-review` и до `agent-project-auditor`. Брать AC7/R7; architect-review — писатель (RR25), поэтому «после qa-review» позже него.
- K5 пишет `first_verdict` «до первого спавна closer». AC15 требует `runs=1` и для зелёного юнита, где спавна нет. Первый вердикт нужно писать на каждой точке решения, включая пропуск.
- K7 исключает `manual` из статистики; R6 теперь говорит то же (RR21).
- K4 увеличивает счётчик перед каждым спавном. По RR18 счётчик тратит только завершённый прогон с вердиктом; падение, salvage, ретрай после падения и пропуск — нет. Разделять «спавн» и «завершённый прогон».
- IMP7 говорит «восемь веток R8»; в req-4 перечень длиннее. Явно покрыть AC4 (формат `dod:`), AC6 (кэш), AC8 (снапшоты), AC9 (промпт), AC18 (`full`/`code-health`), AC19 (`init`), AC16/AC17 (json и пустой набор), AC24 (неверный `closer_attempts`), AC27 (`skip_if` без `write_check: off`).
- В YAML `write_check` — строка `"off"`; голый `off` отвергается загрузчиком (`awf/pipeline.py:342-350`).
- Локальный `.agentic/pipelines/default.yaml` не коммитится: `.agentic/` в `.gitignore`. DONE юнита должен назвать файл рабочего дерева; тест AC19 читает его, а не diff.
- Таблица открытых вопросов arch-1 всё ещё перечисляет RR1-RR3 и RR7 как открытые; после req-3 это история, а не текущее состояние.
- Бюджет пакета: 7382/11000; на test-spec и readiness остаётся 3618 слов. Держать оба документа компактными.

## Маршрут исправлений

1. PASS относится к req-4. RR25-RR27 — MINOR: закрыть в req-5 (тогда нужен повторный PASS на новой ревизии) или принять с обоснованием; на проектирование не блокируют.
2. Супервизор закрывает RR24: называет происхождение правки `review_passes` и решает, отдельный это юнит или принятое исключение; шапки и DONE приводятся в соответствие.
3. Архитектор выпускает arch-2 по AC7/R7 и заметкам выше; статус REVIEW, без кода.
4. Затем test-spec и readiness. Readiness проверяет RR25-RR27, бюджет пакета и актуальность всех ревизий.
5. Повторное ревью проверяет RR25-RR27 и зависимые AC7, AC11, AC18, AC19, INV3 — не только дифф.
