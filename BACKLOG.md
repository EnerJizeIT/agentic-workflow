# BACKLOG

> История закрытых записей — в [BACKLOG-archive.md](BACKLOG-archive.md).

---

## Открытые задачи

### SMO · State-Machine Orchestration — завершён (.1-.6), .7 после dogfood

**Status:** .1-.6 DONE. .7 parked (after dogfood).

✅ `.1` **Foundation:** `awf/phase.py` — `detect_phase()` + `get_phase_prompt()`
   + `advance_phase()`. Phase в state. 3 новых tool: `awf_current_step`,
   `awf_set_goal`, `awf_confirm_normalized`. Backward compat: нет phase → full supervisor.md.

✅ `.2` **Split supervisor.md:** `templates/roles/supervisor/_core.md` (~40 строк)
   + `phase-{init,goal,form,normalize,brief,run,verify}.md` (each ~30-60 строк).
   Старый `supervisor.md` сохранён для backward compat.

✅ `.3` **Goal step:** `awf_set_goal(goal)` → stores in state → advances goal→form.

✅ `.4` **Form step:** `phase-form.md` — supervisor recommends roles based on goal,
   opens `awf_open_project_setup_form`.

✅ `.5` **Normalize step:** `phase-normalize.md` — 3-part checklist.
   `awf_confirm_normalized()` — gate, advances normalize→brief.

✅ `.6` **Brief step:** `phase-brief.md` + existing R5 pipeline_engine detection.
   Orchestrator writes phase at transitions (brief→run→verify→done).

⬜ `.7` **Escape-hatch'и:** ПОСЛЕ dogfood. Собрать edge-cases с реальных сессий.
   **НЕ проектировать upfront.**

### AUD-12 · [T3] Рефакторинг (закрытые пункты)

✅ `.1` — `_xdg_config_home` ×3 → consolidated в `awf.xdg`
✅ `.2` — `open_form` scans → lazy (только project-setup)
✅ `.3` — `awf.py` wrappers → `_exec` helper
✅ `.5` — `_extract_stage_info_regex` → removed (90 строк)
✅ `.4` — **Resolved by AUD-2026-08-09**: pipeline_engine импортирует напрямую
   из source-модулей. orchestrator больше не re-export hub. 15 re-exports удалено.

### AUD-2026-08-09 · Roundtable audit (9 принятых из 11, все закрыты)

**Source:** roundtable audit (Архитектор + Ревьюер + Чистильщик + Безопасник).
**Rejected:** mock-heavy orchestrator tests (catches real regressions), CSRF bypass
(local-only tool, token = overkill).

#### T2 — точечные фиксы

✅ `.1` **[HIGH] jinja2 undeclared dep** — добавлен в root pyproject.toml deps.
✅ `.2` **[MED] rollback path traversal** — regex `^TODO-\d{4,}$` в rollback().
✅ `.3` **[MED] commit_gate wrong baseline fallback** — warning + all untracked.
✅ `.4` **[MED] git commit no reset on failure** — git reset на commit failure.
✅ `.5` **[LOW] dashboard broad except** — return "dead" вместо false "running".

#### T1 — cleanup

✅ `.6` **Dead test classes** — удалены.
✅ `.7` **assert True test** — переписан с реальными assertions.

#### T3 — рефакторинг

✅ `.8` **Swallowed exceptions** — logging добавлен в 5 местах.
✅ `.9` **model_check refactor** — извлечены _load_opencode_config,
   _load_cli_models, _load_recent_models.
✅ `.10` **signal_watch refactor** — извлечены _build_pre_snapshot,
   _detect_new_signal, _sort_key_by_numeric_id.
✅ `.11` **supervisor refactor** — извлечён _prepare_supervisor_stage (4 kind branches).
✅ `.12` **orchestrator import proxy** — pipeline_engine импортирует из source.
   15 re-exports удалены. Per-file F401 ignore удалён.
✅ `.13` **test boilerplate** — _init_proj_dirs helper, убрано 5x дублирование.
ℹ️ `.14` **plugin coupled to awf internals** — vision не содержит "agnostic" claims
   (обновлён ранее). Coupling acknowledged, не блокирует.
ℹ️ `.15` **pytest-timeout** — pytest-timeout установлен, warning исчез.

### QA-2026-08-10 · QA Roundtable audit (полный отчёт)

**Source:** QA roundtable (Bug Hunter + Edge Case + Test Coverage + Security).
**Спорные закрытые аудитором:** verify.py log overwrite (not a bug), verify.py shlex
injection (non-issue without shell=True).

#### P0 — критические (все закрыты)

✅ `.1` **Python 3.10+ syntax vs >=3.9** — `requires-python = ">=3.10"`.
✅ `.2` **Jinja2 XSS via from_string** — `autoescape=True` (was OFF).
✅ `.3` **pytest tests/unit/ hangs** — conftest intercepts `subprocess.Popen`.

#### P1 — серьёзные

✅ `.4` **OPENCODE_CONFIG_CONTENT leak** — only permission field serialized.
✅ `.5` **yaml.safe_load config.py** — try/except with graceful error.
✅ `.6` **yaml.safe_load pipeline.py** — try/except with graceful error.
✅ `.7` **Signal watch blindspot** — all 6 prefixes, not just DONE/BLOCKED.
✅ `.8` **TODO archived without commit check** — maybe_commit returns bool.
✅ `.9` **Submitting forms stuck** — TTL auto-revert after 10min.
✅ `.10` **inputs/*.yaml chmod** — 0o600 after write.
✅ `.11` **Subprocess timeouts** — git_utils, context, lifecycle (30s).
✅ `.12` **Frontmatter regex** — `[ \t]*` вместо `\s*` (precise delimiter).
✅ `.13` **HTTP unicode form_id** — `isascii()` check added.
⬜ `.14` **[HIGH] PID reuse TOCTOU** — `pipeline.py:58-65`. Между `os.kill(pid,0)`
   и `/proc/<pid>/cmdline` PID может быть переиспользован → awf_kill убивает не тот.
   P2 на pet-проекте (требует rapid PID cycling).
⬜ `.15` **[HIGH] threading.Thread cleanup** — `test_signals.py:246`, 3 места в
   `test_plan_checkpoint.py`. Daemon threads без join. Fix: `thread.join(timeout=...)`.
⬜ `.16` **[HIGH] Entry points coverage** — cmd_init.py 0%, cmd_status.py 10%,
   cmd_analyze_roles.py 0%, cmd_approve.py 0%.

#### P2 — качество и риски

✅ `.17` **AWF_SUPERVISOR_TIMEOUT** — restored after pipeline.
✅ `.18` **forms.py deepcopy** — prevents LLM mutation.
✅ `.19` **pipeline_engine plan→verify** — explicit return.
✅ `.20` **roles_processor path validation** — symlink escape check.
✅ `.21` **todo_id validation** — regex in approve_commit + create_baseline.
✅ `.22` **signal_watch worker log chmod** — 0o600.
✅ `.23` **state.py _load_persisted** — filters expired/submitted.
⬜ `.24` **[MED] CSRF token** — `http_endpoint.py:94` no Origin/Referer → True.
⬜ `.25` **[MED] pipeline_state Disk I/O inside lock** — YAML serialize blocks
   thread pool. (Low priority для pet-проекта.)
✅ `.26` **agent_stage handoff** — sort by numeric ID, not mtime.
✅ `.27` **plan_checkpoint TOCTOU** — port=0, OS assigns free port.
✅ `.28` **forms.py template whitelist** — project-setup, increment-planning, ack.
⬜ `.29` **[MED] test_audit_followup TOCTOU** — 4 теста in-memory only, не file-based.
⬜ `.30` **[MED] test_verify.py edge cases** — partial failure, timeout, exit codes.
⬜ `.31` **[MED] Integration no full pipeline flow** — init→dispatch→start→verify→commit.
⬜ `.32` **Coverage критических путей** — verify.py 14%, plan_checkpoint.py 21%,
   wait_event.py 22%, _stack.py 27%, context.py 34%, setup.py 39%.

#### P3 — minor

✅ `.33` **orchestrator int(cli_timeout)** — try/except.
✅ `.34` **signals.py _short_id** — removed alias.
✅ `.35` **commit_gate APPROVE_TIMEOUT** — lazy _get_approve_timeout().
✅ `.36` **transitions unknown signal** — escalate (was dead-end stop).
✅ `.37` **Test quality** — as_dict content check, log_tail proper test.
ℹ️ `.38` **Accepted** — with-block close is standard pattern, child fd inherited at fork.
✅ `.39` **FIXED** — distinguishes null vs missing key.
✅ `.40` **FIXED** — requires markdown context (bold, checkbox, start-of-line).
✅ `.41` **FIXED** — timestamped backups, keep last 3.
✅ `.42` **FIXED** — reject whitespace-only .md (was accepted).
✅ `.43` **FIXED** — duplicate removed.
ℹ️ `.44` **_atomic.py cross-filesystem** — accepted (mkstemp(dir=...) гарантирует same FS).
ℹ️ `.45` **cmd_baseline depends on git binary** — by design (git is prerequisite).

### BD-35 · Per-role contribution tracking
**Status:** ждать real failure в dogfooding.

### DAUD-6 · plan_checkpoint.py → Jinja2
**Status:** отложить до scenario 2/3.

---

### NEG-2026-09 · Негативная регрессия (ТЗ + прогон)

**Зачем:** happy-path регрессия не поймала ни одной из реальных находок
(silent exit, salvage без эскалации, вводящий в заблуждение handoff).
Негативный слой проверяет поведение awf при отказах: реакция на смерть
воркера, состояние, инварианты.

**Инварианты (общие для сценариев):**
- нет orphan-сигналов в outbox после завершения стадии;
- state не заявляет salvage, если валидный сигнал принят;
- валидный сигнал не затирается (в том числе в race-окне);
- retry-бюджет сбрасывается на новом входе в стадию.

#### Слой 1 — матрица отказов воркера (`tests/negative/test_worker_failure_matrix.py`) ✅

| Сценарий | Ожидаемая реакция awf | Статус |
|---|---|---|
| Тихий выход ×2, затем DONE | 2 авторетрая с push → успех | ✅ |
| Тихий выход с реальным diff | без ретрая → salvage | ✅ |
| Stale-сигнал от прошлой попытки | не считается успехом | ✅ |
| Крэш (exit≠0) / зависание | hard stop | ✅ |
| DONE в окне ожидания | успех, без ретрая | ✅ |
| DONE в зазоре перед cleanup | сигнал не затирается | ✅ FIX |
| Отказ с фантомной rollback-целью | эскалация вместо stop | ✅ FIX |

⬜ **Вопрос к дизайну:** крэш/зависание сейчас — hard stop без salvage-записки,
супервизор узнаёт только из статуса. Авторетраить или писать заметку —
решить до включения.

#### Слой 2 — матрицы решений (`tests/negative/test_transition_matrix.py`) ✅

| Что | Проверка | Статус |
|---|---|---|
| `resolve_transition`: 8 политик × 9 сигналов | валидные действия, цель только у rollback | ✅ |
| Неизвестный сигнал | всегда escalate (P3-гарантия) | ✅ |
| `signal_type`: 23 мусорных имени | тотальность + детерминизм | ✅ |
| Множественные сигналы | побеждает свежий (mtime) | ✅ |
| `.ready` + пустой `.md` | не сигнал | ✅ |
| `clean_stage_signals` | не трогает чужие префиксы, идемпотентен | ✅ |
| Rollback-цели в pipeline.yaml | warning при загрузке | ✅ |
| Генератор пайплайна | явные безопасные политики | ✅ FIX |

#### Находки прогона

✅ **NEG-1 · TOCTOU: сигнал затирается в retry-cleanup.** Между таймаутом
ожидания и `clean_stage_signals` мог лечь валидный DONE — очистка его
удаляла, стадия перезапускалась впустую, а при крэше следующей попытки
сигнал терялся. Фикс: повторное чтение сигналов перед очисткой
(`pipeline_engine.py`).

✅ **NEG-2 · Фантомная rollback-цель по умолчанию.** `on_rejected/on_failed`
по умолчанию указывали на стадию `implement`, которой нет в сгенерированных
пайплайнах (стадии названы по ролям: `agent-implementer`). Любой
REVIEW-REJECTED / TEST-FAILED → «Rollback target 'implement' not found» →
жёсткая остановка посреди прогона. Фикс: дефолт `escalate`, генератор пишет
явные политики, loader предупреждает о несуществующих целях, диспетчер при
фантомной цели эскалирует вместо остановки.

✅ **NEG-3 · Флейк `test_maybe_commit_auto_accepts_ack_signal_bd17`.**
Тест патчил глобальный `time.sleep` и требовал пустой список вызовов, но
`git`-команды идут через `subprocess.run(..., timeout=...)`, а CPython
внутри `Popen.wait(timeout)` busy-wait'ит микро-sleep'ами (1 µs → ×2 → кап
50 мс). На нагруженной машине git живёт дольше → в список попадают десятки
чужих снов → ложное падение (2 раза за день, оба под нагрузкой). Фикс:
проверяем отсутствие именно `APPROVE_POLL_INTERVAL` (2 с) — сабпроцессные
микро-sleep'ы до 50 мс его не имитируют. Механизм доказан демо-скриптом:
запись `[0.001, 0.002, 0.004, ..., 0.05]`.

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

### NEG-2026-09-18 · День 2: сигналы, auto-DONE, восстановление после BLOCKED

**Source:** `awf-bug-report-day2-signals.md` (TODO-0009, topic-trainer).

✅ **DAY2-1 · Auto-DONE на чужом диффе (критично).** Реальный механизм
инцидента: `attempt_auto_done` видел НЕПУСТОЙ дифф (2 строки, оставленные
QA-стадией) и «работа + verify зелёный» → синтезировал DONE для стадии,
чей воркер не написал ни строки. Пайплайн «прошёл» стадию реализации
с пустым результатом. Версия репорта про «старый сигнал» не подтвердилась:
файл стадии 1 чистился при старте стадии 2.

Фикс: `verify.work_fingerprint` — хэш `git diff <baseline>` + untracked
(минус baseline-снапшот). Снимок на входе в стадию, сравнение после
прогона: auto-DONE и F7-ретрай смотрят только на работу ЭТОЙ стадии.
Плюс `NEG-4`: потребление `.ready` на переходе (`DONE.ready` удаляется,
`DONE.md` остаётся как улика) — одно имя больше не валидно для всех
последующих стадий.

✅ **DAY2-2 · Replan-ожидание самоудовлетворялось (критично).**
Orphan-pickup (UX «создал TODO → awf start») работал и в replan: мгновенно
«завершал» ожидание тем самым TODO, который ретраился, а `_find_active_todo`
его отфильтровывал (BLOCKED) → «Supervisor did not create a new TODO.
Stopping.» за секунду. ACK супервизора потом читать было некому.

Фикс: для replan принимаются только сигналы, СОЗДАННЫЕ ПОСЛЕ эскалации
(mtime), плюс ACK/APPROVE текущего TODO. `_handle_escalate` по ACK:
`_unblock_todo` (BLOCKED → `.agentic/context/`) и ретрай той же стадии.

✅ **DAY2-3 · `awf continue` не оживлял blocked TODO.** Закрытый ACK/BLOCKED
TODO не виден `newest_active`, CLI рано печатал «No active TODO found»,
а `--from-stage` и ACK-файл не помогали. Плюс ACK, написанный после смерти
процесса, был тупиком.

Фикс: `continue` резолвит pending-закрытие — потребляет ACK/APPROVE
(только когда TODO реально закрыт: APPROVE живого verify не трогается),
переносит BLOCKED в context/, резюмирует с `state.stage_name`. Без ответа —
внятная инструкция вместо тупика. Новый флаг: `awf continue --ack TODO-NNNN`
(api + CLI + MCP `awf_continue(ack=...)`). Убран преждевременный CLI-пречек.

✅ **DAY2-4 · `awf_retry_stage` сломан** — уже исправлен вчера (fbe61b7);
проверено в живом окружении плагина: `awf.api.retry_stage` доступен.

**Тесты:** +17 негативных сценариев (`tests/negative/test_escalation_recovery.py`,
`TestAutoDoneScope` в `test_worker_failure_matrix.py`). Всего 1221, ruff чист.

### DAY3-2026-09-18 · Ревью дашборда (волна 1)

**Source:** разбор кода + реальный проект topic-trainer (папка `handoff/`).

✅ **1. Чат-свалка handoff'ов.** `_read_handoffs` читал весь каталог: файлы
   старых TODO и легаси-имена агентов показывались как текущие. Причины: awf
   сам учил агентов писать `{role}.md` (`agent_stage.py`), агенты писали
   `-final`-варианты, архивация брала только `*-{todo}.md`. Фикс: инструкция
   переведена на PROGRESS/DONE-заметки (handoff собирает awf), архивация
   ловит `*-{todo}.md` и `*-{todo}-*.md` двумя точными глобами, дашборд
   фильтрует по текущему TODO и дедуплицирует роли (канон побеждает).
✅ **2. Чат не обновлялся** при перезаписи handoff'а или смене TODO с теми же
   длительностями. Ключ перерисовки теперь `role|rev` (mtime + размер).
✅ **3. Порт переживает рестарт:** оркестратор переиспользует прежний порт из
   `state/dashboard_port` (fallback на случайный, если занят) — старая вкладка
   оживает сама вместо «Pipeline exited».
✅ **4. Timeline: реальные commit sha** одним `git log` (поиск
   `awf(verify): TODO-NNNN`); раньше искался несуществующий
   `done/{id}/BASELINE.sha` — tooltip'ы были всегда пустыми.
✅ **5. XSS:** markdown-исходники (TODO/handoff) экранируются до рендера;
   `<` в embedded INITIAL_STATE JSON экранируется (`\u003c`).
✅ **6. CORS `*` снят** с `/api/state` (страница отдаётся с того же origin).

✅ **Волна 2 (сделано).**
- `elapsed_frozen` рендерится в нижнем регистре (`'true'`), JS больше не
  пропускает заморозку до первого поллинга.
- Initial paint: страница делает немедленный `poll()` сразу после первой
  отрисовки — встроенный снапшот может быть на стадию старым.
- Notifications просятся на первом жесте (клик/клавиша) — Chrome молча
  отказывает автоматическим запросам.
- Чат автоскроллит вниз только если пользователь уже был у нижней кромки.
- `poll()` считает HTTP 500 сбоем (не только сетевые ошибки).
- Worker PID: выбирается ребёнок с `opencode` в cmdline (фолбэк — первый);
  раньше панель могла показать мёртвый чужой PID.
- **Handoff по стадии, а не по роли** (`{stage}-{todo}.md`): два QA-этапа
  больше не перезаписывают handoff друг друга, длительности в чате
  совпадают со стадией; для файлов до перехода — легаси-фолбэк по роли.
- **Структурный рефактор:** `generate_dashboard` строит Jinja-контекст из
  одного источника (`generate_state_dict`) — удалено ~180 строк дублирующей
  логики (статус/elapsed/handoffs считались дважды и уже расходились).

---

### SPEC A-run · Автономный забег (v1) — сделан

**Source:** `awf-autonomous-run-spec.md` (супервизор topic-trainer). Владелец
выбрал вариант (а): супервизор-чат в цикле ожидания, awf даёт механику.

✅ **A-run.1** — состояние забега `.agentic/state/run.yaml` (`awf/run_state.py`),
   `awf_run_start/status/next/finish` (api + MCP), статус в `awf_status.run_state`,
   чип «🏃 забег 2/3 · ~40м» в дашборде.
✅ **A-run.2** — цикл через `awf_wait_for_event`: `suggested_timeout` (медиана
   длительностей стадий / 3, кламп [60,300]), `actionable_only` (не будить на
   stage_changed), next_action забега в каждом событии; verify-payload несёт
   diff-stat против baseline.
✅ **A-run.3/A-run.8** — evidence-gate: в забеге `awf_approve` без evidence
   отклоняется; evidence → `RUN-EVIDENCE-{todo}.md`, отчёт ссылается на него.
✅ **A-run.4** — механические гейты: queue exhausted, бюджет, stop-флаги
   (манифест очереди), два reject'а, `rollback(hard)` в забеге запрещён.
✅ **A-run.5** — reject-счётчик: второй отказ по TODO останавливает забег.
✅ **A-run.6** — `RUN-REPORT-{ts}.md` в outbox: очередь, завершённые, отказы,
   дневник вердиктов, health (salvage-события).
✅ **A-run.7** — состояние переживает смерть процесса (run.yaml, awf_run_status).
⬜ Не сделано сознательно: «вероятность мусора» в callback (эвристика);
   авто-стоп «verify красный дважды» (перекрыт reject-лимитом и stop-флагами).

---

✅ **SPEC-2026-09-18 · Спека супервизора topic-trainer — взятое в работу.**
- **D9**: дизамбигуация ролей обновляется автоматически после сборки
  пайплайна (`apply_project_setup` → `analyze_roles_core`); тесты: первое
  наполнение + пересборка со сменой позиций (FIRST/LAST agent).
- **Валидация ролей** при загрузке `pipeline.yaml`: warning, если роль
  не резолвится в .md (project/global) или пустая — опечатка больше не
  даёт «стадию без роли» молча.
- **Бонус-находка**: кастомные роли с заглавных/пробелов («Auditor»,
  «My Agent») сохранялись как slug-файлы (`auditor.md`), а в пайплайн
  писались сырыми — рантайм бы упал на «role file not found». Сборка
  пайплайна теперь slug-нормализует роли.
- **Dashboard**: `todo_diff_stat` (`git diff --stat` vs baseline) в
  `/api/state` и в панели «Задача» — одно место вместо ручного diff.
- **`continue` и REVIEW**: лежащий REVIEW-файл больше не тупик — в ответе
  подсказка «REVIEW для TODO-NNNN ждёт: доработай TODO / `--ack`».
- D8 (роли ≠ стадии пайплайна) — отложено решением владельца.

---

## Future scenarios

| Сценарий | Что | Сложность |
|---|---|---|
| 2 · Decision fork | Runtime ad-hoc forms | Low |
| 3 · Blockage recovery | Multi-step flow | Medium |
| 5 · Priority planning | Drag-and-drop UI | High |
| 6 · Onboarding wizard | Multi-form logic | High |

---

### ТИРАЖ-2026-08-11 · Подготовка к публикации (4 reviewer: Claude Code, DeepSeek, Qwen, Kimi)

#### Quick wins — делаем сейчас

⬜ `.1` **Удалить `qa-audit-*.md` из root** — мусорный файл, подрывает впечатление.
   Добавить `qa-audit-*` в .gitignore.

⬜ `.2` **Починить badges** — `tests` badge ведёт на `#`. Заменить на shields.io
   CI badge: `https://github.com/EnerJizeIT/agentic-workflow/actions/workflows/test.yml/badge.svg`.

⬜ `.3` **Дополнить pyproject.toml metadata** — classifiers, keywords, project.urls
   (Homepage, Repository, Issues). Для PyPI и поиска.

⬜ `.4` **Requirements/Compatibility секция в README** — Python 3.10+, opencode,
   OS (Linux tested, macOS probably, Windows unknown), модели (model-agnostic,
   tested Qwen vLLM, должно работать с Claude/GPT).

⬜ `.5` **Quick Start секция в README** — 5-минутный блок: install → configure →
   first pipeline. Один copy-paste блок.

⬜ `.6` **Troubleshooting секция в USAGE** — pipeline завис (как сбросить state),
   порт занят, orphan TODO, commit failed.

⬜ `.7` **Limitations секция в USAGE** — средние затраты токенов (~2M per session),
   pipeline depth tested (5 stages), single-machine (не distributed).

⬜ `.8` **Comparison table в README** — awf vs Aider vs Claude Code vs Devin.
   Планирование, human-in-the-loop, кастомные пайплайны, MCP-native, self-hosted.

⬜ `.9` **Пример pipeline.yaml + role в USAGE** — показать как создать кастомный
   пайплайн (1 stage, 3 stages, 5 stages) + пример файла роли.

⬜ `.10` **"Dogfood" → "Real-world results"** — жаргон, не все поймут. Везде в README.

⬜ `.11` **GitHub topics/tags + social preview** — `opencode`, `mcp`, `ai-agent`,
   `pipeline`, `multi-agent`, `orchestrator`, `developer-tools`. + social preview
   image (Settings → Social preview).

⬜ `.12` **ASCII → Mermaid диаграммы** — GitHub рендерит нативно. В README и
   architecture.md.

⬜ `.13` **CONTRIBUTING.md** — dev setup, тесты, линтеры, PR process, стиль коммитов.

⬜ `.14` **CHANGELOG.md** — Keep a Changelog формат. Начать с v1.0.0.

⬜ `.15` **GitHub Release v1.0.0** — description: SMO, 29 tools, dashboard v2,
   6 dogfood sessions.

⬜ `.16` **SECURITY.md** — базовая политика (для pet-проекта).

⬜ `.17` **Issue templates** — `.github/ISSUE_TEMPLATE/`: bug_report, feature_request.

⬜ `.18` **Roadmap секция в README** — краткий список (3-5 пунктов) что планируется.
   Упомянули Qwen + Kimi. Отдельно от BACKLOG.md (который — полный список).
   Например: SMO.7 escape-hatch'и, coverage critical paths, PyPI, GitHub Pages.

#### Нужно внешнее resource

⬜ `.19` **Скриншоты/GIF dashboard в README** — нужен реальный pipeline run.
   3-4 скриншота: chat handoffs, TODO timeline, events, worker status.

⬜ `.20` **PyPI публикация** — `pip install awf agent-workflow-ui`. build + twine.
   Пока GitHub install — приемлемо для showcase.

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

- `tools/awf.py` (~940 строк) — разбить по зонам (pipeline/state/forms) или схлопнуть через helper
- `plan_checkpoint.py` (201 строка, 21% coverage) — добавить покрытие при dogfood
- `supervisor.py` — разделить `wait_for_supervisor_signal` до новых stage kind
- Два HTTP-сервера: one-shot core + long-lived plugin
- Event journal — `.agentic/state/journal.jsonl` для replay
- Model discovery consolidation — `awf` как единственный владелец знания об opencode-окружении
