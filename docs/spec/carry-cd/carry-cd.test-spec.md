# Тестовая спецификация: carry batch C+D — диагностика, документация, паритет и approve-bind

Scope: carry-cd
Ревизия: qa-3
Входы: `carry-cd.md@req-7` (PASS `review-7`); `carry-cd.requirements-review.md@review-7`; `carry-cd.architecture.md@arch-5`; `carry-cd.context.md@ctx-3`; TODO-0266, TODO-0279
База кода: commit `13fb5e4` + WIP TODO-0276 в рабочем дереве; пакет `docs/spec/carry-cd/` untracked
Статус: REVIEW

Проверки спроектированы, не пройдены; ожидания выведены из R#/AC#/INV# и K1–K14, расхождения — раздел 5.

## 1. Границы и среда

Покрыты R1–R13, AC1–AC34, INV1–INV7, C1–C7. Новые: IMP1 — `tests/unit/test_worker_exit.py`, `tests/negative/test_carry_diag.py`; IMP2 — `tests/unit/test_suite_command.py`, `tests/negative/test_carry_suite_gate.py`; IMP3–IMP6 — `tests/negative/test_carry_{barriers,paths}.py`, `tests/unit/test_{carry_docs,config_write}.py`.

Изоляция — `tmp_path`, `tmp_git_repo`, `api.init_project`; каждый тест строит проект. Сеть и production не нужны; внешние процессы — CLI и `scripts/suite-canonical.sh` в temp-проекте; воркер, лог, сигнал подменяются; порядок не влияет.

На базе: `check-contracts.sh` ok; целевые 103 passed, IMP1 — 96 (`DONE-TODO-0276.json`); полный сьют и `run-all.sh` не запускались.

## 2. Матрица покрытия

| ID | Сценарий/контракт | T# | IMP# |
|---|---|---|---|
| R1, AC1–AC3 | C1; K1 | T1 | IMP1 |
| R2, AC4–AC5 | C2; K2 | T5 | IMP3 |
| R3, AC6–AC7 | K3 | T13 | IMP5 |
| R4, AC8–AC11 | K4 | T13 | IMP5 |
| R5, AC12–AC15 | K5 | T8 | IMP4 |
| R6, AC16–AC19 | C3; K6, D2 | T9 | IMP4 |
| R7, AC20–AC21 | K7, D8 | T10 | IMP4 |
| R8, AC22–AC23 | C4; K8, D3 | T11 | IMP5 |
| R9, AC24–AC25 | C5; K9, D4 | T6 | IMP3 |
| R10, AC26–AC27 | K10, D5 | T12 | IMP6 |
| R11, AC28–AC29 | C6; K11 | T7 | IMP3 |
| R12, AC30–AC32 | C1; K12, D6, D11, D12 | T2 | IMP1 |
| R13, AC33–AC34 | C7; K13, K14, D7, D9 | T3, T4 | IMP2 |
| INV1–INV4 | отказы до эффекта; бинд; только pipeline; архив | T5–T7, T9, T11, T12, T14 | IMP3–IMP6 |
| INV5, INV7 | канарейки; закрытые C1, C3, C4-подсказка, C9, S2 | T14 | регрессия |
| INV6 | сеть важнее silent | T2, T14 | IMP1 |

## 3. Проверки

### T1 — ленивый хвост, один текст сигнала, 64 КиБ
- Покрывает R1, AC1–AC3; C1; K1. Уровень: unit `_worker_exit` + integration `agent_stage._run_agent_stage` (заглушки спавна, лога, сигнала).
- Данные: `tail_fn` с AssertionError; лог 70 КиБ с маркерами по краям, `log_start_offset=100`; пустой/отсутствующий файл; rc 1, 137, −9; сигнал DONE.
- Действие: `worker_exit_line(rc, 12.4, tail_fn=..., signal_present=True)`; без сигнала; `signal_present_line("DONE")`; `_late_signal_done`; смерть стадии без сигнала.
- Ожидания: при сигнале `""`, `tail_fn` не вызван; ровно `U6b: signal present (DONE)`; `BD-20: signal already detected` нет; без сигнала — последние 64 КиБ после offset, первый байт на `size−65536`; пустой путь/файл → `""`; одна строка `worker-exit: rc=1 signal=none duration=12s class=silent`; 137/−9 → `SIGKILL`; `503` → `network`.
- Ловит: чтение хвоста при сигнале; два текста «сигнал на диске»; хвост прошлой попытки.

### T2 — класс сбоя и BLOCKED-заметка (auth-семантика R12)
- Покрывает R12, AC30–AC32; C1; K12, D6, D11, D12; INV6. Уровень: unit `_net.classify_failure` и `_worker_exit.classify_worker_exit` + integration обеих смертельных веток движка.
- Данные (ctx-3 §0.1): реальные строки провайдера и свои тест-строки (значения ниже); хвост 401+503; `rc=137`/`-9`/`Killed`; забег, silent 1/3, net 0/3.
- Действие: `classify_failure(text)`, `classify_failure(text, rc=137)`, `classify_worker_exit`; смерть rc=1 без сигнала по классам; `awf blocked-draft TODO-0001`; чтение state и лога воркера от `log_start_offset`.
- Ожидания: в контексте провайдера (`AI_APICallError`/`error.error=`) код ИЛИ фраза → `auth`: `AI_APICallError: statusCode: 401`, `…Invalid credential`, `AI_APICallError: unauthorized`, `{"code": 401, "message": "invalid api key"}`; вне контекста — код-токен И фраза (`401 Invalid credential`); свои строки, `GET /login -> HTTP 401` и `unauthorized` без кода — не `auth`; connect/reset/timeout/Endpoint/workers/admission/capacity → `network`; 5xx/429/`overloaded`/голые `Service Unavailable`/`Too Many Requests` → `transient`, `is_network_failure` истинно: строка печатает `network`, backoff, net+1; request-shape и голые числа → `silent`; D6: 401 и 503 в одном хвосте → `network`; OOM: `classify_failure(..., rc=137)` → `""`, `classify_worker_exit` → `oom-like`, auth и 5xx не перебивают; auth-смерть: `outbox/BLOCKED-TODO-0001.md` с `## ВЕРДИКТ`, `## НАХОДКИ`, `## НАПРАВЛЕНИЯ-ФИКСА`, `## ПЕРЕНОСЫ`, `last_signal=BLOCKED-TODO-0001`, rc 1, silent 1/3, net 0/3, `awf run` после offset один; `class` строки равен классу.
- Ловит: обёртка съедает auth (F1); код без фразы и фраза без кода; свои строки как auth; OOM как network; второй спавн.

### T3 — одна сборка канонической команды
- Покрывает R13 (K14, A5, RR24). Уровень: unit `awf.suite_command.resolve`, CLI `awf suite-command`, скрипт.
- Данные: `verification.test_cmd: "npm test"`; ключа нет; `""`; `null`; битый YAML; `AWF_PYTHON=/opt/py`; `AWF_SUITE_CMD="pytest tests -k smoke"`.
- Действие: resolve; CLI; `scripts/suite-canonical.sh --project` с безопасной командой; чтение артефакта.
- Ожидания: config → `SuiteCommand("npm test", "config")`, rc 0; без ключа → `/opt/py -m pytest tests/ -q -n auto --timeout=300 -p no:cacheprovider`, `legacy`, rc 1; пусто/null/битый → `None`, rc 2; `AWF_SUITE_CMD` не читается; артефакт равен выводу `awf suite-command`, `cmd8` — её sha256; литерал pytest в Python один.
- Ловит: две копии команды; расхождение `AWF_PYTHON`; `AWF_SUITE_CMD` как канон.

### T4 — артефакт сьюта, предупреждение и факт в handoff
- Покрывает R13, AC33–AC34; C7; K13; A5. Уровень: unit `awf.suite_guard` + `collect_handoff(engine_facts=...)`; integration конца стадии.
- Данные: `tmp_git_repo` с коммитом; лог от offset: `pytest tests/ -q`, `pytest tests --maxfail=1`, `bash scripts/run-all.sh`, `pytest tests/unit/test_x.py`, `pytest tests -k smoke`; артефакты: канон, `AWF_SUITE_CMD`, чужой sha; проект без ключа.
- Действие: проверка по каждой паре; сборка handoff.
- Ожидания: полный прогон без артефакта → `SUITE-WARN:` и `suite_gate=raw-full-suite` в «Engine facts»; стадия и коммит идут; совпадающий (`sha`=`tree_fingerprint`, `command`=резолвер) → тишина; чужой command/sha → предупреждение; легаси с фолбэк-командой → тишина, узкая `AWF_SUITE_CMD` → предупреждение; полного прогона нет → нет проверки; `DONE.json` не меняется.
- Ловит: обход невидим; чужой артефакт гасит; канон блокируется.

### T5 — POSIX-барьер архивации
- Покрывает R2, AC4–AC5; C2; K2; INV1, INV4. Уровень: unit `todos.archive_todo` на `tmp_git_repo`.
- Данные: `inbox/TODO-0001.md`, `outbox/DONE-0001.md`, `DONE-0001.json`, `.ready`; симлинки `done`, `inbox`, `outbox`, `handoff` на внешнюю канарейку; `monkeypatch.setattr(managed_fs, "is_posix", lambda: False)`.
- Действие: archive; повтор на POSIX; снимок дерева и канарейки.
- Ожидания: не-POSIX → `OSError` с `archive TODO-0001` и `POSIX`; ни один файл не перемещён/не удалён, `done/TODO-0001` нет, байты inbox/outbox те же; POSIX → `TODO.md`, `DONE.md`, `DONE.json` на местах, `.ready` удалены; симлинк любой поверхности → отказ до эффекта, канарейка цела.
- Ловит: эффект до барьера; потерянный no-follow; регрессию переноса.

### T6 — APPROVE: preflight раньше записей
- Покрывает R9, AC24–AC25; C5; K9, D10; INV1, INV2. Уровень: integration `api.approve_commit` в активном забеге.
- Данные: `api.init_project`; generation 3; baseline; stale `CANDIDATE-<id>.json`; чистая и с `VERIFIED-<id>.sha`; `inbox` → симлинк; `APPROVE-<id>.ready` → симлинк на внешний файл; забег и вызов вне забега.
- Действие: approve с evidence и verified_sha; чтение сигнала, state, отпечатка и состава `context`/`inbox`/`outbox`; `commit_plan.binding_refusal`.
- Ожидания: симлинк inbox или сигнала → `AwfApiError` unsafe managed path; ни `RUN-EVIDENCE`, ни сигнала, ни файлов; чистая поверхность → нет и `VERIFIED`; существующий `VERIFIED` те же байты; stale `CANDIDATE` удалён, внешний файл цел; забег → `generation=3`, `verified_sha`, `files_digest`, `evidence_digest`, гейт принимает; вне забега — пустой маркер; порядок «preflight → evidence → отпечаток → сигнал».
- Ловит: evidence до preflight; своп после `managed_mkdir`; откат отпечатка; потерю полей бинда.

### T7 — повреждённая запись нарушения закрывает коммит
- Покрывает R11, AC28–AC29; C6; K11. Уровень: unit `stage_manifest.open_violations` и `commit_gate.maybe_commit`.
- Данные: `paths: "oops"`, `paths: []`, запись без `after`, валидная; новая поверх повреждённой.
- Действие: `open_violations`; запись новой; коммит.
- Ожидания: строка → открытое `<paths: invalid>`, `reason()` называет запись и файл, коммит rc≠0 без стейджа; `[]` → пусто и текст «record names no path»; без `after` → путь открыт; валидные как раньше; после новой записи повреждение остаётся открытым.
- Ловит: fail-open на не-dict; потерю повреждения при слиянии; утечку индекса.

### T8 — тривия и фенсы в контракте и заголовке
- Покрывает R5, AC12–AC15; K5; RR25. Уровень: unit `parse_todo_contract`, `_find_contract_block`, `commit_gate._todo_title`.
- Данные: копия front matter TODO-0266; `<!-- a` + `b -->` перед `---`; незакрытый комментарий; фенс с `# TODO-9999` и H1 после; нет/пустой H1; `---` без закрытия; H1 и контракт ниже; горизонтальная линия.
- Действие: разбор контракта и заголовка.
- Ожидания: двухстрочный комментарий → блок найден, ключи есть; копия 0266 → блок `(0, 3)`; незакрытый комментарий → ни блока, ни ошибки; `---` без закрытия → ошибка с именем файла; `# TODO-9999` в фенсе не влияет, H1 после фенса даёт subject; нет или пустой H1 → legacy `awf(<stage>): TODO-NNNN`; контракт после H1 и горизонтальная линия как раньше (`test_unit_contract.py:40-50`).
- Ловит: тривию построчно; фенс как заголовок; потерю legacy.

### T9 — черновик, legacy-заметка и паритет флагов
- Покрывает R6, AC16–AC19; C3; K6; INV1. Уровень: unit `make_todo_draft`, `api.blocked_draft`, `cmd_dispatch.run`.
- Данные: проект с `AUDIT-INDEX.md`; выход `.agentic/drafts/x.md`, симлинк, `../out.md`, абсолютный `<project>/notes/x.md`; `BLOCKED-0042.md`/`BLOCKED-TODO-0042.md` с разными маркерами; `notes.md` в корне; чужой CWD.
- Действие: draft в каждый выход; `blocked_draft`; CLI dispatch и MCP с тремя флагами.
- Ожидания: симлинк или путь вне `.agentic` → отказ до файла и родителей, `.agentic` байт-в-байт; `--force` перезаписывает обычный файл; только legacy → фикс с его находками; оба → канонический маркер; нет обоих → ошибка с двумя путями; `notes.md` найден от корня из чужого CWD; TODO, state, `source_ref`, `allow_mismatch`, `carry_over_from`, `include_untracked` совпадают с MCP; отказ не оставляет TODO, `.ready`, baseline.
- Ловит: запись до барьера; legacy не найден; CWD вместо проекта; потерян флаг.

### T10 — классификация пути метрик
- Покрывает R7, AC20–AC21; K7; D8. Уровень: unit `metrics._write_cache_managed` и `_resolve_out_path`.
- Данные: CWD `tmp/other`, `project_dir=Path("proj")`, лист `<tmp>/proj/.agentic/state/models.json`; симлинк `proj/cache`; внешний `../external/models.json`; `out=None`.
- Действие: запись; чтение warnings и файлов; выбор пути отчёта.
- Ожидания: относительный `project_dir` + абсолютный лист внутри → файл записан, warnings пуст, барьер от корня проекта; симлинк-компонент → отказ `managed_fs`, внешний файл не создан; внешний лист → записан с предупреждением, текст содержит путь; `_resolve_out_path(None, ...)` → `~/Desktop/awf-metrics-*.md` как раньше.
- Ловит: относительный корень; молчаливую внешнюю запись; ослабленный барьер.

### T11 — паритет `pipeline-write --variant` и код возврата
- Покрывает R8, AC22–AC23; C4; K8; D3; INV3. Уровень: integration CLI (`run_awf`) + unit `write_variant_pipeline`, `pipeline_variant_yaml_text`.
- Данные: `api.init_project`; вариант `full`; имена `default`/`dup.yaml`; `nope`; pipelines только для чтения; роль без файла.
- Действие: CLI `pipeline-write default --variant full`/`--help`/`--force`; форма `write_pipeline(team, flow_variant="full")`; снимок `.agentic`.
- Ожидания: rc 0, файл равен `pipeline_variant_yaml_text("full", name="default")`, `load_stages` валиден; роли, `config.yaml`, `supervisor.md` не тронуты, diff — только pipeline; `--help`/вывод называют «роли не создаются» и маршрут `awf add-role`/форма; unresolved → `WARNING` с slug, rc 0; занятое имя без `--force` → rc 1, байты те же; ошибка записи → rc 1, файла нет; `nope` → rc 2; форма материализует роли при том же тексте YAML.
- Ловит: CLI материализует; текст расходится; rc зависит от ролей.

### T12 — запись setup-варианта сохраняет конфиг
- Покрывает R10, AC26–AC27; K10; D5; INV1. Уровень: unit `awf.config_write.patch_mapping` + integration `update_config_role_mapping`.
- Данные: config с комментариями, пустыми строками, порядком ключей и блоком `models:`; model/agent_name, новая роль, снятие model через `""`; flow-стиль `models: {a: {}}`; нестандартный отступ; битый YAML.
- Действие: патч; повтор; запись через API; diff.
- Ожидания: меняются только строки целевых ролей; комментарии, пустые строки и порядок прочих ключей сохранены; `yaml.safe_load` равен целевому словарю; повтор байт-в-байт; `.bak` есть; битый YAML, flow-стиль и нестандартный отступ → `AwfApiError` без перезаписи; diff — только изменённые ключи.
- Ловит: потерю комментариев; перестановку; частичную запись.

### T13 — платформа и документация совпадают с кодом
- Покрывает R3, AC6–AC7; R4, AC8–AC11; K3, K4. Уровень: статическая проверка текста, новый `tests/unit/test_carry_docs.py`.
- Данные: `README.md`, `awf/_lock.py`, `docs/{design,practices,RELEASE-NOTES}.md`, `USAGE.md`, `USAGE.ru.md`, `awf/api/{roles,_templates}.py`; `git diff --name-only` IMP5.
- Действие: чтение и поиск строк; список изменённых файлов.
- Ожидания: README без `Windows (untested)` и обещания запуска; `_lock.py` — `msvcrt`, `locked()`, `dead`/`reserve`, без `kept for Windows support`; `design.md` ссылается на `design/agentic-writers.md`, без `_transact.py`, про отказ не-POSIX; `practices.md` без симлинка `templates/`; оба USAGE: код `5` в `config-get`, `AWF_SUITE_CMD`, `AWF_PYTHON`; `add_role` — пять шаблонов; `awf_templates_dir` без «все копии»; `RELEASE-NOTES.md` — `1 → 2`/`1 to 2`, `default 2`, без `default 1`; diff IMP5 не трогает `dashboard.py`/`phase.py`/`doctrine.py`.
- Ловит: ложное обещание; потерянный код 5; «все копии».

### T14 — регрессия, ворота и инварианты
- Покрывает INV1–INV7; A1; C1–C7. Уровень: измерение и ворота.
- Ожидания: T1–T13 в названных файлах; у каждого IMP `prove_red` (`prove_red_kind: new-api` для новых символов); красная фаза — падение утверждений, не импорт; `run-all.sh` rc 0; `check-contracts.sh` ok; `instruction-gates.sh` в бюджете; safety-грепы `pid > 1` и `_forbid_session_kill` находят строки, tripwire зелёный; INV7 — `test_worker_exit.py`, `test_agent_stage_exit_code.py`, `test_miniwave_m2_silent.py`, `test_git_utils.py`, `test_verify_pack.py`, `test_spec_e2e.py` зелёные; коммит — только diff юнита.
- Ловит: ложную красную фазу; ослабленный guard; рост бюджета; регрессию закрытых C.

## 4. Условия приёмки реализации

- T1–T13 зелёные; T14 пройден; каждый IMP имеет красный `prove_red` на базе и зелёный после.
- Отказы T5–T7, T9, T11, T12 не оставляют файла, каталога, `.ready`, baseline, `.bak`; текст называет путь/запись/роль; повтор детерминирован.
- `run-all.sh` зелёный; бюджеты не выросли; канарейки целы; коммит — только diff юнита.
- Старые проекты идут прежним путём; handoff без «Engine facts» читается; успех — по командам, не по чтению кода.

## 5. Находки

- QA1 CLOSED — AC30 называет лог воркера от `log_start_offset`; T2 проверяет его. Владелец — аналитик; закрыто req-7.
- QA2 MINOR — нет критерия на легаси-фолбэк (RR24); T3/T4 покрывают по A5/K14. Владелец — аналитик; OPEN.
- QA3 CLOSED — RR25: fixture R5 — копия front matter, не живой TODO; T8 так и делает.
- QA4 CLOSED — FR9: T6 проверяет отсутствие `RUN-EVIDENCE`, `VERIFIED`, сигнала и удаление stale `CANDIDATE`. Владелец — тест-спека.
- QA5 MINOR — AC31 говорит «`network`», K12 различает `network` и `transient` при одном backoff; T2 проверяет оба уровня, расхождения поведения нет. Владелец — аналитик; OPEN.
- QA6 MINOR — WIP `test_carry_diag.py` (`:393-396`, `:412`, `:717-732`) закрепляет старое правило (`statusCode: 401 -> ""`, `Upstream request failed -> network`); T2 его переворачивает, IMP1 обновляет матрицу вместе с `_net.py`. Владелец — IMP1; OPEN.

BLOCKER и MAJOR нет: все AC#/INV# покрыты, ожидания выведены из R#/AC# и K1–K14; QA2, QA5, QA6 проверок не меняют.
