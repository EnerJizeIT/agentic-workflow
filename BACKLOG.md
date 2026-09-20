# BACKLOG

> История закрытых записей — в [BACKLOG-archive.md](BACKLOG-archive.md).

---

## Открытые задачи

### DOGFOOD-2026-09-20 · Хвосты после аудита и инцидента
**Status:** решения владельца 20.09. Источник: `~/Desktop/awf-audit/RECOVERY-RUNBOOK.md`.

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
