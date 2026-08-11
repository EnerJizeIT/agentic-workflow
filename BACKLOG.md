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

⬜ `.11` **GitHub topics/tags** — `opencode`, `mcp`, `ai-agent`, `pipeline`,
   `multi-agent`, `orchestrator`, `developer-tools`.

⬜ `.12` **ASCII → Mermaid диаграммы** — GitHub рендерит нативно. В README и
   architecture.md.

⬜ `.13` **CONTRIBUTING.md** — dev setup, тесты, линтеры, PR process, стиль коммитов.

⬜ `.14` **CHANGELOG.md** — Keep a Changelog формат. Начать с v1.0.0.

⬜ `.15` **GitHub Release v1.0.0** — description: SMO, 29 tools, dashboard v2,
   6 dogfood sessions.

⬜ `.16` **SECURITY.md** — базовая политика (для pet-проекта).

⬜ `.17` **Issue templates** — `.github/ISSUE_TEMPLATE/`: bug_report, feature_request.

#### Нужно внешнее resource

⬜ `.18` **Скриншоты/GIF dashboard в README** — нужен реальный pipeline run.
   3-4 скриншота: chat handoffs, TODO timeline, events, worker status.

⬜ `.19` **PyPI публикация** — `pip install awf agent-workflow-ui`. build + twine.
   Пока GitHub install — приемлемо для showcase.

⬜ `.20` **Демо-видео (2-3 мин)** — полный цикл от init до approve.

#### Отклонено / отложено

ℹ️ `.21` **GitHub Pages / ReadTheDocs** — отдельный effort, текущих .md файлов
   достаточно для начала.
ℹ️ `.22` **Docker образ** — интересная идея, но opencode требует локального
   окружения. Не приоритет.
ℹ️ `.23` **Экосистемная расширяемость (standalone, другие агенты)** —
   aspirational. Сейчас: "The missing orchestration layer for opencode".
ℹ️ `.24` **Английский как основной** — уже сделано. README.md (EN) primary,
   README.ru.md (RU) secondary.

- `tools/awf.py` (~940 строк) — разбить по зонам (pipeline/state/forms) или схлопнуть через helper
- `plan_checkpoint.py` (201 строка, 21% coverage) — добавить покрытие при dogfood
- `supervisor.py` — разделить `wait_for_supervisor_signal` до новых stage kind
- Два HTTP-сервера: one-shot core + long-lived plugin
- Event journal — `.agentic/state/journal.jsonl` для replay
- Model discovery consolidation — `awf` как единственный владелец знания об opencode-окружении
