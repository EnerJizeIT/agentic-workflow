# BACKLOG

> История закрытых записей — в [BACKLOG-archive.md](BACKLOG-archive.md).

---

## Открытые задачи

### RUN6-2026-09-23 · Супервизор-фокус: фидбек с аудита topic-trainer + дашборд

**Status:** закрыто 23.09 забегом RUN6 (5 юнитов, 0 салважей, ~200 мин, `no_checkpoints`). Принцип владельца: «супервизор не думает об инструменте».
**Источник:** 4 отчёта `awf feedback` (22.09) + наблюдение владельца по дашборду.

✅ **#1 `done`-событие после approve** — wait_for_event отдаёт `done` с точной следующей командой (state очищен/phase=done + мёртвый pid + коммит/архив); супервизор больше не смотрит git log. `922c188`
✅ **#7 BUG дашборд «путал роли»** — движок пишет имя разрешённого пайплайна в pipeline_state; дашборд рисует стадии фактического TODO (state → очередь забега → front-matter → дефолт) и показывает имя чипом. `716e29a`
✅ **#2+#3 Тихий status + честный cap** — в забеге очередь «ждёт хода» (без «rollback or reset»); потолок ожидания из `wait.cap_seconds`/`AWF_WAIT_CAP` (наш: 300), suggested_timeout адаптивен до потолка, устаревший совет убран. `eb9fd9e`
✅ **#4+#5 `awf todo-update` + MCP `awf_tree_sha`** — правка TODO с сохранением номера (бэкап, отказ на стартовавшем); tree-sha в типизированном контуре (MCP == CLI). `b9332b8`
✅ **#6 Онбординг без подсказок** — бриф: Defaults + Scenarios + ссылка USAGE; `agents_md.py` (единый источник блока в глобальном AGENTS.md, «начни с awf_brief», самолечение при старте плагина); глобальный SKILL `awf-supervisor` (роль/цикл/ритуалы/карта/5 сценариев); описания всех тулов и next_action-свип под тестами. `2407f27`

⬜ **Хвост RUN6 (из QA 0056): ложный `done` после kill на остатках salvage.** Kill после salvage-попытки оставляет в state только `salvage_count`/`salvage_count_key` (маркеры стёрты) — при наличии прошлого цикла (архив/коммит) `wait_for_event` отдаёт `done` за СТАРЫЙ TODO. Восстановление работает (`run_next` откажет), но сообщение вводит в заблуждение. Фикс: «exited» = state отсутствует ИЛИ `phase=done` (без остатков salvage) + тест сценария «TODO-0001 завершён, TODO-0002 убит после salvage».

### RUN5-2026-09-22 · leak-гейт + todo-retire + релиз 1.2.0

**Status:** закрыто 22.09 забегом RUN5 (3 юнита, 1 сетевой салваж, 271 мин). Релиз 1.2.0 опубликован (PyPI: awf + agent-workflow-ui, установка из чистого venv проверена).

✅ **#1 Leak-гейт** — `REJECT-<todo>.files` при отклонении, `carry_over_from` при перевыдаче (пути исключаются из baseline → коммит их включает), WARNING в verify-pack при «утечке»; QA починил high-дефект (`run_next` съедал carry-over при пере-базелиновании). `a9b16aa`
✅ **#2 `awf todo-retire`** — архив отклонённого/брошенного активного TODO в `done/<id>/` с RETIRED-заметкой; наш призрак TODO-0035 убран этим инструментом. `2c37d3f`
✅ **#3 Релиз 1.2.0** — версии в 5 пиннингах, CHANGELOG [1.2.0] (14 пунктов), счётчики 45; PR #6 влит (`683e5f4`), тег `v1.2.0`. `9aa074a`

⬜ **БАГ: `retry_stage` не закрепляет TODO.** Воскресил salvage-стадию TODO-0052, но движок подхватил новейший активный TODO (0054) и запустил его сразу с QA, минуя implementer; run.yaml и current.yaml разошлись. Пришлось kill → `unblock` → пинованный `start --todo … --from-stage …`. Фикс: пиновать todo_id из salvage-состояния (класс AUD08-02 в retry-пути) + тест «несколько активных TODO — тот же id». Отчёт: `~/Desktop/awf-bug-20260922-retry-stage-voskreshaet-stadiyu-no-ne-zakreplyaet-todo.md`

ℹ️ Из практики: `awf_kill` нет в CLI (только MCP) — при недоступном MCP остаётся искать PID руками.

⬜ **Мелочь упаковки: у `awf` нет консольной команды.** В колесе нет `[project.scripts]` → после `pip install awf` доступен только `python -m awf`; локальный `~/.local/bin/awf` — это bash-обёртка репо. Добавить `awf = awf.cli:main` (или явно закрепить `python -m awf` в доках как единственный способ).

### RUN4-2026-09-22 · `awf brief` + фидбек-контур супервизора (решения владельца 22.09)

**Status:** закрыто 22.09 забегом RUN4 (2 юнита, 0 салважей, 31 мин).

✅ **#1 `awf brief`** — карточка погружения/восстановления (CLI+MCP): шапка (версия/проект/фаза), «что дальше», состояние (забег/бюджет/`no_checkpoints`/активные/флаги), **карта всех инструментов по ситуациям** (данные `awf/data/tool_map.yaml`, покрытие проверяется тестом в обе стороны), ритуалы, рецепты восстановления (`awf/data/recovery.md`) + доктрина проекта, «что нового» из CHANGELOG, фидбек-строка; ≤900 слов, детерминирован. `8df1b0d`
✅ **#2 Фидбек-контур** — `awf feedback --type bug|feature` (CLI+MCP): структурированный отчёт на рабочий стол (`feedback.dir`, факты собираются сами, транслит-слаг, суффикс при повторе, env не читается); строка-приглашение в промптах супервизора и USAGE. `ba3aa59`

⬜ **Дыра гигиены (из фидбека 22.09): отклонённый TODO остаётся активным.** `DONE`-файлы без сигнала (reject-путь не пишет `.ready`), `.ready` в inbox, `PROGRESS` есть → `unblock` не берёт (нет BLOCKED/ACK), `todo-remove` отказывает, `reset --orphans` не считает сиротой, `--tasks_only` слишком широко. Нужен явный путь вывода из активных без работы: `awf todo-retire TODO-NNNN` (архив в `done/` с причиной) или `todo-remove --rejected`. Отчёт: `~/Desktop/awf-bug-20260922-otklonennyy-todo-ostaetsya-aktivnym-done-bez-signala-reset.md`

### RUN3-2026-09-22 · Отчёт супервизора topic-trainer — сценарий «аудит по доменам»

**Status:** закрыто 22.09 забегом RUN3 (6 юнитов, 0 салважей, 208 мин). Источник: `~/Desktop/awf-supervisor-report-2026-09-22.md`.

✅ **#1 Именованные пайплайны** — `awf pipeline-write <имя> --role …` (CLI+MCP): пишет только `.agentic/pipelines/<имя>.yaml`, config/supervisor не трогает; `awf pipelines` + запуск по имени с внятной ошибкой на неизвестное. `2018003`
✅ **#2 Пайплайн на элемент очереди забега** — очередь принимает `{todo_id, pipeline}` (строки — совместимость, старые state читаются), `awf_dispatch_todo(pipeline=…)` пишет front-matter, `run_next` пробрасывает. `ad640b9`
✅ **#3 Роль из глобального скилла** — `awf add-role <роль> --from-skill <скилл>`: тело SKILL.md без YAML-шапки + провенанс; проектные скиллы затеняют глобальные; traversal закрыт. `b2ca7e9`
✅ **#4+#5 Гигиена состояния** — `awf unblock TODO-NNNN` (снимает stale BLOCKED/ACK, DONE неприкосновенен) + автоснятие при перевыдаче; `awf todo-remove` для не стартовавших (след в `done/`). `56af3d1`
✅ **#6 `no_checkpoints` на одиночный старт** — параметр у `start`/`continue` (CLI+MCP), процессный скоуп (env ребёнка), в config/state не пишется. `68771b7`
✅ **#7 `awf_current_step` и живой проект** — «живой» = настроен (пайплайн/роли, init-стаб не считается) И непустой `done/` → рабочая фаза вместо setup-ритуала. `2d71168`

ℹ️ Фланг foreground+no_checkpoints (из RUN2) остаётся открытым: в `run_next` флаг забега в guard не пробрасывается (через MCP недостижимо) — см. запись RUN2.

### RUN2-2026-09-22 · Баг-репорт из topic-trainer (забег 2, awf 1.1.0)

**Status:** закрыто 22.09 забегом RUN2 (3 юнита, 0 салважей). Источник: `~/Desktop/awf-bug-report-run2-checkpoints.md`.

✅ **B1+B4 · Чекпоинты забега** — флаг `no_checkpoints` (start→state→движок, уважается только активным забегом)
   + решение формы переживает смерть пайплайна (`context/CHECKPOINT-<todo>.json`, подхват без формы, `.consumed`).
   `f40b988`
✅ **B2 · Бюджет** — продуктивные минуты (elapsed − простой: чекпоинт-ожидание, salvage-обработка, net-backoff);
   status/brief/RUN-REPORT показывают оба числа. `bc7af08`
✅ **B3 · Потолок ожидания** — `TRANSPORT_CAP=55` в suggested_timeout, потолок + рецепт в `next_action`
   каждого ответа, заметки в USAGE (EN+RU). `db47cc0`
✅ **Право супервизора** — «replan/переписывание ТЗ/сплит без approve владельца; эскалация — только стоп-лист»
   в шаблонах supervisor.md и phase-run.md (обе копии) + USAGE (EN+RU). `db47cc0`

⬜ **Фланг B4:** прямой API `run_next(background=False)` в активном забеге с `no_checkpoints` получает ложный
   отказ «Foreground mode incompatible with BD-36» (через MCP недостижимо — там всегда background). Правка
   ~3 строки: прокинуть флаг забега в guard (зеркало `pipeline_engine.py:447-459`) + тест. Guard инцидента
   DF6-5 — менять вместе с владельцем.

**Работает хорошо (не трогать):** очередь забега по порядку; `awf_run_note` на дашборде;
`awf_run_finish` (внятный RUN-REPORT); salvage/retire-stage (трижды спас забег);
commit-механика verify (15 коммитов без сбоев).

### DOGFOOD-2026-09-20 · Хвосты после аудита и инцидента
**Status:** решения владельца 20.09. Источник: программа аудита 2026-09 (рунбук).

**Закрыто в ходе программы аудита:**
- ✅ **Подписи коммитов** — ветка пересобрана без корпоративных X.509-подписей (38 коммитов, force-push; теги `verified/*` и `audit-known-good` пересозданы; SHA в реестре обновлены). Новый head: `ffa5e12`.
- ✅ **Герметичность тестов** (U7a) — `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, личность через env + тест-страж.
- ✅ **Rollback-таймауты** (FU-19) — git diff/reset в rollback-пути через timeout; контракт-исключение закрыто.
- ✅ **Actions Node20** (FU-21) — checkout@v5, setup-python@v6.
- ✅ **`files:` в `_CONTRACT_KEYS`**, **`verify_pack._git_lines`**, **doc-формулировка гейтов**, **ратчет-исключение BACKLOG.md**, **DONE.json пример**, **CI-дубль** (test.yml удалён — ci.yml покрывает всё) — FU-21/FU-20.

**Открыто:**
⬜ **Кириллица в slugify** — плагин транслитерирует («QA Лид» → `qa-lid`), ядро нет (`qa`); pre-existing рассинхрон (FU-14). Перенести `_CYRILLIC_MAP` в `awf/api/_helpers.slugify_role`.
⬜ **Ротация логов не атомарна** — `awf/_log.py::_maybe_rotate` (unlink+rename) при двух конкурентных ротаторах может перезаписать архив (FU-17b2). Сделать atomic (rename с уникальным суффиксом + prune).
⬜ **Evidence-гейт в auto-mode watcher** — auto-verify watcher не гейтится evidence (недостижимо через `awf_run_next`, всегда auto=False) (FU-18). Реоткрыть, если появится auto-режим.
⬜ **Сканер timeout-контракта и алиасы** — AST-чекер ловит `subprocess.run`/`_sp.run`, но не алиас (`import subprocess as _s`). Расширить правило (refine).
⬜ **Флейк `test_salvage_signal` под xdist** — `tests/unit/test_salvage_signal.py::TestSalvageSignalDetection::test_verify_still_works_after_salvage_fix` даёт TimeoutError 2с в полном прогоне с `-n auto` (изолированно и в повторах — зелёный). Поднять таймаут теста или устранить гонку.


### SMO · State-Machine Orchestration

**Status:** .1–.6 DONE (в архиве). Открыт только .7.

⬜ `.7` **Escape-hatch'и:** ПОСЛЕ dogfood. Собрать edge-cases с реальных сессий.
   **НЕ проектировать upfront.**

### BD-35 · Per-role contribution tracking
**Status:** ждать real failure в dogfooding.

### DAUD-6 · plan_checkpoint.py → Jinja2
**Status:** отложить до scenario 2/3.

---

### NEG-2026-09 · Негативная регрессия — открытые слои

**Status:** слои 1–2 и находки NEG-1…3 DONE (в архиве).

**Инварианты (общие для сценариев):**
- нет orphan-сигналов в outbox после завершения стадии;
- state не заявляет salvage, если валидный сигнал принят;
- валидный сигнал не затирается (в том числе в race-окне);
- retry-бюджет сбрасывается на новом входе в стадию.

⬜ **Вопрос к дизайну:** крэш/зависание сейчас — hard stop без salvage-записки,
супервизор узнаёт только из статуса. Авторетраить или писать заметку —
решить до включения.
#### Слои 3–5 (не сделаны)

⬜ **Слой 3** — битые входы: порченый YAML state/pipeline, мусор в baseline,
   TODO с пустым телом. Ожидание: без трейсбеков, внятная ошибка или
   деградация.
⬜ **Слой 4** — opencode-шим: PATH-подмена бинарника, сценарные скрипты
   (пишет сигналы/файлы, падает, висит). Проверяет реальный subprocess-контур
   без модели.
⬜ **Слой 5** — mutation testing (mutmut) на `pipeline_engine` + `awf/api`:
   выжившие мутанты = непокрытая логика.

---

## Future scenarios

| Сценарий | Что | Сложность |
|---|---|---|
| 2 · Decision fork | Runtime ad-hoc forms | Low |
| 3 · Blockage recovery | Multi-step flow | Medium |
| 5 · Priority planning | Drag-and-drop UI | High |
| 6 · Onboarding wizard | Multi-form logic | High |

---

### ТИРАЖ-2026-08-11 · хвосты публикации

**Status:** пункты 1–18 и 20 сделаны в v1.0.0 (в архиве).

⬜ `.19` **Скриншоты/GIF dashboard в README** — нужен реальный pipeline run.
   3-4 скриншота: chat handoffs, TODO timeline, events, worker status.

⬜ `.21` **Демо-видео (2-3 мин)** — полный цикл от init до approve.

#### Отклонено / отложено

ℹ️ `.22` **GitHub Pages / ReadTheDocs** — отдельный effort, текущих .md файлов
   достаточно для начала.
ℹ️ `.23` **Docker образ** — интересная идея, но opencode требует локального
   окружения. Не приоритет.
ℹ️ `.24` **Экосистемная расширяемость (standalone, другие агенты)** —
   aspirational. Сейчас: "The missing orchestration layer for opencode".
ℹ️ `.25` **Английский как основной** — уже сделано. README.md (EN) primary,
   README.ru.md (RU) secondary.

---

### Заметки / кандидаты

- `agent_workflow_ui/src/agent_workflow_ui/tools/awf.py` (~1600 строк) — разбить по зонам (pipeline/state/forms) или схлопнуть через helper
- `plan_checkpoint.py` (689 строк, 87% coverage) — покрытие есть; оставшееся — через dogfood
- `supervisor.py` — разделить `wait_for_supervisor_signal` до новых stage kind
- Два HTTP-сервера: one-shot core + long-lived plugin
- Event journal — `.agentic/state/journal.jsonl` для replay
- Model discovery consolidation — `awf` как единственный владелец знания об opencode-окружении
- wait_for_event heartbeat (вариант «в» из R3): редкие сообщения «очередь + стадия + ETA» вместо тишины
- CLI `awf run` (start/next/status) — паритет управления забегом в bash

### Этап 6 спеки · остаток (решения владельца 21.09)

**Сделано** (ветка `post-audit`, теги `verified/*`, CI зелёный; `awf 1.1.0` опубликован на PyPI):
U8 `awf metrics`; U8b/U8d (модели, подписки, GLM по докам, дробные доли, кэш); U8c (супервизорский контур,
зеркало отчётов); U9 (доктрина в промптах); U11 (`tree-sha`, `awf mutations`, `awf todo-draft`).

⬜ **U10 · Модели ролей** — эскалация после отказа + разные модели у исполнителя и проверяющего.
   Отложено владельцем 21.09 (в конфиге пока только `vllm/llm`).
⬜ **U12 · Изоляция среды прогона** — отдельный пользователь/контейнер, чтобы прогон не мог
   сигналить процессам сессии. Отложено владельцем 21.09.
⬜ **PR `post-audit` → `main`** — ждёт вместе с U10 (main защищён, вливание только через PR).
   При следующем релизе поднять версию (1.1.1): артефакт PyPI 1.1.0 собран из ветки.

### Универсализация awf · бэклог направления (решение владельца 21.09)

⬜ **Не только разработка кода.** Целевые сценарии: системная аналитика, анализ требований,
   сбор документации по коду и др. Прежде чем менять код — ревизия завязок: что из
   `verification.*` (тесты/линт/сборка), коммит-гейта, diff-минимальности и blast-radius
   должно стать настраиваемым профилем задачи (код / документы / аналитика), а что —
   универсальным. Заводить внимательно, по одному шву, начиная с одного сценария.
   Обсуждение и декомпозиция — в спецификации программы аудита 2026-09 (архив), U13.

### Хвост процесса · коммит-гейт и отвергнутые попытки (2-й случай)

⬜ **Новые файлы отклонённого захода не попадают в коммит повтора.** Файл, созданный
   в отклонённом юните, на baseline повтора классифицируется как pre-existing untracked
   и исключается гейтом (случаи: `fdb3fd0` — модули FU-09; `f0fea09` — `awf/data/subscriptions.json`
   из отклонённого TODO-0035). Починить: считать untracked-снапшот от базы ветки, а не от
   момента dispatch, либо включать всё, что изменено в окне юнита (по mtime/списку путей).
   Место: коммит-гейт (`awf/commit_gate.py`) + dispatch-baseline.
