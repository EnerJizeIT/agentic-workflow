# Архитектура: контекст стадии

Статус: REVIEW
Ревизия: arch-4 (req-9, rr-9 PASS; закрыты FR10, FR11, FR13, RR64)
Входы: `stage-context.md@req-9`; `requirements-review@rr-9`; `readiness@rd-2`; `context@ctx-3`; `test-spec@ts-3`; TODO-0374/0377; `.agentic/pipelines/*.yaml`; `.agentic/roles/`; `awf/`.
База кода: `main` @ `c523183`; код не менялся.

## 1. Решение

1. **Один сборщик:** блоки → один текст аргументом `opencode run`; `--file` и `build_prompt` уходят.
2. **Тип — `stage_types.<имя>`**: `<семейство>` или `reviewer/checklist`, из пайплайнов (`init` пишет, `doctor` сверяет).
3. **Блоки — данные `FAMILIES`**: пять строк и `checklist` с пределами `reviewer`.
4. **Потолок один — 9 000**; сумма пределов строки ≤ 9 000.
5. **Роль — существующий файл, выборка по `##`**.
6. **Текст пишется** в `.agentic/context/STAGE-<todo>-<stage>.md`, путь — в `CTX`.
7. **Замер — `CTX`** в `orchestrator.log` и `awf report`.

## 2. Точки изменения

- Типы — `awf/stage_context_types.py` (K1, K3, K4); конфиг — `awf/config_write.py` (K2), `awf/config_keys.py`; `init`/`doctor` — `awf/api/lifecycle.py`, `awf/cmd_init.py`, `awf/doctor.py`.
- Сборщик, рендеры, роль — `awf/stage_context*.py` (три новых); маскирование — `awf/redact.py` (новый), `awf/worker_tree.py`.
- Запуск — `awf/agent_stage.py`, `awf/supervisor.py`; старые бюджеты — `awf/verify_context.py`, `awf/rejection.py`, `awf/bounded_prompt.py`; пакет — `awf/context_pack.py`, `awf/cmd_report.py`.

Не меняются сигналы, коммит-гейт, DONE/REVIEW, allowlist, `STAGE-DIFF`, права, доктрина, схема.

## 3. Словарь блоков

Закрытый словарь R2, один владелец на блок. `DIFF` — `git diff --stat` (3 827 у TODO-0370), патч ссылкой; `GATES` — факты `verify_pack.read_gates_facts` (76), не файл (10 875). `COMMIT-AUDIT`, `METRICS`, `MISSING` в текст не входят; факт — `(fact_id, value, owner)`, расхождение — `CTX-CONFLICT` (AC8). Секрет — `<redacted: имя>`/`[redacted: файл-секрет]` (§9, AC27); блок остаётся.

## 4. Тип стадии

**Генерация (R14).** Источник — `.agentic/pipelines/*.yaml` (имя, роль): `plan` → `plan`, `verify` → `supervisor`, `spec-*` → `spec`, `checklist-*` → `reviewer/checklist`, иначе по роли: `supervisor`; `agent-architect-review`, `agent-qa-review`, `agent-project-auditor`, `agent-requirements-reviewer` → `reviewer`; остальные `agent-*` → `execute`. Позиция не участвует (AC21); роль вне карты — отказ.

**`init`/`doctor`.** `init` пишет `stage_types`, если ключа нет или он отличается; `config_write` меняет только блок с перечитыванием; повторный `init` конфиг не меняет (AC33, RR55). `doctor` сверяет имена и значения, rc≠0, ничего не пишет (RR58).

**Рантайм.** Нет/пуст ключ — `CTX-TYPE-MISSING` (AC40); нет имени — `CTX-TYPE-DEFAULT`, семейство по роли; `strict` — `CTX-TYPE-STRICT`; значение вне пяти семейств и строки `reviewer/checklist` — `CTX-TYPE-MISSING` (AC41, RR60). Подпроцесса нет.

## 5. Таблица «тип → блоки»

| Тип | Блоки в порядке текста |
|---|---|
| `plan` | `STAGE`, `PHASES`, `VISION`, `ROLE`, `DOCTRINE`, `STATE`, `PREVLINK`, `CRIT`, `RULES`, `OUTPUT` |
| `execute` | `STAGE`, `UNIT`, `REVIEW`, `DONE`, `BLOCKED`, `ROLE`, `DOCTRINE`, `STATE`, `PREV`, `PREVLINK`, `CRIT`, `RULES`, `OUTPUT` |
| `reviewer` | `STAGE`, `UNIT`, `INPUTART`, `DONE`, `DIFF`, `GATES`, `REVIEW`, `ROLE`, `DOCTRINE`, `PREV`, `PREVLINK`, `CRIT`, `RULES`, `OUTPUT` |
| `reviewer/checklist` | `STAGE`, `CHECK`, `STATE`, `CRIT`, `RULES`, `OUTPUT` |
| `supervisor` | `STAGE`, `STATE`, `DONE`, `DIFF`, `GATES`, `REVIEW`, `ROLE`, `DOCTRINE`, `PREVLINK`, `CRIT`, `RULES`, `OUTPUT` |
| `spec` | `STAGE`, `UNIT`, `INPUTART`, `REVIEW`, `ROLE`, `DOCTRINE`, `PREV`, `PREVLINK`, `CRIT`, `RULES`, `OUTPUT` |

Порядок (AC4): `STAGE`/`UNIT` в начале, `CRIT`/`RULES`/`OUTPUT` в конце, `REVIEW` — до материала (C8); `checklist` без роли, доктрины, находок и handoff-ов (C3).

## 6. Бюджет и обрезка

Потолок один — 9 000; `checklist` наследует предел `reviewer` (R3). Предел — доля блока, сумма по строке ≤ 9 000 (AC35). P0 не влез — `CTX-FAIL`; P1–P5 мягкие, остаток забирает R5.

| Блок | `plan` | `execute` | `reviewer` | `checklist` | `supervisor` | `spec` |
|---|---:|---:|---:|---:|---:|---:|
| `STAGE` | 400 во всех шести | | | | | |
| `UNIT` | | 1 800 | 1 800 | | | 2 800 |
| `CRIT` | 1 200 | 2 500 | 2 500 | 2 500 | 800 | 2 500 |
| `RULES` | 350 во всех шести | | | | | |
| `OUTPUT` | 800 во всех шести | | | | | |
| `REVIEW` | | 800 | 350 | | 500 | 450 |
| `DONE` | | 400 | 300 | | 400 | |
| `INPUTART` | | | 100 | | | 200 |
| `ROLE` | 900 | 1 200 | 400 | | 1 000 | 800 |
| `GATES` | | | 250 | | 300 | |
| `DIFF` | | | 500 | | 3 830 | |
| `DOCTRINE` | 250 | 150 | 50 | | 150 | 100 |
| `STATE` | 400 | 150 | | 300 | 400 | |
| `PREV` | | 250 | 50 | | | 400 |
| `PREVLINK` | 100 | 100 | 50 | | 70 | 200 |
| **Σ** | **5 600** | **9 000** | **7 900** | **5 150** | **9 000** | **9 000** |

Пустая клетка — блока нет. Вне матрицы: `CHECK` 800 (`checklist`), `PHASES` 700, `VISION` 500 (`plan`), `BLOCKED` 100; `reviewer` = 7 900, запас 1 100 (FR10).

**Фикстуры (AC36).** `execute` TODO-0374: `UNIT` 1 569 + `CRIT` 2 374 + `STAGE` 400 + `RULES` 350 + `OUTPUT` 800 = 5 493 (блоки; пределы 5 850, RR61), фон ссылкой (D5). `supervisor` verify TODO-0370: прежний промпт 11 983 (`A42`), `DIFF` 3 827 при 3 830, `GATES` 76 при 300, прочее по пределам — 8 773 ≤ 9 000, `CTX-FAIL` нет; длиннее — срез с пометкой (RR43).

**Рез** (R5): P5 `PREV`/`PREVLINK` → P4 `DOCTRINE`/`STATE`/`VISION` → P3 `GATES`/`DIFF`/`BLOCKED` → P2 `ROLE` → P1 `REVIEW`/`DONE`/`INPUTART`; в `REVIEW` общее первым, `BLOCKER` последним; срез оставляет «ещё N: см. `<файл>`» (AC10, AC34).

## 7. Роль

Маркер — `##`; девять файлов (`supervisor.md`, восемь `agent-*`). Шапка до первого `##` (500), затем разделы по приоритетам до предела `ROLE`; непопавшие — `- §<заголовок> — <путь>`.

| Приоритет | Маркеры |
|---|---|
| 1 | `Instructions`, `Инструкции`, `Quick Reference`, `Роль` |
| 2 | `Выход`, `Handoff contract`, `Отчёт`, `Готовность`, `Формат находок`, `Итог`, `Результат` |
| 3 | `Режим`, `Режимы`, `Вход` |
| 4 | `Запрет`, `Prohibitions`, `Проверка`, `Находки` |
| 5 | `AWF-OVERRIDE`, `Политика тестов`, `Дисциплина доставки инвариантов` |

**Покрытие.** `total` — ядро до предела, `taken` — вошедшее; `floor(taken × 100 / total)`, ниже 70 — `CTX-ROLE-LOW` (AC38, D3).

## 8. Сборка и контракты

Снимок → K3 → `FAMILIES` → рендер → порядок §5 → P0 проверяется, P1–P5 режутся → текст → файл → `CTX` → `opencode run`. Нет каталога или записи — `CTX-FAIL` до подпроцесса (AC31).

| ID | Контракт | Вход → выход |
|---|---|---|
| K1 | `generate_stage_types(pipelines)` | `{имя: значение}`; неизвестная роль — отказ |
| K2 | `write_stage_types(config_text, mapping)` | заменённый блок, идемпотентно |
| K3 | `resolve_stage_type(config, name, role)` | `(family, row, source)`; `StageTypeMissing/Default/Strict` |
| K4 | `engine_kind(family)` | `plan`/`execute`/`verify` для сигналов и гейта |
| K5 | `render_block(id, ctx)` | `Block(...)` или «нет данных: причина — путь» |
| K6 | `select_role_core(text, family, limit)` | `text`, `kept`, `taken`, `core_total` |
| K7 | `build_stage_context(...)` | `text`, `path`, `blocks`, `cut`, `missing`; пишет файл |
| K8 | `fit_blocks(blocks, cap)` | порядок §5, `cut`, `failed`; P0 не режет |
| K9 | строка `CTX` | `orchestrator.log` и раздел `awf report` по `(todo, stage, attempt)` |
| K10 | снимок | тип из конфига, только чтение, `CTX-SNAPSHOT` |
| K11 | `RULES`, `OUTPUT` | дословный контракт вывода и запретов |
| K12 | `redact(text)` | маскированный текст и число замен |

`context-pack --stdout` — предпросмотр K7 без записи.

Строки после сборки (`assignment`, `## Stage identity`, `retry_note`) входят в текст: файл равен аргументу (AC31). `replan`/`salvage` — `family=supervisor` (AC14). `STAGE-*.md` — не сигнал (INV10).

## 9. Нет данных, противоречия, секреты

Нет артефакта — «нет данных: <причина> — <путь>» (AC9); пустой блок — причина (AC25); `COMMIT-AUDIT` не блок (RR53). `CTX-CONFLICT`: `unit.status`, `gates.verdict`, `finding.<id>.state`, `stage.list` (AC8).

Секреты (RR17): владелец — `awf/redact.py`, `worker_tree.py` импортирует его. Именованное (`token|key|secret|password|auth|cookie`) → `<redacted: имя>`; файл-секрет (`.env*`, `*.pem`, `*.key`, `id_rsa*`) → `[redacted: файл-секрет]`; блок остаётся.

## 10. Снимок

`PIPELINE-<todo>.yaml` только читается (INV9), форма не расширяется (AC15). Старые снимки (213, 30 `implementer-close`) не правятся: имя вне карты — `CTX-TYPE-DEFAULT` (AC11, AC32); нет или пуст ключ, значение вне списка — `CTX-TYPE-MISSING` (AC40, AC41). Неизменность — `sha256`.

## 11. Фикс-круг и reuse

Находки — из `REVIEW`: новые входят, закрытые не пересказываются (AC12); `PREV` — один handoff, остальные ссылками (AC5). Reuse прежний, текст собирается заново, в `RULES` — «прошлая сессия не источник фактов» (AC13, D7).

## 12. Замер и отчёт

```
CTX <todo> <stage> attempt=<n> type=<family>[/<row>] cap=9000 total=<n>
    file=<abs> role=<взято>/<всего> role_pct=<n>
    blocks=<id>:<chars>,... cut=<id>:<chars>,... missing=<id>:<reason>,...
```

Пишется в `.agentic/logs/orchestrator.log` — журнал `awf report` и `budget` (FR11); отдельного файла нет. Пример: `CTX TODO-0374 spec-design attempt=1 type=spec total=5812 file=… role=610/820 role_pct=74 duration=42`.

Сигналы: `CTX-TYPE-DEFAULT`, `CTX-TYPE-MISSING`, `CTX-TYPE-STRICT`, `CTX-ROLE-LOW`, `CTX-CONFLICT`, `CTX-SNAPSHOT`, `CTX-FAIL`; останавливают `CTX-FAIL`, `CTX-TYPE-MISSING`, `CTX-TYPE-STRICT` (AC40, AC41; INV11 дополняет аналитик — RR62).

**`awf report`** (RR6): раздел «Контекст стадий» из строк `CTX` плюс `STAGE-DIFF` и `COMMIT-AUDIT`: тип, `total/cap`, `file=`, `role=` с процентом, блоки, срез, пропуски, длительность, витки, минуты QA (AC19, AC25, AC26).

**Контрольные точки (AC37).** В IMP3 и IMP4 после трёх юнитов три числа против базы `.agentic/done/TODO-0320…0370/handoff/*.md` (26 находок `BLOCKER`/`MAJOR` в 13 юнитах, максимум 4; окно 0338–0347 — медиана 12,12 мин): находки (второй столбец `BLOCKER`/`MAJOR`) — стоп при сумме ≥ 4; витки (`## REVIEW-фикс`) — стоп при любом блоке; медиана — стоп при > 14,5 мин. Порог строже базы (26 на 42 юнита; 0344 даёт 4 в одном — RR65): числа — константа `QUALITY_STOPS` (D9).

## 13. Решения

| ID | Решение | Основание |
|---|---|---|
| D1 | Тип — `stage_types.<имя>`; нет ключа или значение вне списка — отказ, имя вне карты — WARN; `init` пишет, `doctor` сверяет | R1/R14, A3 |
| D2 | Один сборщик и путь: `--file`, старый рендер, старые бюджеты уходят; нет записи — `CTX-FAIL` | R10/R13, AC14 |
| D3 | Роль — выборка по `##`, без переименований; покрытие — от ядра | R4/AC6, AC38 |
| D4 | P0 — `CTX-FAIL`, P1–P5 — рез; сумма пределов строки ≤ 9 000 | AC3/AC35 |
| D5 | Доктрина — дайджест, `DIFF` — diffstat, `GATES` — факты, `UNIT` — границы | AC36 |
| D6 | Handoff: последний текстом, остальные ссылками | AC5 |
| D7 | Замер — `CTX`; reuse прежний; противоречия — `CTX-CONFLICT` | RR6/AC19, AC13, AC8 |
| D8 | Роль вне карты: генерация — отказ, рантайм — `execute` + WARN | AC32 |
| D9 | Пороги качества — константа `QUALITY_STOPS` | AC37; RR65 |

A-arch1: P0 фикстур влезают в §6. A-arch2: `config.load` читает YAML словарём. A-arch3: `plan` — 5 600, `Stage.kind` из семейства. A-arch4: 5 493 — блоки, 5 850 — пределы (RR61). A-arch5: `checklist` — строка `reviewer`.

## 14. План IMP

**IMP1 — тип как данные.** R1, R7, R14; AC2, AC11, AC15, AC21, AC32, AC33, AC39, AC40. Файлы — §2 (K1–K4). Миграция одним коммитом: `init` пишет ключ, K3 читает, `_compute_kind` удаляется (RR55).

**IMP2 — один сборщик и файл.** R2–R6, R8–R10, R13; AC1, AC3–AC16, AC22–AC24, AC27, AC31, AC34–AC36, AC38. Файлы — §2. Зависимость IMP1. Миграция тем же коммитом: `--file`, `build_prompt`/`build_replan_context`, `resolve_prev_handoffs` и старые бюджеты уходят вместе с K7; флага нет (D2, AC14).

**IMP3 — отчёт и первая точка.** R11, R12 (часть); AC19, AC23, AC25, AC26, AC29, AC37. Файлы: `awf/cmd_report.py`, модель отчёта, `QUALITY_STOPS` (D9). Зависимость IMP2.

**IMP4 — эффект и вторая точка.** R12; AC17, AC18, AC20, AC28, AC30, AC37. Кода нет: окно 10 юнитов, три spec-рана, пересчёт базы, сравнение. Зависимость IMP3.

Покрытие: R1 §4/D1/K3; R2 §3/K5/K12; R3 §6/D4/D5; R4 §7/D3/K6; R5 §6/§9/K8; R6 §5/§8; R7 §10/K10; R8 §11; R9 §11/D7; R10 §8/D2; R11 §12/K9/D3; R12 §12/IMP3–IMP4/D9; R13 §8/D2; R14 §4/D1. INV1–INV11 не затрагиваются.
