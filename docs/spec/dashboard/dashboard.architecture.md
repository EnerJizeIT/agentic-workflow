# Архитектура: дашборд awf — рендер фактов, а не второй вычислитель

Статус: REVIEW
Ревизия: arch-2
Входы: dashboard.md@req-6 (PASS @rrv-6), dashboard.context.md@ctx-1, dashboard.test-spec.md@ts-1, TODO-0357
База кода: e6878c3

## 1. Экраны и блоки

| Блок | Поле K2 |
|---|---|
| Шапка: статус, стадия+роль, снимок, граница | `status`, `status_text`, `stage`, `generated_at` |
| Сайдбар: стадии и роли | `stages` |
| События: последние 30, красное при `exit≠0` | `events` |
| Verify и коммит: вердикт, секции, таймер | `verify`, `verify_waiting` |
| Забег: позиция, бюджет, простой | `run` |
| Worker: дерево, вывод, длительность | `worker` |
| Дифф: база, коммиты, контракт | `diff` |
| Сводка TODO и полный текст | `todo` |
| Уведомление и проблемы | `notification`, `problems` |

## 2. Границы и точки изменения

Дашборд — читатель: новых писателей и файлов состояния нет (A1).

| Компонент | Ответственность | Файл | R |
|---|---|---|---|
| Снимок | `/api/state`, статус от `get_status` | `awf/api/dashboard.py` | R5 |
| Статус | поле `phase` | `awf/api/_results.py`, `lifecycle.py` | R5, R6 |
| Пак и коммит | один ответ `verify_facts` | `awf/brief_sections.py` | R4, R7 |
| Выход движка | чтение `ENGINE-EXIT-*.md` | `awf/_engine_liveness.py` | R3 |
| События, уведомление | классификатор, граница, ключ | `dashboard.py`, `_log_reader.py` | R1, R8 |
| Попытка, ETA | счётчик, медиана по логу | `awf/api/dashboard.py` | R9, R16 |
| Дерево воркера | общий читатель | `awf/worker_tree.py` | R9 |
| Дифф | база, коммиты, контракт | `awf/verify.py` | R10 |
| Авто-открытие | одно правило | `dashboard_open.py`, `dashboard_server.py`, `orchestrator.py`, `cmd_dashboard.py`, `tools/awf.py` | R12 |
| Экран | блоки, пустое, финал | `awf/templates/dashboard.html.j2` | R1–R11, R16 |
| Документация | вкладки, 3 с | `USAGE*.md`, `BACKLOG.md` | R13 |

`read_gates_facts`, `read_commit_audit`, `run_brief`, `live_members_of_state`,
`_state_coalescer`, `read_log_snapshot` не меняют владельца; расширяется
только `verify_facts` (K1).

## 3. Решения

| D | Решение | Почему и что отвергли |
|---|---|---|
| D1 | `generate_state_dict(*, status=None)` берёт `StatusResult` из `get_status`; `_determine_status` → `_status_view`; `phase` — в `StatusResult` | второй вычислитель расходился (CTX3); probe и лог дублировали `get_status`. Нет `.agentic` — `idle` и `problems` |
| D2 | пак и коммит — только `verify_facts` (K1) | шаблон парсил `GATES`/`COMMIT-AUDIT` сам; два читателя расходятся. Старый аудит — `fresh=false`, не `None` |
| D3 | попытка из `stage_attempts`, лог — только `log_n` | движок пишет до запуска, лог отстаёт; при расхождении на экране число state |
| D4 | ETA — медиана завершённых стадий × остаток, <2 — `null` | среднее врёт на выбросах; ETA — не факт |
| D5 | события — лог + синтетические `gates`/`engine-exit` при смене; память процесса и `reset_fact_memory()`; граница — `run.yaml.started_at` при `active=true`, иначе строка `Pipeline started` | старый `run.yaml` резал события (RR28); `Pipeline exited` — не событие |
| D6 | `should_open` — одна реализация, два маркера, явный путь 60 с | три пути — три поведения (CTX8, CTX20); MCP импортирует правило |
| D7 | снимок до `clear_state`; вкладка замораживает последний ответ | после `clear_state` вердикта нет (RR36) |
| D8 | дерево из `live_members_of_state`; орфан — `ppid` 1 или вне набора; дифф из `diff_facts_for_todo` | `ps --ppid` — второй разбор (CTX9); размер диффа выдавался за работу юнита (CTX10) |
| D9 | блоки читаются отдельно, ошибки — в `problems` | один битый файл ронял экран (R11) |
| D10 | один разбор сводки и полного текста: ведущий front matter и комментарии пропускаются, `---` → `<hr>` | сводка показывала YAML (CTX1) |
| D11 | на снимок — по одному вызову `get_status`, лога, `run_brief`, дерева, git; отпечаток — кэш 10 с | опрос 3 с упирается в AC14 (RR18) |
| D12 | объём spec-пакета — AC36: шесть документов, ≤ 2200 слов каждый, ≤ 11 000 суммарно, проверяет `awf spec check`; длительность процессов — K6 | RR46 смешал объём пакета и размер выборки verify |
| D13 | `notification` считает сервер (K8), память — `set`, сброс как D5; JS только `notify` | JS-дедуп недоказуем (QA1) |

## 4. Контракты

### K1 — ответ `verify_facts`

Владелец — `awf/brief_sections.py`; бриф и дашборд читают его. Прежние
поля (`unit`, `report`, `note`, `waiting_on`, `close_with`) сохраняются. Новые:

```
status: ok|failed|nothing-measured|error|unreadable|missing|hidden
failed_sections: [str]; first_failure: str
pack_hidden_reason: ""|not-current|no-tree-sha|other-tree
commit: {status, reason, excluded: [[path, reason]], mtime: ISO|"",
         fresh: bool, from_previous_pass: bool}
```

`missing` — «файла нет». В verify и при `verify_waiting` пак виден всегда; вне —
файл новее `updated_at` **и** отпечаток дерева совпал, иначе `hidden`:
`not-current`, `no-tree-sha`, `other-tree`. `commit.fresh` — verify идёт или
`mtime` новее `updated_at`; `from_previous_pass` — `mtime` старше
`verify_waiting.since`. Времени в `COMMIT-AUDIT` нет. `{}` — без `todo_id`;
отпечаток — кэш D11.

### K2 — `/api/state`

Каждое поле читает шаблон (INV5). `verify` — K1; `run` — поля `run_brief`;
`todo_diff_stat` → `diff.stat`.

| Поле | Значения | Источник |
|---|---|---|
| `generated_at` | ISO-время расчёта | снимок |
| `status`, `status_text`, `phase` | `StatusResult` | `get_status` |
| `stage` | `{name, role, label, icon, color}`/`null` | `pipeline.yaml` |
| `stages` | те же поля + `status` | снимок юнита |
| `verify` | K1, включая `commit` | `verify_facts` |
| `verify_waiting` | `{since, elapsed_seconds, deadline_seconds, reason}`/`null` | `get_status` |
| `checkpoint` | `{pending, url}`/`null` | `get_status` |
| `attempt` | `{n, log_n}` | `stage_attempts`, лог |
| `eta` | `{estimate_sec, median_sec, stages_done, stages_total, elapsed_sec}`/`null` | лог |
| `events` | K4, последние 30 | лог + факты |
| `notification` | K8 | `dashboard.py` |
| `worker` | `{active, last_line, members[]}` | дерево D8 |
| `diff` | `{stat, base_sha, commits_since_base, base_stale, contract_files, outside_contract}` | `diff_facts_for_todo` |
| `log` | `{path, age_minutes, stale}` | `read_log_snapshot` |
| `engine_exit` | `{found, path, rc, signal, stage, todo, pid, traceback_tail, ts}` | K4 |
| `blocked` | `{todo, reason, note_path}`/`null` | outbox |
| `todo` | `{id, summary, full_text}` | TODO (D10) |
| `run` | `position`, `completed`, `stop_reason`, `budget_minutes`, `budget_left_minutes`, `elapsed_minutes`, `downtime_minutes`, `stall`, `report_file` | `run_brief` |
| `problems` | K7 | читатели блоков |

`run.active=false` — «забег закончен»; `worker.active=false` — «воркер не
запущен»; `blocked.reason` — первый абзац `outbox/BLOCKED-<id>.md`; без границы
`events` пусты и шапка говорит «граница прогона неизвестна».

### K3 — попытка и ETA

`attempt.n = stage_attempts["<todo>:<stage>"]` (нет ключа — 1); `log_n` — число
вхождений стадии в лог; на экране `n`, в подсказке оба. ETA:
`estimate_sec = median_sec * (stages_total - stages_done) + elapsed_sec`;
медиана — по стадиям текущего прогона с началом и концом; меньше двух — `null`
и «оценки нет».

### K4 — событие и выход движка

```
{epoch:int, ts:"HH:MM:SS", type:"start|info|warn|done|fail",
 source:"log|gates|engine-exit", msg:str}
```

`Agent stage finished … (exit=0)` → `done`; `exit≠0` → `fail` с кодом;
`checklist-check: skipped`, `A7: fix_round`, `Transition: … action=fix_round`,
`BD-30: supervisor signal timeout`; вердикт пака и выход движка — `gates`/
`engine-exit`, только при смене. Старше границы (D5) скрыты.

`_engine_liveness.read_exit_facts(project_dir, todo_id)` — единственный читатель
формата: новый по времени и имени `ENGINE-EXIT-*.md` с `- todo:` юнита; `None` —
«файла нет». Снимок отдаёт `path` (`context/ENGINE-EXIT-*.md`) и `found=false`.

### K5 — авто-открытие

```python
should_open(project_dir, url, *, mode: "auto" | "explicit",
            run_started_at: float | None = None) -> tuple[bool, str]
```

Причины: `opened`, `already-open`, `not-this-run`, `server-failed`, `no-gui`.
`run_started_at` — старт движка/CLI, у MCP — `launched_at`. Авто: живой
`http://`, `dashboard_boot` свежий (`ts >= run_started_at - 5`), в запуске не
открывали (`opened.boot_ts == boot.ts`), для URL прошло больше 600 с или URL
другой. Нет/битый boot — URL текстом. `file://` — только 600 с. Явно — 60 с.

```json
{"dashboard_boot": {"pid": 1234, "ts": 1762300000.0},
 "dashboard_opened": {"ts": 1762300100.0,
                      "url": "http://127.0.0.1:43129",
                      "boot_ts": 1762300000.0}}
```

Объекты: `state/dashboard_boot` и `context/dashboard_opened`.
`start_dashboard_with_reuse` пишет boot на каждом старте сервера, включая тот же
порт, до файла порта. `open_dashboard` зовёт `gui_available()`; CI, тесты,
`AWF_NO_DASHBOARD_OPEN`, нет GUI — не открывается ничего (AC30).

### K6 — дерево, роль стадии, дифф

`members[]` — `{pid, ppid, label, stage, orphan, duration_sec}` из `ProcInfo`;
`label` — `cmdline` без секретов; `orphan` — `ppid` 1 или вне набора `pid`;
`duration_sec` — `now - (btime + starttime/CLK_TCK)` (`/proc/<pid>/stat` поле 22,
`/proc/stat`), `null` — «длительность неизвестна». `last_line` — последняя
непустая строка без `^>\s*build\s*·` (нет — «нет вывода»).

Запись чата: `role` — имя стадии, `role_name` — роль, `label`/`icon`/`color` —
`_role_visual`; та же пара в сайдбаре и уведомлении.

`diff.base_stale = commits_since_base > 0`; `contract_files` — `files` контракта
(пусто без него); `outside_contract` — изменённые вне списка. При `base_stale` —
«всё после базы, не только этот юнит» (AC25).

### K7 — пустое и битое

`problems[]` — `{block, text}`; `block` ∈ `project`/`todo`/`handoff`/`gates`/
`commit`/`pipeline`. Пустое дерево — `project`: «нет `.agentic/`, создайте:
`awf_init`». `pipeline.yaml` с 0 стадий — `pipeline`: «стадий нет,
заполните `stages`» (AC27, T17). Битый TODO/handoff — свой `block` и текст
ошибки; `unreadable` пак — `gates`, «данные повреждены»; `COMMIT-AUDIT` `None` —
«аудита коммита нет» (AC28). Прошлый юнит не подставляется.

### K8 — уведомление

```
notification: {key: "<kind>|<todo>", kind: "blocked|dead|stale|done",
               text: "<одна строка причины>"} | null
```

Текст: `blocked.reason`, первая строка `traceback_tail`,
`stale_stage`, вердикт финала. `notification_fact(status, blocked, engine_exit)` —
чистая функция; `maybe_notify(fact, memory)` — дедуп через
`_NOTIFY_MEMORY: set[str]`: ключ в ответе один раз за процесс, повтор молчит. Сброс — старт
процесса, смена границы (D5), `reset_fact_memory()`. Терминальное — в снимке D7.

## 5. Пути и отказы

| Отказ | Что видит пользователь |
|---|---|
| нет `.agentic` или 0 стадий | страница, `idle`, `problems` (AC27) |
| пак скрыт/битый | причина K1 или «данные повреждены» (AC8, AC28) |
| нет `COMMIT-AUDIT` | «аудита коммита нет» (AC28) |
| нет `ENGINE-EXIT` | «файла нет» и путь (AC7) |
| лог молчит | «не обновлялся N мин»; «устарел» только при `stale_stage` (AC18) |
| сервер остановлен | заморозка, снимок от HH:MM:SS (AC15) |
| порт занят чужим | старт на новом, маркер обновлён (A-1) |
| свой сервер не стартовал или boot битый | авто не открывает, `--open` открывает (AC29, AC31) |

## 6. Владельцы фактов

| Факт | Владелец |
|---|---|
| статус, стадия, ожидание, `phase` | `get_status` |
| вердикт и видимость пака, возраст коммита | `verify_facts` |
| стадии и роли | `pipeline.yaml` (снимок юнита) |
| попытка, лог, ETA, события, выход, дерево, забег, дифф | `stage_attempts`, `read_log_snapshot`, `read_exit_facts`, `live_members_of_state`, `run_brief`, `diff_facts_for_todo` |
| уведомление и память ключей | `dashboard.py` (K8) |
| решение об открытии | `should_open` |

## 7. Совместимость

- `/api/state`: `todo_diff_stat` → `diff`; `run` — все поля; шаблон читает
  новые ключи. Пинящие тесты — в тех же IMP: `test_dashboard*`,
  `test_reports29*`, `test_reports30*`, `test_stale_stage_detect.py`,
  `test_audit12_incidents.py`.
- `awf_status`: +`phase`, остальные поля прежние.
- Открытие: фикстура `test_dashboard_open.py` получает boot и `run_started_at`;
  `test_reports30_dashboard_open_reuse.py` ждёт «нет/битый маркер — не открываем».
- MCP: явный тул — 60 с, запускающие — авто; `awf_start` не меняется.
- Снимок атомарный, `file://` без сервера (INV3).

### 7а. Поверхность × исход

| Поверхность | Исход |
|---|---|
| движок, `_maybe_open_dashboard` | переведено: авто + старт прогона |
| CLI `awf dashboard` без `--open` | исключено: только печатает URL |
| CLI `awf dashboard --open` | переведено: явно, окно 60 с |
| MCP `awf_start`/`continue`/`run_next` | переведено: авто с `launched_at` |
| MCP `awf_open_pipeline_dashboard` | переведено: явно, окно 60 с |
| `file://` без сервера | переведено: только окно 600 с |
| CI, тесты, `AWF_NO_DASHBOARD_OPEN`, нет GUI | переведено: не открывают ничего |
| `dashboard_boot` нет/битый или сервер не стартовал | переведено: авто не открывает, URL текстом |
| порт занят чужим, старт на новом | переведено: маркер обновлён, новый URL по окну |
| поля `/api/state` и финал | переведено: ответ владельца, заморозка |

## 8. План юнитов

| IMP | Что | Файлы | Зависит | Проверка |
|---|---|---|---|---|
| IMP1 | статус, `phase`, `_status_view`, блок `blocked` | `dashboard.py`, `_results.py`, `lifecycle.py` | — | равенство `get_status`; stale-тест; T3 |
| IMP2 | `verify_facts` K1, блоки verify/commit/waiting, кэш отпечатка | `brief_sections.py`, `dashboard.py`, шаблон | IMP1 | `ok`/`failed`/`unreadable`/`hidden`/`missing`; C10 |
| IMP3 | попытка и ETA | `dashboard.py`, шаблон | IMP1 | C9; <2 стадий — «оценки нет» |
| IMP4 | события K4, `read_exit_facts`, граница, уведомление K8 | `_log_reader.py`, `_engine_liveness.py`, `dashboard.py`, шаблон | IMP1, IMP2 | C1, C11; `exit=1` красный; T13 |
| IMP5 | авто-открытие K5 на всех путях | `dashboard_open.py`, `dashboard_server.py`, `orchestrator.py`, `cmd_dashboard.py`, `tools/awf.py` | — | 10 случаев C8; один путь |
| IMP6 | дерево, длительность, стадия+роль, дифф, сводка | `dashboard.py`, `worker_tree.py`, `verify.py`, шаблон | IMP1 | C5, C6; орфан; T14 |
| IMP7 | финал, пустое/битое, `problems` | `dashboard.py`, шаблон | IMP1–IMP4 | C3, C4, C12; 0 стадий |
| IMP8 | доки, замер опроса, бюджет пакета | `USAGE*.md`, `BACKLOG.md` | IMP1–IMP7 | R13; AC14; AC36 |

Порядок: IMP1 → IMP2 → IMP3/IMP4 → IMP5/IMP6/IMP7 → IMP8; IMP5 параллелен.

## 9. Покрытие

R1 — D5/K4/IMP4. R2 — D10/IMP6. R3 — D1/D5, K2/K4, IMP1/4. R4 — D2, K1/K2,
IMP2. R5 — D1/D2/D11, K1/K2, IMP1/2. R6 — D7/D8, K2, IMP6/7. R7 — D2/D7,
K1/K2, IMP2/6. R8 — D13/K8/IMP4. R9 — D8/K6/IMP6. R10 — D8/K6/IMP6. R11 —
D9/K7/IMP7. R12 — D6/K5/IMP5. R13 — IMP8. R16 — D4/K3/IMP3.

INV1 — IMP2/6/7; INV2 — не трогаем; INV3/INV8 — D9/IMP7; INV4 — D11/IMP8;
INV5 — K2 (каждый IMP); INV6 — D1; INV7 — IMP1–8; INV9 — D6/K5/IMP5;
INV10 — D11/IMP2.

## 10. Допущения и вопросы

A-1 — RR43: занятый порт — старт на новом; «вкладки нет» — если сервер не
стартовал.

A-2 — RR44: «одна вкладка за запуск» держится `boot_ts` в `dashboard_opened`.

A-3 — RR45: MCP `awf_open_pipeline_dashboard` — явно 60 с; запуск — авто.

A-4 — RR46: длительность процессов — K6; объём spec-пакета — AC36 (D12).

Q-1 (тест-спеке): C8 различает «сервер не стартовал» и «порт занят»; AC14 —
медиана 5 запросов; T13 — `notification` и сброс памяти; T14 — «стадия + подпись роли».
