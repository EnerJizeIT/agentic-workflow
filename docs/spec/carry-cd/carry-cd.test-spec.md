# Тестовая спецификация: carry batch C+D — диагностика, документация, паритет и approve-bind

Scope: carry-cd
Ревизия: qa-2
Входы: `carry-cd.md@req-5` (PASS `review-5`); `carry-cd.requirements-review.md@review-5`; `carry-cd.architecture.md@arch-3`; `carry-cd.context.md@ctx-1`; TODO-0266
База кода: commit `bdd10743eae8400a22755846bb95b9bf0b3e75a9`; в дереве — untracked `docs/spec/carry-cd/`
Статус: REVIEW

Проверки спроектированы, не пройдены: кода нет. Ожидания выведены из R#/AC#/INV# и K1–K14; расхождения — раздел 5.

## 1. Границы и среда

Покрыты R1–R13, AC1–AC34, INV1–INV7, C1–C7. Новые файлы: IMP1 — `tests/unit/test_worker_exit.py`, `tests/negative/test_carry_diag.py`; IMP2 — `tests/unit/test_suite_command.py`, `tests/negative/test_carry_suite_gate.py`; IMP3 — `tests/negative/test_carry_barriers.py`; IMP4 — `tests/negative/test_carry_paths.py`; IMP5 — `tests/unit/test_carry_docs.py`; IMP6 — `tests/unit/test_config_write.py`.

Изоляция — `tmp_path`, `tmp_git_repo`, `api.init_project`; каждый тест строит проект. Сеть и production не нужны; внешние процессы — CLI и `scripts/suite-canonical.sh` в temp-проекте, воркер, лог и сигнал подменяются; порядок не влияет.

На базе: `scripts/check-contracts.sh` ok; целевые тесты 103 passed; полный сьют, `run-all.sh`, `awf spec check` (rc 1 до readiness) не запускались.

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
| R12, AC30–AC32 | C1; K12, D6 | T2 | IMP1 |
| R13, AC33–AC34 | C7; K13, K14, D7, D9 | T3, T4 | IMP2 |
| INV1 | отказы до эффекта | T5–T7, T9, T11, T12 | IMP3–IMP6 |
| INV2 | бинд | T6 | IMP3 |
| INV3 | только файл пайплайна | T11 | IMP5 |
| INV4 | архив | T5, T14 | IMP3 |
| INV5 | канарейки | T14 | регрессия |
| INV6 | сеть важнее silent | T2, T14 | IMP1 |
| INV7 | закрытые C1, C3, C4-подсказка, C9, S2 | T14 | регрессия |

## 3. Проверки

### T1 — ленивый хвост, один текст сигнала, 64 КиБ
- Покрывает R1, AC1–AC3; C1; K1. Уровень: unit `_worker_exit` + integration `agent_stage._run_agent_stage` с заглушками спавна, лога и сигнала.
- Данные: `tail_fn`, поднимающий AssertionError; лог 70 КиБ с маркерами в начале и конце, `log_start_offset=100`; пустой путь и файл, отсутствующий файл; rc 1, 137, −9; сигнал DONE.
- Действие: `worker_exit_line(rc, 12.4, tail_fn=..., signal_present=True)`; без сигнала; `signal_present_line("DONE")`; `_late_signal_done`; смерть стадии без сигнала.
- Ожидания: при сигнале `""`, `tail_fn` не вызван; строка ровно `U6b: signal present (DONE)`; `BD-20: signal already detected` нигде нет; без сигнала — последние 64 КиБ после offset, первый байт на `size−65536`; пустой путь/файл → `""`; строка одна: `worker-exit: rc=1 signal=none duration=12s class=silent`; 137 и −9 → `SIGKILL`; текст 503 → `network`.
- Ловит: чтение хвоста при сигнале; два текста «сигнал на диске»; хвост прошлой попытки.

### T2 — класс сбоя и BLOCKED-заметка
- Покрывает R12, AC30–AC32; C1; K12; INV6. Уровень: unit `_net.classify_failure` + integration обеих смертельных веток движка.
- Данные: auth-тексты, `HTTP 500`, `503 Service Unavailable`, `429 Too Many Requests`, `overloaded`, connect/reset/timeout, негативы `1500 passed`, `took 429ms`, `status: 200`, смесь `401` и `503`; забег, silent 1/3, net 0/3.
- Действие: классификация; смерть rc=1 без сигнала по каждому классу; `awf blocked-draft TODO-0001`; чтение state и лога воркера от `log_start_offset`.
- Ожидания: auth, network/transient и `""` по списку; смесь → `network` (D6); auth-смерть: `outbox/BLOCKED-TODO-0001.md` с `## ВЕРДИКТ`, `## НАХОДКИ`, `## НАПРАВЛЕНИЯ-ФИКСА`, `## ПЕРЕНОСЫ`, `last_signal=BLOCKED-TODO-0001`, rc 1, silent 1/3, net 0/3, `awf run` после offset один; network/transient → `net_retry_count=1`, silent 1/3; `class` строки равен классу.
- Ловит: auth как silent; 5xx как silent; голое число как сеть; второй спавн.

### T3 — одна сборка канонической команды
- Покрывает R13 (K14, A5, RR24). Уровень: unit `awf.suite_command.resolve`, CLI `awf suite-command`, скрипт.
- Данные: `verification.test_cmd: "npm test"`; ключа нет; `""`; `null`; битый YAML; `AWF_PYTHON=/opt/py`; `AWF_SUITE_CMD="pytest tests -k smoke"`.
- Действие: resolve; CLI; `scripts/suite-canonical.sh --project` с безопасной командой; чтение артефакта.
- Ожидания: config → `SuiteCommand("npm test", "config")`, rc 0; без ключа → `/opt/py -m pytest tests/ -q -n auto --timeout=300 -p no:cacheprovider`, source `legacy`, rc 1; пусто/null/битый → `None`, rc 2; `AWF_SUITE_CMD` резолвер не читает; артефакт несёт ровно вывод `awf suite-command`, `cmd8` — её sha256; литерал pytest в Python один.
- Ловит: две копии команды; расхождение `AWF_PYTHON`; `AWF_SUITE_CMD` как канон.

### T4 — артефакт сьюта, предупреждение и факт в handoff
- Покрывает R13, AC33–AC34; C7; K13; A5. Уровень: unit `awf.suite_guard` + `collect_handoff(engine_facts=...)`; integration конца стадии.
- Данные: `tmp_git_repo` с коммитом; лог от offset: `pytest tests/ -q`, `pytest tests --maxfail=1`, `bash scripts/run-all.sh`, `pytest tests/unit/test_x.py`, `pytest tests -k smoke`; артефакты: канон, `AWF_SUITE_CMD`, чужой sha; проект без ключа.
- Действие: проверка по каждой паре; сборка handoff.
- Ожидания: полный прогон без артефакта → `SUITE-WARN:` и `suite_gate=raw-full-suite` в «Engine facts»; стадия и коммит не блокируются; совпадающий артефакт (`sha = tree_fingerprint`, `command` = резолвер) → тишина; чужой command/sha → предупреждение; легаси с фолбэк-командой → тишина, узкая `AWF_SUITE_CMD` → предупреждение; полного прогона нет → проверки нет; `DONE.json` не меняется.
- Ловит: обход невидим; чужой артефакт гасит; канон блокируется.

### T5 — POSIX-барьер архивации
- Покрывает R2, AC4–AC5; C2; K2; INV1, INV4. Уровень: unit `todos.archive_todo` на `tmp_git_repo`.
- Данные: `inbox/TODO-0001.md`, `outbox/DONE-0001.md`, `DONE-0001.json`, `.ready`; симлинки `done`, `inbox`, `outbox`, `handoff` на внешнюю канарейку; `monkeypatch.setattr(managed_fs, "is_posix", lambda: False)`.
- Действие: archive; повтор на POSIX; снимок дерева и канарейки.
- Ожидания: не-POSIX → `OSError` с `archive TODO-0001` и `POSIX`; ни один файл не перемещён и не удалён, `done/TODO-0001` нет, байты inbox/outbox те же; POSIX → `TODO.md`, `DONE.md`, `DONE.json` на местах, `.ready` удалены; симлинк любой из четырёх поверхностей → отказ до эффекта, канарейка цела.
- Ловит: эффект до барьера; потерянный no-follow; регрессию переноса.

### T6 — APPROVE: preflight раньше записей
- Покрывает R9, AC24–AC25; C5; K9, D10; INV1, INV2. Уровень: integration `api.approve_commit` в активном забеге.
- Данные: `api.init_project`; generation 3; baseline; stale `CANDIDATE-<id>.json`; чистая и с `VERIFIED-<id>.sha` известных байтов; `inbox` → симлинк; `APPROVE-<id>.ready` → симлинк на внешний файл; обычный забег и вызов вне забега.
- Действие: approve с evidence и verified_sha; чтение сигнала, state, отпечатка, состава `context`, `inbox`, `outbox`; `commit_plan.binding_refusal`.
- Ожидания: симлинк inbox или сигнала → `AwfApiError` с unsafe managed path; ни `RUN-EVIDENCE`, ни сигнала, ни новых файлов; чистая поверхность → нет и `VERIFIED`; существующий `VERIFIED` те же байты; stale `CANDIDATE` удалён, внешний файл цел; обычный забег → `generation=3`, `verified_sha`, `files_digest`, `evidence_digest`, гейт принимает; вне забега — пустой маркер; порядок «preflight → evidence → отпечаток → сигнал».
- Ловит: evidence до preflight; своп после `managed_mkdir`; откат отпечатка; потерю полей бинда.

### T7 — повреждённая запись нарушения закрывает коммит
- Покрывает R11, AC28–AC29; C6; K11. Уровень: unit `stage_manifest.open_violations` и `commit_gate.maybe_commit`.
- Данные: записи `paths: "oops"`, `paths: []`, `{"a.txt": {"before": "<missing>"}}` без `after`, валидная; поверх повреждённой — новая.
- Действие: `open_violations`; запись новой; коммит.
- Ожидания: строка → открытое `<paths: invalid>`, `reason()` называет запись и файл, коммит rc≠0 без стейджа; `[]` → пусто и текст «record names no path»; без `after` → путь открыт; валидные как раньше; после новой записи повреждение остаётся открытым.
- Ловит: fail-open на не-dict; потерю повреждения при слиянии; утечку индекса.

### T8 — тривия и фенсы в контракте и заголовке
- Покрывает R5, AC12–AC15; K5; RR25. Уровень: unit `parse_todo_contract`, `_find_contract_block`, `commit_gate._todo_title`.
- Данные: копия front matter TODO-0266; `<!-- a` + `b -->` перед `---`; незакрытый комментарий; фенс с `# TODO-9999` и H1 после; нет и пустой H1; `---` без закрытия; H1 и контракт ниже; горизонтальная линия.
- Действие: разбор контракта и заголовка.
- Ожидания: двухстрочный комментарий → блок найден, ключи загружены; копия 0266 → блок `(0, 3)`; незакрытый комментарий → нет блока и нет ошибки; `---` без закрытия → ошибка с именем файла; `# TODO-9999` в фенсе не влияет, H1 после фенса даёт subject; нет или пустой H1 → legacy `awf(<stage>): TODO-NNNN`; контракт после H1 и горизонтальная линия как раньше (`test_unit_contract.py:40-50`).
- Ловит: тривию построчно; фенс как заголовок; потерю legacy.

### T9 — черновик, legacy-заметка и паритет флагов
- Покрывает R6, AC16–AC19; C3; K6; INV1. Уровень: unit `make_todo_draft`, `api.blocked_draft`, `cmd_dispatch.run`.
- Данные: проект с `AUDIT-INDEX.md`; выход `.agentic/drafts/x.md`; симлинк и `../out.md`; абсолютный `<project>/notes/x.md`; `BLOCKED-0042.md` и `BLOCKED-TODO-0042.md` с разными маркерами; `notes.md` в корне; чужой CWD.
- Действие: draft в каждый выход; `blocked_draft`; CLI dispatch и MCP `api.dispatch_todo` с тремя флагами.
- Ожидания: симлинк или путь вне `.agentic` → отказ до файла и родителей, `.agentic` побайтно тот же; `--force` перезаписывает обычный файл; только legacy → фикс с его находками; оба → канонический маркер; нет обоих → ошибка с двумя путями; `notes.md` найден от корня из чужого CWD; TODO, state, `source_ref`, `allow_mismatch`, `carry_over_from`, `include_untracked` совпадают с MCP; отказ не оставляет TODO, `.ready`, baseline.
- Ловит: запись до барьера; legacy не найден; CWD вместо проекта; потерян флаг.

### T10 — классификация пути метрик
- Покрывает R7, AC20–AC21; K7; D8. Уровень: unit `metrics._write_cache_managed` и `_resolve_out_path`.
- Данные: CWD = `tmp/other`, `project_dir=Path("proj")`, абсолютный лист `<tmp>/proj/.agentic/state/models.json`; симлинк `proj/cache` наружу; внешний `../external/models.json`; `out=None`.
- Действие: запись; чтение warnings и файлов; выбор пути отчёта.
- Ожидания: относительный `project_dir` и абсолютный лист внутри → файл записан, warnings пуст, барьер от корня проекта; симлинк-компонент → отказ `managed_fs`, внешний файл не создан; внешний лист → записан с предупреждением, текст содержит путь; `_resolve_out_path(None, ...)` → `~/Desktop/awf-metrics-*.md` как раньше.
- Ловит: относительный корень; молчаливую внешнюю запись; ослабленный барьер.

### T11 — паритет `pipeline-write --variant` и код возврата
- Покрывает R8, AC22–AC23; C4; K8; D3; INV3. Уровень: integration CLI (`run_awf`) + unit `write_variant_pipeline`, `pipeline_variant_yaml_text`.
- Данные: `api.init_project`; вариант `full`; имя `default`; занятое `dup.yaml`; неизвестный `nope`; каталог pipelines только для чтения; роль без файла.
- Действие: CLI `pipeline-write default --variant full`, `--help`, `--force`; форма `write_pipeline(team, flow_variant="full")`; снимок `.agentic`.
- Ожидания: rc 0, файл равен `pipeline_variant_yaml_text("full", name="default")`, `load_stages` валиден; роли, `config.yaml`, `supervisor.md` не созданы/не изменены, diff — только pipeline; `--help` и вывод называют «роли не создаются» и маршрут `awf add-role`/форма; unresolved → `WARNING` с slug, rc 0; занятое имя без `--force` → rc 1, байты те же; ошибка записи → rc 1, файла нет; `--variant nope` → rc 2; форма материализует роли при том же тексте YAML.
- Ловит: CLI материализует; текст расходится; rc зависит от ролей.

### T12 — запись setup-варианта сохраняет конфиг
- Покрывает R10, AC26–AC27; K10; D5; INV1. Уровень: unit `awf.config_write.patch_mapping` + integration `update_config_role_mapping`.
- Данные: config с комментариями, пустыми строками, порядком ключей и блоком `models:`; обновления model/agent_name, новая роль, снятие model через `""`; flow-стиль `models: {a: {}}`; нестандартный отступ; битый YAML.
- Действие: патч; повтор; запись через API; diff.
- Ожидания: меняются только строки целевых ролей; комментарии, пустые строки и порядок прочих ключей сохранены; `yaml.safe_load` равен целевому словарю; повтор байт-в-байт; `.bak` есть; битый YAML, flow-стиль и нестандартный отступ → `AwfApiError` без перезаписи; diff — только изменённые ключи.
- Ловит: потерю комментариев; перестановку; частичную запись.

### T13 — платформа и документация совпадают с кодом
- Покрывает R3, AC6–AC7; R4, AC8–AC11; K3, K4. Уровень: статическая проверка текста, новый `tests/unit/test_carry_docs.py`.
- Данные: `README.md`, `awf/_lock.py`, `docs/design.md`, `docs/practices.md`, `USAGE.md`, `USAGE.ru.md`, `awf/api/roles.py`, `awf/api/_templates.py`, `docs/RELEASE-NOTES.md`; `git diff --name-only` IMP5.
- Действие: чтение и поиск строк; список изменённых файлов.
- Ожидания: README без `Windows (untested)` и обещания запуска; докстринг `_lock.py` содержит `msvcrt`, `locked()`, `dead`/`reserve`, без `kept for Windows support`; `design.md` ссылается на `design/agentic-writers.md`, без `_transact.py`, про отказ не-POSIX; `practices.md` без обещания симлинка `templates/`; оба USAGE: код `5` в `config-get`, `AWF_SUITE_CMD`, `AWF_PYTHON`; docstring `add_role` называет пять шаблонов; `awf_templates_dir` без `every copy`/`все копии`; `RELEASE-NOTES.md` — `1 → 2`/`1 to 2` и `default 2`, без `default 1`; diff IMP5 не трогает `dashboard.py`, `phase.py`, `doctrine.py`.
- Ловит: ложное обещание; потерянный код 5; «все копии».

### T14 — регрессия, ворота и инварианты
- Покрывает INV1–INV7; A1; C1–C7. Уровень: измерение и ворота.
- Ожидания: T1–T13 в названных файлах; каждый IMP имеет `prove_red` (новые символы — `prove_red_kind: new-api`), красная фаза — падение утверждений, не импорт; `bash scripts/run-all.sh` rc 0; `check-contracts.sh` ok; `instruction-gates.sh` в бюджете; `grep -n "pid > 1" awf/_proc.py` и `grep -n "_forbid_session_kill" tests/conftest.py` находят строки, tripwire зелёный; INV7 — `test_worker_exit.py`, `test_agent_stage_exit_code.py`, `test_miniwave_m2_silent.py`, `test_git_utils.py`, `test_verify_pack.py`, `test_spec_e2e.py` зелёные; коммит — только diff юнита.
- Ловит: ложную красную фазу; ослабленный guard; рост бюджета; регрессию закрытых C.

## 4. Условия приёмки реализации

- T1–T13 зелёные; T14 пройден; каждый IMP-юнит имеет красный `prove_red` на `bdd1074` и зелёный после.
- Отказы T5–T7, T9, T11, T12 не оставляют файла, каталога, `.ready`, baseline и `.bak`; текст называет путь, запись или роль; повтор детерминирован.
- `run-all.sh` зелёный; бюджеты инструкций и пакета не выросли; канарейки целы; коммит — только diff юнита.
- Старые проекты идут прежним путём; handoff без «Engine facts» читается.
- Успех — по запущенным командам, не по чтению кода.

## 5. Находки

- QA1 MINOR — AC30 (`carry-cd.md:156`) называет stage-log, review-4 (RR23) и K12 — лог воркера от `log_start_offset`; T2 проверяет лог воркера. Тест на `orchestrator.log` был бы вечно красным. Владелец — аналитик; OPEN.
- QA2 MINOR — AC33–AC34 без критерия на легаси-фолбэк (RR24); T3/T4 покрывают по A5/K14. Владелец — аналитик; OPEN.
- QA3 SUGGESTION — RR25: fixture R5 — копия front matter, не путь `inbox/TODO-0266.md`; T8 так и делает. Владелец — тест-спека; CLOSED.
- QA4 CLOSED — FR9: T6 проверяет отсутствие `RUN-EVIDENCE`, `VERIFIED`, сигнала и удаление stale `CANDIDATE`. Владелец — тест-спека.

BLOCKER и MAJOR нет: все AC#/INV# покрыты, ожидания выведены из R#/AC# и K1–K14. QA1 и QA2 не меняют проверки; решает readiness.
