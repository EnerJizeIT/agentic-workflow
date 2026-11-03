# Аудит A44 (TODO-0359): дерево воркера стадии

**Scope:** широкий срез проекта вокруг изменения «после стадии не остаются
жить внуки воркера»: переносимость, межмодульные несогласованности, шероховатости.
**Ревизия:** ctx-1, 2026-11-03. **База кода:** `cca9221` + рабочее дерево
(tree-sha `1193ce61…`). **Статус:** REVIEW — контекст собран, ограничения ниже.

## Краткий итог

Механика уборки сделана цельно: маркер стадии ставится в env одного спавна
(`_env.awf_subprocess_env(stage_anchor=…)`), пишется в state
(`worker_anchor`/`worker_pgid`), деревья собираются по группе, родителю и
маркеру (`worker_tree.live_members`), убиваются раундами TERM→KILL
(`worker_reap`), отчёт и счёт в `awf status`. Все известные точки
отсоединения маркер снимают. Главные дыры — не в самой уборке, а на
соседних путях: ручной `awf kill` хвосты не добирает, а сквозного теста
«после стадии» нет — проверен только сам уборщик.

## Карта затронутых путей

| Вход | Компоненты | Данные/эффект |
|---|---|---|
| спавн стадии | `agent_stage.run_agent_stage:316-390`, `supervisor.run_supervisor_via_subprocess:1624-1690` | токен → `awf_subprocess_env` → env воркера; pid/pgid/токен → state |
| выход стадии | `finally` в обоих путях → `worker_tree.reap_stage_tree` → `worker_reap.reap_worker_tree` | TERM/KILL группе, потомкам, носителям маркера; строка в лог |
| старт стадии | `worker_tree.log_stage_leftovers` | только чтение state + /proc, строка о хвостах прошлой стадии |
| `awf status` / MCP | `api/lifecycle.get_status:1387-1400` → `live_members_of_state`; `cmd_status:61-73` | `worker_processes {count,pids,role,todo_id}` — read-only |
| `awf start --background`, фоновый сьют | `api/_background:208-216` (`without_anchor`), оба `suite-canonical.sh` (`env -u`) | маркер снят — движок переживает стадию |
| `awf kill` | `api/pipeline.kill_pipeline:3065-3190` | **маркер не читается** — см. CTX1 |

Переиспользовано: `_proc.snapshot_rows/stat_row/is_live_state/signalable_group`
(общие факты о процессах), `_proc.signal_pid` (единственный одиночный сигнал),
`_log.log`, `pipeline_state.write_state` (merge-запись).

## Baseline

| Проверка | Где | Итог |
|---|---|---|
| `sh scripts/check-contracts.sh` | корень | PASS, rc 0 (8 контрактов, 18 токенов, 8 гейтов) |
| `pytest tests/unit/test_process_safety_gate.py tests/unit/test_stage_anchor_isolation.py tests/unit/test_worker_tree_reap.py -q` | корень | PASS, 25 passed |
| `sh scripts/ratchet.sh` | корень | PASS, within baseline |
| полный сьют | `.agentic/context/SUITE-1193ce61…-4ea28cc4.json` | PASS, 6935 passed, exit 0 (артефакт QA на том же tree-sha; не перезапускал) |
| проба гейта на scratch-файлах | `/tmp/opencode/audit359/gate` | см. CTX4: оба направления воспроизведены |
| замер `live_members` | этот хост, 535 процессов | 31 мс на вызов |
| живой осмотр дерева стадии | `/proc/867890, 867899, 868194-868261` | маркера нет у всего дерева — движок загрузил `agent_stage` до правки (self-hosted прогон), не дефект кода; приёмку «после юнита» в этом прогоне наблюдать нельзя |

## Находки в scope

| ID | Важность | Файл / символ | Факт и доказательство | Влияние | Следующий шаг | Уверенность |
|---|---|---|---|---|---|---|
| CTX1 | MAJOR | `awf/api/pipeline.py` `kill_pipeline`, `_collect_worker_pids:2896-2939`; `_proc.kill_pid_tree:329` | Ручной kill собирает только `worker_pid` и прямых детей, зовёт `kill_pid_tree`; `worker_anchor`/`worker_pgid` из state не читает. `kill_pid_tree` сигналит группу лишь когда pid — её лидер, а `opencode serve` и MCP живут в своих сессиях (`setsid`). После kill `_clear_state_keep_salvage` стирает state — хвосты исчезают и из `status`, и из `log_stage_leftovers`. | Цель A44 «внуки не остаются» нарушается на штатном пути остановки: MCP-серверы и serve переживают `awf kill` и больше нигде не видны. | Читать токен из state до очистки и звать `worker_tree.reap_worker_tree(token=…)`; тест: kill при живом setsid-внуке. | высокая |
| CTX2 | MAJOR | `tests/unit/test_worker_tree_reap.py:84`, `test_stage_anchor_isolation.py`; `awf/agent_stage.py:316-390` | Приёмка «после стадии оба мертвы» проверена вызовом `reap_worker_tree` напрямую. Нет теста, который гоняет `run_agent_stage`/`run_supervisor_via_subprocess` и проверяет три звена: env несёт `AWF_STAGE_ANCHOR`, state пишет `worker_anchor`, `finally` зовёт уборку. `grep ANCHOR_ENV tests/` — только модульные тесты. | Убери `stage_anchor=` или `finally` — 6935 тестов останутся зелёными; регресс проводки не поймается. | Тест с подменённым `run_subprocess_until_signal`: проверить env, state и вызов reap на обоих исходах. | высокая |
| CTX3 | MINOR | `worker_tree._scan:109`, `_proc.snapshot_rows:148`; README:50 | Без `/proc` (macOS) `snapshot_rows()` → None, `live_members` → [], уборка молча ничего не делает; `_scan` обещает «Linux or an equivalent probe», пробы нет. README: macOS «should work». | На macOS A44 не работает и не сообщает об этом; статус всегда 0. | Документировать «уборка только Linux» или добавить probe через `ps -axo pid,ppid,pgid,state`. | высокая |
| CTX4 | MINOR | `scripts/check_process_safety.py:39-64`; `tests/unit/test_process_safety_gate.py` | Пробы: `from os import kill as stop; stop(-1, 9)` → rc 0 (пропуск, хотя алиас импорта статичен); локальная `def kill(x)` + `kill(0)` → rc 1 (ложное срабатывание). Docstring строки 10 говорит «bare or as a method», код методы намеренно исключает. В дереве таких мест сейчас нет. | Гейт охраны сессии обходится сменой имени импорта; ложные срабатывания провоцируют глушение. | Разрешать `ImportFrom`-алиасы из `os`; голое имя считать сигналом только при таком импорте; выправить docstring. | высокая |
| CTX5 | MINOR | `docs/contracts/process-safety.md:2`; `awf/api/pipeline.py:3135,3157`; `_proc.py:148,177`; `api/dashboard.py:452-467`; `_proc.child_pids:305` | Контракт объявил все сигналы принадлежащими `_proc` («только здесь»), но `kill_pipeline` шлёт `os.kill` напрямую (практически прикрыт `pid_is_ours`, формально — нет). `snapshot_rows` назван «единственным обходом /proc», `stat_row` — «единственным парсером», а `child_pids` сам листает /proc, `dashboard` сам парсит stat, `_liveness` сам читает cmdline. | Ровно тот класс расхождения, который юнит чинил в CTX2: владелец заявлен один, копий несколько. Усиление guard'а снова придётся делать в нескольких местах. | Либо провести эти чтения через `_proc`, либо смягчить формулировки владения. | высокая |
| CTX6 | SUGGESTION | `worker_tree._scan:109-124` | На каждый `awf status` (и MCP status) читается cmdline **всех** процессов, хотя нужен он только для лога членов дерева; при 535 процессах — 31 мс на вызов, плюс чтение `environ` при наличии токена. | Заметная цена на частом read-only пути; лишнее чтение чужих cmdline. | Собирать cmdline лениво — только для найденных членов. | средняя |
| CTX7 | SUGGESTION | `docs/contracts/process-safety.md:5`; `AGENTS.md:25-26`; `awf/_proc.py:77` | Гейт проверяет инвариант `grep -q "pid > 1" awf/_proc.py`, а эта строка есть только в комментарии (line 77). Удаление реальной проверки в `signalable_group`/`signal_pid` гейт не покраснит; прямого теста `signal_pid(1, sig)` нет. | Ворота создают видимость проверки самого чувствительного инварианта. | Проверять поведение (`signal_pid(1,…) is False`) тестом гейта, а не текст комментария. | высокая |

## Вне scope (замечено попутно)

| ID | Важность | Факт | Следующий шаг |
|---|---|---|---|
| CTX8 | SUGGESTION | `.ratchet-counters` (блок TODO-0359) пишет «`_proc.py` 394 -> 437»; база в HEAD — 390 строк (`git show HEAD:awf/_proc.py | wc -l`), то есть и рост, и старт указаны неточно. | Поправить цифру в долговой заметке. |
| CTX9 | SUGGESTION | `api/dashboard.py:470` берёт `os.sysconf(os.sysconf_names["SC_CLK_TCK"])` — на не-Linux это исключение, тогда как `_proc_cpu` уже имеет безопасный fallback. | Переиспользовать `_proc_cpu`-путь или обернуть. |

## Что проверено и работает

- Все точки отсоединения известны и маркер снимают: `_background` (python) и
  `scripts/suite-canonical.sh` + шаблон (обе копии совпадают).
- Чужое дерево не трогается: тест с параллельной меткой и процессом в другой
  группе; `pid <= 1`, своя группа — отказ.
- `awf status` после уборки даёт 0; строка печатается и при 0.
- Полный сьют на текущем tree-sha зелёный (артефакт QA), контракты и ratchet зелёные.

## Вопросы / непроверенное

1. Живая приёмка «после юнита нет висящих `opencode serve`/`agent_workflow_ui`»
   в этом прогоне не наблюдаема: движок держит в памяти дорабочий
   `agent_stage`. Проверить на следующем запуске пайплайна (или после
   рестарта движка).
2. Нужен ли `awf kill` в scope A44 — решает владелец; сейчас это единственный
   штатный путь, где хвосты остаются и становятся невидимыми.

---

# Ревизия ctx-3 (аудит TODO-0359 после цикла фиксов)

**Scope:** тот же широкий срез вокруг A44 — переносимость, межмодульные
несогласованности, шероховатости; код стадии не меняется.
**Ревизия:** ctx-3, 2026-11-03. **База кода:** `cca9221` + рабочее дерево,
tree-sha `cde042f8…`. **Статус:** REVIEW — контекст собран, ограничения ниже.
**Прошлые ревизии:** ctx-1 (этот файл, аудит TODO-0359), ctx-2
(`docs/audit-2026-11-03-a44-fix.context.md`, аудит TODO-0362).

## Краткий итог

Ядро A44 после двух витков фиксов цельно: маркер ставится в env одного
спавна, пишется в state, деревья собираются по группе/родителю/маркеру,
убиваются раундами TERM→KILL, статус печатает число живых. Новых
поведенческих дефектов в самой уборке не нашёл.

Свежие находки — на границах: отчёты стадий этого прогона лежат вне
границ юнитов и попадут в коммит; гейт безопасности молча зеленеет на
отсутствующем пути; строка статуса про дерево воркера живёт на устаревшем
state и обходится в полный обход `/proc` при каждом вызове.

## Что стало с находками ctx-2 (AUD1–AUD10)

| ID | Статус | Чем проверено |
|---|---|---|
| AUD1 `awf kill` не читает якорь | **открыт**, без изменений | `awf/api/pipeline.py` по-прежнему трогает только `worker_pid`; `worker_anchor`/`worker_pgid` в kill-пути не упоминаются |
| AUD2 macOS: уборка молча no-op | **открыт** | `worker_tree._scan` → `snapshot_rows()` → None без `/proc`; `live_members` → `[]` |
| AUD3 бюджет доктрины 250/250 | **открыт** | `sh scripts/instruction-gates.sh`: доктрина 250 из 250 |
| AUD4 «guard `proc.pid > 1` больше нет» | **неверно, снимаю** | `awf/_proc.py:85-87`: `candidate == proc.pid and proc.pid > 1 and signalable_group(candidate)` — guard на месте; доктрина, `AGENTS.md:22`, `docs/design.md:180` ссылаются на существующую строку |
| AUD5 владелец `/proc`-фактов заявлен один, копий несколько | **открыт** | `_proc.snapshot_rows` сам оговаривает, что dashboard и liveness читают `/proc` сами |
| AUD6 гейт проверяет текст, поведенческого теста guard'а нет | **открыт** | в тестах нет вызова `signal_pid(1, …)`/`signalable_group(1)` |
| AUD7 цифры долговой заметки | **исправлено** | `.ratchet-counters`: «390 -> 446», факт `git show HEAD:awf/_proc.py \| wc -l` = 390 |
| AUD8 цена status | **открыт, уточнён** | см. CTX3-3: замер 26 мс на обход, 38 мс с чтением `environ` |
| AUD9 `run_subprocess_until_signal(env=None)` без метки | **открыт** | код не менялся |
| AUD10 стале-факты DONE.json | **открыт** | `stage_id` в `DONE-TODO-0359.json` по-прежнему `agent-implementer` |

## Находки

| ID | Важность | Файл / символ | Факт и доказательство | Влияние | Следующий шаг / владелец | Уверенность |
|---|---|---|---|---|---|---|
| CTX3-1 | MINOR | `docs/audit-2026-11-03-a44-worker-tree.context.md`, `docs/audit-2026-11-03-a44-fix.context.md` | Отчёты аудита этого прогона лежат в корне `docs/`, а не в каталоге `docs/audit-<дата>-<slug>/NN-*.md`, как прошлый аудит (`docs/audit-2026-09-27-gpt6-sol-xhigh/`). В `BASELINE-TODO-0359.untracked` записан только `tests/unit/test_runner_drift_sha.py`, значит оба файла созданы после бейзлайна и входят в изменённый набор юнита. Их нет и в `files:` ни одного юнита. | Коммит A44 утащит два отчёта как «код юнита»; в истории появится документация, которую никто не планировал коммитить. | verify/супервизор: вынести отчёты в каталог по конвенции до коммита (или исключить из коммита осознанно). | высокая |
| CTX3-2 | MINOR | `scripts/check_process_safety.py::scan_paths` | `if not root.exists(): continue` — отсутствующий путь молча пропускается. Проба: `python3 scripts/check_process_safety.py awf no_such_dir` → rc 0, вывод пуст. Плюс docstring обещает «bare or as a method», а `_is_signal_call` методы намеренно исключает. | Переименование каталога или опечатка в `Gate:` оставляет гейт охраны сессии зелёным, не проверив ничего; расхождение docstring/кода провоцирует «починку» не той половины. | Владелец гейта: печатать пропущенный путь и возвращать 2 (как `main` без аргументов); поправить docstring. | высокая |
| CTX3-3 | MINOR | `awf/api/lifecycle.py:1388-1404`, `awf/worker_tree.py::state_anchor/has_anchor`, `.agentic/state/current.yaml` | `worker_pid`/`worker_pgid`/`worker_anchor` пишутся при спавне и **никем не чистятся** (grep по `awf/`: только запись в `record_worker_state`). Живой пример: `current.yaml` хранит `worker_pid: 1565999`, `worker_pgid`/`worker_anchor` там нет вовсе. `has_anchor()` проверяет только наличие записи, поэтому каждый `awf status`, MCP-статус и дашборд (опрос раз в 5 с) делает полный обход `/proc`: замер на 477 процессах — 26 мс обход, 38 мс с чтением `environ`; строка печатается для давно закрытого юнита. При переиспользовании pid запись «дерево воркера» покажет чужие процессы. | Ложный факт в наблюдении и лишняя цена на частом read-only пути; на решения об остановке не влияет — из state сигналов никто не шлёт. | Отдельный юнит: гасить якорь в state на выходе стадии/юнита (или спрашивать живость, как lease). | высокая |
| CTX3-4 | SUGGESTION | `awf/worker_reap.py::reap_stage_tree` (строки 170-176) и `awf/worker_tree.py::state_anchor` (240-248) | Правило «годный якорь: группа > 1, иначе pid» записано дважды; архитектор уже записал это как долг F4. Следствие конкретное: следующая правка правила (например, `pgid == 0`) требует двух мест, и расхождение снова даст дрейф статуса и уборки. | Низкое сейчас, среднее при следующей правке правила. | Бэклог: `usable_anchor(pid, pgid)` в `worker_tree`, зов из обоих мест. | высокая |
| CTX3-5 | SUGGESTION | `awf/worker_tree.py` (398 строк) | До порога храповика `files_over_400` — 2 строки. Один поясняющий комментарий поднимает счётчик 137 → 138 и требует записи долга. | Шум в храповике на следующем юните, который просто допишет docstring. | Следующий автор: либо уложиться в 400, либо сразу переносить отчётные чтения (`_read_cmdline`, `ProcInfo.label`) в отдельный модуль. | высокая |

## Вне scope (замечено попутно)

| ID | Важность | Факт | Следующий шаг |
|---|---|---|---|
| CTX3-6 | SUGGESTION | `awf/unit_contract.py:957-984`: строка «check vs DONE.json: mismatched» печатается для каждой не-авторской стадии, хотя `docs/unit-contract.md:591-597` велит ревьюеру/аудитору писать `PROBES`, а не переписывать `DONE.json`. В этом прогоне так у трёх стадий подряд. | Либо сверять с `PROBES` для не-авторских стадий, либо не печатать строку, когда `PROBES` на месте. |
| CTX3-7 | SUGGESTION | `awf/worker_reap.py::report_line`: при пустом `killed` и непустых `survivors` даёт «reaped: 0 process(es); N survived» — читается как «ничего не нашли», хотя речь о переживших KILL. | Переформулировать ветку с выжившими. |

## Baseline (это дерево, tree-sha `cde042f8…`)

| Проверка | Где | Итог |
|---|---|---|
| `git_utils.tree_fingerprint` | корень | `cde042f8…` — совпадает с sha артефакта QA `SUITE-cde042f8…-4ea28cc4.json` (6943 passed, exit 0); сьют не перезапускал (политика awf-suite-canon) |
| `sh scripts/check-contracts.sh` | корень | PASS, rc 0: 8 контрактов, 5 юнит-контрактов, 21 токен путей, 8 гейтов |
| `sh scripts/instruction-gates.sh` | корень | PASS, rc 0: инструкции 939/950, доктрина 250/250 |
| `sh scripts/ratchet.sh` | корень | PASS, rc 0: within baseline (137) |
| `pytest test_worker_tree_reap + test_worker_tree_qa + test_stage_anchor_isolation + test_process_safety_gate -q` | корень | PASS, 37 passed за 7.0 с |
| `ruff check .` | корень | PASS, All checks passed |
| проба гейта: `check_process_safety.py awf no_such_dir` | корень | rc 0 — CTX3-2 воспроизведён |
| замер `_scan` / `live_members(token=…)` | корень, 477 процессов | 26 мс / 38 мс — CTX3-3 |
| `python -m awf status` (изнутри дерева стадии) | корень | печатает `Worker tree: 0 live process(es) (agent-project-auditor/TODO-0359)` при живом дереве — это QA1, подтверждено живьём; вне дерева поведение не проверял |

Ограничения: полный сьют не гонял (артефакт QA на этом же tree-sha);
живую приёмку «после юнита нет висящих MCP» в self-hosted прогоне
наблюдать нельзя — движок исполняет дорабочую копию `agent_stage`;
macOS-машины в прогоне нет, AUD2 — вывод из кода.

## Вопросы / непроверенное

1. Входит ли `awf kill` (AUD1) в цель A44 — решение владельца; это
   единственный штатный путь, где хвосты остаются и исчезают из наблюдения.
2. Куда девать отчёты аудита (CTX3-1): каталог по конвенции или исключение
   из коммита — решает verify/супервизор.
3. Живой статус из оболочки вне дерева стадии не проверял: проба шла
   изнутри дерева, где `live_members` осознанно отказывает (QA1).
