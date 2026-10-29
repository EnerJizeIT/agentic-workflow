# Тестовая спецификация: экономия контекста стадии и продолжение сессии

Статус: REVIEW
Ревизия: ts-4
Входы: context-efficiency.md@req-7; context-efficiency.architecture.md@arch-3; context-efficiency.requirements-review.md@rev-7; context-efficiency.readiness.md@readiness-2; context-efficiency.context.md@ctx-2
База кода: `401680bbfd568ea03793a6fcbd107180fcc49da4`
Изменения: ts-3 — явные AC, AC37 через D13; ts-4 — req-7/arch-3: AC37 и тай-брейк (RR54), секция 9 (RR51), журнал (RR50), QA7/QA8 закрыты.

## Scope и статус

Проверки спроектированы, не пройдены; scope — R1–R9, AC1–AC43, INV1–INV7 на `401680b`. FR1 — T14 по D13, FR2 — T22 по K8/D14, FR3 — команды ниже, FR4 закрыт arch-3 (2117/10912). QA3, QA4, QA9 открыты; QA8 закрыт req-7.

## Карта покрытия

| Требование | Критерии | Контракт | T# | IMP |
|---|---|---|---|---|
| R1 | AC1–AC4 | C1/K1 | T1–T2 | IMP1 |
| R2 | AC5–AC9, AC41 | C1, C2/K2 | T3–T6 | IMP2 |
| R3 | AC10–AC13, AC36 | C2/K4 | T3, T9 | IMP2/3 |
| R4 | AC14–AC17 | C1, C2/K5 | T10–T12 | IMP4 |
| R5 | AC18–AC20 | C1/K3 | T7 | IMP2 |
| R6 | AC21–AC23, AC37–AC40 | C3, C4/K6, K10, D13 | T13–T17 | IMP5 |
| R7 | AC24–AC28, AC42 | C1, C3, C4/K7, D12 | T18–T20, T26 | IMP5 |
| R8 | AC29–AC32 | C3, C4/K8, D14 | T21–T23 | IMP6 |
| R9 | AC33–AC35, AC43 | C2/K9 | T8 | IMP2 |
| INV1 | — | C1/K5 | T12 | IMP4 |
| INV2–INV3 | — | C1–C4/K5 | T24 | IMP4/5 |
| INV4–INV5, INV7 | — | C1–C4/K5, K8 | T25 | IMP4 |
| INV6 | — | C3/K10 | T17 | IMP5 |

## Проверки

### T1 — Восемь последних юнитов и подсказка
- R1/AC1, AC2, AC4; C1/K1; IMP1. `done/` = TODO-0001…0100, текущий TODO-0101, набор 3. `Prior completed TODOs:` — 0100…0093, не 0092 и не 0101; при 3 — все; подсказка `awf report` и `.agentic/done/`; блоки Pipeline context, Stage assignment, Stage identity. Ловит: весь архив, текущий юнит, нет подсказки.

### T2 — Фрагмент не растёт с архивом
- R1/AC3; C1/K1; IMP1. 1000 каталогов и те же 8 последних. Фрагмент от `Prior completed TODOs:` до подсказки ≤300 символов и побайтово равен фрагменту архива из 8. Ловит: вывод 1000 id.

### T3 — Файл пакета: путь, секции, детерминизм
- R2/AC5, AC6; R3/AC36; C1, C2/K2; IMP2. TODO с пятью блоками, baseline, diff, untracked, handoff 30 строк, `GATES`, stage-log 40, REVIEW нет. Путь `.agentic/context/STAGE-TODO-0001-agent-dev.md`; `## 1`…`## 8` по порядку, девятой нет; UTF-8, LF, один финал; две сборки равны; 1 — todo, стадия, роль, задание; 3 — baseline и diffstat; 7 — «проверяй утверждения пробами»; 8 — `sha256:` для TODO, baseline, роли, доктрины, handoff. Ловит: время в файле, девятая секция, нет строки о пробах.

### T4 — Секция 5 несёт все тела REVIEW-фикс
- R2/AC5, AC41; C2/K2/D9; IMP2. Контракт 169 строк, шесть `## REVIEW-фикс`. Секция 5 — шесть заголовков и все тела дословно по порядку, обрыв до `## `; `review_headings` — только заголовки; файл >150 строк при уложившихся 1/3/4/6/7. Ловит: одна секция, заголовки без тел, порезанное тело.

### T5 — Потолки секций и сумма 150
- R2/AC7, AC41; C2/K2/D9/D10; IMP2. diff 500 файлов, handoff 200, дельта 50, задача 30, ссылки 30; границы 9/10/11 и 19/20/21. Потолки 1→10, 3→20, 4→20, 6→10, 7→12, 9→15 (K2; R2 молчит о 9 — QA9); переполнение — `- … сокращено: <ссылка>`; сумма 1/3/4/6/7/9 ≤150; 2/5/8 побайтово; AC41 — счёт 1/3/4/6/7. Ловит: 150 ко всем, обрезка без ссылки, порезанный REVIEW, секция 9 вне счёта.

### T6 — Нет входа — «n/a»; источники и секреты
- R2/AC8, AC9; C1/K2; IMP2; RR45. Проект без handoff, `GATES`, stage-log; TODO без блоков; окружение `AWS_SECRET_ACCESS_KEY`, `OPENCODE_TOKEN`. Секции 2/4/5/6 — `n/a`, rc 0; секретов нет; источники — контракт, git, outbox DONE, handoff, `GATES`, stage-log, `.agentic/context/`. Ловит: падение на пустом, утечка окружения, отказ stage-log.

### T7 — CLI context-pack: файл, stdout, отказ
- R5/AC18, AC19, AC20; C1/K3; IMP2. `context-pack TODO-0001 --stage agent-dev [--stdout]`; готовый файл, `TODO-9999`, стадия `nope`. Без `--stdout` rc 0, путь R2, повтор те же байты; `--stdout` побайтово, файл не тронут; неизвестное — rc 2, значение в сообщении, файла нет, состояние не тронуто. Ловит: `--stdout` пишет, частичный файл, неверный код.

### T8 — Дельта «С прошлого хода»
- R9/AC33, AC34, AC35, AC43; C2/K9/D9; IMP2. reuse; A — изменённый файл и `## REVIEW-фикс (…виток 3)`; B — пустой дифф; C — свежая. A — секция 9: `- виток: 3`, файл, `- новая секция: ## REVIEW-фикс (…); тело — в секции 5`, `- прошлая сессия не является источником фактов`; тело только в 5; B — `- нет изменений`; C — 9 нет. Ловит: дубль тела, неверный виток, нет предупреждения.

### T9 — Свежесть пакета в verify-pack
- R3/AC10, AC11, AC12, AC13; C2/K4/D2/D8; IMP3. `verify_pack()` и `verify-pack`: свежий, TODO изменён, пакет удалён, `off`. Свежий — pass `актуален`; изменён — fail `устарел: изменились входы: <список>` с TODO, команда `regenerate with awf context-pack`, вердикт failed; удалён — fail `устарел: пакет не найден`; `off` — pass `выключен (engine.context_pack: off)`, вердикт прежний; `STAGE-`/`context-pack` только в `stage_context`. Ловит: свежесть по дереву, нет файла = норма, пакет в вердикте.

### T10 — Пакет в argv: место и порядок
- R4/AC14, AC15; C1, C2/K5; IMP4. Спавн с `_capture_run`, шим, роль, доктрина, TODO, два handoff; пакета нет. Пакет до capture; порядок `--agent`, `--title`, `--file роль`, `--file доктрина`, `--file TODO`, `--file пакет`, `--model`, handoffs, `-- prompt`; handoffs не изменены. Ловит: пакет после handoffs или в промпте.

### T11 — Сбой сборки не валит стадию
- R4/AC16; C1/K5/D8; IMP4. Сборщик → исключение; контрольный прогон. Старт без пакета, причина в stage-log, код равен контролю, сигнал не изменён. Ловит: исключение убивает стадию, тихий пропуск.

### T12 — Выключенные ключи не меняют argv
- R4/AC17; INV1; C1/K5; IMP4. `off/off`, затем `on/off`. `off/off` — argv побайтово прежний (пакета и сессии нет); `on/off` — только `--file <пакет>` после TODO; промпт не сравнивается. Ловит: флаг протекает в `off`, лишний файл.

### T13 — Заголовок, файл пары и счётчик попыток
- R6/AC21, AC39; C3/K6/D13; IMP5. Пара TODO-0001/`agent-developer`, роль `agent-implementer`, `fix-only`; откат и `continue`; `off`; база с `time_created=1793237369671`, пустой `time_updated`. Заголовок `awf-<role>-agent-developer-TODO-0001-a1`; после отката/continue — `-a2`, `attempt: 2`, `session_id` из файла пары; файл — ровно `session_id`, `title`, `agent`, `model`, `attempt`, `ts`, где `ts` — ISO-8601 UTC из `time_updated`, иначе `time_created`; `off` — `awf-<role>-TODO-0001`, база не читается, файла нет. Ловит: нет стадии, стадия = роль, `attempt` из файла, `ts` записи, `off` читает базу.

### T14 — Захват id по title и окну time_created (v1 и v2)
- R6/AC37; C3/K6/D13; IMP5. Фейковая sqlite `session` и `session_v2`, колонки `(id,title,directory,time_created,time_updated)`, ms; старт `T`. Набор 1: A — title пары, `$HOME`, `T+500`; B — тот же title, `$HOME`, `T-60000`; C — другой title, `T+500`; D — title второй стадии, пусто, `T+500`. Набор 2: A2 — `$HOME`, `T+500`; E — каталог проекта, `T+500` (две записи одного title в окне). Набор 3: только A. Обе схемы: набор 1 → A; B вне окна `[T-2000, now_ms+120000]`; C не берётся; у стадий свои заголовки/id (A, D); набор 2 → E по `directory` (тай-брейк, RR54); набор 3 → A (`$HOME` не фильтр); пусто — id не пишется, файла нет. AC37 = D13: `title`+окно, `directory` — тай-брейк. Ловит: захват по `directory`, прошлый прогон, перекрёстный захват, секунды, пустой `directory` как фильтр.

### T15 — База недоступна, незнакома и медленна
- R6/AC22, AC23; R7/D6; C3, C4/K6; IMP5; RR44. Нет базы; чужая схема; открытие >1 с; продолжение при мёртвой базе. Файла нет, DONE пишется, причина в stage-log, код тот же; чтение ≤1 с, id не пишется; на продолжении fresh, файл пары сохранён. Ловит: запись непроверенного id.

### T16 — Убийство до захвата двигает счётчик
- R6/AC40; C3/K6; IMP5. Первый спавн убит до захвата (rc 1, строки нет). Следующий вход пары — `-a2`, `attempt: 2`; файла первой попытки нет. Ловит: откат счётчика.

### T17 — Метрики читают оба заголовка
- R6/AC38; INV6; C3/K10; IMP5. `awf-implementer-agent-developer-TODO-0001-a1`, старый `awf-implementer-TODO-0002`, роли с общим префиксом. `parse_worker_title` → `(role, stage, todo, attempt)`, у старого stage пуст; `workers_by_role` — одна строка `implementer`; префиксный поиск находит оба; неоднозначность — длинное имя. Ловит: `$`-якорь, стадия считается ролью.

### T18 — fix-only и off: выбор сессии
- R7/AC24, AC25; C1, C3, C4/K7/D5/D12; IMP5. `off`; вход 1; вход 2 после отката/continue; TODO с секцией и без. `off` — argv и выбор прежние, база не читается; вход 1 — fresh; вход 2 — reused с `--session <id>` независимо от секции; несовместимость (шаг 0 IMP5) — fresh с причиной. Ловит: привязка к секции, сессия на входе 1.

### T19 — always: пара, роли, потерянный счётчик
- R7/AC26, AC42; C3, C4/K7/D7; IMP5. `always`; роли qa-review, project-auditor, supervisor; файл другой стадии/юнита; живой файл без `attempt`. Вне списка — fresh `role not allowed`; чужая стадия/юнит не берутся; `always` без `attempt` — `--session`; `fix-only` тот же вход — fresh `attempt 1`. Ловит: переход между парами, требование `attempt`.

### T20 — Несовпадение даёт fresh, продолжение сохраняет argv
- R7/AC27, AC28; C2, C3/K7; IMP5. Другая роль/модель, `ts` 241 мин, нет/будущий `ts`, битый файл, id не найден; контроль reused. Fresh с `role/model mismatch`, `session too old`, `no session file`, `session not found`; reused сохраняет `--agent`, `--model`, все `--file`, добавляет `--session`. Ловит: reuse при несовпадении, потерянные вложения.

### T21 — Две неудачи отключают продолжение
- R8/AC29; C2, C4/K8; IMP6. Две попытки с `--session` без сигнала/с отказом id; третья; успешная fresh; четвёртая; `continue`. Первые две — reused; третья и далее — fresh `reuse disabled after 2 failures`; успех fresh запрет не снимает; continue сохраняет. Ловит: fresh включает продолжение, счётчик по юниту.

### T22 — Строка stage-log и reuse k/n по журналу
- R8/AC30, AC31; C3/K8/D11/D14; IMP6. Журнал `SESSIONS-TODO-0001.jsonl` и `awf-<role>-TODO-0001.out`; reused и fresh стадии `execute`, reused стадии `close` (та же роль, тот же лог), `off`, юнит без журнала. Журнал — строка на попытку с `stage`, `attempt`, `decision`, `reason`, `ts`, при `off` есть; stage-log — ровно одна строка `session: reused <id>` или `session: fresh (<причина>)`; `n` — строки пары, `k` — reused; `execute` `1/2`, `close` `1/1`; стадия из поля, не из `.out`; `awf status` — `Stage <stage>: reuse k/n`; RUN-REPORT — `**Session reuse:** TODO-NNNN <stage> k/n`; `off` — `0/n`; журнал переживает очистку/откат/continue; нет журнала — поля нет. Носитель — D14 (R8 молчит, QA3). Ловит: счёт по юниту или имени лога, потерю `off`, две строки лога, потерю после continue.

### T23 — Счётчик неудач принадлежит юниту
- R8/AC32; C4/K8; IMP6. Юнит A с одной неудачей и `continue`; старт B. У A 1; B 0; ключи `session_fail_key`=todo_id, `session_fail_count`. Ловит: сброс при continue.

### T24 — Завершение только сигналом, чистка и симлинки
- INV2, INV3; C1–C4/K5; IMP4/5. Молчащий воркер; старый DONE; симлинк в outbox. Молчание — провал; старые сигналы удалены до спавна; симлинк отвергнут, вне корня не удалено. Ловит: текст как сигнал.

### T25 — Статические канарейки инвариантов
- INV4, INV5, INV7; C1–C4/K5, K8; IMP4. `awf/_proc.py`, `tests/conftest.py`, роли qa-review/project-auditor, handoff. `grep` находит `pid > 1` и `_forbid_session_kill`; readonly без `edit`/`write`; пакет не даёт инструментов; handoff, его место и изоляция коммита прежние; `test_process_gates.py` зелёный. Ловит: ослабленный guard.

### T26 — Живой прогон `--session` с остальными флагами
- R7/AC25, AC28; Q3/D12; IMP5. Живой `opencode run` 1.19.4 в scratch `/tmp/opencode/…`: роль, дешёвая модель, TODO, `--file`; первая попытка создаёт сессию. Вторая — те же флаги + `--session <id>`, вопрос «назови вложенный файл». Тот же id, вложение видно, rc 0, сигнал; отказ `--session` — fresh с причиной, запись в DONE. Ловит: синтаксис. Фиксация: вывод и id в DONE.json.

## Среда и выполнение

Фикстуры: `tmp_path`, `tmp_git_repo`, `project` (`test_pipeline_context.py`), `_capture_run` (`test_agent_stage_prompt.py`), шим `tests/infra/opencode_shim/opencode`. Фейковая sqlite для T14/T15/T17 — `tmp_path`: схемы `session` и `session_v2`, `time_created` в ms (база 1 793 237 369 671), `spawn_started_ms` = `T`. Журнал T22 — `SESSIONS-<todo>.jsonl` в `tmp_path`, `ts` подменён. Сеть — только T26.

Планируемые команды (venv `/home/pklochkov/.local/share/awf-venv/bin/python`; не выполнялись):
- IMP1 `tests/unit/test_agent_stage_prompt.py` (T1–T2);
- IMP2 `tests/unit/test_context_pack.py` (T3–T8);
- IMP3 `tests/unit/test_verify_pack_stage_context.py` (T9);
- IMP4 `tests/negative/test_stage_context_pack.py` (T10–T12), T24 в `test_agent_stage_exit_code.py` и `test_signal_watch.py`, T25 в `test_process_gates.py`;
- IMP5 `tests/negative/test_session_reuse.py` (T13–T20, T26 вручную), T17 в `tests/unit/test_metrics.py`;
- IMP6 `tests/unit/test_session_reuse_status.py` (T21–T23).

Существующие файлы базы: `test_pipeline_context.py`, `test_agent_stage_prompt.py`, `test_verify_pack.py`, `test_metrics.py`, `test_metrics_contract.py`, `test_process_gates.py`, `test_signal_watch.py`, `test_agent_stage_exit_code.py`. Зелёные: 21 тест prompt/handoff, 85 brief/stage identity; полный сьют не запускался.

## Условия приёмки реализации

Обязательны T1–T25 в автотестах и T26 как ручной замер IMP5. Ожидания буквальны: пути, заголовки, шесть ключей, коды 0/2, отсутствие лишних файлов, `k/n` по стадиям. T10–T12 — подменённый раннер; T13–T17, T19–T23 — фейковая sqlite и фиксированные часы. Регрессия: перечисленные файлы зелёные.

## Findings

| ID | Важность | Источник | Влияние | Владелец | Состояние |
|---|---|---|---|---|---|
| QA1 | MINOR | RR44; R6/AC22, R7/AC27 | «Файл есть, база мертва» — K7/D6; T15. | аналитик | ЗАКРЫТО req-7 |
| QA2 | MINOR | RR45; R2/AC8 | stage-log в источниках AC8; T6. | аналитик | ЗАКРЫТО req-7 |
| QA3 | MINOR | RR50; R8/AC31; A5; K8/D14 | AC31 требует журнал, R8 его не заводит; носитель — D14; T22. | аналитик | OPEN |
| QA4 | SUGGESTION | Q3; R7/AC25/D12 | Семантика `--session` не проверена; T26. | архитектор | ACCEPTED (D12) |
| QA7 | MAJOR | K6 | Бюджет 2815 > 2200, пакет > 11 000. | архитектор | ЗАКРЫТО arch-3 |
| QA8 | MAJOR | R6/AC37; FR1; K6/D13 | AC37 расходился с D13. | аналитик | ЗАКРЫТО req-7 |
| QA9 | MINOR | RR51; R2/AC7, AC41 | AC7 считает 9, AC41 — нет; R2 молчит, K2 — 15. | аналитик | OPEN |
| QA10 | SUGGESTION | RR54; R6/AC37 | Тай-брейк `directory`; T14 набор 2. | аналитик | ЗАКРЫТО ts-4 |
