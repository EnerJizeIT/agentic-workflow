# Тестовая спецификация: единая инструкция супервизора через `awf_brief`

Статус: REVIEW
Ревизия: ts-5
Входы: supervisor-brief.md@req-11 (PASS @reqrev-11), supervisor-brief.architecture.md@arch-5, supervisor-brief.context.md@ctx-2, supervisor-brief.requirements-review.md@reqrev-11, TODO-0326 (витки 1–5)
База кода: 40bb6cc (56 → 54 MCP-имени; карта 66 → 64)

Проверки **спроектированы**, не пройдены; ts-5 следует arch-5.

## 1. Scope

R1–R11 (R2→T3, R6→T14/T15, R10→T17), AC1–AC49, INV1–INV10 на наблюдаемом поведении: ответ `awf_brief`, набор и описания тулов, ответы `awf_status`/`awf_wait_for_event`, читатели `GATES`/`COMMIT-AUDIT`, тексты носителей; состояния S0–S13 и S1a. Вне scope: движок, `verify_pack.verify_pack`, `DONE.json`/handoff, поведение коммит-гейта.

## 2. Матрица покрытия

| Критерий | Сценарий | T |
|---|---|---|
| AC1–AC3 | C1 | T2 |
| AC4, INV9 | C10 | T1 |
| AC5–AC8 | C2 (2d), C7 | T3 |
| AC9–AC12, AC45, AC47 | C9 | T16 |
| AC42, INV1 | C7 (2a) | T4 |
| AC13, AC14 | C1, C5 | T5 |
| AC15–AC17, AC41, AC46, INV10 | C2, C4 (2c), C5 (2c) | T6 |
| AC16, AC26, AC44 | C3 | T7 |
| AC27, AC29 | C3 (2a, 2g), C6 (2b) | T8 |
| AC28 | C3 (2e, 2f) | T9 |
| AC43 | C6 | T10 |
| AC18–AC21, INV3 | C8 | T11 |
| AC30–AC33, INV2 | C8 | T12 |
| AC34–AC36 | C2, C5 (2a) | T13 |
| AC22, AC24, AC25 | C2 (2a–2c) | T14 |
| AC23, INV5 | C2 | T15 |
| AC37–AC40, INV6 | C9 | T17 |
| INV4, INV7 | C4 (2b) | T18 |
| AC48, AC49 | C8 (2c) | T20 |
| INV8 | — | T19 |

## 3. Карточки проверок

Общее: unit/contract в `tmp_path`; реальны файлы проекта, YAML, реестр и MCP (`importorskip`), без живых процессов; изоляция — свой проект на тест.

### T1 — Набор имён MCP сокращается
- Покрывает: AC4, INV9, C10, D1, IMP1.
- Вход: `registry.tool_names()`, `create_server()._tool_manager._tools`, `brief.map_coverage()`; эталон `40bb6cc` в тексте теста — 56 имён (51 `awf_*` + 5 UI).
- Ожидания: 54 имени (49 + 5); разница ровно `{awf_current_step, awf_load_supervisor_context}`; реестр, сервер и карта дают один набор.
- Красное/ловит: добавленное имя, снят другой тул, имя только в `tool_map.yaml`; новый тул вместо правки старого, тихое возвращение дубля.

### T2 — Один вход, один маркер, три заявки
- Покрывает: AC1–AC3, C1, K6, IMP6/IMP8.
- Вход: описания 54 тулов, докстринги, `agents_md.py`, сгенерированный `AGENTS.md`, текст и JSON `api.brief`.
- Ожидания: «START of a new session» — только у `awf_brief`; «START of every interaction» нет ни у кого; `entry == {"tool": "awf_brief", "single": true, "recall_when": ["unclear moment", "lost context", "stage changed"]}`; карточка называет себя единственным входом. Пусты три места: `tools/awf.py:2526` «Replaces 5-6 separate tool calls at session start or before writing», `agents_md.py:94` «Replaces 5-6 separate calls.», `api/context.py:317` «Replaces 5-6 separate tool calls at session start:»; по дереву и `AGENTS.md` не матчатся `[Rr]eplaces?\s+\d+([–-]\d+)?\s+separate` и «заменяет N вызовов».
- Красное/ловит: второй маркер, четвёртое место, `single: false`, пропуск в `recall_when`; CTX1, FR15.

### T3 — Совет совпадает у брифа и статуса
- Покрывает: AC5–AC8, C2 (2d), D4, IMP2.
- Вход: `api.brief`, `api.get_status`, MCP `awf_status`; фикстуры S1–S13, S1a.
- Ожидания: `next_action` трёх поверхностей совпадает посимвольно; статус отдаёт непустой совет; снятый API не импортируется.
- Красное/ловит: расхождение в одном S, пустой совет у статуса; второй владелец совета (CTX2).

### T4 — Пустой каталог
- Покрывает: AC42, INV1, C7 (2a), IMP5.
- Вход: `api.brief`, `awf_status` на несуществующем пути и каталоге без `.agentic/`.
- Ожидания: бриф — карточка с setup-цепочкой (`awf_init`), без исключения; статус — словарь с отказом, содержащим `awf_brief`; `verify` пуст, файлов не создано.
- Красное/ловит: исключение, отказ без ссылки; тупик новичка.

### T5 — Четыре элемента в каждом состоянии
- Покрывает: AC13, AC14, C1, C5, K1, IMP5.
- Вход: `api.brief(...).as_dict()`; 15 фикстур S0–S13, S1a.
- Ожидания: `situation` из набора; `situation_line` непуст и без кода `S\d`; `next_action` непуст; `advice_tool` входит в `tool_registry()`.
- Красное/ловит: тул вне реестра, состояние кодом; AC44.

### T6 — Приоритет и ветки S1a, S11–S13
- Покрывает: AC15–AC17, AC41, AC46, INV10, C2, C4 (2c), C5 (2c), D5.
- Вход: `classify_status`/`refine_with_run`, поле `situation`.
- Комбинации: checkpoint + salvage → salvage; `foreign_commit` + salvage → S1a; blocked/budget при живом ране → S7/S8; `stale_stage` + `pipeline_running` → S5; reject при живом ране → S11a (`awf_run_revise`/`awf_run_next`), без рана → S11b (`awf_dispatch_todo` + `awf_start`); approve → S12; форма → S13.
- Ожидания: id и `advice_tool` по K2; S1a — чужой коммит, `git log -1`, `awf rollback`, повтор, не salvage; S2 — юнит и вопрос; S3 — бюджет и владелец; S7 — `mode`, S8 — `verify`.
- Красное/ловит: salvage над чужим коммитом, `awf_start` при живом ране, повторный approve; потеря решения владельца (RR36), регрессия A31.

### T7 — Поля verify и «чего ждёт движок»
- Покрывает: AC16, AC17, AC26, AC44, C3, K1/K4, IMP3/IMP5.
- Вход: `api.brief` с отчётом `failed` (`contracts` fail, `tests` pass), живой цикл.
- Ожидания: `verify.unit == "TODO-0326"`; `status` из `ok`/`failed`/`nothing-measured`/`error`/`missing`/`unreadable`; `report` — `GATES-TODO-0326.md`; `failed_sections == ["contracts"]`; `first_failure` из T8; `waiting_on` ∈ {`supervisor`,`engine`,`commit`,`none`}; ждёт approve — `advice_tool == "awf_approve"` без перепроверки; ждёт гейт — `waiting_on == "commit"`, `close_with == ""` («закрывает движок»); иначе `close_with` из реестра.
- Красное/ловит: выдуманный `close_with`, `commit` подменён `engine`, молчание; незнание, чего ждёт движок (A24/A29).

### T8 — Первая причина и аварийный отчёт
- Покрывает: AC27, AC29, C3 (2a, 2g), C6 (2b), K4, IMP3.
- Вход: `verify_pack.read_gates_facts(path)`, затем `api.brief`.
- Данные: две секции fail, в первой строка `- ...: FAIL (rc=1)` и ниже второй FAIL; заголовок `## 1. contracts: FAIL`; `**error**` без JSON; пустой контекст.
- Ожидания: `first_failure` — первая маркерная строка по `^- .*: FAIL \(rc=\d+\)`; заголовок не причина; при `ok` пусто; аварийная заметка — `status == "error"`, причина — её текст, не JSON, без падения; файла нет — `missing` и `awf verify-pack`.
- Красное/ловит: причина из лога или второй секции, битый JSON роняет карточку; чтение логов (K4), потеря аварийного вердикта (RR47).

### T9 — Какой юнит и когда вердикт жив
- Покрывает: AC28, AC29, C3 (2a, 2b, 2e, 2f), D7, IMP3/IMP5.
- Вход: `api.brief`, `get_status().current_todo`, `state_updated_at`.
- Данные: (a) одиночный `awf start`, пак собран; (b) забег, тот же юнит; (c) state новее артефакта, не verify; (d) нет `todo_id`; (e) нет артефакта, 100 чужих `GATES-*.md`.
- Ожидания: (a) и (b) — `unit == "TODO-0326"`, вердикт одинаков; (c) `verify == {}`, старый погашен; (d) `unit is None`, «вне живого цикла»; (e) чужие не читаются, glob нет, `missing`. Стадия verify даёт вердикт и без свежего `mtime`.
- Красное/ловит: вердикт в (c)/(d)/(e); пропажа вердикта в одиночном старте (FR8), вчерашние артефакты.

### T10 — Вердикт коммит-гейта из артефакта
- Покрывает: AC43, C6, A8, RR54, K4, IMP3.
- Вход: `commit_gate._audit_record`, `read_commit_audit`, `api.brief`.
- Данные: `COMMIT-AUDIT-TODO-0326.json` (`refused`, `reason: "sha mismatch"`); по записи на `committed`/`skipped`/`error`; без файла; причина только в логе; аудит прошлого прохода.
- Ожидания: `commit == {"status": "refused", "reason": "sha mismatch"}`; `reason` при всех четырёх статусах; `skip_reason` только для `plan-skip-inconsistent` и не подменяет `reason`; без файла — `missing`, «коммита ещё не было»; старый не читается; `api/run.py` читает публичную функцию.
- Красное/ловит: `reason` только при `skip_inconsistent`, glob; потеря причины (A8), слияние полей (RR54).

### T11 — Ситуационная карта
- Покрывает: AC18–AC21, C8, INV3, D8, K2, IMP4.
- Вход: `api.brief` по S0–S13 и S1a, `load_tool_map()`, `map_coverage()`, поле `tool_map`.
- Ожидания: ≤5 строк, непустой `why`, первая — `advice_tool`; у записи и группы свои `situations`; группа без записи не выводится; `inventory_only` (helper'ы, снятые записи) в карточку не идёт, но есть в перечне; флага `--full` нет; перечень 64 записи; карта и реестр совпадают в обе стороны; строка S1a есть (K2).
- Красное/ловит: 6 строк, пустой `why`, первый ≠ совет, `inventory_only` в карточке, нет S1a; стена имён (CTX7), AC20 — A-4.

### T12 — Бюджет и детерминизм
- Покрывает: AC30–AC33, INV2, C8, D9, A-1, IMP5.
- Вход: `api.brief` на «тяжёлой» фикстуре (§4).
- Ожидания: `len(text.split()) <= 900`; два вызова различаются только датой; есть `## What's next`, `## State`, `## Tool map`, совет и verify; `budget.clipped` непуст при урезании, `pointer` называет все отброшенные секции; состояние, `next_action`, `tools`, `verify` не урезаны.
- Красное/ловит: 954 слова (факт базы), молчаливое урезание нужного; CTX4.

### T13 — Хвост лога текущего юнита
- Покрывает: AC34–AC36, C2, C5 (2a), D10, K5, IMP7.
- Вход: `current_stage_log(project, state)`, `get_status`, `api.brief`.
- Данные: старый `logs/awf-start-TODO-0325.out` с «Pipeline complete», новый на три строки, state на `TODO-0326`; без файлов.
- Ожидания: хвост из 0326 без «Pipeline complete» и баннера прошлого юнита; статус и бриф называют один юнит и стадию; без лога — «лога нет» и путь.
- Красное/ловит: хвост из 0325, разные юниты; CTX5.

### T14 — Одно правило ожидания в текстах
- Покрывает: AC22, AC24, AC25, C2 (2a–2c), D11, K3, IMP6/IMP8.
- Вход: статическая проверка носителей и карточка.
- Носители: докстринг `awf_wait_for_event`, карточка, `phase-run.md`, `agents_md.py`, `SKILL.awf-supervisor.md`, `USAGE.md`, `USAGE.ru.md`.
- Ожидания: EN «wait only inside an active run» / «react to the user, no loop»; RU «жди только внутри активного рана» / «реагируй на пользователя, без цикла»; `wait.rule` по `mode`, `wait.loop` по режиму; реактивный без `tool`/`timeout`; при забеге `timeout` не выход, вызов повторяется.
- Красное/ловит: иная фраза у носителя, оба правила в одном ответе; CTX3.

### T15 — `run_active` в ответе ожидания
- Покрывает: AC23, INV5, C2, IMP6.
- Вход: `api.wait_for_event` и MCP на активной и закрытой записи рана.
- Ожидания: `run_active is True` при забеге, `False` без рана; прочие ключи прежние; `timeout` не даёт `done`.
- Красное/ловит: `run_active` из `pipeline_running`.

### T16 — Старые вызовы и удалённые имена
- Покрывает: AC9, AC10–AC12, AC45, AC47, C9, K6, D1/D2, IMP1.
- Вход: MCP-обёртки, `python -m awf brief|status`, `api/__init__.py`, `registry.REMOVED_TOOLS`.
- Ожидания: `awf_status` — ключи AC9 плюс `current_todo` и `state_updated_at`; вызов снятого имени — `ToolError` с именем, «removed»/«удалён» и `awf_brief`, не `Unknown tool` и не трейсбек; прочее неизвестное — стандартная ошибка; имён нет в `list_tools`/`tool_names()`/карте; подкоманд `current-step`/`supervisor-context` нет, `awf brief`/`awf status` дают rc=0 и прежнюю форму; `from awf.api import load_supervisor_context` и `SupervisorContextResult` падают, helper'ы `_extract_stage_info`, `_compute_expected_action`, `_compute_stage_kind` импортируются, `get_status` работает. Восемь пинящих тестов из §6 arch (`test_context*`, `test_run_plan_recovery`, `test_carry_batch_engine_queue`, `test_tool_descriptions`, `test_server_smoke`, `test_awf_tools`, `test_coverage_gaps`) обновлены или заменены, не удалены.
- Красное/ловит: имя в списке, `Unknown tool`, снятый helper, удалённый тест без замены; поломка потребителей (AC45), «удалили нужное» (AC47).

### T17 — Носители не зовут снятый тул рабочим
- Покрывает: AC37–AC40, INV6, C9, D12, IMP8.
- Вход: статическая проверка текстов, `test_supervisor_skill.py`.
- Ожидания: все носители §1 arch называют входом `awf_brief`; в `_core.md:48`, `phase-init.md:9`, `phase-normalize.md:6`, `lifecycle.py:1158`, `phase.py:276`, `tools/awf.py:2273` нет совета звать снятый тул; в skill, `USAGE*.md`, `README.md`, `docs/design.md`, `docs/file-bus.md` — числа 54/49 и ни одного рабочего упоминания; `design.md` не рисует `awf_verify_pack` шагом; описание `awf_status` перечисляет поля или ссылается на схему; установленные `AGENTS.md`/skill равны источникам байт-в-байт; пара «тул + режим» совпадает в EN и RU.
- Красное/ловит: совет зовёт снятый тул, числа не сходятся, расхождение фразы; CTX11, подсказка звать устаревший тул (FR9).

### T18 — Read-only статус и guard'ы
- Покрывает: INV4, INV7, C4 (2b).
- Вход: `get_status` при мёртвом движке; grep по двум файлам.
- Ожидания: хеши `.agentic/state` и `.agentic/logs` до/после равны; в `awf/_proc.py` есть `pid > 1`, в `tests/conftest.py` — `_forbid_session_kill`; тесты guard'ов зелены.
- Красное/ловит: запись состояния, снятие защиты сессии.

### T19 — Verify-пак остаётся, писатель и читатель сходятся
- Покрывает: R7, INV8, D6, K4, IMP3.
- Вход: integration в tmp-проекте и contract.
- Ожидания: `awf_verify_pack` есть среди 54; пишет `GATES-<id>.md`; `read_gates_facts` на этом файле даёт те же `verdict`, `sections`, `details`, `measured`, что JSON-блок; контракт `DONE.json` и handoff не меняются.
- Красное/ловит: второй парсер расходится на юникоде; снятая диагностика.

### T20 — Полный промпт фазы
- Покрывает: AC48, AC49, R11, C8 (2c), D3, K1, IMP5.
- Вход: `api.brief(...).phase_prompt`, `get_phase_prompt(phase, project_dir)`, `text`.
- Данные: промпт 775 слов / 5076 символов; S0; фаза без промпта.
- Ожидания: `phase_prompt == get_phase_prompt(...)` посимвольно, все строки, без усечения и ссылки на снятый тул; `phase_summary` остаётся выдержкой, `phase_clipped` на поле не влияет; для S0 пусто; карточка называет поле `phase_prompt`, не снятый тул, и остаётся ≤900 слов.
- Красное/ловит: обрезанный промпт, указатель на удалённый тул, сломанный бюджет; CTX14, потеря инструкции.

## 4. Среда, фикстуры, команды

- Runner: pytest (`pyproject.toml`, `timeout=120`), `tests/{unit,negative,agent_workflow_ui,integration}`; полный сьют — `scripts/suite-canonical.sh` (не запускался). Фикстуры: `_setup_project`, `_make_live`, `_make_active_todo`, `_mask_date` из `test_brief.py`, `_project(tmp_git_repo)` из `test_context_single_source.py`, новые — в `tmp_path`.
- Фикстура T12: 64 записи карты, 8 групп, живой юнит, goal 300 слов, непустые `defaults`/`scenarios`/`doctrine`/`what_new`, лог 300 строк. Состояния T5/T6/T11 — по фикстуре на S0–S13 и S1a; S11a/S11b — разные записи рана; S13 — форма; S1a — `foreign_commit` плюс salvage. T9 — `todo_id`/`updated_at` в `state/current.yaml`, `mtime` после state; T10 — аудит по четырём статусам.
- Файлы в `tests/unit/`: `test_tool_names_frozen.py`, `test_brief_contract.py`, `test_brief_verify_facts.py`, `test_brief_budget_live.py`, `test_log_tail_current_unit.py`, `test_text_single_rule.py`, `test_brief_advice_agreement.py`, `test_brief_invariants.py`.
- Смежные прогоны на ctx-2 (не запускались): 124 и 61 passed; `ruff check .`.

## 5. Приёмка реализации

1. Все T зелёные; T3, T5–T11, T13–T14, T16–T17, T20 сначала красные на базе (proven-red по поведению, не по импорту).
2. Пинящие тесты AC45 обновлены или заменены; полный сьют зелёный; `ruff check .` чист. Контракт K1 проверен по S0–S13 и S1a.
3. Предпосылки: git в tmp, MCP импортируется, живые процессы не нужны. Недоказанно: поведение коммит-гейта (T10 — запись `reason` и чтение) и смысл `when` в карте (проверяется непустота).

## 6. Findings

QA1–QA8 закрыты; открытые — MINOR, дизайн не блокируют.

| ID | Важность | Суть | Владелец | Состояние |
|---|---|---|---|---|
| QA9 | MINOR | RR52: AC18/AC19 — «S0–S13» без S1a, arch K2 и T11 его требуют | аналитик | открыт, T11 держит S1a по arch |
| QA10 | MINOR | RR53: AC11/AC40 не называют докстринги удаляемого API (`api/__init__.py:23`, `planning.py:4`, `run_plan_read.py:3`, `api/brief.py:31`, `brief.py:134`, `_results.py:475`); T17 их по букве не проверяет | аналитик, затем архитектор | открыт |
