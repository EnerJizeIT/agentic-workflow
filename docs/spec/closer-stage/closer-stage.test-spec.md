# Тестовая спецификация: closer-стадия Definition-of-done

Scope: closer-stage
Ревизия: qa-1
Входы: `closer-stage.md@req-4` (REVIEW); `closer-stage.requirements-review.md@review-4` (PASS); `closer-stage.architecture.md@arch-2` (REVIEW); `closer-stage.context.md@ctx-1`; TODO-0265; протокол `specification-pipeline.md`
База кода: commit `258f28a43f91663011c3a88967f15a2dd894d4b8`; в дереве — untracked `docs/spec/closer-stage/`, `M awf/budget.py`, `M tests/negative/test_unit_budget.py` (RR24, вне scope)
Статус: REVIEW

Проверки спроектированы, не пройдены: closer-кода нет. Ожидания выведены из R#/AC#/INV# и K1–K7; разночтения — в разделе 5.

## 1. Границы и среда

Покрыты R1–R8, AC1–AC28, INV1–INV6, C1–C6; R9 и AC22–AC23 — статическая проверка готовности (T10), не поведение. T1–T7 — новый файл `tests/negative/test_dod_closer_stage.py`; T8 — он же плюс `tests/unit/test_api_setup.py`; T9 — новый файл и существующие тесты; T10 — readiness. Запуск: `pytest tests/negative/test_dod_closer_stage.py tests/unit/test_api_setup.py -q`.

Живой пайплайн фикстурой не служит. Тесты строят проект в `tmp_path`: `.agentic/`, TODO с блоком `## Чек-лист завершения` и пробами `file_exists`, контракт с `wave`, `verify`, `gates`, `prove_red`. Гейт — настоящий `awf.checklist_gate.run`, движок — in-process; замены — воркер, лог и сигнал. Фикстуры: `tmp_git_repo`, `empty_project`, `api.init_project`; помощник `make_closer_project` создаёт TODO, конфиг и волну. Подменяются `_run_agent_stage`, `_log`, `wait_for_signal`; состояние читается из файлов.

Команды:
- Проверены на базе: `pytest tests/negative/test_dod_checklist_core.py tests/unit/test_pipeline.py tests/unit/test_pipelines_api.py -q` — 177 passed, 26.78 с; `pytest tests/unit/test_pipeline_engine.py tests/unit/test_run.py -q` — 133 passed, 13.39 с; `bash scripts/instruction-gates.sh` — 948/950 и 250/250.
- Не запускались: полный сьют и `bash scripts/run-all.sh` (бюджет стадии); `awf spec check docs/spec/closer-stage` сейчас NOT READY — нет test-spec и readiness.
- Планируемые: T1–T9 (IMP1–IMP7), T10 (readiness). Ни один не создан.

## 2. Матрица покрытия

| ID | Сценарий/ветка | T# | IMP# |
|---|---|---|---|
| R1 | C6, снапшоты | T1 | IMP1 |
| R2 | C1, C2 | T2, T3 | IMP2 |
| R3 | C1, C2 | T3, T4 | IMP3 |
| R4 | C3 | T5 | IMP4 |
| R5 | C4 | T6 | IMP5 |
| R6 | C5 | T7 | IMP6 |
| R7 | C6 | T8 | IMP7 |
| R8 | C1–C6 | T9 | IMP7 |
| R9 | план | T10 | план |
| AC1–AC3 | `skip_if`, снапшоты | T1, T8 | IMP1 |
| AC4–AC6 | `dod:`, пропуск | T2, T3 | IMP2 |
| AC7–AC9 | стадия | T4 | IMP3 |
| AC10, AC11, AC24 | бюджет | T5 | IMP4 |
| AC12–AC14 | контракт | T6 | IMP5 |
| AC15–AC17, AC28 | статистика | T7 | IMP6 |
| AC18–AC21 | поставка | T8, T9 | IMP7 |
| AC22, AC23 | план | T10 | readiness |
| AC25, AC26 | первый вердикт | T2, T3 | IMP2 |
| AC27 | дефект стадии | T3 | IMP3 |
| INV1 | кэш | T2, T9 | IMP2 |
| INV2 | пробы | T9 | IMP7 |
| INV3 | бюджет | T5, T9 | IMP4 |
| INV4, INV5 | канарейки | T9 | IMP7 |
| INV6 | старые файлы | T8, T9 | IMP7 |

## 3. Проверки

### T1 — `skip_if` в схеме, загрузчике, записи и обоих снапшотах
- Покрывает R1, AC1–AC2; C6; K1. Уровень: unit загрузчика и записи.
- Данные: YAML `plan → worker → verify`; worker с `skip_if: checklist_green`, без ключа, с `checklist-grenn`, `""`, `1`, `true`, `null`; стадия с `output`, `write_allow`, `write_check: "off"`.
- Действие: `load_stages`, `validate_pipeline_stages`, `api.write_pipeline`, `pipeline_snapshot_text` → `load_stages`, `awf.api.run._write_service_pipeline` от источника с двумя воркерами.
- Ожидания: ключ читается в `Stage.skip_if`; оба снапшота после повторной загрузки несут `checklist_green`; нет ключа или `null` — `skip_if == ""`, стадия идёт всегда без вызова гейта; неизвестное значение, пустая строка, число и `bool` — `AwfApiError` с именем стадии и значением до TODO, `.ready`, baseline и файла; непустой `write_allow` сохраняется, пустой не пишется; service-run заменяет `output` первого воркера и снимает его `write_allow`, но сохраняет `skip_if` и поля второго.
- Ловит: ключ, потерянный в одном сборщике; опечатку как «всегда идти»; чужой allowlist.

### T2 — точка решения: вердикт, `dod:`, пропуск, первый вердикт
- Покрывает R2, AC4–AC6, AC25, AC26; C1, C2; K2, K3. Уровень: unit `awf.closer_stage.decide` с настоящим гейтом.
- Данные: TODO-0001 — три пункта `file_exists`: `unit-01` и `unit-03` проходят, `unit-02` нет, контракт `wave: w1`; TODO-0001b — два пункта: `unit-01` проходит, `unit-02` нет, плюс `manual unit-03`; TODO-0002 — три пункта, все проходят; TODO-0003 — блока нет; TODO-0004 — заголовок без пунктов; TODO-0005 — нечитаемая строка; TODO-0006 — только `manual`.
- Действие: `decide`; второй вызов после правки цели пробы и перезаписи `CHECKLIST`; до первого `decide` — `cmd_checklist check` и `verify_pack`.
- Ожидания: красный — `kind=run`, `dod: 2/3 (missing: unit-02)`; с manual — `dod: 1/3 (missing: unit-02) (manual: unit-03)`; зелёный — `skip`, `dod: 3/3`; нет блока — `skip`, `dod: none`; пустой — `blocked`, `dod: 0/0 (missing: (empty block))`, «починить блок», без спавна и траты; нечитаемый — `blocked`, `missing` называет пункт или `(unreadable block)`; только manual — `skip`; `CLOSER-TODO-0001.json` пишется на первом входе, включая пропуск, с `wave: w1` и `first_verdict`; второй вход `first_verdict` не перезаписывает; тот же вход — `cached=true`, изменённая проба — `cached=false`; `check` и verify-pack `CLOSER` не создают.
- Ловит: неверный `dod:`; manual запускает closer; нет блока = красный; история затирается.

### T3 — вход стадии: пропуск без спавна и статические дефекты
- Покрывает R2 AC5/AC6/AC26, R3 AC27; C1, C2 1a; K2. Уровень: integration `pipeline_engine.execute_agent_stage`.
- Данные: `plan → checklist-close → verify`; зелёный и красный TODO; closer с `skip_if` и `write_check: on`; конфиг `closer_attempts: 3`; старый пайплайн без ключа.
- Действие: вызов с подменёнными `_run_agent_stage` (счётчик), `_ensure_baseline_sha`, `wait_for_signal`, `_log`; чтение `pipeline_state`.
- Ожидания: зелёный — 0 спавнов, PID нет, `stage_skipped=true` и причина в state, лог `SKIPPED (checklist_green)`, переход к verify; следующий вход сбрасывает поля. Дефект стадии — `checklist_gate.run` и спавн не вызываются, `BLOCKED-TODO-0001.md` называет стадию, `skip_if` и `write_check`, бюджет 0, rc 1. Неверный `closer_attempts` — тот же отказ с именем ключа. Старая стадия без ключа — один спавн, гейт не вызывается.
- Ловит: пропуск со спавном; нет следа в state; closer запускается; регрессию старого пайплайна.

### T4 — стадия, промпт и позиции
- Покрывает R3 AC7, AC8, AC9; R7 AC18 частично; C1, C2; K6. Уровень: unit шаблонов и локального файла.
- Данные: `.agentic/pipelines/default.yaml`, `awf/templates/pipelines/full.yaml`, `code-health.yaml`; синтетическая стадия с `output`/`write_allow`/`write_check`.
- Действие: `load_stages`; поиск `checklist-close` и `checklist-check`; `agent_stage._stage_assignment_block`; `pipeline_snapshot_text` → `load_stages`; service-run снапшот.
- Ожидания: `default`/`full` — `plan`, `agent-implementer`, `agent-architect-review`, `agent-qa-review`, `checklist-close`, `checklist-check`, `agent-project-auditor`, `verify`; `code-health` — `plan`, `agent-architect-review`, `agent-refactoring-specialist`, `checklist-close`, `checklist-check`, `verify`; у `checklist-close` `role=agent-implementer`, `session_key=agent-implementer`, `skip_if=checklist_green`, `write_check="off"` строкой, `task` содержит «Закрой ТОЛЬКО пункты», «`manual` вне scope», «блок не править», «`awf checklist check`», «отчитайся пунктами»; у `checklist-check` `role=agent-qa-review`, `session_key=agent-qa-review`, `skip_if=checklist_close_skipped`, `task` содержит «Проверь ТОЛЬКО дифф checklist-close»; обе `kind=execute`, не первые и не последние, `output` и `write_allow` пусты; промпт содержит задачу; per-TODO снапшот сохраняет объявленные поля и не пишет пустой `write_allow`.
- Ловит: closer первой/последней; супервизорский промпт; потерянный `write_check`; ложный allowlist.

### T5 — бюджет завершённых прогонов
- Покрывает R4 AC10, AC11, AC24; C3; K4. Уровень: unit `decide` для конфига, integration движка для цикла.
- Данные: `closer_attempts` отсутствует, 1, 2; 0, 3, -1, `"2"`, 2.5, `true`, `""`, `null`; красный TODO, где closer пишет DONE и не меняет пробу; версия, где проба зеленеет; спавн без сигнала.
- Действие: `decide` с каждым значением; `execute_agent_stage` с пределом 1 и 2; падение, ретрай и salvage.
- Ожидания: без ключа — 1; 1 и 2 приняты; остальные — `blocked` с именем `checklist.closer_attempts` и значением, без спавна и записи `attempts`; предел 1: один спавн, `attempts=1`, `BLOCKED-TODO-0001.md` с `missing` и «решение супервизора», rc 1, третий запуск и `continue` не спавнят; предел 2: ровно два спавна, после второго BLOCKED, третий невозможен; зелёный после closer — `attempts=1`, переход к verify, `first_verdict` не меняется, `dod:` один; падение без сигнала — `attempts` не меняется, ретрай и salvage не тратят; завершённый прогон с `DONE`, `APPROVED` или `BLOCKED` — один расход; пропуск и статические дефекты — 0.
- Ловит: счёт спавнов вместо прогонов; обнуление; третий closer; падение списано.

### T6 — неизменяемость блока и контракта
- Покрывает R5 AC12, AC13, AC14; C4; K5. Уровень: unit `decide` + integration движка.
- Данные: TODO с блоком (один `missing`); два снимка `before` после первого красного решения — контракт A `verify`, `gates`, `prove_red`, `flake`, `files` и контракт B `verify`, `gates`, `prove_red_pin`, `flake`, `files` (`prove_red` и `prove_red_pin` взаимоисключимы).
- Действие: closer завершается и правит блок (отметка или текст), `verify`, `gates`, `prove_red` (в A), `prove_red_pin` (в B), `flake` или только `files`; повторный вход после рестарта; нечитаемый контракт.
- Ожидания: правка блока или любого замороженного ключа (`verify`, `gates`, `prove_red`, `prove_red_kind`, `prove_red_note`, `prove_red_cmd`, `prove_red_pin` в B, `flake`, `xfail_allow`/`skip_allow`) — `blocked` с `closer edited the checklist/contract` и именем блока, даже если пункты закрыты; правка остаётся, коммита нет; только `files` — обычное завершение; нет контракта — сравнение контракта не падает, правка блока ловится; нечитаемый контракт — тот же BLOCKED; `before` после рестарта не меняется, сравнение с исходным снимком.
- Ловит: закрытые пункты отменяют отказ; `files` заморожен; снимок перезаписан.

### T7 — статистика `awf checklist stats`
- Покрывает R6 AC15–AC17, AC28; C5; K3, K7. Уровень: CLI-действие `awf.cmd_checklist.run` и парсер.
- Данные: A — `unit-01` красный на первом вердикте, затем зелёный; B — `unit-02` сразу зелёный; C — только `manual`; D — тот же `unit-01`, сразу зелёный; волны `w-a`, `w-b` и юнит без волны; пустой проект.
- Действие: `stats`, `stats --json`, `stats --wave w-a`, повторный `check` и verify-pack, переименование файла волны после диспатча.
- Ожидания: заголовок ровно `| Пункт | caught | runs |` и разделитель; A — `caught=1`, `runs=1`; B — `caught=0`, `runs=1`; D даёт `unit-01` `caught=1`, `runs=2`; manual-строки нет; пустой набор или фильтр — только заголовок, rc 0, нулей нет; `--json` — те же строки объектами (`item`, `caught`, `runs`, QA1); после смены дерева и `CHECKLIST` значения не меняются; `check` и verify-pack `CLOSER` не пишут; `--wave` берёт замороженное поле, юнит без волны не попадает, переименование файла волны не влияет.
- Ловит: последний вердикт вместо первого; manual в отчёте; нули без данных; волну из живого файла.

### T8 — поставка, совместимость и документ
- Покрывает R7 AC18, AC19; R1 AC3; INV6; C6; K1, K6. Уровень: unit генератора и шаблонов; новый файл и `tests/unit/test_api_setup.py`.
- Данные: команда по умолчанию (implementer, architect-review, qa-review, project-auditor); `full.yaml`; `code-health.yaml`; локальный `.agentic/pipelines/default.yaml`; старый пайплайн; temp-проект `api.init_project`.
- Действие: `build_pipeline_stages`, `pipeline_yaml_text`, `api.write_pipeline`; загрузка шаблонов; `init_project` и повторный; `git diff` старого файла.
- Ожидания: новый default содержит пару `checklist-close` + `checklist-check` после `qa-review` и до `agent-project-auditor`, обе загружаются как `kind=execute`; `full` и `code-health` несут пару на местах; `init` не пишет `.agentic/pipelines/*` и не меняет байты файла; старый пайплайн не переписывается, грузится с `skip_if == ""` и идёт без гейта; `docs/unit-contract.md` описывает `skip_if`, `checklist.closer_attempts`, `awf checklist stats` и `CLOSER-<todo>.json`; бюджет пакета 2200/11000 не превышен (`check_budgets`), итоговый `awf spec check` на READY — rc 0.
- Ловит: генератор без closer; init пишет пайплайны; старый файл переписан; документ без ключа.

### T9 — регрессия, `prove_red`, ворота и инварианты
- Покрывает R8 AC20, AC21; INV1–INV5; C1–C6; K1–K7. Уровень: измерение и ворота.
- Ожидания: новый файл содержит T1–T8; `prove_red` на базе `258f28a` красный, после — зелёный. Красная фаза — падение утверждений, не `ModuleNotFoundError`: первый IMP создаёт скелет; для новых символов — `prove_red_kind: new-api` с `prove_red_note`. Имена-минимум: `test_skip_if_round_trips_through_both_snapshots`, `test_unknown_skip_if_value_refused_before_side_effects`, `test_decision_writes_one_dod_line_and_skips_without_spawn`, `test_static_block_defect_blocks_without_spawn_or_budget`, `test_closer_stage_position_and_prompt`, `test_budget_counts_completed_runs_not_spawns`, `test_stats_first_verdict_and_wave_filter`, `test_generator_and_templates_carry_closer`.
- `bash scripts/run-all.sh` — rc 0; `scripts/instruction-gates.sh` — 948/950 и 250/250; `grep -n "pid > 1" awf/_proc.py` и `grep -n "_forbid_session_kill" tests/conftest.py` находят строки; полный сьют зелёный; INV1 — тесты кэша `test_dod_checklist_core.py`; INV2 — тесты проб; INV3 — переходы, ретраи, salvage, BLOCKED, write-манифест; INV4 — канарейки и коммит только diff юнита, без `.agentic/`; INV5 — бюджет инструкций.
- Ловит: ложную красную фазу из-за импорта; рост бюджета; ослабленный guard; `.agentic/` в коммите.

### T10 — план IMP (готовность)
- Покрывает R9 AC22, AC23; раздел 11 `arch-2`. Уровень: статическая проверка readiness, не тест.
- Ожидания: 7 IMP; каждый ≤6 R#; требование не дублируется; у каждого `verify` и `prove_red` либо названный метод; порядок «схема → вердикт → стадия → бюджет/неизменяемость → статистика → поставка/тесты».

## 4. Условия приёмки реализации

- T1–T9 зелёные; T10 записан; новый файл содержит случаи R8; `prove_red` каждого IMP-юнита красный на базе и зелёный после.
- Отказы T1–T3, T5, T6 не оставляют TODO, `.ready`, baseline, PID и стадии; сообщение называет стадию, ключ или пункт; повтор детерминирован.
- `bash scripts/run-all.sh` зелёный; бюджеты инструкций и пакета не выросли; канарейки целы; коммит содержит только diff юнита, `.agentic/` не коммитится.
- Старые пайплайны и юниты без блока идут прежним путём; `default`/`full`/`code-health` не теряют порядок.
- Успех не заявляется по чтению кода: каждая команда запускается, результат фиксируется. Тесты спроектированы, не пройдены.

## 5. Находки

- QA1 MINOR — AC16 (`closer-stage.md:101`): имена ключей `--json` не заданы. T7 предполагает `item`, `caught`, `runs`; архитектору подтвердить или назвать другие. Последствие: тест фиксирует неоговорённые имена, смена ключей ломает проверку. Владелец — архитектор; OPEN.
- QA2 MINOR — AC8 (`:64`) против K1 (`architecture.md:28`): AC8 требует сохранять объявленные поля в обоих снапшотах, K1 снимает `write_allow` у воркера service-run при замене `output`. T1/T4 проверяют исключение K1; аналитику назвать его в AC8. Последствие: две трактовки AC8 расходятся на service-run. Владелец — аналитик; OPEN.
- QA3 MINOR — AC27 (`:66`) против K2 (`architecture.md:43`): AC27 относит статический дефект к closer, K2 — к любой стадии с `skip_if`. T3 проверяет closer; прочие стадии не расширяются без решения. Последствие: неясно, блокировать ли любую стадию с `skip_if`. Владелец — аналитик; OPEN.
- QA4 SUGGESTION — AC4 (`:49`): точный формат `dod:` для нечитаемого блока не задан (задан только для пустого). T2 требует `blocked` и маркер в `missing`, не фиксирует N/M. Последствие: точный `dod:` для нечитаемого блока не проверяется. Владелец — аналитик; OPEN.

BLOCKER и MAJOR нет: все AC# и INV# покрыты, ожидания выведены из требований и K1–K7. QA1–QA3 не меняют проверки; readiness решает, закрывать их или принять.
