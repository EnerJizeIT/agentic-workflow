# BACKLOG

> История закрытых записей — в [BACKLOG-archive.md](BACKLOG-archive.md).

---

## Открытые задачи

### N1 · [HIGH] Project-setup form: system-analyst → architector mapping bug

**Проблема:** В dogfood-сессии пользователь выбрал «Аналитик и архитектор» в форме
project-setup. Форма отправила `agent-architector` дважды, `agent-system-analyst`
отсутствовал. Pipeline материализовался с дублем architector. Supervisor вручную
восстанавливал system-analyst из git + правил config.yaml + pipeline.yaml.

**Что сделать:** Диагностировать root cause:
1. Проверить рендеринг формы — правильно ли маппятся выбранные роли на submit
2. Проверить сериализацию submit → `apply_project_setup` → pipeline.yaml
3. Проверить `scan_global_roles()` — все ли роли попадают в список выбора
4. Добавить regression-тест: выбор 5 разных ролей → pipeline имеет 5 разных ролей

**Файлы:** `agent_workflow_ui/.../render/project-setup.html.j2`,
`agent_workflow_ui/.../apply_project_setup`, `awf/api/roles.py:scan_global_roles`,
`awf/api/setup.py:apply_project_setup`.

---

### N2 · [MEDIUM] Worker log files слишком пустые для диагностики

**Проблема:** Worker system-analyst отработал 15 сек (transient vllm сбой). Log-файл
`.agentic/logs/awf-agent-system-analyst-TODO-0003.out` содержал 5 строк: header +
2 noise строки. Нет вывода модели, нет stderr, нет tool calls. Supervisor потратил
12 сообщений на forensic debug.

**Что сделать:**
1. `agent_stage.py:run_agent_stage` — передавать `--print-logs` в `opencode run`
   (или хотя бы capture stderr в log-файл)
2. Log-файл должен содержать полный output сессии opencode, не только header
3. При transient сбое (exit 0 + пустой output) — логировать отдельно

**Файлы:** `awf/agent_stage.py:79-118` (cmd construction + signal_watch),
`awf/signal_watch.py` (stdout/stderr redirect to worker_log).

---

### N3 · [MEDIUM] Нет шаблона TODO для multi-stage pipeline

**Проблема:** Supervisor 3 итерации писал TODO для pipeline из 5 ролей
(system-analyst → architector → implementer → qa-review → project-auditor):
1. Расписал микро-менеджемент всех 5 стадий (нарушение «TODO = задача 1-го агента»)
2. Добавил skills прямо в TODO (бессмысленно — skills из role.md)
3. Наконец понял: TODO = задача 1-й стадии + context о последующих

Каждая итерация = kill pipeline → edit → restart → checkpoint approve.

**Что сделать:** Добавить шаблон в supervisor.md для multi-stage pipeline:
«TODO для 1-го агента должен содержать: (1) Goal для всей итерации (из Brief),
(2) Конкретная задача для 1-й стадии, (3) Контекст: какие роли следуют и что они
будут делать с результатом 1-й стадии».

Пример шаблона для pipeline analyst → architector → developer:
```
## Context
This iteration goes through: system-analyst (you) → architector → developer.
You produce requirements. Architector designs from them. Developer implements.

## Your task
Analyze and document requirements for <feature>.

## What follows you
Architector will design modules from your requirements.
Developer will implement from architect's design.
```

**Файлы:** `templates/roles/supervisor.md` (Step 4 — добавить multi-stage template).

---

### S1 · [LOW] Supervisor: прямой вопрос вместо догадок (prompt injection)

**Проблема:** В dogfood-сессии supervisor три итерации гадал «что такое
нормализация скиллов», подсовывая пользователю multiple-choice варианты своих
неверных интерпретаций. Пользователь был вынужден выбирать из ошибок supervisor,
а не корректировать его понимание. Один прямой вопрос решил бы это за один шаг.

**Что сделать:** Добавить правило в Quick Reference supervisor.md:
«Не уверен в указании пользователя? Задай один прямой вопрос. Не гадай через
варианты — это заставляет пользователя выбирать из твоих ошибок».

**Файл:** `templates/roles/supervisor.md` (Quick Reference, правило #6 или в Step 0).

**Почему важно:** Экономит итерации и токены. Для pet-проекта с диалоговым
стилем — прямой вопрос всегда дешевле угадывания.

---

### S2 · [LOW] Supervisor: простой тест раньше deep-debug (prompt injection)

**Проблема:** На salvage (worker отработал 15 сек пусто) supervisor полез читать
исходники `awf/agent_stage.py`, воспроизводить запуск с `--print-logs`. Пользователь
сразу предположил «vllm не стартанул» — и был прав. Простой тест (запустить worker
с тривиальным промптом, убедиться что инфраструктура жива) быстрее и продуктивнее.

**Что сделать:** Добавить правило в supervisor.md:
«Salvage или непонятный сбой? Сначала проверь инфраструктуру простым тестом
(`opencode run --auto --agent <role> -- 'say hello'`). Потом разбирай логи и код».

**Файл:** `templates/roles/supervisor.md` (раздел Error handling или Salvage).

---

### P1 · [LOW] Checkpoint skip после kill+start

**Проблема:** После `awf_kill` + `awf_start` BD-36 checkpoint открывается заново на
тот же план. Для `awf_continue` checkpoint опускается (правильно), но
fresh-start-after-kill тоже мог бы пропускать если план не менялся. Три approve
подряд на идентичный план — лишний friction.

**Что сделать:** В `plan_checkpoint.py` проверять: если с момента последнего
checkpoint-approve план (plan.md или TODO .md) не менялся (сравнение по mtime
или hash) — пропустить checkpoint. Логировать skip.

**Файлы:** `awf/plan_checkpoint.py` (`is_checkpoint_enabled` или новая функция
`_should_skip_checkpoint`). Сохранять hash последнего approved плана в state.

---

### P2 · [LOW] Salvage UX — явный retry вместо kill+continue

**Проблема:** В salvage supervisor не сразу понял что делать: работы нет → не ACK;
но и не block. Пришлось `awf_kill` + `awf_continue(from_stage)`. Механизм salvage →
retry мог бы быть прямее.

F4 (auto-retry transient failures) частично закрыл это — но когда salvage всё-таки
наступает, supervisor всё ещё должен убивать pipeline и перезапускать вручную.

**Что сделать:** Добавить явную опцию retry в salvage-путь. Варианты:
- (a) `awf_continue(from_stage)` с параметром `retry=True` — переигрывает стадию
- (b) В salvage-prompt добавить инструкцию: «создай RETRY-TODO-NNNN.ready для
      повтора стадии, или ACK/REVIEW для решения»
- (c) Новый MCP tool `awf_retry_stage` — перезапускает текущую стадию

Рекомендация: (b) — минимальное изменение, reuse signal-механизма.

**Файлы:** `awf/pipeline_engine.py` (salvage branch), `awf/supervisor.py`
(salvage prompt + signal detection), `templates/roles/supervisor.md` (salvage Step).

---

### R1 · [MEDIUM] Чистый init (runtime clean, config saved)

**Проблема:** `awf init` с `force=false` ничего не чистит (осторожный дефолт).
`force=true` сносит всё включая config. В dogfood после ручного удаления `.agentic/`
и re-init возник конфликт: git HEAD содержал старые pipeline/roles, init создал
новые — несогласованное состояние.

**Что сделать:** `awf init` без флагов = чистить runtime, сохранять config:
- **Чистить всегда:** inbox, outbox, handoff, done, state, logs, context, dashboards, inputs
- **Сохранять:** config.yaml, roles/*.md, pipelines/*.yaml, phases/plan.md
- `awf init --hard` = полный сброс (текущий `force=true` behavior)
- Для re-init после ручного удаления: проверять git-tracked `.agentic/` файлы,
  предупреждать о конфликте, предлагать `--hard`

Переиспользовать логику из `cmd_reset.py`.

**Файлы:** `awf/cmd_init.py:run()`, `awf/api/setup.py:init_project()`,
`awf/cmd_reset.py`, `awf/paths.py`.

---

### R8 · [LOW] Verify по Brief — полная имплементация

**Проблема:** R5 добавил BRIEF-NNNN.md и verify snippet читает Brief как contract.
Но verify prompt только упоминаает «check success criteria from the Brief» — нет
структурированной сверки. Supervisor может проигнорировать Brief при verify.

**Что сделать:**
1. В `_SNIPPET_VERIFY` добавить: «Сверь каждый success criterion из Brief с
   фактическим результатом. Отметь выполненные/невыполненные. Если хотя бы один
   не выполнен → REVIEW с конкретикой».
2. В `build_prompt(verify)` передавать Brief content inline (не только ссылку на
   файл — supervisor может не открыть).
3. Опционально: в `verify.py` добавить `check_brief_criteria()` — парсит Brief,
   сравнивает с git diff / test results.

**Файлы:** `awf/supervisor.py` (_SNIPPET_VERIFY, build_prompt verify kind),
`awf/verify.py` (опционально).

**Зависимость:** R5 (已完成 — BRIEF-NNNN.md существует).

---

### R2 · [LOW] Goal elicitation — цель перед load_context

**Проблема:** Сейчас `awf_load_supervisor_context` даёт всё (vision, plan, status,
git-diff) — это перегружает. Supervisor изучает проект «вообще», а не под
конкретную задачу. В dogfood это привело к расфокусу — supervisor начал
микро-менеджить все стадии вместо того, чтобы сфокусироваться на цели.

**Что сделать:**
1. Supervisor спрашивает цель ПЕРЕД `load_supervisor_context` (короткий диалог
   1–3 вопроса).
2. Под цель — фильтрованно: какие артефакты читать, какие роли рекомендовать.
3. Цель сохраняется в `.agentic/state/goal.txt` или `BRIEF-NNNN.md`.
4. Связка «цель → recommended roles»: анализ = system-analyst + qa-review;
   разработка = developer + qa; ревью = project-auditor + qa-review.

**Файлы:** `templates/roles/supervisor.md` (новый шаг перед Step 1),
`awf/api/context.py:load_supervisor_context` (опциональный параметр `goal`).

---

### R3 · [LOW] Форма с рекомендацией ролей (prefill)

**Проблема:** Сейчас `awf_open_project_setup_form` auto-populates глобальными
skills/roles, но без рекомендаций под цель. Пользователь выбирает роли «вслепую».

**Что сделать:**
1. Supervisor рекомендует роли исходя из цели (R2) + характера итерации.
2. Форма project-setup **предзаполняется** рекомендацией: preselected roles,
   suggested models, recommended pipeline template.
3. Пользователь подтверждает/правит.
4. Технически: `awf_open_project_setup_form` принимает параметр `recommended`
   (dict: roles, models, pipeline). Шаблон формы рендерит preselected.

**Файлы:** `awf.py:awf_open_project_setup_form`, `awf/api/setup.py`,
`agent_workflow_ui/.../render/project-setup.html.j2`.

**Зависимость:** R2 (goal elicitation) — рекомендация ролей от цели.

---

### R4 · [LOW] Skills normalization (3-part checklist + tool)

**Проблема:** После формы roles могут перекрываться (qa-review + project-auditor
оба «verify»), быть не адаптированы под характер итерации (system-analyst для
ревью без кода), и не иметь формализованных handoff-контрактов. В dogfood
supervisor правивал role.md вручную — процесс не формализован.

**Что сделать:** 3-частный checklist после формы:
1. **Адаптация под итерацию.** Каждая роль: соответствует ли skill характеру
   запуска? Если нет — секция адаптации в `role.md`.
2. **Разруливание перекрытий.** `awf_analyze_roles` уже детектит — сделать
   **обязательным шагом** после формы.
3. **Handoff-контракты.** Каждая роль знает: что получает от предыдущей, что
   передаёт следующей.

Новый tool `awf_normalize_skills(iteration_type, pipeline)` — прогоняет checklist,
выдаёт список правок role.md (с возможностью apply).

**Файлы:** `awf/cmd_analyze_roles.py`, `awf/api/roles.py`, `templates/roles/`,
новый tool в `awf.py`.

---

### R7 · [MEDIUM] Phase-prompts (supervisor state machine)

**Корневая проблема:** supervisor.md — один системный промт на 500+ строк.
Supervisor получает весь flow сразу и путается (S1: гадал вместо прямого вопроса;
S2: deep-debug вместо простого теста; смешение слоёв TODO/role.md — до R5).
Добавление правил (S1, S2) делает промт ещё длиннее — paradox.

**Решение:** Modular phase-prompts — supervisor = state machine.
На каждом шаге flow supervisor получает короткий промт (5-20 строк):
- `init` — «изучи проект, спроси цель»
- `goal` — «сформулируй цель, рекомендуй роли»
- `form` — «открой форму, дождись submit»
- `normalize` — «проверь role.md под итерацию»
- `todo` — «напиши Brief → после approve → напиши TODO»
- `run` — «открой dashboard → idle»
- `verify` — «прочитай Brief, сверь с результатом, реши»

Текущий шаг определяется полем `phase` в `.agentic/state/current.yaml`.

Новый tool `awf_supervisor_step` — возвращает промт для текущего шага + что
сделать. Supervisor не держит в голове весь flow, только актуальный шаг.

**Почему важно:** Меньше промт → меньше путаницы → меньше LLM-ошибок → выше
качество. Философия проекта: «детерминизм каркаса + мощь LLM». Phase-prompts =
больше детерминизма в подаче инструкций.

**Файлы:**
- `templates/roles/supervisor.md` — разбить на phase-секции (или вынести в
  отдельные template-файлы: `templates/phases/init.md`, `goal.md`, ...)
- `awf/supervisor.py:build_prompt()` — phase-detection по state field
- `awf/pipeline_state.py` — добавить поле `phase` (init/goal/form/normalize/todo/run/verify)
- Новый tool в `awf.py`: `awf_supervisor_step` — читает `phase` из state,
  возвращает промт для этого шага

**Связь:** S1/S2 — quick fix до R7. R7 — архитектурное решение, после которого
S1/S2 естественно растворяются (каждый phase-prompt короче и сфокусированнее).

---

### N4 · [LOW] config.yaml.bak — origin не исследован

F2 (*.bak gitignore) закрыл cosmetic. Но источник `.bak` файлов не найден:
`awf/api/setup.py:230` (backup config при записи), `setup.py:140` (default.yaml.bak),
`opencode_agents.py:120` (.bak-{ts}). Нужно определить кто и зачем создаёт backup
при каждом запуске стадии — если это worker's edit tool, шум будет в любом проекте.

**Status:** low priority — gitignore маскирует. Investigate when touching setup.py.

### AUD-12 · [T3] Рефакторинг

- **`_xdg_config_home` ×3 копии** → consolidate в `awf.xdg` (комментарий "avoid circular import" в state.py неверен)
- **`open_form` scans everything** → `scan_global_skills/roles` лениться по шаблону (нужны только project-setup)
- **`awf.py` 824 строк** → схлопнуть 19 wrapper'ов через `_wrap(api.fn)` helper (~824→~250)
- **Circular import orchestrator↔pipeline_engine** → поднять `_handle_*` в третий модуль (до T4)
- **`_extract_stage_info_regex` (cognitive 90) + `_detect_supervisor_signal`** → regex/mtime fallback'ы к structured state. State пишется с T4.1 — объявить дату удаления и убрать ~90 строк хрупкого кода

### BD-35 · Per-role contribution tracking

Каждая роль работает в той же директории. Free-rider / zone violation / audit opacity.
**Status:** ждать real failure в dogfooding.

### DAUD-6 · plan_checkpoint.py → Jinja2

~200 строк HTML через конкатенацию. Перевести на Jinja2 template.
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

## Architecture notes

- `tools/awf.py` — разбить по зонам до scenario 2 (или схлопнуть через helper — см. AUD-12)
- `supervisor.py` — разделить `wait_for_supervisor_signal` до новых stage kind
- Два HTTP-сервера: one-shot core + long-lived plugin
- Event journal — `.agentic/state/journal.jsonl` для replay (если проект растёт)
- Model discovery consolidation — `awf` как единственный владелец знания об opencode-окружении
