# Архитектура: контекст стадии

Статус: REVIEW
Ревизия: arch-2 (req-5: RR6, RR17 — закрыты; RR9/RR10 учтены; бюджет тот же)
Входы: `stage-context.md@req-5` (PASS, `rr-5`), `stage-context.requirements-review.md@rr-5`, `stage-context.context.md@ctx-2`, TODO-0374, `.agentic/pipelines/*.yaml`, `.agentic/config.yaml`, `.agentic/roles/`, `awf/`.
База кода: `main` @ `99e98ed`; в дереве только untracked `docs/spec/stage-context/`.

Код, роли, пайплайны и конфиг не менялись; ниже план.

## 1. Решение

1. **Один сборщик:** блоки → один текст аргументом `opencode run`; `--file`, `resolve_prev_handoffs`, ветки `build_prompt`, бюджеты verify/replan уходят.
2. **Тип — `config.yaml stage_types.<имя>`**; пять семейств, `checklist` — строка `reviewer/checklist` внутри `reviewer`.
3. **Блоки — данные `FAMILIES`** (шесть строк).
4. **Один потолок:** 8 000 для `execute`/`reviewer`/`reviewer/checklist`; 6 000 для `supervisor`/`spec`/`plan`.
5. **Роль — существующий файл, выборка по заголовкам `##`**, без тегов и копий.
6. **Замер — строка в лог и `awf report --context`** (RR6).

## 2. Границы и точки изменения

| Компонент | Ответственность | Файл |
|---|---|---|
| Резолвер типа | имя → строка, отказ | `awf/stage_context_types.py` (новый) |
| Сборщик | блоки, порядок, потолок, обрезка | `awf/stage_context.py` (новый) |
| Рендеры | артефакт → текст и факты | `awf/stage_context_blocks.py` (новый) |
| Выборка роли | ядро по маркерам | `awf/stage_context_roles.py` (новый) |
| Запуск стадии | один текст вместо `--file` | `awf/agent_stage.py` |
| Супервизор | `plan`/`verify`/`replan` через сборщик | `awf/supervisor.py` |
| Проверка и replan | снять бюджеты | `awf/verify_context.py`, `awf/rejection.py` |
| Обрезка | один механизм приоритетов | `awf/bounded_prompt.py` |
| Маскирование | одно правило секрета (RR17) | `awf/redact.py` (новый), `awf/worker_tree.py` |
| Конфиг | объявление типов | `.agentic/config.yaml`, `awf/pipeline.py` |
| Отчёт | строка замера, инциденты | `awf/cmd_report.py`, `awf/api` |

Не меняются сигналы, коммит-гейт, DONE/REVIEW, allowlist, `STAGE-DIFF`, права, доктрина.

## 3. Словарь блоков и владельцы

Закрытый словарь; расширение — новая ревизия (R2). Владелец один:

- `PIPELINE-<todo>.yaml` — `STAGE` (task, input, output, allowlist, skip_if), `INPUTART`;
- TODO — `UNIT`, `CRIT` (`AC#` дословно и команды; для `plan` — формат TODO), `CHECK` (пункты, пробы, результат), `REVIEW` (`## REVIEW-фикс`);
- `.agentic/roles/<role>.md` — `ROLE` (ядро, остальное ссылки); `.agentic/doctrine/*.md` — `DOCTRINE` (заголовок и первый абзац);
- `BASELINE-<todo>.sha` + `git status` — `STATE`; дерево против baseline — `DIFF` (diffstat, файлы, патч в доле);
- `GATES-<todo>.md` — `GATES`; `COMMIT-AUDIT-<todo>.json` — `AUDIT` (в текст не входит); `DONE-<todo>.md`/`.json` — `DONE`; `BLOCKED-<todo>.md` — `BLOCKED`;
- handoff-файлы — `PREV` (последний в доле) и `PREVLINK` (остальные: путь, статус, итог);
- `phases.current` — `PHASES`; `paths.find_vision_file` — `VISION`; константы движка — `RULES`, `OUTPUT`.

`METRICS` и `MISSING` в текст не входят. Один факт — один блок: `(fact_id, value, owner)`. `DIFF` файла не имеет, `REVIEW` берётся из TODO, поэтому удаление `.ready` находки не стирает (AC12).

## 4. Тип стадии: один носитель

Тип объявляется в `.agentic/config.yaml`; позиция, роль и `action:`/`kind:` его не задают. Тип — одно из пяти семейств (`plan`, `execute`, `reviewer`, `supervisor`, `spec`); `checklist` — строка внутри `reviewer`, поэтому значение — `<семейство>` или `<семейство>/<строка>`. Одно поле, а не второй носитель (RR9): уточняющего ключа нет.

```yaml
stage_types:
  plan: plan
  agent-implementer: execute
  agent-architect-review: reviewer
  agent-qa-review: reviewer
  agent-project-auditor: reviewer
  checklist-close: reviewer/checklist
  checklist-check: reviewer/checklist
  verify: supervisor
  spec-context: spec
  spec-requirements: spec
  spec-requirements-review: spec
  spec-design: spec
  spec-tests: spec
  spec-finalize: spec
  spec-readiness: spec
  implementer-close: reviewer/checklist
```

Пятнадцать имён объявлены в 12 пайплайнах (семь `spec-*`); `implementer-close` добавлен, потому что он есть в 30 из 212 замороженных снимков, а AC11 запрещает их править; это строка данных того же носителя, не запасное правило. Нет объявления, семейство вне пяти или неизвестная строка → `StageTypeUndeclared` с именем и путём конфига; валидация до запуска.

Вид: `plan` → `plan`; `supervisor` → `verify`; остальные → `execute`. Следствие: `spec-context` на нулевой позиции получает контракт DONE (AC21/AC22). `_compute_kind` удаляется, `Stage.kind` заполняет резолвер.

## 5. Таблица «тип → блоки»

Порядок строки — порядок текста. Пять семейств, шесть строк: `checklist` — строка `reviewer`.

| Строка | Блоки | Потолок |
|---|---|---|
| `plan` | `STAGE`, `PHASES`, `VISION`, `ROLE`, `DOCTRINE`, `STATE`, `PREVLINK`, `RULES`, `CRIT`, `OUTPUT` | 6 000 |
| `execute` | `STAGE`, `UNIT`, `ROLE`, `DOCTRINE`, `STATE`, `GATES`, `BLOCKED`, `PREV`, `REVIEW`, `PREVLINK`, `RULES`, `CRIT`, `OUTPUT` | 8 000 |
| `reviewer` | `STAGE`, `UNIT`, `ROLE`, `DOCTRINE`, `DONE`, `DIFF`, `GATES`, `REVIEW`, `PREV`, `PREVLINK`, `RULES`, `CRIT`, `OUTPUT` | 8 000 |
| `reviewer/checklist` | `STAGE`, `CHECK`, `STATE`, `PREV`, `RULES`, `CRIT`, `OUTPUT` | 8 000 |
| `supervisor` | `STAGE`, `STATE`, `ROLE`, `DOCTRINE`, `GATES`, `DIFF`, `DONE`, `REVIEW`, `PREVLINK`, `RULES`, `CRIT`, `OUTPUT` | 6 000 |
| `spec` | `STAGE`, `UNIT`, `ROLE`, `DOCTRINE`, `INPUTART`, `PREV`, `REVIEW`, `PREVLINK`, `RULES`, `CRIT`, `OUTPUT` | 6 000 |

`plan` без handoff-ов и диффа; `reviewer/checklist` без роли; `spec` с `INPUTART`. Таблица — данные `FAMILIES`: новая стадия — строка в `stage_types`, роль — файл; код не меняется (AC2).

## 6. Бюджет, приоритет и обрезка

Потолок — на строку после `--` (инструкции, блоки, рамки): `--file` уходит: «сумма файлов» и «текст» — одна величина. `plan` — 6 000 по семейству `supervisor` (A-arch4).

Классы: P0 — `OUTPUT`, `RULES`, `STAGE`, `UNIT`/`CHECK`, `CRIT`, `PHASES` в `plan`: не режутся, не влезли — сборка падает (AC3); P1 `ROLE` — по разделам, срез помечен; P2 `DOCTRINE` — до заголовков и ссылок; P3 `REVIEW`, `GATES`, `DONE`, `BLOCKED`, `DIFF` — по важности, затем до ссылки; P4 `PREV`, `STATE`, `INPUTART`, `VISION` — до ссылки; P5 `PREVLINK` — первым, «ещё N — см. файл».

Обрезка снизу: P5, P4, P3, P2, P1; внутри `REVIEW` и `PREV` прочее первым, `BLOCKER` последним (AC10); срез оставляет «ещё N — см. <путь>». Пределы, символы: `STAGE` 400, `UNIT` 600, `CRIT` 1 800, `CHECK` 1 200, `ROLE` 2 200, `DOCTRINE` 900, `PHASES` 700, `STATE` 400, `DIFF` 900, `GATES` 600, `DONE` 500, `PREV` 800, `PREVLINK` 300, `REVIEW` 700, `BLOCKED` 300, `INPUTART` 700, `VISION` 800, `RULES` 500, `OUTPUT` 1 100. Предел не квота: остаток — следующему; `CRIT` не режется, не влез — `CTX-FAIL`.

Порядок — таблица §5; проверка индексами `(block_id, start, end)` (AC4).

## 7. Роль: один файл, выборка по маркерам

Маркер — существующий заголовок `##`, новых тегов и копий нет (RR10). Набор миграции — девять файлов `.agentic/roles/`: `supervisor.md` и восемь `agent-*` (`architector`, `architect-review`, `implementer`, `project-auditor`, `qa-review`, `refactoring-specialist`, `requirements-reviewer`, `system-analyst`); файлы вне набора не трогаются (R4). Шапка до первого `## ` (500); разделы из списка до предела `ROLE` (2 200); остальные — строки `- §<заголовок> — <путь>`; нет совпадений — первый раздел тела.

| Приоритет | Маркеры |
|---|---|
| 1 | `Instructions`, `Инструкции`, `Quick Reference` |
| 2 | `Выход`, `Handoff contract`, `Отчёт`, `Готовность`, `Формат находок`, `Итог`, `Результат` |
| 3 | `Режим`, `Режимы`, `Вход` |
| 4 | `Запрет`, `Prohibitions`, `Проверка`, `Находки` |
| 5 | `AWF-OVERRIDE`, `Политика тестов`, `Дисциплина доставки инвариантов`, `BD-31` |

AC6: три фразы на роль, список в тесте; нет фразы — переименовать раздел или расширить список; выборка по имени роли запрещена.

## 8. Сборка и контракты

Сборка: снимок → `resolve_stage_type` (нет объявления → `StageTypeUndeclared`) → `FAMILIES[family/row]` → рендер из владельцев → P0→P5 → при нехватке P0 `CTX-FAIL`, иначе текст в порядке таблицы → строка `CTX` → `opencode run`. Время и окружение в текст не входят. `STAGE-*.md` не пишется (AC7); `context-pack --stdout` — предпросмотр того же текста.

| ID | Контракт | Выход |
|---|---|---|
| K1 | `resolve_stage_type(config, name)` | `(family, row)`; `StageTypeUndeclared` |
| K2 | `engine_kind(family)` | `plan`/`execute`/`verify` |
| K3 | `build_stage_context(...)` | `text`, `blocks`, `cut`, `missing`, `conflicts`, `positions`; на диск не пишет |
| K4 | `render_block(id, ctx)` | `Block(id, title, text, owner, facts, priority, cap)` или «нет данных» |
| K5 | `select_role_core(text, family, limit)` | `text`, `kept/toc/dropped_sections` |
| K6 | `fit_blocks(blocks, cap)` | порядок, `cut`, `failed` |
| K7 | строка `CTX` | одна строка в лог |
| K8 | `awf report`, `--context` | строка на стадию, разбивка, реестр владельцев; числа равны `CTX` |
| K9 | правило снимка | тип из конфига; снимок только читается; `CTX-SNAPSHOT` |
| K10 | `RULES`, `OUTPUT` | дословный контракт вывода и запретов |
| K11 | `redact(text)` | маскированный текст и число замен |

## 9. Отсутствие данных, противоречия, секреты

Нет артефакта: `нет данных: <причина> — <путь>`, сигнал не меняется (AC9).

Противоречие: блоки дают `(fact_id, value, owner)`; сборщик пишет `CTX-CONFLICT <todo> <stage>: <fact_id> = A (owner A) != B (owner B)`, стадия не падает (AC8). Пары: `unit.status`, `gates.verdict`, `finding.<id>.state`, `stage.list`; строка идёт в отчёт, не в текст.

Секреты (R2, AC27, RR17) — правило, а не значение фикстуры. `DIFF` даёт diffstat и файлы; патч попадает в текст, только если прошёл `redact` (K11). Владелец правила один — `awf/redact.py`; `worker_tree.py` его импортирует, второй список запрещён. Правила два:

1. Именованное: ключевое слово `token|key|secret|password|auth|cookie` в имени, значение до пробела, `=` или `:` → `<redacted: имя>` (существующее выражение `awf/worker_tree.py:63` плюс `:`).
2. Файл-секрет: патч файлов `.env*`, `*.pem`, `*.key`, `id_rsa*`, `*credential*`, `*secret*` в текст не идёт — diffstat и строка `[redacted: файл-секрет]`.

Блок не выбрасывается, вместо значения — пометка; фикстура пишет секрет как `token=…` и файлом `.env` — тест проверяет правило, а не строку (Q2).

## 10. Снимок и совместимость

`PIPELINE-<todo>.yaml` только читается (INV9); тип — из конфига по имени; форма не расширяется (AC15). Правило: `CTX-SNAPSHOT <todo> <stage>: тип из config stage_types.<name>; снимок без типа — форма не меняется`. Покрывает 212 снимков, включая `implementer-close`; необъявленное имя — отказ. Побайтовая неизменность — тест (AC11).

## 11. Фикс-круг и reuse

Находки — из `REVIEW` и `DONE` ревьюера, сигнал не читается (AC12); закрытые не пересказываются. `PREV` один, блоки не растут (AC5).

Reuse не меняется; при продолжении текст собирается заново, в `RULES` строка «прошлая сессия не источник фактов; факты — из артефактов». AC13: различие со свежей стадией только в строке.

## 12. Замер

Формат: `CTX <todo> <stage> attempt=<n> type=<family> cap=<n> total=<n> blocks=<id>:<chars>,... cut=<id>:<chars>,... missing=<id>:<reason>,...`. Пример: `CTX TODO-0374 spec-design attempt=1 type=spec cap=6000 total=5812 blocks=STAGE:390,UNIT:540,ROLE:1480 cut=REVIEW:120 missing=PREV:нет-handoff`. Длительность — из пары `Agent stage started/finished`. Команда замера одна — `awf report --context` (RR6): тот же отчёт, флаг печатает разбивку; второй команды и нового артефакта нет. Отчёт соединяет по `(todo, stage, attempt)` и печатает тип, `total/cap`, блоки, срез, пропуски, витки, длительность, минуты QA. Там же реестр владельцев (AC23): файловый блок — путь и sha256, вычисляемый — «на лету» и источник. Инциденты считаются автоматически из `STAGE-DIFF` и `COMMIT-AUDIT` (AC26, AC29); окно из 10 — по порядку номеров юнитов, привязка — первые 10 после внедрения (RR24); readiness берёт числа из отчёта, а не из логов (AC19, AC25).

Точки: IMP1 — ноль `StageTypeUndeclared`; IMP2 — `total`/`cut`; IMP3 — витки, длительность, инциденты; IMP4 — совпадение отчёта и строки, реестр владельцев.

## 13. Решения, допущения, вопросы

| ID | Решение | Последствие |
|---|---|---|
| D1 | тип — `stage_types.<имя>`, одно поле `<семейство>[/<строка>]` (префикс и карта роли дают цепочку RR9 или не различают типы) | неизвестное имя — отказ |
| D2 | один сборщик, один текст (иначе два способа и два счёта) | старые сборщики удаляются |
| D3 | роль — выборка по заголовкам `##` (копия нарушает AC6, теги меняют роли) | непопавшие разделы — ссылки |
| D4 | один потолок, P0–P5 (квоты теряют качество) | крупный `CRIT` останавливает стадию |
| D5 | доктрина — дайджест и ссылка (2 958 не оставляют места задаче) | правка A4 |
| D6 | `DIFF` — diffstat, патч после `redact` (полный патч несёт секреты) | у файлов-секретов патча нет |
| D7 | последний handoff текстом, остальные ссылками (подстановка роли даёт дубли) | ссылки не растут |
| D8 | замер — `CTX` и `awf report --context` (JSON метрик нарушает AC7) | второй команды нет |
| D9 | reuse сохраняется, контекст пересобирается; предпросмотр `context-pack --stdout` | строка в `RULES` |
| D10 | противоречия — `CTX-CONFLICT` (молчаливый выбор теряет расхождение) | набор `fact_id` закрыт |

A-arch1: `AC#` влезают в 1 800 (TODO-0374: 1 406), больше — `CTX-FAIL`. A-arch2: заголовки ролей не переименовываются. A-arch3: `stage_types` не меняет схему, `config.py::load` читает YAML как словарь. A-arch4: `plan` получает 6 000 по семейству `supervisor`, хотя R3 его не называет, — порог выведен.

Вопросов нет: RR9–RR11 закрыты в req-5; Q1–Q4 архитектуру не блокируют.

## 14. План IMP

**IMP1 — тип как данные.** R1, AC2, AC11, AC15, AC16; K1, K2. `stage_context_types.py` (`FAMILIES`: пять семейств, шесть строк; `resolve`, `engine_kind`, `StageTypeUndeclared`), `.agentic/config.yaml` (15 имён + `implementer-close`), `pipeline.py` (убрать `_compute_kind`). Зависимостей нет. Проверка: 12 пайплайнов, 15 имён, 212 снимков, отказ на необъявленном и неизвестной строке. Миграция одним коммитом. Замер: ноль `StageTypeUndeclared`.

**IMP2 — сборщик.** R2–R6, AC3–AC6, AC9, AC10, AC23, AC24, AC27; K3–K6, K11. `stage_context.py`, `stage_context_blocks.py`, `stage_context_roles.py`, `redact.py` (правило из `worker_tree.py`), `context_pack.py`, `cmd_context_pack.py`. Зависимость IMP1. Проверка: таблицы, индексы, потолки, «нет данных», обрезка с `BLOCKER`, детерминизм, три фразы на девять ролей, путь проекта, секрет-фикстура в двух формах, размеры; код до переключения. Замер: `total`/`cut` ≤ потолка.

**IMP3 — один путь.** R7–R10, AC7, AC8, AC11–AC15, AC22; K7, K9, K10. `agent_stage.py` (удалить `--file` и `resolve_prev_handoffs`), `supervisor.py`, `verify_context.py`, `rejection.py`, `doctrine.py`, `session_store.py`, `stage_context.py` (`CTX`). Зависимость IMP2. Проверка: нет `--file`; verify ≤6 000 без среза TODO/`AC#` (CTX9); spec получает контракт DONE; старый снимок не меняется; фикс-круг видит находки; продолжение равно свежему. Миграция одним коммитом, старый путь удаляется. Замер: три юнита.

**IMP4 — отчёт.** R11, AC19, AC25, AC26, AC29, AC30; K7, K8. `cmd_report.py`, модель в `awf/api`, `stage_context_metrics.py`. Зависимость IMP3. Проверка: отчёт равен строкам, реестр владельцев с sha256, инциденты из `STAGE-DIFF`/`COMMIT-AUDIT`, минуты QA, пустой блок даёт причину. Замер: совпадение на трёх юнитах.

AC20: четыре юнита, у каждого точка замера; миграция разовая — IMP1 и IMP3 по одному коммиту, двойного поведения нет (AC14).

## 15. Покрытие требований

R1 → AC1, 2, 21, 22 (IMP1/2); R2 → AC8, 23, 27 (IMP2/4); R3 → AC3–5 (IMP2); R4 → AC6 (IMP2); R5 → AC9, 10 (IMP2); R6 → AC24 (IMP2); R7 → AC11 (IMP1/3); R8 → AC12 (IMP3); R9 → AC13 (IMP3); R10 → AC7, 14–16 (IMP3); R11 → AC19, 25, 26 (IMP4); R12 → AC17, 18, 20, 28–30 (IMP4). INV1–INV9 не затрагиваются.

