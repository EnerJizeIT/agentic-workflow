# Agentic Workflow Framework

> Декларативный multi-agent фреймворк: **Supervisor планирует → Agents реализуют → Supervisor проверяет**. Коммуникация — через файлы на диске (file bus). Состоит из двух независимых продуктов в одном monorepo:

- **`awf`** — Python-оркестратор пайплайнов (CLI, file bus, state machine).
- **`agent-workflow-ui`** — standalone MCP plugin для opencode: HTML-формы для визуального взаимодействия пользователя с агентом.

Оба продукта публикуются отдельно на PyPI (планируется). Awf-core не зависит от plugin'а и работает без него.

---

## Ключевые особенности

### awf (orchestrator)

- **Supervisor ↔ Agents через file-bus.** Задачи и отчёты передаются через `.agentic/inbox/` и `.agentic/outbox/` с `.ready`-сигналами. Никаких HTTP, WebSocket, очередей.
- **Pipeline как YAML.** Стадии, роли, transition-политики — всё декларативно в `.agentic/pipelines/default.yaml`.
- **Kind-based pipeline (BD-29).** Stage kind (`plan`/`execute`/`verify`) вычисляется по позиции, а не action-полю. Supervisor всегда первый и последний; между ними — произвольные agent roles.
- **Произвольные роли.** Пользователь выбирает роли через форму (например `agent-system-analyst`, `agent-weak-llm-implementer`, `agent-qa-review`). Skill content встраивается прямо в `.agentic/roles/<role>.md`.
- **Interactive supervisor (BD-30).** В интерактивном режиме supervisor = текущий opencode в чате пользователя (не subprocess). awf печатает инструкции и ждёт signal file.
- **Auto-DONE.** Если agent не успел записать сигнал, но verify-команды прошли и есть work evidence (git diff) — оркестратор синтезирует DONE автоматически.
- **Auto-commit с isolation (A1).** Коммиты содержат только diff vs baseline — supervisor's mid-flight edits не попадают в agent commit.
- **Plan progress auto-tracking (BD-33/34).** После verify автоматически отмечается `[x]` в `phases/plan.md` и печатается progress report.
- **Skill-aware role analysis (BD-31).** `awf analyze-roles` находит дублирования зон ответственности между ролями и добавляет disambiguation patches.

### agent-workflow-ui (MCP plugin)

- **5 MCP tools:** `open_form`, `read_submit`, `cancel_form`, `list_pending_forms`, `list_templates`.
- **Composite template `project-setup`** — одна HTML-форма для полной настройки проекта: контекст + ТЗ-файлы + supervisor + команда агентов с моделями.
- **Per-role model selection (BD-32).** Форма показывает dropdown с моделями из `opencode.json` — выбор сохраняется в `config.yaml` как `models.<role>.model`.
- **HTTP endpoint** (всегда включён) — browser POST'ит submit автоматически. CSRF protection через Origin whitelist (A2).
- **FormRegistry persistence (A10).** Pending forms сохраняются в `~/.config/awf/state/forms_registry.yaml` — переживают crash/restart.
- **XDG-aware (A9).** Уважает `XDG_CONFIG_HOME` для всех config paths.
- **Custom roles persistence** в `$XDG_CONFIG_HOME/awf/roles/` — сохранение/удаление через форму, переиспользование между проектами.
- **Lazy skill install** — SKILL.md автоматически копируется в opencode skills dir при первом старте plugin'а.

Подробнее: [`agent_workflow_ui/README.md`](agent_workflow_ui/README.md), [`vision/agent-ui-plugin.md`](vision/agent-ui-plugin.md).

---

## Установка

### Требования

- **bash** 4+ (только wrapper).
- **python3** ≥ 3.9 для awf, ≥ 3.10 для plugin (зависимость `mcp>=1.0`).
- **PyYAML** (устанавливается автоматически).
- **git** (целевой проект должен быть git-репозиторием).
- **opencode** CLI в `$PATH` (нужен только для `awf start`).

### awf

```bash
# 1) Клонировать
git clone git@github.com:EnerJizeIT/agentic-workflow.git
cd agentic-workflow

# 2) Установить
pip install -e .

# 3) Вариант A — вызывать напрямую
./bin/awf

# Вариант B — symlink для доступа из любого каталога
ln -s "$PWD/bin/awf" ~/.local/bin/awf
awf
```

Если `~/.local/bin/` нет в `$PATH`, добавь в `~/.bashrc`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### agent-workflow-ui plugin (опционально)

```bash
pip install -e ./agent_workflow_ui
```

После установки `awf init` предложит автоматически добавить MCP-конфиг в `~/.config/opencode/opencode.json`. SKILL.md установится автоматически при первом старте opencode (lazy install).

### Обновление

```bash
cd agentic-workflow
git pull && pip install -e . && pip install -e ./agent_workflow_ui
```

---

## Быстрый старт

```bash
# 1. Установить awf (один раз)
git clone git@github.com:EnerJizeIT/agentic-workflow.git
cd agentic-workflow && pip install -e .
ln -s "$PWD/bin/awf" ~/.local/bin/awf

# 2. В любом git-проекте:
cd /path/to/your-project
awf init                            # отвечай на вопросы (без --template)
vim .agentic/phases/plan.md         # напиши план
awf start                           # запусти пайплайн
```

### Что произойдёт

1. **`awf init`** создаст `.agentic/` с supervisor.md, config.yaml и пустым plan.md.
2. **План** (`.agentic/phases/plan.md`) — чеклист шагов, который читает Supervisor.
3. **`awf start`** запустит оркестратор:
   - **Supervisor plan stage:** awf печатает инструкции в лог, ждёт signal file. Ты (в opencode CLI) читаешь инструкции, создаёшь TODO, пишешь baseline, создаёшь `.ready` signal.
   - **Agent stages:** `opencode run --auto` запускается для каждой роли, читает TODO + handoffs от предыдущих ролей, пишет DONE/BLOCKED.
   - **Supervisor verify stage:** awf печатает инструкции — ты проверяешь aggregate handoffs, делаешь `git diff`, создаёшь ACK signal или REVIEW.

---

## Команды awf

| Команда | Описание |
|---|---|
| `awf` или `awf help` | Показать справку |
| `awf init` | Создать `.agentic/` в проекте (supervisor.md + config.yaml + plan.md stub) |
| `awf init --force` | Пересоздать без подтверждения |
| `awf start [опции]` | Запустить пайплайн |
| `awf start --pipeline <name>` | Конкретный пайплайн |
| `awf start --from-stage <name>` | Начать с указанной стадии |
| `awf start --auto` | Supervisor делает subprocess (для CI/тестов) |
| `awf start --background` | Фоновый запуск (detached через setsid, лог в `.agentic/logs/`) |
| `awf continue [опции]` | Продолжить прерванный пайплайн |
| `awf status` | Текущее состояние воркфлоу |
| `awf report` | Сводный отчёт о работе |
| `awf add-role <name>` | Создать шаблон новой роли |
| `awf approve <id>` | Approve auto-commit для TODO в `--auto` режиме |
| `awf baseline <id>` | Снимок состояния перед задачей |
| `awf rollback <id>` | Откат к baseline |
| `awf reset` | Очистить runtime-данные (inbox/outbox/logs) |
| `awf analyze-roles` | BD-31: проанализировать роли, добавить disambiguation patches |

---

## Архитектура

```
agentic-workflow/                  # monorepo (два независимых продукта)
├── awf/                           # orchestrator (Python package)
│   ├── orchestrator.py            # state machine + transition handlers
│   ├── supervisor.py              # supervisor stages (plan/verify/replan)
│   ├── agent_stage.py             # agent stages + handoff collection
│   ├── signal_watch.py            # BD-20/22 signal-watch + grace termination
│   ├── commit_gate.py             # auto-commit + A1 baseline isolation
│   ├── plan_progress.py           # BD-33/34 plan-step tracking + report
│   ├── pipeline.py                # Stage dataclass, kind by position
│   ├── signals.py                 # signal prefix filtering
│   ├── transitions.py             # policy lookup
│   ├── verify.py                  # auto-DONE, work evidence
│   ├── _log.py                    # file logger
│   ├── _env.py                    # BD-22/25 subprocess env setup
│   ├── xdg.py                     # XDG_CONFIG_HOME helpers
│   ├── cmd_analyze_roles.py       # BD-31 skill-aware role analysis
│   └── cmd_*.py                   # 10 команд (init, start, status, ...)
├── agent_workflow_ui/             # MCP plugin (standalone dist)
│   └── src/agent_workflow_ui/
│       ├── server.py              # FastMCP server (5 tools)
│       ├── http_endpoint.py       # localhost HTTP + CSRF (A2)
│       ├── state.py               # FormRegistry + persistence (A10)
│       ├── opencode_config.py     # models discovery + roles CRUD
│       ├── roles_processor.py     # form submit processing
│       ├── tools/forms.py         # MCP tool impl + temp cleanup (A4)
│       ├── render/                # Jinja2 engine + templates
│       │   ├── engine.py          # create_env + lazy init (A3)
│       │   └── default_templates/
│       │       ├── project-setup.html.j2
│       │       └── ack.html.j2    # submit confirmation (A3)
│       └── SKILL.md               # LLM policy (auto-copied to opencode skills)
├── bin/awf                        # thin bash-wrapper → python3 -m awf
├── templates/roles/supervisor.md  # supervisor instruction template
├── protocols/communication.md     # file bus specification
├── vision/                        # product vision + architecture docs
├── tests/                         # 657 tests (e2e + unit + plugin)
└── BACKLOG.md                     # roadmap
```

---

## Как это работает

### Pipeline (kind-based, BD-29)

Pipeline всегда: `[supervisor:plan] + [agents...] + [supervisor:verify]`. Kind вычисляется по позиции:

```yaml
stages:
  - name: "plan"           # kind=plan (position 0)
    role: "supervisor"
  - name: "implement"      # kind=execute (middle)
    role: "agent-weak-llm-implementer"
    on_blocked: "escalate"
    max_retries: 3
  - name: "qa"             # kind=execute (middle)
    role: "agent-qa-review"
  - name: "verify"         # kind=verify (last)
    role: "supervisor"
    on_approved: "commit_and_next"
```

**Supervisor — встроенная роль**, всегда на plan и verify positions. Между ними — произвольные agent roles, выбранные пользователем через форму.

### Сигналы

Worker пишет сигналы в `.agentic/outbox/`. Каноничный формат — `{PREFIX}-TODO-{NNNN}` (например `DONE-TODO-0001`).

| Сигнал | Файл | Кто пишет |
|---|---|---|
| `TASK_READY` | `inbox/TODO-{NNNN}.md` + `.ready` | Supervisor (plan) |
| `TASK_DONE` | `outbox/DONE-TODO-{NNNN}.md` + `.ready` | Agent (execute) |
| `TASK_BLOCKED` | `outbox/BLOCKED-TODO-{NNNN}.md` + `.ready` | Agent (execute) |
| `TASK_ACK` | `inbox/ACK-TODO-{NNNN}.ready` | Supervisor (verify) |
| `TASK_PROGRESS` | `outbox/PROGRESS-TODO-{NNNN}.md` (append-only) | Agent |
| `REVIEW_APPROVED`/`REVIEW_REJECTED` | `outbox/REVIEW-{APPROVED\|REJECTED}-TODO-{NNNN}.md` | Agent |
| `TEST_PASSED`/`TEST_FAILED` | `outbox/TEST-{PASSED\|FAILED}-TODO-{NNNN}.md` | Agent |

Подробнее: [protocols/communication.md](protocols/communication.md).

### Transition policies

- `on_approved: commit_and_next` — закоммитить и перейти дальше.
- `on_blocked: escalate` — эскалация на supervisor (replan).
- `on_blocked: rollback_to:<stage>` — откат к указанной стадии.
- `on_rejected: replan` — supervisor переделывает план.

### Auto-DONE

Если agent не записал сигнал, но выполнены оба условия:

1. Прошли **verify-команды** из `config.yaml` (`test_cmd`, `build_cmd`, `typecheck_cmd`).
2. Есть **work evidence** — `git diff` показывает изменения.

Отключается через `automation.auto_done: false` в `config.yaml`.

### ⚠️ Security note (H8)

awf subprocesses (`opencode run` spawned by agent/supervisor stages) run with
**blanket permissions** (`edit: allow`, `bash: allow`, `write: allow`,
`webfetch: allow`) — see `awf/_env.py`. This is required for autonomous
pipeline operation, but means **any role.md can execute arbitrary commands**.

**For pet projects** (single user, trusted role sources): acceptable.

**For team / PyPI publish**: this is a known limitation. Before sharing
roles externally, audit their `.md` content. Future work: scoped permissions
per role (e.g. reviewer → only `edit: ask`).

---

## Файлы `.agentic/`

```
.agentic/
├── config.yaml              # модели, verify-команды, default_pipeline
├── roles/                   # инструкции для ролей (skill content встроен)
│   └── supervisor.md
├── pipelines/
│   └── default.yaml         # стадии пайплайна
├── phases/
│   └── plan.md              # план с [ ]/[x] чекбоксами
├── inbox/                   # TODO от supervisor → agents [gitignored]
├── outbox/                  # DONE/BLOCKED/PROGRESS от agents [gitignored]
├── handoff/                 # per-role handoffs (BD-15) [gitignored]
├── inputs/                  # form submits от plugin [gitignored]
├── context/                 # baseline SHA и тест-логи [gitignored]
├── logs/                    # orchestrator.log + awf-start.out [gitignored]
└── reports/                 # сводные отчёты [gitignored]
```

---

## Тестирование

```bash
# Все тесты:
python3 -m pytest tests/ -v

# Только awf E2E:
python3 -m pytest tests/e2e/ -v

# Только awf unit:
python3 -m pytest tests/unit/ -v

# Только plugin:
python3 -m pytest tests/agent_workflow_ui/ -v

# Coverage plugin:
python3 -m pytest tests/agent_workflow_ui/ --cov=agent_workflow_ui --cov-report=term-missing
```

**657 тестов:** e2e + unit (awf) + integration/unit (plugin).

**Покрытие:**
- **agent-workflow-ui:** **90%** (target ≥80%, enforced в CI через `--cov-fail-under=80`).
- **awf-core:** unit + e2e (через bin/awf subprocess).

**Линт:** `ruff check tests/ awf/ agent_workflow_ui/src/agent_workflow_ui/` — весь репо проходит чисто.

**CI:** GitHub Actions, Python 3.10/3.11/3.12, ruff + pytest + coverage gate.

---

## Спецификации

- [protocols/communication.md](protocols/communication.md) — спецификация файловой шины.
- [vision/agent-ui-plugin.md](vision/agent-ui-plugin.md) — Product Vision plugin'а.
- [vision/architecture.md](vision/architecture.md) — Architecture plugin'а.
- [BACKLOG.md](BACKLOG.md) — roadmap (BD-35 + Future scenarios).

---

## Лицензия

MIT (см. `pyproject.toml`).
