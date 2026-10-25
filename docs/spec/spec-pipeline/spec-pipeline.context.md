# Контекст: пайплайн спецификации и передача source_ref

Scope: spec-pipeline (SPEC-ран: только документы, поведение кода не меняем)
Ревизия: ctx-1
Входы: TODO-0252; общий протокол `agent-system-analyst/references/specification-pipeline.md`; `.agentic/pipelines/spec.yaml`; код awf на базе ниже
База кода: commit `3b7cb07944932cb72fb99455e9f0a1f1ca314edf`; на входе стадии рабочее дерево чистое (`git status --porcelain` пуст)
Статус: REVIEW

## 1. Итог для аналитика

Что уже есть и можно не изобретать:

- пайплайн `spec` из шести агентских стадий и финальной verify (`.agentic/pipelines/spec.yaml:1-61`); роли материализованы в `.agentic/roles/` из глобальных скиллов;
- движок стадий: порядок, ретраи, сигналы `DONE`/`BLOCKED`/`REVIEW-*`, проверка объявленного выхода стадии, verify с коммитом (`awf/pipeline.py`, `awf/pipeline_engine.py`, `awf/transitions.py`, `awf/signals.py`);
- `source_ref` в `dispatch_todo`: строка `<файл>#<секция>` превращается в блок `## Первоисточник` с чек-листом (`awf/api/dispatch.py:319-420`, вставка — `:731-735`);
- контракт юнита: `verify`, `gates`, `prove_red`, `files` (`awf/unit_contract.py:130-135`); `files` — жёсткая проверка «изменены только объявленные файлы» в verify-pack (`awf/verify_pack.py:413-427`);
- DoD-гейт с пробами `grep_present`, `grep_absent`, `file_exists`, `file_absent`, `json_field`, `command_rc` (`docs/design/dod-gate.md:37`, `awf/checklist_gate.py:493`) — готовый движок машинных проверок;
- «снимок» ревизии: `awf tree-sha`, `todo_sha` в плане забега (`awf/api/run.py:214`), `awf checklist hash`.

Чего нет — это и предстоит спроектировать:

- статусов `DRAFT/REVIEW/PASS/READY/BLOCKED` как данных. В движке есть только сигналы стадий; документы спецификации никто не читает;
- гейта готовности: ничто не мешает запустить разработку по пакету без `READY`;
- передачи пакета: `source_ref` принимает ровно одну пару «файл + секция»;
- ограничения объёма: бюджеты инструкций считают только `AGENTS.md`, `docs/contracts/*` и доктрину; `docs/spec/**` не измеряется ничем.

Главные ограничения, которые сужают решения:

- роли-авторы `agent-project-auditor` (стадия 0) и `agent-qa-review` (стадия 4) перечислены в `automation.readonly_roles`; им принудительно запрещены `edit`/`write` (`awf/_env.py:374-401`), писать они могут только через `bash`;
- `spec` существует только локально: в поставке awf его нет (`awf/templates/pipelines/` содержит `full` и `code-health`), а `.agentic/` в `.gitignore:18`;
- базовая команда проверок проекта не уложилась в 300 секунд (см. раздел 6).

## 2. Карта затронутых путей

| Пункт TODO | Вход | Компоненты | Данные и эффекты | Источник |
|---|---|---|---|---|
| 1. Пайплайн `spec` | `.agentic/pipelines/spec.yaml` | схема стадии, загрузчик, переходы, сигналы | `.agentic/state/current.yaml`, файлы сигналов в `outbox/` | `awf/pipeline.py:63-110`; `awf/pipeline_engine.py`; `awf/transitions.py:10` |
| 2. Гейт готовности | TODO, DONE/BLOCKED, verify | `signals.py`, `verify_pack.py`, `checklist_gate.py` | `context/GATES-<todo>.md`, `context/CHECKLIST-<todo>.json` | `awf/verify_pack.py`; `awf/checklist_gate.py` |
| 3. Передача в разработку | TODO dev-юнита | `dispatch_todo(source_ref=...)` | TODO-файл: блок `## Первоисточник`; baseline | `awf/api/dispatch.py:558, 635, 731`; `docs/unit-contract.md:212-238` |
| 4. Границы и объём | контракт TODO | `unit_contract.py`, секция diff в `verify_pack.py`, `scripts/instruction-gates.sh` | отчёт verify-pack; коммит юнита | `awf/unit_contract.py:130-135`; `awf/verify_pack.py:343-427` |
| 5. Приёмка dev-рана | тесты и гейты проекта | `scripts/run-all.sh`, `docs/contracts/` | зелёный прогон ворот | `scripts/run-all.sh`; `docs/contracts/` |
| 6. Ограничения среды | конфиг проекта | `awf/_env.py`, `awf/api/setup.py` | права ролей, состав поставки | `.agentic/config.yaml:35`; `awf/api/setup.py:76-79` |

Точки входа, через которые всё запускается: MCP `awf_dispatch_todo`, `awf_start`, `awf_approve`/`awf_reject`; CLI-эквиваленты в `awf/cli.py` (у dispatch — `--source-ref`, `awf/cli.py:301`).

## 3. Переиспользуемые компоненты и контракты

- **Схема стадии.** Допустимые ключи и политики: `name`, `role`, `id`, `task`, `input`, `output`, `tools`, `on_blocked`, `on_approved`, `on_rejected`, `on_failed`, `max_retries`, `max_rollbacks` (`awf/pipeline.py:108`). `kind` (plan/execute/verify) вычисляется по позиции, а не пишется в YAML.
- **Объявленный выход стадии.** `output:` — путь внутри проекта; движок требует, чтобы файл существовал и был не старше начала стадии (`awf/pipeline_engine.py:949-978`, проверка `:1763`). Сейчас у стадий `spec.yaml` этого поля нет.
- **Сигналы.** Execute-стадия принимает `DONE`, `BLOCKED`, `REVIEW-APPROVED`, `REVIEW-REJECTED` (`awf/signals.py:54-72`). Имя файла — `<PREFIX>-TODO-NNNN.ready`.
- **Контракт юнита.** Ключи `verify`, `gates`, `prove_red`, `prove_red_pin`, `prove_red_kind`, `prove_red_note`, `files`, `pipeline`, `replan_for`, `checklist_classes`, `wave` (`awf/unit_contract.py:130-135`). Валидация — до любых побочных эффектов.
- **`files` — граница диффа.** Если список объявлен, любой изменённый файл вне списка даёт `fail` секции в verify-pack (`awf/verify_pack.py:413-427`). В `docs/unit-contract.md` ключ не описан — см. CTX5.
- **Пробы DoD.** Проба — короткая читающая проверка; список типов закрыт, `command_rc` работает по allowlist без shell (`docs/design/dod-gate.md:37-58`). Годится для «шесть файлов на месте», «в readiness написано READY», но не для смысловой проверки матрицы.
- **Ревизии.** `awf tree-sha` даёт отпечаток дерева; `todo_sha` фиксирует содержимое TODO; `awf checklist hash` — хэш блока. Готового учёта ревизий документа (`req-3`, `arch-2`) нет: строка `Ревизия:` в шапке — пока только текст.
- **Роли из скиллов.** `add_role(from_skill=...)` копирует тело скилла и хранит происхождение (`awf/api/roles.py:1164-1300`); поиск роли идёт «проект → глобальные» (`awf/supervisor.py:401-415`).
- **Поставка пайплайнов.** Реестр встроенных стартеров — `BUILTIN_PIPELINES` (`awf/api/setup.py:76-79`); роли для стартера берутся только из `awf/templates/roles/`, скиллы там не читаются (`awf/api/setup.py:143-205`).

## 4. Поведение, которое нельзя сломать

- Пайплайны `default` и `code-health` и их политики переходов; `on_approved: next` у execute-стадий и `commit_and_next` у verify.
- Запрет `edit`/`write` для ролей из `readonly_roles`: в `default` это ревьюеры, которые не пишут исходники.
- Отказы `dispatch_todo` до побочных эффектов: нет файла/секции, неоднозначный заголовок, неверный `source_ref` — ни TODO, ни baseline не появляются.
- Нынешняя семантика `source_ref`: точное совпадение заголовка, тело до следующего заголовка того же или более высокого уровня, чекбоксы только из пунктов списка, проза и код-фенсы сохраняются.
- Изоляция коммита: коммит содержит только дифф против baseline; `.agentic/` не коммитится.
- Инварианты безопасности: `proc.pid > 1` в `awf/_proc.py` и tripwire `_forbid_session_kill` в `tests/conftest.py` — не трогать (см. `AGENTS.md`).
- Бюджет инструкций: 948 из 950 слов и 250 из 250 слов доктрины (`scripts/instruction-gates.conf:17,32`) — добавление текста в эти файлы требует удаления другого.

## 5. Тестовые средства

- `tests/negative/test_miniwave_m5_sourceref.py` — контракт `source_ref`: вставка секции, сохранение прозы и фенсов, отказы (нет файла, нет секции, выход за проект).
- `tests/unit/test_pipeline.py`, `tests/unit/test_pipelines_api.py`, `tests/unit/test_unit_contract.py` — схема пайплайна, запись пайплайна, ключи контракта.
- `tests/unit/test_pipeline_engine.py`, `tests/unit/test_run.py` — переходы, объявленный выход, verify-стадия.
- Ворота: `bash scripts/run-all.sh` (контракты, храповик, бюджет инструкций, тесты, линт по `scripts/project-commands.txt`).

## 6. Baseline

| Проверка | Команда | Каталог | Результат | Наблюдение |
|---|---|---|---|---|
| Контракты | `bash scripts/check-contracts.sh` | корень проекта | PASS | 8 контрактов, 6 контрактов юнитов, 0 сработавших матриц, 8 гейтов |
| Храповик | `bash scripts/ratchet.sh` | корень проекта | PASS | счётчики в baseline |
| Бюджет инструкций | `bash scripts/instruction-gates.sh` | корень проекта | PASS | 948/950 слов, доктрина 250/250 |
| Целевые тесты | `pytest tests/negative/test_miniwave_m5_sourceref.py tests/unit/test_pipeline.py tests/unit/test_pipelines_api.py tests/unit/test_unit_contract.py -q` | корень проекта | PASS | 153 passed за 5.03 с |
| Тесты движка | `pytest tests/unit/test_run.py tests/unit/test_pipeline_engine.py -q` | корень проекта | PASS | 133 passed за 8.45 с |
| Базовая команда проекта | `verification.baseline_cmd` из `.agentic/config.yaml` | корень проекта | NOT_RUN | таймаут 300 с: собрано 2385 тестов, основная масса — `tests/negative` |
| Полный сьют | `verification.test_cmd` | корень проекта | NOT_RUN | не запускался: стадия аудита не требует; safety-прогон перед полным сьютом отдельный |

`BASELINE-TODO-0252.tests.log` фиксирует тот же таймаут. Это ограничение измерения, а не дефект фичи: целевые тесты по затронутым путям зелёные.

## 7. Находки в scope

| ID | Важность | Файл / символ | Факт и доказательство | Влияние на фичу | Следующий шаг / владелец | Уверенность |
|---|---|---|---|---|---|---|
| CTX1 | MAJOR | `.agentic/config.yaml:35`; `awf/_env.py:374-401` | `agent-project-auditor` и `agent-qa-review` в `readonly_roles`; `edit`/`write` принудительно `deny`. Стадия 0 обязана писать контекст, стадия 4 — тест-спеку. В этой стадии инструментов `write`/`edit` действительно нет, файл создан через `bash`. | Авторы документов пишут в обход инструмента записи: нет защит managed-write (симлинки), граница «только этот файл» держится только на дисциплине. | Решение архитектора: убрать роли из `readonly_roles` для spec-рана, развести «роль» и «режим стадии», либо принять запись через `bash` и зафиксировать это в спеке. | Высокая |
| CTX2 | MAJOR | `awf/signals.py:54-72`; `awf/verify_pack.py`; `awf/api/hygiene.py:1038` | Статусов `DRAFT/REVIEW/PASS/READY/BLOCKED` в движке нет. `unit_readiness` в hygiene означает другое — «юнит ещё не начат». Гейта, читающего readiness-документ, нет. | Пункт «не пускать в разработку без READY» не на что опереть: нужен новый носитель статуса и проверка. | Архитектор: выбрать носитель (файл, блок в TODO, артефакт состояния) и точку отказа. | Высокая |
| CTX3 | MAJOR | `awf/pipeline.py:64`; `awf/pipeline_engine.py:949,1763`; `.agentic/pipelines/spec.yaml` | Механизм «объявленный выход стадии» есть, но ни одна стадия `spec.yaml` не объявляет `output`. | Стадия может отправить `DONE`, не создав свой файл; самая дешёвая машинная проверка «файл появился» не используется. | Архитектор/дев-ран: добавить `output` каждой стадии и тест на отказ при отсутствии файла. | Высокая |
| CTX4 | MAJOR | `awf/api/dispatch.py:319-356, 395-420` | `source_ref` — ровно одна пара «файл#секция», заголовок обязан совпасть точно; чекбоксами становятся только пункты списка. Таблица матрицы покрытия остаётся прозой. | Передать «весь READY-пакет» одной ссылкой нельзя; матрица покрытия не превратится в проверяемый список. | Архитектор: либо нормативная часть readiness оформляется списком в одной секции, либо расширяется `source_ref` (несколько ссылок, таблицы). | Высокая |
| CTX5 | MAJOR | `awf/unit_contract.py:130-135, 592`; `awf/verify_pack.py:413-427`; `docs/unit-contract.md` | Ключ `files` валидируется и жёстко проверяется в verify-pack, но в документации контракта его нет. В TODO-0252 ключ не объявлен, блока `## Чек-лист завершения` тоже нет. | Правило «диф только `docs/spec/spec-pipeline/**`» сегодня держится вручную; механизм готов, но не включён и не описан. | Аналитик/архитектор: включить `files` в контракт dev-юнита и описать ключ; дев-ран обновляет `docs/unit-contract.md`. | Высокая |
| CTX6 | MINOR | `awf/api/hygiene.py:1290-1299` | `source_ref` есть только у `dispatch_todo`; `update_todo`, `review_append`, `restore` его не принимают. | После replan или REVIEW-фикса норму приходится вставлять вручную; риск потерять первоисточник. | Архитектор: решить, нужен ли перенос `source_ref` в правки TODO. | Высокая |
| CTX7 | MINOR | `awf/api/hygiene.py:1038` | Имя `unit_readiness` уже занято смыслом «юнит не начат». | Новый предикат «спек-пакет готов» с похожим именем будет путать читателя и API. | Архитектор: выбрать отдельное имя (например, `spec_ready` / `spec_gate`). | Средняя |
| CTX8 | MINOR | `awf/api/setup.py:76-79`; `awf/templates/pipelines/`; `.gitignore:18` | `spec` не входит во встроенные стартеры; в поставке только `full` и `code-health`. Роли `agent-system-analyst`, `agent-architector`, `agent-requirements-reviewer` в `awf/templates/roles/` отсутствуют, а `.agentic/` не коммитится. | Пайплайн живёт только на этой машине. Для переносимой поставки нужны шаблон пайплайна и шаблоны ролей (или материализация из скиллов в setup). | Архитектор/дев-ран: включить в план поставку стартера и ролей. | Высокая |
| CTX9 | MINOR | `scripts/instruction-gates.conf:7,17,31,32`; `.ratchet-counters` | Бюджеты инструкций на пределе (948/950, 250/250); `docs/spec/**` в них не входит; счётчика длины документов в храповике нет. | «Защита от графомании» не может опереться на существующий бюджет без чистки; для лимита объёма спеки нужен отдельный счётчик или проверка. | Архитектор: задать измеримый лимит (слова/строки) и место проверки. | Высокая |
| CTX10 | MINOR | `.agentic/context/BASELINE-TODO-0252.tests.log` | `baseline_cmd` упал по таймауту 300 с; собрано 2385 тестов, основная масса — `tests/negative`. | Baseline не подтверждён целиком. Влияние на фичу низкое: спека не меняет код; целевые тесты зелёные. | Супервизор/QA: при реализации гонять полный сьют отдельно, не через baseline-снимок. | Высокая |

## 8. Несвязанные находки

- В `inbox/` лежат ещё пять активных юнитов (0243, 0245, 0246, 0250, 0251). Запуск без явного `todo_id` выбирает «самый новый активный» — для spec-рана нужен пин, он уже стоит в `state/run.yaml`.
- `docs/playbooks/` отсутствует, хотя `HASH_FILES` в `scripts/instruction-gates.conf:25` на него ссылается. Гейт проходит на пустом глобе; на поведение спеки не влияет.

## 9. Вопросы и непроверенные предположения

- Q1 (архитектор): передача в разработку — одна ссылка на нормативную секцию readiness или несколько ссылок на файл+секцию? От ответа зависит, расширять ли `source_ref`.
- Q2 (архитектор): где живёт вердикт READY — отдельный файл, блок в TODO или артефакт состояния? От этого зависит, чем проверять «нет READY — нет разработки».
- Q3 (архитектор): `readonly_roles` — менять конфиг проекта (влияет и на `default`) или оставить запись через `bash` и закрепить это правилом?
- Q4 (архитектор): `spec` поставляется как встроенный стартер (нужны шаблоны ролей и запись в `BUILTIN_PIPELINES`) или остаётся локальной конфигурацией?
- Допущение A1: отсутствие инструментов `write`/`edit` в этой стадии вызвано именно `readonly_roles`; проверено косвенно (конфиг + фактический набор инструментов), прямого лога разрешений нет.
