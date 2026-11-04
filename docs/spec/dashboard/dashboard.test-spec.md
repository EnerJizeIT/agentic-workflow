# Тестовая спецификация: дашборд awf

Статус: REVIEW
Ревизия: ts-2
Входы: dashboard.md@req-6, dashboard.architecture.md@arch-2, dashboard.requirements-review.md@rrv-6, dashboard.readiness.md@rdy-1, TODO-0357
База кода: e6878c3

## 1. Scope

Спецификация до реализации: проверки **спроектированы**, не пройдены, код и
тесты не менялись. Покрыты R1–R13, R16, AC1–AC35, INV1–INV10
(`dashboard.md@req-5`), техническая база — K1–K7, D1–D12, IMP1–IMP8
(`dashboard.architecture.md@arch-1`); R14/R15 отложены.

Три вопроса: **не врёт** (T1, T5–T8, T12), **не молчит** (T1, T3, T10, T13,
T17), **не падает на пустом** (T17, T22). Уровни unit/integration,
кроме T9 (измерение) и T20 (статические документы); живые API, сеть и
прогоны не запускаются. Вне покрытия: длительность субпроцессов (A-4),
R14/R15.

## 2. Карта покрытия AC → T

R# покрыт теми же T#, что его AC#.

AC1–AC3 → T1, T2; AC4–AC5 → T4; AC6–AC7 → T3; AC8–AC9, AC12 → T5;
AC10 → T6; AC11, AC13 → T7, T8; AC14 → T9; AC15 → T10; AC16–AC17 → T11;
AC18–AC19 → T12; AC20 → T13; AC21 → T14; AC22–AC23 → T15; AC24–AC26 → T16;
AC27–AC28 → T17; AC29 → T18; AC30–AC31 → T19; AC32 → T20; AC33–AC34 → T21;
AC35 → T1, T6, T7; AC36 → T23.

INV1–INV3, INV5–INV8 → T22; INV4, INV10 → T9; INV9 → T18, T19.

## 3. Среда и запуск

Раннер — существующий `python -m pytest` (`pyproject.toml:62`). Фикстуры:
`tmp_git_repo` (`tests/conftest.py:243`), `write_state`, `_report` по образцу
`tests/unit/test_brief_verify_facts.py:45-70` (тот же писатель, что в
жизни), `monkeypatch` часов и `os.utime`
для mtime (`test_dashboard.py:254`); состояние — только `tmp_path`.

Команда на файл: `/home/pklochkov/.local/share/awf-venv/bin/python -m pytest
<файл> -q --timeout=300 -p no:cacheprovider`; перед вердиктом — канон
`scripts/suite-canonical.sh`, не `pytest tests/`.

| Файл | Проверки | Статус |
|---|---|---|
| F1 `test_dashboard_events.py` | T1, T2, T3, T13 | планируемый |
| F2 `test_dashboard_facts.py` | T5, T6–T8, T12, T17 | планируемый |
| F3 `test_dashboard_layout.py` | T4, T10, T11, T14, T15, T16, T20, T21 | планируемый |
| F4 `test_dashboard_open_rule.py` | T18, T19 | планируемый |
| F5 `test_dashboard_perf.py` | T9 | существующий, расширяется |
| F6 `test_dashboard_spec_package.py` | T23 | планируемый |
| R1 `test_dashboard*.py`, `test_brief_verify_facts.py`, `test_brief_card_verify.py`, `test_worker_tree_qa.py` | регрессия T22 | существующие |

## 4. Проверки

### T1 — События не врут и приходят при смене (AC1, AC2, AC35; F1)
- Данные: лог: `(exit=1)`, `(exit=0)`, `checklist-check: skipped`,
  `BD-30: supervisor signal timeout after 900s`, `A7: fix_round`,
  `Transition: … action=fix_round`, `Pipeline exited`; `GATES-TODO-0001.md`
  через `_report` (`failed`), `ENGINE-EXIT-…md` с `- todo: TODO-0001`.
- Ожидания: exit=1 → `fail` с кодом `1`; exit=0 → `done`; пропуск, таймаут,
  оба ретрая → события; `Pipeline exited` не событие; `gates` и
  `engine-exit` — только при смене (два опроса, затем `ok` даёт одно новое),
  вердикт не из лога.
- Ловит: зелёный «complete» при rc=1, дубль и выпавшую проблему.
- Изоляция: `tmp_path`, сброс памяти фактов (K8).

### T2 — Граница событий (AC3; F1)
- Данные: лог с двумя `Pipeline started with N stages:`; `run.yaml`: (а) `active=false`, `queue` 4, старый `started_at`, (б) `active=true`, (в) ни забега, ни строки.
- Ожидания: (а) граница по последней строке запуска, события прошлого прогона скрыты; (б) граница `started_at`; (в) `events=[]` и «граница прогона неизвестна»; при границе шапка называет прогон.
- Ловит: мёртвую ленту после забега и события чужого прогона.

### T3 — Причина Blocked и выхода (AC6, AC7; F1)
- Данные: `outbox/BLOCKED-TODO-0001.md` (два абзаца); три
  `ENGINE-EXIT-<ts>-<pid>.md`: новее с `- todo: TODO-0009`, старше с
  `TODO-0001`; вариант без файла.
- Ожидания: `blocked={todo, reason}` — первый абзац, `note_path`;
  `engine_exit={found:true, path, rc, signal, stage, todo, pid,
  traceback_tail, ts}` из самого нового файла TODO-0001; чужой TODO
  игнорируется; без файла `found=false`, `path` указывает на
  `context/ENGINE-EXIT-*.md`, текст «файла нет».
- Ловит: «Blocked» без причины, `pid N dead` без трейсбека и пути.

### T4 — Сводка и полный текст (AC4, AC5; F3)
- Данные: копия TODO-0357 (front matter, `<!-- role_hint -->`, заголовок,
  цитата, `---`); вариант без цели.
- Ожидания: сводка с цитаты «По дашборду лучше через spec…»; YAML и
  комментарии не видны, заголовки до абзаца пропущены; `> ` и `- [ ]` —
  содержание; полный текст сохраняет строки, `---` → `<hr>`; без тела —
  «нет данных».
- Ловит: YAML вместо цели (CTX1).

### T5 — Баннер verify, ожидание, чекпойнт (AC8, AC9, AC12; F2)
- Данные: `_report` для `ok`, `failed`, `nothing-measured`, `error`,
  `unreadable`, нет файла; `verify_waiting` и часы +3 с;
  `checkpoint_form_url`.
- Ожидания: `state.verify.status` равен владельцу; `failed_sections` и
  `first_failure` видны, рендер `- contracts: FAIL (rc=…)`; `unreadable` →
  «данные повреждены», нет файла → «файла нет», `hidden` → причина AC11;
  прошедшее за +3 с выросло на 3, ссылка чекпойнта экранирована.
- Ловит: пустой/зелёный баннер, статичный таймер, потерянную форму.

### T6 — Статус коммита и исключённые (AC10, AC35; F2)
- Данные: `COMMIT-AUDIT-TODO-0001.json` для `committed`/`skipped`/`refused`/`error` с `reason` и `excluded=[[путь,причина]]`; файла нет; битый JSON.
- Ожидания: `commit.status/reason` равны файлу; при `skipped`/`refused` обе пары видны; при `error` — `reason`; отсутствие и битый файл → «аудита коммита нет», без «повреждены».
- Ловит: скрытый отказ коммита.

### T7 — Один владелец видимости пака (AC11, AC13, AC35; F2)
- Данные: `GATES` с `tree_sha`: verify идёт; вне verify свежий и дерево
  совпало; файл старше; другой `tree_sha`; без `tree_sha`; несколько причин.
- Ожидания: `state.verify` **равен** `verify_facts(...)`; видим в verify и в
  свежем совпавшем; иначе `hidden` и `pack_hidden_reason` по приоритету
  `not-current` → `no-tree-sha` → `other-tree`; шаблон `GATES` не читает.
- Кэш: два вызова за 10 с → один `_current_tree_sha`, сброс при смене HEAD
  или `updated_at`, на диске нет.
- Ловит: два ответа о свежести, git на каждый опрос.

### T8 — Попытка и честный бейдж (AC13; F2)
- Данные: `stage_attempts={"TODO-0001:spec-requirements": 2}`, в логе одно вхождение; `engine_dead`; `stale_stage`; `pipeline_running` без них.
- Ожидания: `attempt.n=2`, `attempt.log_n=1`, подсказка содержит оба; `engine_dead`/`stale_stage` → бейдж не зелёный, текст содержит строку владельца; `pipeline_running` без dead/stale → не `Dead`.
- Ловит: попытку по логу, зелёный экран при мёртвом движке.

### T9 — Бюджет опроса (AC14; F5)
- Данные: синтетический лог ≥ 35 000 строк, прогретый сервер.
- Действие: 5 запросов `/api/state`; 10 параллельных; счётчик
  `generate_state_dict`; `git status --porcelain` до/после.
- Ожидания: медиана — холодный ≤ 2 с, тёплый ≤ 1 с (`perf_counter`, без
  `cProfile`); 10 параллельных → 1 расчёт, один в полёте (INV4); индекс и
  дерево не меняются, `tree_sha` в кэше 10 с (INV10).
- Ловит: рост стоимости опроса, git-мусор. Риск флейка — QA4.

### T10 — Финал: снимок и вкладка (AC15; F3)
- Данные: state с `current_stage_kind=verify` и видимым паком; затем
  `clear_state`; отдельно пака нет.
- Ожидания: снимок `dashboards/current.html` пишется до очистки: «прогон
  завершён», статус, время, вердикт; после `clear_state` файл отдаёт тот же
  вердикт; без пака — «вердикта нет»; «Pipeline exited» как ошибка не
  появляется.
- Вкладка (статически): после 3 ошибок — «обновление остановлено, снимок от
  HH:MM:SS», без пересчёта (QA2).
- Ловит: потерю вердикта после выхода.

### T11 — Чип забега и штамп (AC16, AC17; F3)
- Данные: `run.yaml` со всеми полями AC16; часы на `12:34:56`; вариант
  `active=false`.
- Ожидания: `state.run` равен `run_brief`; ссылка на отчёт только внутри
  `outbox` (INV2); `active=false` → «забег закончен», не «устарел»;
  `generated_at` — ISO `12:34:56`, в шапке «снимок от 12:34:56» один раз.
- Ловит: чип из 5 полей, данные без возраста.

### T12 — Свежесть и запрет подмены (AC18, AC19; F2)
- Данные: `COMMIT-AUDIT` старше `updated_at`; `verify_waiting.since` позже
  mtime; лог −20 мин, `stale_stage=null` и строка; state на TODO-0002 без
  файла, файл есть у TODO-0001.
- Ожидания: вне verify `fresh=false` и «последний известный коммит юнита от
  HH:MM»; в verify `from_previous_pass=true` и «от прошлого прохода»; лог
  «не обновлялся 20 мин» без «устарел» при `stale_stage=null`, с ним при
  строке; `run.active=false` → «забег закончен»; сводка «нет данных», текст
  TODO-0001 не подставлен.
- Ловит: старое как текущее, прошлый юнит вместо пустоты.

### T13 — Уведомление считает сервер (AC20; F1)
- Данные: `status` + `blocked` + `engine_exit` для `blocked`, `dead`,
  `stale`, `done`; память `_NOTIFY_MEMORY`, сброс `reset_fact_memory()`;
  два опроса подряд.
- Ожидания: `notification={key, kind, text}`, `key="<kind>|<todo>"`, `text` —
  одна строка из `blocked.reason`/первой строки `traceback_tail`/
  `stale_stage`/вердикта финала; 1-й опрос отдаёт, 2-й — `null`; смена
  `kind` или юнита даёт новое уведомление; без `notification` экран не падает.
- Ловит: молчание о проблеме и спам на каждом опросе (K8).

### T14 — Имя стадии и роль рядом (AC21; F3)
- Данные: две стадии роли `agent-system-analyst` — `spec-requirements`,
  `spec-finalize`; handoff-и обеих; активна `spec-finalize`.
- Ожидания: `stage={name, role, label, icon, color}` из `pipeline.yaml`;
  запись чата `role` = имя стадии, `role_name` = роль, `label`/`icon`/`color`
  из `_role_visual`; сайдбар и `notification` называют ту же пару.
- Ловит: «Spec Context» при роли аудитора и неразличимые стадии.

### T15 — Панель Worker: строка, дерево, длительность (AC22, AC23; F3)
- Данные: `ProcInfo` с `starttime`: 100 (ppid 50) → 101 (ppid 100), орфаны
  102 (ppid 1) и 103 (ppid 999); вывод с баннером `> build · deepseek`;
  пустой список при старом `worker_pid`; `starttime` недоступен.
- Ожидания: 4 члена с `pid`, `label`, `stage`, `duration_sec`; `orphan`
  только 102/103; `duration_sec = now - (btime + starttime/CLK_TCK)`;
  без `starttime` — `null` и «длительность неизвестна»; `last_line` —
  содержательная, не баннер; без неё «нет вывода»; плоский
  `worker_processes` не дерево; без живых членов `worker.active=false`,
  «воркер не запущен», чужой pid не показан.
- Ловит: баннер, потерю орфанов, выдуманную длительность.

### T16 — Дифф: база, коммиты, контракт (AC24–AC26; F3)
- Данные: `tmp_git_repo`, `BASELINE-TODO-0001.sha` = коммит 1, после него
  2 коммита и 2 файла; контракт `files: [awf/x.py]`; без базы.
- Ожидания: `diff.base_sha`, `commits_since_base=2`, `base_stale=true`,
  «всё после базы, не только этот юнит»; `contract_files=["awf/x.py"]`,
  `outside_contract` — второй файл; размер не работа юнита; без базы «нет
  данных».
- Ловит: 76 файлов `+6470/−342` как работу юнита.

### T17 — Пустое и битое состояние (AC27, AC28; F2)
- Данные: пустой `tmp_path` без `.agentic`; `pipeline.yaml` с 0 стадий; TODO
  и handoff с невалидным UTF-8; `GATES` без JSON; битый `COMMIT-AUDIT`.
- Ожидания: `generate_state_dict` не падает и не висит; `problems` с
  `block=project` («нет `.agentic/`», `awf_init`) и `block=pipeline`
  («стадий нет, заполните `stages`»); для TODO/handoff — «данные
  повреждены»; `gates` → `unreadable` и «данные повреждены», секции
  нарисованы; битый `COMMIT-AUDIT` → «аудита коммита нет»; прочие блоки и
  опрос работают; JSON сериализуем.
- Ловит: 500 и пустой экран, молчаливое исчезновение (K7).

### T18 — Правило открытия, десять случаев C8 (AC29; F4)
- Данные: `dashboard_boot`, `dashboard_opened` (`ts`, `url`, `boot_ts`),
  часы; занятый порт; упавший старт.
- Ожидания: 1) новый запуск — `opened`; 2) повтор запуска — `already-open`;
  3) тот же URL за 10 мин — нет; 4) другой URL — да; 5) старше 10 мин в
  новом запуске — да; 6) `file://` — окно 600 с; 7) старт упал — маркера и
  вкладки нет, включая file:// (QA7); 8) порт занят, новый сервер поднялся —
  новый URL открывается; 9) boot нет/битый — `not-this-run`, URL текстом;
  10) CI/нет GUI — нет.
- Ловит: второй таб, «окно не наступает», file-вкладку после упавшего старта.

### T19 — GUI-гейт, явное открытие, MCP (AC30, AC31; F4)
- Данные: `CI=1`, `PYTEST_CURRENT_TEST`, `AWF_NO_DASHBOARD_OPEN`, нет
  `DISPLAY`/`WAYLAND_DISPLAY`; битый boot; подмена `webbrowser.open`.
- Ожидания: все пути не открывают в CI/тестах/при выключателе/без GUI
  (C8-10); `--open` и MCP `awf_open_pipeline_dashboard` при битом маркере
  открывают (окно 60 с); запускающие тулы — авто с `launched_at`; авто не
  закрывает и не переоткрывает вкладку.
- Ловит: открытие из тестов, три реализации правила.

### T20 — Документация совпадает с экраном (AC32; F3)
- Данные: `dashboard.html.j2`, `USAGE.md`, `USAGE.ru.md`, `BACKLOG.md`.
- Ожидания: подписи вкладок шаблона (`Agent Chat`, `Итерация`, `События`) есть в обоих USAGE; назван опрос 3 с и таймлайн; в BACKLOG «Модели на карточках чата» и «Agent Chat после передачи обратно» закрыты, «Авто-открытие при реюзе» отсылает к R12.
- Ловит: документ о старом экране.

### T21 — ETA как оценка (AC33, AC34; F3)
- Данные: лог с 3 стадиями (60, 90, 120 с), 5 стадий, прошедшее 300 с; одна стадия.
- Ожидания: `eta.median_sec=90`, `stages_done=3`, `stages_total=5`, `elapsed_sec=300`, `estimate_sec=90*2+300`, подпись «оценка по прошлым виткам»; при <2 стадий `eta=null` и «оценки нет»; `status` не зависит от ETA.
- Ловит: выдуманное число и ETA-дедлайн.

### T22 — Безопасность и регрессия (INV1–3, INV5–8; R5; R1)
- Данные: TODO и handoff с `<script>`, `javascript:`, `</script>`; хэши state/артефактов до и после `generate_state_dict`; существующие 84 теста.
- Ожидания: текст экранирован, `href` нейтрализован, JSON в `<script>` экранирует `<`; `/api/state` без `Access-Control-Allow-Origin`; запись атомарная; каждое поле читает шаблон (`test_dashboard.py:1057`); чтение не меняет `current.yaml`, `run.yaml`, артефакты (INV6); 84 теста зелёные (INV7); пустое дерево без падения и висяка (INV8).
- Ловит: XSS, CORS, мутацию читателем, регрессию экранов.

### T23 — Объём пакета (AC36; F6)
- Данные: каталог `docs/spec/dashboard`; `discover_documents`,
  `count_words`, `check_budgets`.
- Ожидания: шесть ролей (context, requirements, requirements-review,
  architecture, test-spec, readiness), лишних файлов нет; каждый ≤ 2200
  слов, сумма ≤ 11 000; `dashboard.finalize.md` нет; `check_budgets` = `[]`.
- Ловит: седьмой файл-отчёт (CTX21) и пакет сверх бюджета.
- Выполнение: `test_spec_budget.py` + `awf spec check` (F6 — каталог).

## 5. Условия приёмки

Обязательны T1–T23; без них «зелёный» не принимается. TDD: падающая проверка,
затем код; падение импорта красной фазой не считается.
Регрессия — INV7 и канон `scripts/suite-canonical.sh`; T9 фиксирует числа
AC14 в отчёте. Внешние предпосылки (GUI, сервер, API) не используются.

## 6. Findings

| ID | Важность | Источник | Проблема | Владелец | Состояние |
|---|---|---|---|---|---|
| QA2 | MINOR | AC15 | Заморозка вкладки — клиентский JS, T10 статичен. | architector | OPEN |
| QA4 | MINOR | AC14 | Порог ≤2/≤1 с на 35k строк может флейкать на CI; лечится медианой, не ослаблением AC. | qa-review | OPEN |
| QA7 | MINOR | AC29, RR50 | K5 не запрещает file-вкладку после упавшего старта, а C8-7 её не ждёт; T18 кодирует C8-7. Нужна норма в AC29. | analyst | OPEN |
| QA8 | SUGGESTION | AC11, RR51 | У пака нет пометки «от прошлого прохода»: K1 не отдаёт `generated`, AC18 требует её только для коммита. | analyst / architector | OPEN |

## 7. Что не доказано

`live_members_of_state` (T15) и `webbrowser.open` (T19) моделируются; их
границы закрывают `test_worker_tree_qa.py` и `test_dashboard_open.py`.
Клиентский JS не исполняется: T10 статичен (QA2), T13 — серверный (K8).
