# BACKLOG

> История закрытых записей — в [BACKLOG-archive.md](BACKLOG-archive.md).

---

## Открытые задачи

### RUN3-2026-09-22 · Отчёт супервизора topic-trainer — сценарий «аудит по доменам»

**Источник:** `~/Desktop/awf-supervisor-report-2026-09-22.md`. Сценарий: шесть аудит-слоёв, у каждого свой пайплайн и роль; находки сведены в один план. Забег RUN3: 6 юнитов.

⬜ **#1 Именованные пайплайны** — `awf pipeline-write` (CLI+MCP) и список пайплайнов; запись в `.agentic/pipelines/<имя>.yaml` без правки config и supervisor (`awf_start(pipeline=…)` имя уже принимает, создать его нечем).
⬜ **#2 Пайплайн на элемент очереди забега** — `awf_run_start(queue=[{todo_id, pipeline}, …])` (строки — совместимость) + `pipeline=` в `awf_dispatch_todo` (пишется в TODO, читается при запуске); иначе шесть слоёв = шесть одиночных стартов без бюджета/нот/отчёта.
⬜ **#3 Роль из глобального скилла** — `awf_add_role(..., from_skill="agent-security-auditor")` копирует содержимое скилла в `.agentic/roles/`; сейчас только пустой шаблон, а skill-файлы видны лишь форме setup.
⬜ **#4 БАГ: перевыдача TODO не снимает старый BLOCKED** — stale `outbox/BLOCKED-<id>.ready` держит `todos.is_closed` → `awf_status` пуст, `awf_start` без закрепления: «No active TODO». Чинить: `awf unblock TODO-NNNN` + автоснятие stale-закрытия (BLOCKED/ACK) при перевыдаче того же номера.
⬜ **#5 Убрать никогда не стартовавший TODO** — `awf todo-remove TODO-NNNN` (след в `done/`, лог) или `reset --stale`: `reset --orphans` требует `.ready`, а такие TODO инертны и путают.
⬜ **#6 `no_checkpoints` на одиночный старт** — параметр у `awf_start`/`awf_continue` (CLI+MCP), сейчас флаг есть только у забега.
⬜ **#7 `awf_current_step` на живом проекте** — пустой `goal` у настроенного проекта с архивом уводит в setup-ритуал; различать «новый» и «живой» проект.

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
