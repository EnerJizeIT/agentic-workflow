# BACKLOG

> История закрытых записей — в [BACKLOG-archive.md](BACKLOG-archive.md).

---

## Открытые задачи

### SMO · State-Machine Orchestration

**Status:** .1–.6 DONE (в архиве). Открыт только .7.

⬜ `.7` **Escape-hatch'и:** ПОСЛЕ dogfood. Собрать edge-cases с реальных сессий.
   **НЕ проектировать upfront.**

### QA-2026-08-10 · QA Roundtable — открытые пункты

**Status:** закрытые пункты — в архиве; ниже только то, что осталось.

⬜ `.14` **[HIGH] PID reuse TOCTOU** — `pipeline.py:58-65`. Между `os.kill(pid,0)`
   и `/proc/<pid>/cmdline` PID может быть переиспользован → awf_kill убивает не тот.
   P2 на pet-проекте (требует rapid PID cycling).
⬜ `.15` **[HIGH] threading.Thread cleanup** — `test_signals.py:246`, 3 места в
   `test_plan_checkpoint.py`. Daemon threads без join. Fix: `thread.join(timeout=...)`.
⬜ `.16` **[HIGH] Entry points coverage** — cmd_init.py 0%, cmd_status.py 10%,
   cmd_analyze_roles.py 0%, cmd_approve.py 0%.

⬜ `.24` **[MED] CSRF token** — `http_endpoint.py:94` no Origin/Referer → True.
⬜ `.25` **[MED] pipeline_state Disk I/O inside lock** — YAML serialize blocks
   thread pool. (Low priority для pet-проекта.)
⬜ `.29` **[MED] test_audit_followup TOCTOU** — 4 теста in-memory only, не file-based.
⬜ `.30` **[MED] test_verify.py edge cases** — partial failure, timeout, exit codes.
⬜ `.31` **[MED] Integration no full pipeline flow** — init→dispatch→start→verify→commit.
⬜ `.32` **Coverage критических путей** — verify.py 14%, plan_checkpoint.py 21%,
   wait_event.py 22%, _stack.py 27%, context.py 34%, setup.py 39%.

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

- `tools/awf.py` (~940 строк) — разбить по зонам (pipeline/state/forms) или схлопнуть через helper
- `plan_checkpoint.py` (201 строка, 21% coverage) — добавить покрытие при dogfood
- `supervisor.py` — разделить `wait_for_supervisor_signal` до новых stage kind
- Два HTTP-сервера: one-shot core + long-lived plugin
- Event journal — `.agentic/state/journal.jsonl` для replay
- Model discovery consolidation — `awf` как единственный владелец знания об opencode-окружении
