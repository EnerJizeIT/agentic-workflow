# Архитектура: единый вход супервизора через `awf_brief`

Статус: REVIEW
Ревизия: arch-5
Входы: supervisor-brief.md@req-11 (PASS @reqrev-11), context@ctx-2, test-spec@ts-4, readiness@rd-4, TODO-0326 (REVIEW-фиксы 1–5)
База кода: HEAD `40bb6cc`; дерево грязное (`?? docs/spec/supervisor-brief/`), факты сняты с текущего дерева.
Ограничение: MCP-имён становится меньше — 54 против 56 (AC4, INV9); двух дублей нет ни в реестре, ни в доках.

## 1. Границы и точки изменения

| Компонент | Дельта | Файл | R |
|---|---|---|---|
| реестр | удалить две записи; имена удалённых — отдельно | `tools/registry.py` | R1, R3 |
| сервер | свой отказ для удалённых имён | `server.py` | R3 |
| MCP-обёртки | удалить два тула и `_PHASE_NEXT` | `tools/awf.py` | R1, R3 |
| Python-API | снять публичную функцию; helper'ы оставить | `awf/api/context.py`, `api/__init__.py`, `api/_results.py` | R3, AC47 |
| состояние | `current_todo`, `state_updated_at`, `next_action` | `awf/api/lifecycle.py`, `api/_results.py` | R7 |
| бриф | классификатор, совет, карточка, `phase_prompt` | `awf/brief.py`, `api/brief.py` | R2, R4, R7, R8, R11 |
| вердикт пакета | JSON и аварийная заметка | `awf/verify_pack.py` | R7 |
| вердикт коммита | `reason`; публичное чтение | `awf/commit_gate.py`, `api/run.py` | R7 |
| карта | `situations`, `inventory_only`, выборка | `awf/data/tool_map.yaml`, `awf/brief.py` | R5 |
| ожидание | `run_active` в ответе | `api/wait_event.py`, `api/_results.py` | R6 |
| хвост лога | лог текущего юнита | `api/_helpers.py`, `_background.py`, `context.py` | R9 |
| носители | промпты, советы, доки, числа 54/49 | шаблоны ролей, `lifecycle.py`, `phase.py`, `agents_md.py`, skill, `USAGE*.md`, `docs/design.md`, `README.md` | R1, R10 |

Движок, `verify_pack.verify_pack`, `DONE.json`/handoff и коммит-гейт не меняются (INV8).

## 2. Решения

**D1 — дубли удаляются, не оборачиваются.** Из `registry.TOOLS` уходят `awf_current_step` и `awf_load_supervisor_context`; функций в `tools/awf.py` больше нет. `tool_names()` даёт 54 имени: 49 `awf_*` + 5 UI. CLI не меняется: подкоманд у этих тулов нет (CTX18). В `awf/api/context.py` удаляются `load_supervisor_context` и `SupervisorContextResult` с экспортами `api/__init__.py`; `_extract_stage_info`, `_compute_expected_action`, `_compute_stage_kind` остаются — их импортирует `get_status` (CTX13, AC47).

**D2 — отказ удалённого имени живёт в сервере, а не в туле.** В `registry.py` — `REMOVED_TOOLS = {"awf_current_step": "awf_brief", "awf_load_supervisor_context": "awf_brief"}`. Он не входит в `TOOLS`, поэтому не виден в `list_tools`, `tool_names()` и счётчиках. `server.py` подменяет `FastMCP.call_tool`: имя из словаря даёт `ToolError("Tool `X` is removed (удалён). Use `awf_brief` — the single entry point.")`, остальные идут в `super().call_tool`. AC4 и AC10 держатся вместе: имени нет в наборе, вызов получает текст вместо `Unknown tool` (CTX12).

**D3 — полный промпт фазы — поле ответа, не текст карточки.** `BriefResult.phase_prompt` = `get_phase_prompt(phase, project_dir)` без усечения (AC48). Карточка печатает указатель `- full phase prompt: field `phase_prompt` (N words)`; `phase_summary` остаётся 45-словной выдержкой для JSON, `phase_clipped` сохраняется. Бюджет считается по `text` (AC30/AC33), 775 слов промпта его не ломают; AC49 выполнен указателем. Для S0 `phase_prompt=""`.

**D4 — один владелец совета.** `next_action_from_status(status)` остаётся ядром и сохраняет порядок и тексты S1–S9, S11–S13. Над ним — `advice_for(status, run_record)`: добавляет run-aware шаги (`next_action_for_record`, `run_step_after_done`), которые сейчас живут только в `build_brief`. `get_status` заполняет `StatusResult.next_action` через `advice_for`; `build_brief` использует тот же вызов; обёртка `awf_status` совет не досчитывает. `_PHASE_NEXT` удаляется с тулом (CTX2, AC5/AC8).

**D5 — один классификатор состояний.** `classify_status(status)` → `S0`–`S10` плюс `S1a`; `refine_with_run(...)` добавляет ветки S11a/S11b, S12, S13 (id ситуации остаётся S11–S13). Порядок — приоритет AC46: `expected_action` (кроме `stale_stage`+`pipeline_running`) → `foreign_commit` → salvage → blocked → budget → engine_dead → stale → zombie → running → active → suggestion; reject и checkpoint перетирают хвост, checkpoint слабее salvage.

**D6 — вердикт пакета читает тот же модуль, что пишет.** `verify_pack.read_gates_facts(path)` разбирает JSON-блок `_render_report`; второго парсера нет, логи не читаются. Аварийную заметку движка (`**error**`, JSON нет) узнаёт по заголовку и отдаёт `verdict="error"` с текстом заметки как причиной (AC29). Первая причина — маркерная строка `- <cmd>: FAIL (rc=…)`, заголовок секции не считается (AC27).

**D7 — «живой цикл юнита» — состояние, а не `run_record.active`.** `get_status` добавляет `current_todo` (`current.yaml.todo_id`) и `state_updated_at` (`updated_at`, парсер `pipeline_state.parse_updated_at`; FR13). Артефакты читаются только по имени юнита: `GATES-<id>.md`, `COMMIT-AUDIT-<id>.json`; glob запрещён. Секция `verify` видна, когда идёт стадия `verify` **или** `mtime` артефакта позже `state_updated_at`: первое — одиночный `awf start` и забег (пак пишется до пробуждения, `pipeline_engine.py:1590`), второе — коммит-гейт. Старый артефакт гасится, неизвестное время считается старым.

**D8 — карта ситуационная, перечень отдельно.** В `tool_map.yaml` у записи появляются `situations: [S8]` и `inventory_only: true` (helper'ы), у группы — свои `situations`. Выборка `situational_tools(situation, advice_tool)`: записи ситуации в порядке файла, без `inventory_only`, максимум 5; тул совета — первым; `why` — поле `when`. Группа без записи ситуации не выводится. Полный перечень — JSON-поле `tool_map` (64 записи) и путь `awf/data/tool_map.yaml`; флага `--full` нет.

**D9 — бюджет 900 слов держат потолки секций.** `MAX_WORDS` не растёт; `SECTION_CAPS` в `awf/brief.py`; секции отбрасываются в порядке `what_new` → `defaults` → `scenarios` → `doctrine` → `recovery`. Не режутся состояние, `next_action`, `tools`, `verify`; отброшенные — в `budget.clipped` и указателе (AC31/AC32).

**D10 — хвост лога текущего юнита.** `current_stage_log(project, state) -> (Path | None, str)` берёт новейший `logs/awf-*-<todo>.out` для `todo_id`, иначе `orchestrator.log`, иначе пусто и ожидаемый путь. Один helper для статуса и карточки (AC34–AC36).

**D11 — одно правило ожидания с признаком режима.** `WaitEventResult.run_active` из `read_run_record(...).active`; `WAIT_RULE_RUN` («wait only inside an active run» / «жди только внутри активного рана») и `WAIT_RULE_REACTIVE` («react to the user, no loop» / «реагируй на пользователя, без цикла») — константы `awf/brief.py`, их печатают карточка, docstring и шаблоны. Цикл — только при `run_active=True`, `timeout` не выход (AC22–AC25).

**D12 — ни один носитель не зовёт снятый тул рабочим.** Точки и новый текст:

| Носитель | Стало |
|---|---|
| `_core.md:48` | «the phase is in the brief's State» |
| `phase-init.md:9` | «call `awf_brief` to confirm the phase» |
| `phase-normalize.md:6` | «`awf_brief` keeps returning this phase» |
| `lifecycle.py:1158` | «No active tasks. Call awf_brief for the next step.» |
| `phase.py:276` | «act on the actual phase, call awf_brief» |
| `tools/awf.py:2273` | «Check awf_brief» |

Эти файлы теряют упоминание вместе с тулами; skill, `USAGE*.md`, `docs/design.md`, `README.md` знают только `awf_brief` и числа 54/49.

## 3. Контракты

### K1 — ответ `awf_brief`

Поля `BriefResult` сохраняются, новые добавляются. CLI читает `text`, JSON — всё.

```json
{
  "situation": "S8",
  "situation_line": "verify waits for your decision",
  "entry": {"tool": "awf_brief", "single": true,
            "recall_when": ["unclear moment", "lost context", "stage changed"]},
  "mode": "run", "run_active": true,
  "next_action": "VERIFY STAGE — pipeline waiting for YOUR decision",
  "advice_tool": "awf_approve",
  "wait": {"rule": "wait only inside an active run", "loop": true,
           "tool": "awf_wait_for_event", "timeout": 55},
  "phase_prompt": "…full get_phase_prompt(phase)…",
  "verify": {"unit": "TODO-0326", "status": "failed",
             "report": ".agentic/context/GATES-TODO-0326.md",
             "failed_sections": ["contracts"],
             "first_failure": "- `pytest`: FAIL (rc=1) — tail",
             "commit": {"status": "refused", "reason": "sha mismatch"},
             "waiting_on": "supervisor", "close_with": "awf_approve"},
  "tools": [{"tool": "awf_approve", "why": "close verify"}],
  "budget": {"words": 860, "limit": 900, "clipped": [], "pointer": ""}
}
```

`situation` — `S0`–`S13` и `S1a`; `mode` — `reactive`/`run`; `verify.unit` — `current_todo` живого цикла; `verify.status` — `ok`/`failed`/`nothing-measured`/`error`/`missing`/`unreadable`; `commit.status` — `committed`/`skipped`/`refused`/`error`/`missing`; `waiting_on` — `supervisor`/`engine`/`commit`/`none`; `close_with` — имя реестра или пусто («закрывает движок»). `verify == {}` — вердикта текущего прохода нет. Четыре элемента AC13 — `situation_line`, `next_action`, `advice_tool`, `tools[0]`.

### K2 — ситуационная карта

Схема записи и групп:

```yaml
groups:
  - title: verify
    situations: [S8]
    tools:
      - mcp: awf_approve
        cli: approve
        situations: [S8]
        when: "close verify after your probes"
      - mcp: awf_verify_pack
        situations: [S8]
        when: "diagnose without an artifact"
```

| Ситуация | Цепочка (первый — тул совета) |
|---|---|
| S0 нет `.agentic/` | `awf_init` → `awf_open_project_setup_form` → `awf_set_goal` |
| S8 verify ждёт | `awf_approve` → `awf_reject` → `awf_verify_pack` → `awf_tree_sha` |
| S11a reject, ран жив | `awf_run_revise` → `awf_run_next` → `awf_review_append` |

Остальные S1–S13/S1a — по такой же строке в `tool_map.yaml`; первый тул строки всегда равен `advice_tool`. Таблица — норма данных: `situations` связывает запись с ситуацией, группа наследует объединение. Записи `inventory_only` в выборку не идут, но остаются в перечне (AC20).

### K3 — ожидание

`WAIT_RULE_RUN`/`WAIT_RULE_REACTIVE` — дословно одни и те же во всех носителях внутри языка (AC22). Карточка печатает `wait.rule` по `mode`, `loop` — разрешён ли цикл, `timeout` — из `wait.cap_seconds` проекта; в реактивном режиме `tool`/`timeout` пусты. `timeout` не выход: вызов повторяется (AC24).

### K4 — факты verify и коммита

`read_gates_facts(path) -> dict | None`: `{verdict, exit_code, measured, sections, details, report, first_failure, note}`; `None` — файла нет. JSON нет, но есть `**error**` — `verdict="error"`, `note` — первая строка заметки (AC29). `first_failure` — первая маркерная строка в первой секции `fail`; заголовок секции не считается, логи не читаются (AC27).

`read_commit_audit(project_dir, todo_id) -> dict | None` — публичная замена `api/run.py::_read_commit_audit`; `_audit_record` добавляет `"reason": outcome.reason` при всех статусах (A8). Свежесть — по D7.

### K5 — хвост лога

`current_stage_log(project, state)` — путь и причина отказа (`"лога нет"` с ожидаемым путём). `read_log_tail` с баннерами не меняется.

### K6 — отказ удалённого тула

`REMOVED_TOOLS` в `registry.py` + перехват в `create_server()` (D2). Текст содержит имя, «removed»/«удалён» и `awf_brief`; прочие неизвестные идут стандартной ошибкой MCP. `ToolSpec` и счётчики не меняются.

## 4. Пути и отказы

| Отказ | Что видит супервизор |
|---|---|
| нет `.agentic/` | S0 setup-цепочка; `awf_status` — отказ со ссылкой на бриф, без исключения (AC42) |
| нет/битый/аварийный `GATES` | «пак ещё не собран» или «не отработал»; причина — заметка; `awf verify-pack` (AC29) |
| нет `COMMIT-AUDIT` | «коммита ещё не было» (AC43) |
| старый артефакт или нет юнита | `verify == {}`: «пак не собран для прохода» / «вне живого цикла» (AC28, D7) |
| движок мёртв | S4/S5 сильнее run-aware шага || нет лога | «лога нет» + ожидаемый путь (AC36) |

## 5. Владельцы фактов

| Факт | Владелец |
|---|---|
| совет «что делать» | `awf/brief.py::advice_for` (ядро — `next_action_from_status`) |
| id ситуации | `awf/brief.py::classify_status` + `refine_with_run` |
| текущий юнит и время state | `state/current.yaml`; читатель `StatusResult.current_todo`/`state_updated_at` |
| вердикт пакета | `GATES-<id>.md`; читатель `verify_pack.read_gates_facts` |
| вердикт коммита | `COMMIT-AUDIT-<id>.json`; читатель `commit_gate.read_commit_audit` |
| перечень и имена тулов | `awf/data/tool_map.yaml`; `registry.TOOLS`, `registry.REMOVED_TOOLS` |
| фразы ожидания | `awf/brief.py::WAIT_RULE_*` |
| хвост лога | `awf/api/_helpers.py::current_stage_log` |

`docs/contracts/single-owners.md` не расширяем.
## 6. Совместимость

- MCP: 54 имени (49 `awf_*` + 5 UI). Тест замораживает набор `@40bb6cc` минус два имени; удаление другого или добавление — красный (AC4/INV9).
- Доки и числа: `USAGE.md`, `USAGE.ru.md`, `README.md`, `docs/design.md`, `docs/file-bus.md`, skill — 54/49; счётчики сверяет существующий docs-тест.
- `awf_status`: поля прежние, добавляются `current_todo`, `state_updated_at`, `next_action`; docstring перечисляет поля (AC39).
- Python-API: `load_supervisor_context` и `SupervisorContextResult` сняты; тесты, которые брали через них полный контекст, переводятся на `api.brief` (`run_goal`, `run_last_decision`, `run_sources`, `evidence_plan`) либо на helper'ы — по смыслу проверки, без удаления тестов (AC45).
- `awf_wait_for_event`: +`run_active`; `awf_verify_pack` остаётся (AC4).
- Пинящие тесты правятся в том же IMP: `test_context.py`, `test_context_single_source.py`, `test_run_plan_recovery.py`, `test_carry_batch_engine_queue.py`, `test_awf_tools.py`, `test_server_smoke.py`, `test_tool_descriptions.py`, `test_coverage_gaps.py`.

### 6а. Поверхность × исход

| Поверхность | Исход |
|---|---|
| реестр, `list_tools`, счётчики | переведено: двух имён нет (D1/IMP1) |
| вызов удалённого имени | переведено: свой отказ (D2/K6/IMP1) |
| карточка брифа | переведено: совет, ситуация, вердикт, промпт-поле (D3–D8, IMP5) |
| `awf_status`, CLI `brief`/`status` | переведено: тот же совет, юнит, хвост лога; коды прежние (IMP2/IMP7) |
| `awf_verify_pack` | исключён: диагностика, не первый совет (R7) |
| промпты фаз, советы в коде | переведено: `awf_brief` (D12, IMP8) |
| skill, AGENTS.md, USAGE, design.md | переведено: один вход, числа 54/49 (IMP8) |
| `tool_map.yaml` | исключён явно: `inventory_only` + перечень (D8) |
| пинящие тесты | переведено поимённо, без удаления (AC45) |

## 7. План юнитов

| IMP | Что | Файлы | Зависит | Проверка |
|---|---|---|---|---|
| IMP1 | удаление двух тулов, отказ, Python-API, заморозка 54 | реестр, сервер, `tools/awf.py`, `api/context.py`, `api/__init__.py`, `api/_results.py`, тест имён | — | база 56 красная, после 54; вызов даёт `awf_brief` |
| IMP2 | совет и поля статуса | `awf/brief.py`, `awf/api/lifecycle.py`, `api/_results.py`, `tools/awf.py` | IMP1 | `test_brief`, `test_context_single_source`; совет совпадает в S1–S13/S1a |
| IMP3 | факты `GATES`/`COMMIT-AUDIT`, `reason` | `awf/verify_pack.py`, `awf/commit_gate.py`, `awf/api/run.py` | — | фикстуры `ok`/`failed`/`nothing-measured`/`error`/`missing`; оба режима |
| IMP4 | карта: данные и выборка | `awf/data/tool_map.yaml`, `awf/brief.py` | IMP2 | ≤5, первый — совет, непустой `why`; INV3 |
| IMP5 | карточка: `entry`, verify, `phase_prompt`, бюджет | `awf/brief.py`, `awf/api/brief.py` | IMP2–IMP4 | ≤900 слов, детерминизм, пустой проект, указатель на поле |
| IMP6 | `run_active`, фразы ожидания | `awf/api/wait_event.py`, `api/_results.py`, `tools/awf.py` | IMP1 | оба режима; расхождение фразы — красный |
| IMP7 | лог текущего юнита | `awf/api/_helpers.py`, `_background.py`, `context.py` | — | `test_status_log_tail.py`; баннер прошлого юнита не виден |
| IMP8 | носители, указатели, числа | шаблоны ролей, `lifecycle.py`, `phase.py`, `agents_md.py`, skill, USAGE EN/RU, `docs/design.md`, `README.md` | IMP1, IMP6 | ни один совет не зовёт снятый тул; счётчики 54/49 |

Порядок: IMP1, затем IMP2–IMP8.

## 8. Покрытие

R1 (AC1–AC4) — D1–D3, K6, IMP1/8. R2 (AC5–AC8, AC42) — D4/D5, K1, IMP2. R3 (AC9–AC12, AC45, AC47) — D1/D2, §6, IMP1. R4 (AC13–AC17, AC41, AC46) — D5, K1/K2, IMP2/5. R5 (AC18–AC21) — D8, K2, IMP4. R6 (AC22–AC25) — D11, K3, IMP6. R7 (AC26–AC29, AC43, AC44) — D6/D7, K1/K4, IMP3/5. R8 (AC30–AC33) — D3/D9, IMP5. R9 (AC34–AC36) — D10, K5, IMP7. R10 (AC37–AC40) — D1/D3, IMP8. R11 (AC48–AC49) — D3, K1, IMP5.

INV1–INV3 — IMP5/IMP4; INV4–INV7 не трогаем; INV8–INV9 — IMP1; INV10 — D4/D5.

## 9. Допущения и вопросы

A-1 — 900 слов — потолок: урезание D9 допустимо при сохранении состояния, `next_action`, шагов, карты, verify (AC31).
A-2 — «≤5» — по строкам карточки; `inventory_only` — у `helpers` и удаляемых записей.
A-3 — `state_updated_at` — ISO из `current.yaml`; неизвестное время считается старым (D7, FR13).
A-4 — отказ удалённого имени — `ToolError` с нашим текстом: вызывающий должен увидеть, что тула нет.

Q-1 (к тест-спеке) — T1 ждёт 54; T2/T16 держат три места «заменяет N вызовов»; T3 не зовёт удалённый API; T12 меряет только `text`; T17 — 54/49.
Q-2 — поведение коммит-гейта не исполняется: проверяются запись `reason` и чтение артефакта.
