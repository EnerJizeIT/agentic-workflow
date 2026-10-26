# Контекст: carry batch C+D — диагностика/доки/паритет и approve-bind

Scope: carry-cd (SPEC-ран: только документы, код не меняется)
Ревизия: ctx-2
Входы: TODO-0266; retired `done/TODO-0243/TODO.md` (14 требований); DONE-0239/0240/0241; DONE-0230 (аудит M6) и DONE-0233 (виток 4); `BACKLOG-NEXT.md` G.1 (S2/S3/S5) и G (P5–P9); `inbox/TODO-0264.md` — implementer-юнит с тем же scope
База кода: commit `bdd1074`; дельта `b6c4258..bdd1074` — `awf spec init` и разбор непереведённых ролей в `awf/api/setup.py` (+112 строк); факты C10 (`setup.py:488`) и C13 (`setup.py:856,937`) не изменились; пакет `docs/spec/carry-cd/` untracked
Статус: REVIEW

## 1. Итог для аналитика

Волны 0244–0262 закрыли часть сырого списка 0243. Ниже — состояние каждого
требования по факту в дереве, а не по тексту старого TODO.

**Уже закрыто, в требования не включать** (проверено чтением кода):

| Пункт | Где закрыт | Чем доказано |
|---|---|---|
| C1 — один `duration=` во всех ветках | `awf/_worker_exit.py:68-87`; `awf/agent_stage.py:265-272`; `awf/signal_watch.py:83` | оба вызывающих передают `time.monotonic() - start` от собственной точки спавна; других вызовов `worker_exit_line` нет |
| C3 — reduce-заметка 2-го silent-ретрая | `awf/pipeline_engine.py:938-944` | один текст «no full suite / run-all, targeted checks + gates»; второго варианта в коде нет |
| C9 — кодировка git-log | `awf/agent_stage.py:317`; `awf/metrics.py:243` | оба читают через `git_stdout(errors="surrogateescape")` |
| C4 (часть) — подсказка `remove_todo` | `awf/api/hygiene.py:585` | отказ на started юните пишет «Use awf todo-retire» |
| S2 — имя `spec_budget` | `awf/verify_pack.py:1178`; `docs/unit-contract.md:281` | код и документ совпадают, дефиса нет |

**Открыто** (остальное): C2, C4 (`require_posix` архива), C5, C6, C7, C8,
C10, C11, C12 (approve-bind), C13, C14, S3, S5, P7, P8.

Три вещи в этом списке — решения, а не механика, и их нельзя «дописать» без
ответа владельца: паритет `pipeline-write --variant` (C10/S3: материализовать
роли как форма или честно написать разницу), формулировка про Windows (C5), и
какие из четырёх док-расхождений M6 правим сейчас (C14).

Естественный шов на два независимых пакета:

1. **C+D-диагностика, доки и паритет** — C2, C4, C5, C6, C7, C8, C9(закрыт),
   C10, C11, C14 плюс S3, S5, P7, P8. Механические правки в разных файлах.
2. **Approve-bind и сохраняющая запись конфига** — C12 и C13. Оба про
   managed-барьер и целостность файла; C13 меняет библиотеку записи YAML.

Главное ограничение scope: в `.agentic/inbox/` уже лежит `TODO-0264.md` —
implementer-юнит с теми же 14 требованиями и `verify` на
`tests/negative/test_carry_batch_diag_docs.py`, которого в дереве нет.
Пока супервизор не решит, что делать с 0264 (заменить пакетом, отозвать или
сузить), любой новый dev-юнит дублирует уже выданную работу.

## 2. Матрица «требование → состояние»

| ID | Требование | Состояние | Точка в коде |
|---|---|---|---|
| C2 | `worker_log_tail`: докстринг, 64 КиБ только без сигнала, пустая строка, один текст «сигнал уже на диске» | **открыто**: хвост читается всегда — аргумент вычисляется до проверки `signal_present`; текстов «signal already detected» два (`signal_watch.py:108` с `late`, `:506` без) | `awf/agent_stage.py:272`; `awf/_worker_exit.py:52-66`; `awf/_net.py:118` |
| C4 | `require_posix("archive todo")` | **открыто**: `archive_todo` делает только no-follow preflight, POSIX-проверки нет (у соседей есть) | `awf/todos.py:205-233`; ср. `awf/api/_helpers.py:227` |
| C5 | Док-дрейф: README Windows, `design.md:204/205`, `_lock.py:12`, `_transact.py:97` | **частично**: `design.md` уже описывает `msvcrt` как мёртвый резерв; `_transact.py` в дереве нет — пункт протух; README говорит «Windows (untested)», хотя запуск/разрушение на не-POSIX отказывают | `README.md:48`; `awf/_lock.py:12`; `docs/design.md:204-205` |
| C6 | Многострочный `<!-- -->` и фенс `# TODO-9999` | **открыто**: пропуск тривии построчный (`startswith("<!--")`), вторая строка комментария ломает поиск; `_todo_title` берёт первый `# ` в любом месте, включая код-фенс | `awf/unit_contract.py:357-374`; `awf/commit_gate.py:95-120` |
| C7 | `todo-draft --out` барьер, legacy `BLOCKED-<short>`, `--content-file` от проекта, флаги паритета | **открыто**: запись — голый `write_text`; `blocked_draft` ищет только канонический файл; `--content-file` резолвится от CWD; `allow_mismatch`/`carry_over_from`/`include_untracked` в CLI не проброшены | `awf/todo_draft.py:333-341`; `awf/api/hygiene.py:1641`; `awf/cmd_dispatch.py:56-80`; `awf/api/dispatch.py:937-939` |
| C8 | `metrics`: внешний путь через `agentic_root_for` | **открыто**: классификация «внутри/вне» — только `normpath` + `is_relative_to`, `agentic_root_for` не участвует | `awf/metrics.py:708-728` |
| C10 | `pipeline-write --variant` паритет | **открыто**: CLI предупреждает и возвращает 0, роли не материализует; `--help` говорит «same text the setup form offers», но умалчивает про роли | `awf/cmd_pipeline_write.py:73-106`; `awf/api/setup.py:490-528`; `awf/cli.py:840-845` |
| C11 | Строка про `retry.silent_work_attempts` 1→2 | **открыто**: код default 2, релизные заметки говорят 1 | `awf/pipeline_engine.py:1742`; `docs/RELEASE-NOTES.md:111` |
| C12 | APPROVE-бинд через managed-барьер | **открыто**: `managed_mkdir(inbox)`, затем голые `atomic_write_text`/`touch` — своп листа после mkdir не ловится | `awf/api/pipeline.py:940-984` |
| C13 | Вариант setup не стирает комментарии `config.yaml` | **открыто**: чтение `yaml.safe_load`, запись `yaml.safe_dump` — комментарии теряются | `awf/api/setup.py:748-838` |
| C14 | Четыре док-расхождения M6 | **открыто**: (1) `docs/practices.md:42-43` обещает симлинк `templates/`, которого нет; (2) `USAGE.md:143-148` и `USAGE.ru.md:114-119` не знают код 5 и переменные `AWF_SUITE_CMD`/`AWF_PYTHON`; (3) докстринг `add_role` говорит про два built-in, их пять; (4) докстринг `awf_templates_dir` обещает «все копии зовут меня», а три модуля пишут путь руками | `awf/api/roles.py:1258`; `awf/api/_templates.py:151`; `awf/api/dashboard.py:29`; `awf/phase.py:214`; `awf/doctrine.py:68` |
| S3 | `pipeline-write --variant` rc при unresolved | **открыто** — тот же дефект, что C10, но решение про rc | `awf/cmd_pipeline_write.py:96-106` |
| S5 | Не-dict `paths` в записи нарушения | **открыто**: `_open_path_names` возвращает `()` для не-dict, а `open_violations` считает такую запись «исчерпанной» — fail-open | `awf/stage_manifest.py:231-233, 336`; `awf/commit_plan.py:378-390` |
| P7 | Транзиентный сбой провайдера | **открыто**: маркеры сети — только connect/reset/timeout; 401 «Invalid credential» и 5xx попадают в `silent` и тратят silent-бюджет, а не сетевой backoff | `awf/_net.py:29-35, 92-96`; `awf/pipeline_engine.py:1192-1230` |
| P8 | Сырой полный pytest в обход артефакта | **открыто**: в `agent_stage`/`pipeline_engine` нет учёта команд стадии и требования `SUITE-*.json` | `awf/agent_stage.py`, `awf/pipeline_engine.py` |

## 3. Карта затронутых путей

| Требование | Вход | Компоненты | Данные и эффекты |
|---|---|---|---|
| C2 | смерть воркера без сигнала | `agent_stage._run_stage`, `signal_watch._log_timeout_exit`, `_worker_exit` | строка `worker-exit:` в stage-log; чтение хвоста лога |
| C4 | `awf todo` / архивация | `todos.archive_todo`, `managed_fs` | перенос `inbox/outbox → done/<id>` |
| C5, C14 | читатель доков | `README.md`, `USAGE*.md`, `docs/*.md`, докстринги | текст |
| C6 | контракт TODO и subject коммита | `unit_contract._find_contract_block`, `commit_gate._todo_title` | разбор front-matter; subject коммита |
| C7 | CLI dispatch/черновики | `cmd_dispatch`, `todo_draft`, `api.hygiene.blocked_draft`, `api.dispatch_todo` | TODO-файл, baseline, `.ready` |
| C8 | `awf metrics` | `metrics._write_cache_managed`, `_resolve_out_path` | файл кэша/отчёта вне проекта |
| C10, S3 | `awf pipeline-write --variant` | `cmd_pipeline_write._write_variant`, `api.setup.write_variant_pipeline` | `.agentic/pipelines/<name>.yaml` + предупреждения |
| C12 | `awf_approve` в забеге | `api.pipeline.approve` | `inbox/APPROVE-<id>.ready`, `context/VERIFIED-<id>.sha` |
| C13 | запись варианта setup | `api.setup` (config writer) | `.agentic/config.yaml` + `.bak` |
| S5 | коммит-гейт | `stage_manifest.open_violations`, `commit_plan` | `context/…VIOLATION…json` |
| P7, P8 | смерть стадии / запуск команд | `_net`, `pipeline_engine`, `agent_stage` | классификация смерти, бюджет прогонов |

## 4. Переиспользуемые компоненты и контракты

- **Managed-барьер поверхностей.** `refuse_unsafe_surfaces(project_dir, operation, *surfaces)` — POSIX-проверка, два no-follow прохода (raw и канонический), отказ до первого эффекта (`awf/api/_helpers.py:185-235`). Готов для C7 и C12; `archive_todo` уже использует родственный `assert_no_symlink_escape`, но без `require_posix`.
- **Managed-запись.** `managed_fs.managed_mkdir` / `managed_write_text` / `managed_write_api_text` (`awf/api/_helpers.py:237`), `require_posix` (`awf/infra/managed_fs.py:45`).
- **Один разбор Markdown.** `_markdown.skip_leading_trivia` (`:74`) и `first_heading_index` (`:104`) — то, что C6 требует вместо построчных эвристик.
- **Один разбор TODO.** `todos.read_todo_text` (`:57`), `todos.review_round` (`:27`).
- **Один git-канал.** `git_utils.git_stdout` (`:23`) — codec фиксируется одним ключом.
- **Один построитель диагностики.** `_worker_exit.worker_exit_line` / `worker_log_tail`; `_net.read_log_tail(max_bytes=65536)`.
- **Паритет CLI↔MCP как данные.** `api.dispatch_todo` уже принимает `allow_mismatch`, `carry_over_from`, `include_untracked` (`awf/api/dispatch.py:937-939`); не хватает проброса в CLI, а не новой логики.
- **Вариант пайплайна.** `api.setup.write_variant_pipeline` — единственная сборка текста для формы и CLI; разница только в материализации ролей (`api.setup._materialize_variant_roles`, `awf/api/roles.py:1016`).
- **Сохранение YAML.** Библиотеки с сохранением комментариев в зависимостях нет — проверить `pyproject.toml` до выбора ruamel.

## 5. Поведение, которое нельзя сломать

- Отказы до побочных эффектов: небезопасный путь, живой пайплайн, занятый слот забега — ни одного файла, ни `.ready`, ни baseline.
- APPROVE-бинд: отпечаток `VERIFIED-<id>.sha` пишется ДО сигнала; гейт требует согласованности generation/verified-sha/files-digest.
- `pipeline-write` пишет только файл пайплайна и не трогает `config.yaml`/`supervisor.md`.
- Архивация и ретайр не теряют незакоммиченную работу (доктрина 02) и не трогают чужие файлы.
- Guard-канарейки `proc.pid > 1` и `_forbid_session_kill` — не трогать.
- Классификация сети сохраняет приоритет над silent-ретраем; сетевой бюджет отдельный.

## 6. Baseline и тесты

| Проверка | Команда | Результат | Наблюдение |
|---|---|---|---|
| Контракты | `bash scripts/check-contracts.sh` | PASS | 8 контрактов, 7 контрактов юнитов, 0 сработавших матриц, 16 path-токенов, 8 гейтов |
| Целевые тесты | `pytest tests/negative/test_miniwave_m3b_movers.py tests/unit/test_unit_contract.py -q` | PASS | 103 passed за 3.03 с |
| Baseline-команда проекта | `BASELINE-TODO-0266.tests.log` | NOT_RUN | таймаут 300 с на `tests/negative tests/unit/test_run.py tests/unit/test_pipeline_engine.py` |
| Полный сьют | `bash scripts/run-all.sh` | NOT_RUN | не запускался: стадия аудита; перед полным прогоном — safety-грепы из AGENTS.md |

Готовые средства по темам: `tests/negative/test_miniwave_m3b_movers.py`
(архив/ретайр), `tests/unit/test_unit_contract.py` (контракт), 
`tests/negative/test_carry_batch_engine_queue.py` (очередь забега),
`tests/agent_workflow_ui/test_awf_tools.py` (паритет MCP), 
`tests/negative/test_miniwave_m5_sourceref.py`, `tests/unit/test_pipeline.py`,
`tests/unit/test_pipelines_api.py`, `tests/negative/test_miniwave_m6_roles.py`
(вариант setup, роли). Файла `tests/negative/test_carry_batch_diag_docs.py`,
на который ссылается 0264, в дереве нет.

## 7. Находки

| ID | Важность | Файл / символ | Факт и доказательство | Влияние на фичу | Следующий шаг | Уверенность |
|---|---|---|---|---|---|---|
| CTX1 | MAJOR | `.agentic/inbox/TODO-0264.md` | Готовый implementer-юнит с теми же 14 требованиями; `verify` и `prove_red` указывают на несуществующий тест-файл. В очереди забега его нет (`state/run.yaml`: 0265, 0266) | Дублирование работы или провал 0264 на verify | Супервизор: заменить 0264 пакетом, отозвать или сузить | Высокая |
| CTX2 | MAJOR | `awf/api/pipeline.py:940-984` | Бинд пишется после `managed_mkdir`, без preflight листа; своп симлинка в окне не ловится | C12: ложное «одобрено» с чужим сигналом | Dev-юнит: preflight до mkdir + no-follow запись, тест на своп | Высокая |
| CTX3 | MAJOR | `awf/api/setup.py:835` | `yaml.safe_dump` перезаписывает конфиг без комментариев | C13: пользователь теряет пояснения в `config.yaml` | Решение: вводить сохраняющий writer (проверить зависимость) | Высокая |
| CTX4 | MAJOR | `awf/stage_manifest.py:231-233` | Не-dict `paths` → `()` → запись считается исчерпанной (`:336`), коммит проходит | S5: fail-open при повреждённой записи | Dev-юнит: fail-closed + тест | Высокая |
| CTX5 | MINOR | `awf/cmd_dispatch.py:56-80` | `--content-file` резолвится от CWD; три API-флага не проброшены | C7: CLI и MCP ведут себя по-разному | Dev-юнит: проброс + тест паритета | Высокая |
| CTX6 | MINOR | `awf/agent_stage.py:272` | `worker_log_tail` вызывается всегда, до проверки `signal_present` | C2: лишние 64 КиБ чтения на каждой стадии | Dev-юнит: чтение только в ветке без сигнала | Высокая |
| CTX7 | MINOR | `awf/cmd_pipeline_write.py:96-106` | Unresolved-роли: WARNING и rc 0; help обещает «same text» без оговорки про роли | C10/S3: пользователь ждёт рабочий пайплайн | Решение владельца: rc≠0 или строка в help/руководстве | Высокая |
| CTX8 | MINOR | `awf/todos.py:205-233` | В архиве нет `require_posix`, хотя `docs/design.md:204` обещает отказ на не-POSIX | C4: ослабленный барьер на слабой платформе | Dev-юнит: одна строка + тест | Высокая |
| CTX9 | MINOR | `awf/metrics.py:708-728` | Внешний путь определяется по `normpath`/`is_relative_to`; `agentic_root_for` не используется | C8: относительный `project_dir` классифицируется неверно | Dev-юнит: helper + тест с относительным путём | Средняя |
| CTX10 | MINOR | `awf/_net.py:29-35` | 401/5xx не в маркерах сети → классификация `silent` | P7: транзиентный отказ провайдера тратит silent-бюджет и выглядит как «модель молчит» | Решение: расширять маркеры или отдельный класс + salvage-текст | Средняя |

Четыре док-расхождения C14 перечислены в матрице раздела 2; отдельными
находками не дублирую.

## 8. Несвязанные находки

- В `inbox/` лежат ещё пять активных юнитов (0250, 0251, 0254, 0256, 0263,
  0264). Запуск без `todo_id` берёт «самый новый активный» — для spec-рана
  нужен пин, он уже стоит в `state/run.yaml`.
- Baseline-команда проекта стабильно упирается в 300 с; это ограничение
  измерения, а не дефект фичи.

## 9. Вопросы и непроверенные предположения

- Q1 (аналитик/владелец): один пакет или два — «диагностика/доки/паритет» и
  «approve-bind + config writer»? Шов предложен в разделе 1.
- Q2 (владелец): что делать с `TODO-0264` — заменить, отозвать или сузить до
  части требований (CTX1)?
- Q3 (архитектор): для C13 — ruamel уже есть в зависимостях или нужен свой
  сохраняющий writer? Проверить `pyproject.toml` до выбора.
- Q4 (владелец): C10/S3 — материализовать роли в CLI или только честный текст
  (rc остаётся 0)?
- Q5 (владелец): P7/P8 — включать в этот пакет или вынести отдельными
  фичами? Оба меняют поведение движка, а не диагностику.
- Допущение A1: «закрыто» в разделе 1 означает «код уже содержит требуемое
  поведение»; тест-спека всё равно должна закрепить каждое закрытое
  требование регрессионной проверкой, иначе следующая волна его откатит.
