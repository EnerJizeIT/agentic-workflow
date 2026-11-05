# Контекст: дашборд awf — что видит пользователь и что лежит в системе

Статус: REVIEW
Ревизия: ctx-1
Входы: TODO-0357@spec-context; `.agentic/pipelines/spec-dashboard.yaml@spec-context`
База кода: HEAD `e6878c3` (04.11, после TODO-0368); в дереве только runtime-артефакты прогона
Ограничения: исходники не менялись; полный сьют не запускался (целевые проверки — §7). Живые числа и примеры сняты с прогона TODO-0357 04.11, 03:17–03:25 UTC.

## 1. Итог для аналитика

- Дашборд — второй вычислитель состояния: `generate_state_dict`
  (`awf/api/dashboard.py:1055`) сам решает статус, спаны, попытки, таймеры и
  дифф; те же факты полнее считают `awf_status` (`lifecycle.py:1078`) и
  `awf_brief` (`brief_sections.py:217`).
- Ключевые факты уже лежат на диске, у каждого свой читатель: пак —
  `read_gates_facts` (`verify_pack.py:1900`), коммит — `read_commit_audit`
  (`commit_gate.py:496`), ожидание — `verify_waiting` (`supervisor.py:640`),
  забег — `run_brief` (`api/run.py:282`), дерево — `live_members_of_state`
  (`lifecycle.py:1185`). Дашборд не читает ни один из них.
- В `state/current.yaml` 27 ключей, дашборд читает 11; до экрана не доходят
  `verify_waiting`, `checkpoint_form_url`, `stage_attempts`, `worker_pid`,
  `gate_head_sha`.
- Живые дефекты «показывает не то»: front matter вместо цели, `✓ Stage complete`
  при `exit=1`, «Pipeline exited» после штатного финала, `Blocked` без причины
  (§8, CTX1–CTX11).
- Отчёт стадии `spec-finalize` (`dashboard.finalize.md`) лежит в корне пакета:
  `discover_documents` считал его лишним файлом, гейт научили игнорировать
  `*.finalize.md` (CTX21).
- Развилка для архитектора: свой снимок (спаны и попытки из лога) или рендер
  `get_status`/`verify_facts`; сейчас два владельца одного ответа.

## 2. Карта поверхностей

| Слой | Где | Что делает | Кто потребляет |
|---|---|---|---|
| Снимок состояния | `awf/api/dashboard.py::generate_state_dict:1055` | собирает 21 поле: статус, стадии, чат, TODO, события, таймеры, воркер | `/api/state`, первый рендер |
| HTTP | `awf/api/dashboard_server.py:187` | `/`, `/api/state`, `/api/runs`, `/api/run-report/<name>`; 3 с опрос; TTL 1 с + склейка параллельных запросов (`:33`) | браузер |
| Страница | `awf/templates/dashboard.html.j2` | topbar, verify-баннер, runbar, сайдбар, 3 вкладки, уведомления | пользователь |
| Открытие вкладки | `awf/api/dashboard_open.py:98` (движок, CLI) | политика «по URL»: авто — никогда второй раз; явно — окно 60 с (`:29`) | `orchestrator.py:92`, `awf dashboard --open` |
| Открытие вкладки | `agent_workflow_ui/src/agent_workflow_ui/tools/awf.py:2808` | своя политика: окно 10 мин (`:2769`), boot-маркер, `launched_at` | MCP `awf_start`/`awf_continue`/`awf_run_next` |
| Статика | `awf/orchestrator.py:557`, `:613` | перерисовка HTML на старте стадии и на чистом выходе | файл `dashboards/current.html`, `file://` fallback |
| Плагин `render/` | `agent_workflow_ui/.../render/` | формы `project-setup`, `pipeline-compose`, `ack`, `increment-planning` | к дашборду отношения не имеет |

Первый рендер и `/api/state` берут один и тот же словарь, но страница живёт опросом: статический HTML намеренно может отставать на стадию.

## 3. Что видит пользователь против того, что происходит

| Место в UI | Что видно | Что на самом деле | Где факт |
|---|---|---|---|
| Баннер verify | «пайплайн ждёт решения» без времени | вердикт, красные секции, время и дедлайн | `GATES-<id>.md`, `state.verify_waiting` |
| Бейдж статуса | `Blocked` | причина в `outbox/BLOCKED-<id>.md` | `state.last_signal`, outbox |
| Бейдж `Stale`/`Dead` | «pid N dead» и строка лога | rc, сигнал, стадия, хвост трейсбека | `context/ENGINE-EXIT-*` |
| Вкладка «События» | зелёное «✓ Stage complete» | стадия упала: `(exit=1)` | `orchestrator.log:26370` и др. |
| Сводка на «Итерации» | YAML: `pipeline: spec-dashboard` | цель юнита — первый абзац | живой рендер TODO-0357 |
| «Изменения vs baseline» | 76 файлов, `+6470/−342` | работа 17 коммитов других юнитов | `BASELINE-TODO-0357.sha`, `git diff` |
| Чат, активная запись | аватар «S», `Spec Context` | стадия в роли `agent-project-auditor` | `dashboard.py:1230`, `:1140` |
| Сайдбар стадий | два «Analyst» | `spec-requirements` и `spec-finalize` | `dashboard.py:1133` |
| Панель Worker | `> build · deepseek-v4.1-flash` | баннер opencode; работа — в транскрипте | `dashboard.py:879`, `:425` |
| Чип забега | скрыт при `active=false` | `3/4 done`, `stop_reason`, `budget`, `downtime` | `run_brief`, `dashboard.py:605` |
| Финал прогона | «Pipeline exited» синим | штатное завершение, `done` записан | `orchestrator.py:613-629` |
| Чекпойнт | бейдж без ссылки | `checkpoint_form_url` лежит в state | `state.current.yaml` |
| Таймлайн | один TODO, всё «approved» | 365 архивных юнитов, `rejected` нет | `dashboard.py:968` |

## 4. Артефакты: что в них есть и кто это читает

| Артефакт | Писатель | Факты | Читатель сегодня | Дашборд |
|---|---|---|---|---|
| `GATES-<id>.md` | `verify_pack` | verdict/exit, `measured`, 13 секций (часть SKIPPED), `details`, `first_failure`, `tree_sha` | `brief_sections.verify_facts`, `verify_context`, `commit_plan` | нет |
| `COMMIT-AUDIT-<id>.json` | `commit_gate` | `status` (committed/skipped/refused/error), `reason`, `excluded[]`, `worker_staged[]`, override | `read_commit_audit`, RUN-REPORT | нет |
| `state/verify_waiting` | `supervisor` | `since`, `elapsed_seconds`, `deadline_seconds`, `reason` | `get_status` (`_with_live_elapsed:1060`), `cmd_status` | нет |
| `run.yaml` | `run_state` | `active`, `queue`, `index`, `completed`, `outcomes`, `stop_reason`, `budget_minutes`, `downtime_seconds`, `report_file` | `run_brief` | 5 полей из 20 |
| `ENGINE-EXIT-*.md` | `_engine_liveness` | rc, сигнал, стадия, TODO, хвост трейсбека | `get_status` (engine_dead/stale) | нет |
| `state.worker_*` | `worker_tree` | pid/pgid/anchor/role/todo; живые потомки, включая MCP-серверы | `live_members_of_state` | только прямой ребёнок через `ps --ppid` |
| `orchestrator.log` | движок | вердикт пака, `checklist-check: skipped`, `exit=N`, таймауты, `background engine exited`, сигналы | лог-читатель | 8 классов событий из десятков |
| `CLOSER-<id>.json` | `closer_stage` | первый вердикт чек-листа, `attempts` (витки) | `awf_status`/RUN-REPORT | нет |
| `control.json`, `last-kill.json` | `run_state`, kill | решения забега с обоснованием, факт kill | `run_brief`, `status` | нет |
| Метрики | `awf metrics` | токены/стоимость по юниту из opencode DB | CLI/отчёт на рабочий стол | нет |

## 5. Переиспользуемые контракты

- `read_gates_facts(path)` — `verdict`, `sections`, `details`, `failed_sections`,
  `first_failure`, `note`, `tree_sha`; битый JSON деградирует к заголовкам; это
  единственный разбор пака.
- `read_commit_audit(project_dir, todo_id)` — единственный читатель аудита
  коммита, `None` при отсутствии и порче.
- `brief_sections.verify_facts(...)` уже собирает «вердикт + коммит + чего ждём»
  и правило свежести; дашборду нужен тот же ответ, не копия.
- `_with_live_elapsed(event)` пересчитывает возраст ожидания; рядом
  `deadline_seconds`.
- `detect_stale_stage(...)` — общий владелец «стадия замерла / движок мёртв»;
  дашборд его использует (`:344`), поэтому бейдж не расходится со статусом.
- `run_brief(project_dir)` — очередь, завершённые, бюджет, простой,
  `stop_reason`, отчёт, `stall`.
- `diff_stat_for_todo(...)` — `git diff --stat` от бейзлайна плюс новые файлы —
  «всё с бейзлайна», не «работа юнита».
- `live_members_of_state(project_dir)` — дерево воркера с потомками (A44).
- `_state_coalescer` (`dashboard_server.py:33`) — один расчёт на пачку опросов,
  TTL 1 с.
- Шаблонный контракт: `tests/unit/test_dashboard.py:1057` падает, если поле
  `/api/state` не читает шаблон.

## 6. Инварианты, которые нельзя потерять

- Экранирование: TODO и handoff — текст LLM; сначала `html.escape`, потом markdown, затем нейтрализация `href` (`dashboard.py:188`). JSON в `<script>` экранирует `<` (`:523`). Новые поля — тот же путь.
- `/api/state` без `Access-Control-Allow-Origin`; отчёт забега открывается только по имени из строгого шаблона и только внутри `outbox`.
- Запись HTML атомарная; испорченный handoff/TODO не должен давать 500 на опросе.
- Опрос: максимум один запрос в полёте, следующий планируется после ответа; 3 с (`шаблон:276`).
- Авто-открытие не должно срабатывать в тестах и без графической сессии — сейчас это гарантирует только путь движка (`gui_available`), MCP-путь проверки не имеет.
- Порт переиспользуется, вкладка держит URL между запусками; «мёртвый» порт проверяется на живость перед открытием.

## 7. Baseline

| Проверка | Команда | Результат |
|---|---|---|
| Целевые тесты дашборда | `python -m pytest tests/unit/test_dashboard.py tests/unit/test_dashboard_open.py tests/unit/test_dashboard_snapshot.py -q --timeout=300 -p no:cacheprovider` | PASS — 84 passed, 3.51 s |
| Линт | `ruff check .` | PASS — All checks passed |
| Живой снимок | `generate_state_dict(Path('.'))` на прогоне TODO-0357 | 37 826 байт, 0.59 с; 30 событий, 8 стадий, 1 активная запись чата |
| Профиль холодного опроса | тот же вызов под `cProfile` | 1.29 с; холодный проход лога 35 535 строк, 6 обращений к снимку лога |
| Полный сьют | не запускался | NOT_RUN — вне политики стадии |

Отдельного QA-артефакта `SUITE-<sha>-*.json` для этого дерева нет, поэтому опора — целевые тесты.

## 8. Находки

| ID | Важность | Файл / символ | Факт | Влияние | Следующий шаг | Уверенность |
|---|---|---|---|---|---|---|
| CTX1 | MAJOR | `dashboard.py:813-854` | `_read_todo_summary` пропускает `<!-- -->` и `#`, но не ведущий `---`: сводка на TODO-0357 начинается с `--- pipeline: spec-dashboard`; `---` после абзаца становится setext-заголовком. | Первое на вкладке «Итерация» — служебный YAML, не цель. | Аналитик: AC «сводка = цель». Исполнитель: пропускать ведущий front matter. | высокая |
| CTX2 | MAJOR | `_log_reader.py:66-114`, `dashboard.py:172` | `✓ Stage complete` отдаётся на любую строку `Agent stage finished`, включая `(exit=1)` (`orchestrator.log:26370`, `:28175`, `:34506`). | Зелёное «завершено» при падении — прямое «врёт». | Исполнитель: классифицировать exit-код. | высокая |
| CTX3 | MAJOR | `dashboard.py:1055`, `brief_sections.py:217` | Дашборд не читает `GATES` (0 упоминаний); вердикт и красные секции уже собирает `verify_facts`. | Решение в verify принимается вслепую. | Архитектор: один читатель `read_gates_facts`. | высокая |
| CTX4 | MAJOR | `dashboard.py:1055`, `commit_gate.py:496` | `COMMIT-AUDIT-<id>.json` (`status`, `reason`, `excluded`) не читается: у TODO-0340 вне коммита 10 файлов, на экране нет. | Отказ коммита невидим. | Аналитик: AC на статус коммита; `read_commit_audit`. | высокая |
| CTX5 | MAJOR | `шаблон:196`, `supervisor.py:640` | Баннер verify статичен: нет `since`, дедлайна и причины, хотя `verify_waiting` есть в state. | Свежее ожидание не отличить от зависшего. | Исполнитель: поля в `/api/state`, живой таймер. | высокая |
| CTX6 | MAJOR | `dashboard.py:384`, `outbox/BLOCKED-*.md` | Статус `Blocked` без причины; заметка `BLOCKED-TODO-0329.md` содержит разбор, дашборд её не открывает. | Красный бейдж без объяснения. | Исполнитель: причина в бейдже и панели. | высокая |
| CTX7 | MAJOR | `orchestrator.py:613-629`, `шаблон:262` | Финальный HTML пишется до `write_state(phase="done")`, затем сервер останавливается; вкладка после трёх неудачных опросов пишет «Pipeline exited». | Штатный финал выглядит аварией, «Iteration complete» не появляется. | Архитектор: держать сервер или отдавать снимок; исполнитель: порядок записи. | высокая |
| CTX8 | MAJOR | `dashboard_open.py:80-95`, плагин `awf.py:2769-2803` | Две политики открытия: движок — никогда второй раз для URL, MCP — окно 10 минут и boot-маркер; `gui_available` есть только у движка. | Поведение зависит от способа запуска. | Аналитик: одно правило; архитектор: один владелец. | высокая |
| CTX9 | MAJOR | `dashboard.py:1133-1153`, `шаблон:377` | Стадия показывается только по роли: `spec-requirements` и `spec-finalize` обе «Analyst». | Непонятно, какая стадия идёт. | Исполнитель: имя стадии рядом с ролью. | высокая |
| CTX10 | MAJOR | `verify.py:360`, `dashboard.py:628` | `diff_stat_for_todo` считает всё от бейзлайна: `1bfe1d2` отстал на 17 коммитов, на экране 76 файлов `+6470/−342`. | Чужая работа приписана юниту. | Аналитик: что считать работой; архитектор: пометка «база устарела». | высокая |
| CTX11 | MAJOR | `шаблон:669-684` | Уведомления только для `verify` и `salvage`; `Blocked`, `Dead`, `Stale`, финал — молча. | Проблему видно, только открыв вкладку. | Исполнитель: те же события в `Notification`. | высокая |
| CTX12 | MINOR | `_log_reader.py:66-118` | В события не входят `verify-pack`, пропуск чек-листа, таймаут, выход фонового движка, `worker exit`, ретраи; нет границы запуска (последние 30 из всех). | Старые события выглядят текущими. | Исполнитель: новые классы и граница. | высокая |
| CTX13 | MINOR | `dashboard.py:1230-1268`, `:1140` | Чат строит визуал по имени стадии (`spec-context` → «S»), сайдбар — по роли (🔬). | Одна стадия выглядит по-разному. | Исполнитель: визуал по роли. | высокая |
| CTX14 | MINOR | `dashboard.py:879-935`, `:425-499` | «Последняя строка» воркера — `> build · deepseek-v4.1-flash`; дерево потомков не читается, `worker_pid` не используется. | Панель Worker не показывает работу и орфанов. | Исполнитель: дерево из `live_members_of_state`, строка из лога. | средняя |
| CTX15 | MINOR | `шаблон:196`, `state.current.yaml` | При `checkpoint_pending` в state есть `checkpoint_form_url`, в UI только бейдж. | Не найти форму после закрытия. | Исполнитель: ссылка в бейдже. | высокая |
| CTX16 | MINOR | `dashboard.py:605-625` | Чип забега оставляет 5 полей из 20: нет `completed`, `stop_reason`, `report_file`, `stall`, `elapsed/downtime`. | «Сколько сделано и почему встали» — только в отчёте. | Исполнитель: поля из `run_brief`. | высокая |
| CTX17 | MINOR | `dashboard.py:968-983` | Таймлайн строит все `done/` как `approved`, `rejected` не производится; SHA ищется по 300 последним коммитам. | История однообразно-зелёная, tooltip пуст. | Исполнитель: статус из архива, SHA без потолка. | высокая |
| CTX18 | MINOR | `USAGE.md:215-227`, `BACKLOG.md:147-160` | Документация расходится: вкладка «📝 Задача» вместо «🎯 Итерация», «обновление 5 с» вместо 3 с; бэклог считает чипы моделей незакрытыми. | Неверные ожидания, бэклог врёт о состоянии. | Аналитик: что в scope R13; владелец бэклога. | высокая |
| CTX19 | SUGGESTION | `awf/metrics.py:504` | Токены и стоимость по юниту есть в `awf metrics`, на карточке юнита их нет. | Цена этапа не видна. | Владелец решает. | средняя |
| CTX20 | MINOR | `dashboard_server.py:327-372` | Boot-маркер пишется только при смене порта; `_boot_marker_fresh` при отсутствии возвращает `True`. | «Один таб на проект» ломается на давно живущем проекте. | Исполнитель: писать маркер и на реюзе. | средняя |
| CTX21 | MAJOR | `spec-dashboard.yaml:68`, `spec_package.py:330-385` | `dashboard.finalize.md` в корне пакета попадает в «лишний файл»: `discover_documents` не знал суффикса `.finalize.md`, `spec check` стал бы NOT READY. | Прогон упёрся бы в гейт из-за отчёта о сжатии. | Сделано: гейт игнорирует `*.finalize.md`. | высокая |

Несвязанные находки (в scope не тянуть): `docs/audit-2026-11-03-a44-worker-tree.context.md` CTX3-3 — `worker_pid`/`worker_pgid`/`worker_anchor` не чистятся и могут указывать на чужие процессы; это про `state` и `awf_status`, не про дашборд.

## 9. Вопросы и непроверенное

1. Владелец ответа «что происходит» — `get_status` или `generate_state_dict`?
2. Показывать полный пакет (13 секций) или вердикт, красные секции и первую
   причину?
3. Что считать работой юнита в диффе: всё от бейзлайна или пересечение с
   `files`/`exclude` контракта?
4. Должна ли вкладка переживать выход движка?
5. Нужны ли токены и стоимость на карточке юнита?
6. Возраст каждого блока или один общий «снимок от HH:MM:SS»?
7. Не проверено: поведение при пустом `pipeline.yaml`, 0 стадий и без
   `.agentic` — есть только `test_missing_state_still_renders`.
