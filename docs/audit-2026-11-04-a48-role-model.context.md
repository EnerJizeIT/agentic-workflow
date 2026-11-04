# Аудит A48: роль без модели не уходит на чужую модель

Scope: TODO-0370 (A48) — разрешение модели роли
(`models.<role>.model` → `models.worker.model` → отказ), обязательный
`--model` при спавне, строка и WARN в `awf doctor`. Широкий срез вокруг
изменения: переносимость, стыки с соседними юнитами, шероховатости.
Ревизия: ctx-3 (ctx-1 и ctx-2 сохранены ниже)
База кода: HEAD `6fa4f36` + рабочее дерево юнита `59e798b`: 52 изменённых
отслеживаемых файла и 5 новых тестовых; правки A47 в `opencode_db.py`,
`__main__.py` — тоже в дереве.
Статус: REVIEW — правило проверено на дереве и пробами; полный сьют
подтверждён артефактом QA на том же tree-sha, сам не перезапускал (см.
«Ограничения проверки (ctx-3)»).

## Краткий итог

Правило одно и то же на всех поверхностях: движок, чип дашборда и
`check_model_config` берут модель из `supervisor.get_role_model`, второй
копии правила в дереве нет. `--model` строится ровно в двух местах, оба
прошли через `require_role_model`; процесса без флага не появляется.

Главное, чего не увидели прошлые стадии: **отказ наследует `RuntimeError`**,
поэтому на четырёх супервизорских путях он попадает в ветки «crash», а на
rollback-replan движок считает его транзиентным сбоем и продолжает запуск
(`rc 0`). Формально безопасно — следующая стадия всё равно упрётся в тот же
отказ, — но сообщения противоречат решению «отказ, а не падение».

Остальное — гигиена вокруг: `doctor` проверяет ключи `models:`, а не роли
пайплайна; свежий `awf init` даёт проект, который отказывает на первом
спавне; документация и текст role-файла описывают старое «действует модель
по умолчанию».

## Карта затронутых путей

- конфиг → `config.get(config, "models.<role>.model")` →
  `supervisor.get_role_model` (`supervisor.py:79`) →
  `supervisor.require_role_model` (`supervisor.py:93`).
- спавн агента: `agent_stage.run_agent_stage` (`agent_stage.py:111` —
  проверка, `:264` — флаг) → `_net.parse_model_from_cmd` (preflight по
  `baseURL` провайдера).
- спавн супервизора: `supervisor.run_supervisor_via_subprocess`
  (`supervisor.py:1624`, `:1639`), вызывается из пяти мест
  `pipeline_engine` (plan, replan, escalation, rollback, salvage).
- чтение для UI: `api/dashboard._active_stage_model` (`dashboard.py:1049`).
- проверка конфига: `api/model_check.check_model_config`
  (`model_check.py:154-199`) → `doctor._check_models`
  (`doctor.py:342-399`).
- запись в конфиг: `api/roles.add_role` / `apply_project_setup`
  (`roles.py:1076`, `:1017-1045`) → `config_write` (surgical).

## Переиспользованные контракты и соглашения

- **Один резолвер.** `dashboard.py:1049` и `model_check.py:138` импортируют
  `get_role_model` из `supervisor`, а не повторяют приоритет. Это и есть
  анти-дрейф: смена правила не требует правки трёх мест.
- **Структурный ключ вместо парсинга текста.** `missing_model_roles` в
  результате `check_model_config` — doctor не разбирает формулировки
  warning'ов (`model_check.py:131-134`, `doctor.py:352-357`).
- **Шов «отказ до спавна»** перенесён после решения «стадию пропустить»
  (`supervisor.py:1617-1624`), чтобы salvage/неизвестный kind остались
  пропуском (QA-F1).
- **Типизированные остановки уже есть в проекте**: `_unsafe_stage_stop`
  (`pipeline_engine.py:1905`) и BLOCKED-ветка (`:2558-2566`) пишут запись
  стадии и salvage-состояние. Новый отказ этим не пользуется (CTX8).

## Поверхность × исход

| Поверхность | Что делает A48 | Подтверждение |
|---|---|---|
| agent-стадия | `require_role_model` до чистки и спавна; `--model` безусловно | `agent_stage.py:111`, `:264`; `test_agent_spawn_*` |
| plan / replan / verify / salvage | проверка после «пропустить», перед сборкой `cmd` | `supervisor.py:1624`, `:1639`; `test_supervisor_spawn_*` |
| обработчики отказа в движке | типизированная ветка в agent-стадии и в `execute_supervisor_stage`; четыре прямых вызова `_run_supervisor_stage` её не имеют (CTX1) | `pipeline_engine.py:2768`, `:1630`, `:723`, `:1092`, `:1701`, `:3068` |
| 4 супервизорских вызова | отказ приходит в ветку `(RuntimeError, TimeoutError)` | `:1626`, `:1697`, `:3051`; rollback `:1093` |
| чип модели в дашборде | тот же резолвер, наследование видно | `dashboard.py:1049`; `test_reports31_*` |
| `check_model_config` | наследование, `missing_model_roles`, `cli_models_available` | `model_check.py:154-199`; `test_model_check.py` |
| `awf doctor` | строка «роль X: модель Y», WARN без модели | `doctor.py:370-388`; живой прогон ниже |
| роль-файл и доки | не обновлены под новое правило | `roles.py:52`; `USAGE.ru.md:885-889`; `USAGE.md:660-664` |

## Baseline

| Проверка | Команда (cwd — корень проекта) | Результат |
|---|---|---|
| целевые тесты юнита | `python -m pytest tests/unit/test_role_model_required.py -q --timeout=300 -p no:cacheprovider` | PASS — 14 passed |
| модели/doctor/проба | `... tests/unit/test_model_check.py test_doctor.py test_opencode_db.py test_models_probe_cold_start.py test_doctor_audit_fixes.py -q` | PASS — 101 passed |
| линт | `ruff check .` | PASS — All checks passed |
| живой doctor | `python -m awf doctor --project-dir .` | rc=0; 9 строк «роль X: модель opencode-go/deepseek-v4.1-flash», WARN моделей нет |
| пробы аудита | `python /tmp/opencode/probe_audit_0370.py` | 6/6 PASS (подтверждают CTX1–CTX4, QA-F5, QA-F6) |
| полный сьют | — | NOT_RUN: артефакт QA `SUITE-2d80dfb…-4ea28cc4.json` (7011 passed) относится к дереву 2d80dfb; побайтного совпадения с текущим деревом не проверял |

## Findings

| ID | Важность | Файл / символ | Факт и доказательство | Влияние | Следующий шаг | Уверенность |
|---|---|---|---|---|---|---|
| CTX1 | MINOR | `supervisor.RoleModelMissingError`; `pipeline_engine.py:1093-1115`, `:1626`, `:1697`, `:3051` | Класс наследует `RuntimeError` (проба p2), поэтому четыре супервизорских обработчика ловят его как падение: «supervisor stage '…' crashed», «replan after REVIEW failed», «salvage crashed». На rollback-replan ветка `except (RuntimeError, TimeoutError)` пишет `replan_error`, PROGRESS-заметку и возвращает `rc 0` — запуск идёт дальше | Одна конфигурационная ошибка даёт четыре разных сообщения; на rollback-пути неисправимый отказ трактуется как транзиентный. Ложного успеха нет: следующая стадия упрётся в тот же отказ | implementer: ловить `RoleModelMissingError` выше `RuntimeError` в этих четырёх местах (или отдельный базовый класс) | высокая |
| CTX2 | MINOR | `api/model_check.check_model_config` | Проверяются только ключи блока `models:`. Роль, названная в пайплайне, но отсутствующая в `models:`, не попадает ни в `missing_model_roles`, ни в WARN, хотя движок её отвергнет (проба p1: `missing_model_roles=[]`, `require_role_model` бросает). Уточняет и расширяет открытый QA-F5 | Приёмочный пункт «WARN до запуска, а не в момент падения» выполнен не для всех ролей | implementer: обойти роли стадий пайплайна через `get_role_model` (одна правка закрывает CTX2 и QA-F4/F5) | высокая |
| CTX3 | MINOR | `api/_templates._CONFIG_TEMPLATE`; `api/roles.apply_project_setup` | Свежий `awf init` пишет `models.supervisor` только с `description`, `models.worker.model` нет → первый спавн отказывает (проба p3). Форма setup сохраняет роль без модели, если в списке нет ни одной модели или пользователь её не выбрал (`forms.py:574-600`, `roles.py:1017-1021`) | Продукт не приходит в рабочее состояние без ручной правки конфига; сообщение об отказе хорошее, но пути к успеху в тексте нет | analyst/architect: решить, спрашивает ли init модель, или setup-форма обязана её требовать | высокая для шаблона, средняя для формы |
| CTX4 | MINOR | `doctor._check_models` (`doctor.py:366-379`) | Ветка «opencode не готов» возвращается до отчёта о `problem`: при холодном CLI и пустом списке провайдеров нечитаемый `opencode.json` не показывается (проба p5: в строках только WARN о модели и «модели не определены») | Doctor может скрыть сломанный конфиг за нейтральной строкой | implementer: печатать `problem` из этой ветки | высокая |
| CTX5 | MINOR | `USAGE.ru.md:885-889`, `USAGE.md:660-664`, `roles.py:52` | Текст говорит: модель не назначена → «действует модель конфига по умолчанию». После A48 наследуется `models.worker.model`, а без неё запуск отказывает. Новый приоритет и отказ не описаны ни в одном доке | Оператор не может узнать правило из документации; текст role-файла прямо вводит в заблуждение | implementer/docs: обновить оба USAGE и `UNASSIGNED_MODEL_TEXT` | высокая |
| CTX6 | SUGGESTION | `supervisor.py:100-107`, `model_check.py:192-197`, `doctor.py:370-388` | Один и тот же совет продублирован трижды, формулировки разные: английская в `model_check`, русская в doctor/supervisor; doctor для не-worker роли не упоминает `models.worker.model`. Живой doctor печатает 10 строк models на 9 ролей | Дрейф формулировок, шум в выводе | implementer: один текст-источник, одна строка на роль + итог | высокая |
| CTX7 | SUGGESTION | `opencode_db.py:48-58` | `AWF_DEBUG` (A47) включает диагностику пробы моделей, но не описан в доках (grep по `*.md` — только тесты и DONE A47) | Поддержка не знает, как включить диагностику холодного старта | docs: описать переменную рядом с другими `AWF_*` | высокая |
| CTX8 | SUGGESTION | `pipeline_engine.py:2742-2748` | Ветка отказа не пишет структурной причины: ни `last_signal`, ни записи стадии (`_unsafe_stage_stop`), ни salvage-состояния; `ENGINE-EXIT` пишется через `finish(rc)` с пустым `reason` | `awf status` и дашборд показывают причину только в логе | implementer: переиспользовать `_unsafe_stage_stop` или записать `last_signal`/причину | средняя |
| CTX9 | SUGGESTION | `tests/conftest.py:279-292` | Фикстура вставляет модель через `assert "\nmodels:\n" in text` и строковый replace шаблона | Правка шаблона ломает ~20 e2e-тестов непонятным `AssertionError` вместо внятной причины | implementer: править YAML структурно | высокая |

## Проверка открытых пунктов QA

- QA-F4 (нет предполётной проверки в `awf start`) — **подтверждён**:
  `grep -n model awf/api/run.py awf/cmd_start.py` даёт только импорт
  `git_model`; проверки ролей нет.
- QA-F5 (doctor зелёный там, где спавн невозможен) — **подтверждён и
  расширен** пробой p1: вариант с частичным `models:` без worker-модели.
- QA-F6 (ложный WARN о дрейфе модели role-файла) — **подтверждён** пробой
  p6: `role_files=[{file_model: 'p/x', config_model: None, match: False}]`
  при `models.worker.model = p/x`.
- QA-F7 (WARN и ok в одной категории) — **подтверждён** пробой p5: в
  выводе рядом `WARN models: …` и `ok models: …`.
- QA-F1 (пропускаемая стадия не превращается в отказ) — закрыт:
  `test_skipped_supervisor_stage_still_skips_without_a_model` зелёный.

## Ограничения проверки

- Полный сьют не перезапускал (политика: артефакт QA на том же дереве);
  побайтного совпадения текущего дерева с `2d80dfb…` не проверял, поэтому
  «проект чист» не утверждаю — охват: модели, doctor, спавн, доки.
- Ручную replan-сессию живьём не смотрел (дорого, внешняя сессия).
  Косвенно: эта стадия идёт на `opencode-go/deepseek-v4.1-flash` (факт
  handoff), то есть спавн с явным `--model` работает.
- Правки A47 (`opencode_db.py`, `__main__.py`, `dashboard.py`) смотрел
  только там, где они касаются моделей.
- Расхождение `stage_id` в `DONE-TODO-0370.json` (там `agent-implementer`
  на стадиях ревью) — вопрос владения файлом по `docs/unit-contract.md`,
  не дефект A48; в findings не включаю.

## Ревизия ctx-2 (после REVIEW, рефакторинга архитектора и QA)

Проверка на текущем дереве (`17afddf` по артефакту QA, HEAD `6fa4f36`).
Ниже — что изменилось после ctx-1 и новые находки. Нумерация CTX
сохранена, чтобы ссылки в handoff не поехали; новые — CTX10+.

### Что закрыто после ctx-1

| ID | Статус | Подтверждение |
|---|---|---|
| CTX2 | закрыт | `model_check.py:190-201`: проверяемое множество — ключи `models:` ∪ роли стадий пайплайна. Проба p7: роль только из пайплайна попала в `missing_model_roles` и в `warnings` |
| CTX6 | частично закрыт | Отказ при спавне и doctor печатают одну фразу (`supervisor.role_model_hint` → `doctor._no_model_line`). `model_check.warnings` остался со своей английской формулировкой — см. ниже |
| QA-F1 | закрыт | `supervisor.py:1617-1624`: решение «стадия пропускается» стоит до `require_role_model` |
| QA-F5 | закрыт | проба p7 + живой doctor (WARN до запуска, 9 строк ролей) |
| F7 архитектора | принято | `.ratchet-baseline` = 138, `sh scripts/ratchet.sh` rc=0, запись в `.ratchet-counters` на месте. Противоречие с буквой REVIEW названо честно |

### Открытые и уточнённые находки

| ID | Важность | Файл / символ | Факт и доказательство | Влияние | Следующий шаг | Уверенность |
|---|---|---|---|---|---|---|
| CTX1 | MINOR | `supervisor.RoleModelMissingError`; `pipeline_engine.py:1630`, `:723`, `:1701`, `:3068`, `:1092` | Типизированный обработчик есть только в `execute_supervisor_stage` (`:1630`) и в агентской стадии (`:2768`). Три прямых вызова `_run_supervisor_stage` (эскалация `:723`, replan после REVIEW `:1701`, salvage `:3068`) ловят отказ как `(RuntimeError, TimeoutError)` и печатают «… crashed». Rollback-replan (`:1092`) считает его транзиентным сбоем и продолжает запуск с `rc 0`. Утверждение архитектора «обработчик вызван из обеих веток» верно для функции `execute_supervisor_stage`, но не для четырёх её обходных вызовов | Один и тот же конфиг-отказ даёт четыре разных сообщения; на rollback-пути неисправимый отказ выглядит как временный. Ложного успеха нет — следующая стадия упрётся в тот же отказ | implementer: `except _RoleModelMissingError` перед generic-веткой в трёх местах; для rollback — решить, останавливаться или продолжать | высокая |
| CTX3 | MINOR | `api/_templates.py:38-43` | Шаблон `awf init` по-прежнему пишет `models.supervisor` только с `description`; `models.worker.model` нет. Свежий проект отказывает на первом спавне (план → supervisor). Это же чинит `tests/conftest.py:279-292`, вставляя модель строкой во все e2e | Первый запуск «из коробки» красный, лечится ручной правкой конфига | analyst/architect: решить, сеет ли init модель или форма setup обязана её требовать (решение продукта, не рефакторинг) | высокая |
| CTX4 | SUGGESTION | `doctor._check_models` (`doctor.py:369-380`) | Ветка «opencode не готов» (`not cli_available and not providers`) печатает WARN о ролях и `ok models: модели не определены`, но не печатает `problem` — нечитаемый `opencode.json` в этом состоянии не виден | Doctor может скрыть сломанный конфиг за нейтральной строкой | implementer: добавить `problem` в WARN этой ветки | высокая |
| CTX5 | MINOR | `api/roles.py:52`, `USAGE.md:661`, `USAGE.ru.md:886`, `.agentic/roles/supervisor.md:5` | Текст «не назначена (действует модель конфига по умолчанию)» остался прежним. После A48 наследуется `models.worker.model`, а без неё запуск отказывает. В этом репозитории `supervisor.md:5` всё ещё несёт эту фразу, хотя в конфиге модель есть; проверка дрейфа роль-файлов считает отсутствующую строку «match», поэтому doctor молчит | Оператор и сама роль читают в промпте правило, которого больше нет; это ровно тот сценарий, из-за которого случился инцидент 04.11 | implementer/docs: обновить `UNASSIGNED_MODEL_TEXT` и оба USAGE, перегенерировать строку модели в роль-файлах | высокая |
| CTX6 | SUGGESTION | `model_check.py:240-256` vs `supervisor.role_model_hint` | Английская формулировка в `warnings` («add models.X.model (or models.worker.model for the whole project)») и русская в отказе/doctor — два текста об одном. Doctor вычитает английские по членству, поэтому в CLI их не видно, а MCP `awf_check_model_config` возвращает именно их | Два разных совета на двух поверхностях; при правке одной вторая отстанет | implementer: один текст-источник для обоих потребителей | высокая |
| CTX7 | SUGGESTION | `opencode_db._debug`, `AWF_DEBUG` | Переменная включает диагностику пробы моделей (A47), но не описана ни в одном `*.md` (grep: только DONE A47 и отчёты) | Поддержка не знает, как включить диагностику холодного старта | docs: описать рядом с другими `AWF_*` в USAGE | высокая |
| CTX8 | SUGGESTION | `pipeline_engine._stop_on_missing_model` (`:2287-2305`) | Ветка пишет только `_log`; `last_signal`/запись стадии не ставится, `finish(rc=1)` пишет `ENGINE-EXIT` без `reason` | `awf status` и дашборд показывают причину только в логе | implementer: переиспользовать `_unsafe_stage_stop` или передать причину в `finish(reason=...)` | средняя |
| CTX9 | SUGGESTION | `tests/conftest.py:279-292` | Фикстура вставляет модель через `assert "\nmodels:\n" in text` + строковый replace. Шаблон и фикстура связаны текстом, не структурой | Правка шаблона ломает ~20 e2e необъяснимым `AssertionError` вместо внятной причины | implementer: править YAML структурно | высокая |

### Новые находки (ctx-2)

| ID | Важность | Файл / символ | Факт и доказательство | Влияние | Следующий шаг | Уверенность |
|---|---|---|---|---|---|---|
| CTX10 | MINOR | `api/model_check.check_model_config` (`:160`, `:190`) | `models_config = cfg_mod.get(config, "models", {})` не проверяет тип, дальше вызывается `models_config.keys()`. Конфиг `models:\n  - worker\n` (список, не отображение) даёт `AttributeError: 'list' object has no attribute 'keys'` (проба p4). Движок на том же конфиге деградирует штатно: `get_role_model` возвращает `None`, `require_role_model` отказывает с именем роли (проба p1) | MCP `awf_check_model_config` возвращает «Unexpected AttributeError» вместо отчёта; это расхождение в устойчивости двух читателей одного конфига. Doctor ловит исключение и пишет `unknown`, то есть валидатор тихо выключен | implementer: `if not isinstance(models_config, dict): models_config = {}` — одна строка; тест на форму | высокая |
| CTX11 | MINOR | `api/model_check.py:162-166`, `agent_workflow_ui/.../opencode_config.py:49-54`, `metrics.default_db_path` (`metrics.py:254`) | Путь к `opencode.db` знают три места. Два уважают `XDG_DATA_HOME`, `metrics.default_db_path()` — нет (проба p2: при `XDG_DATA_HOME=/tmp/opencode/xdg-data` model_check смотрит в новый путь, metrics — в `~/.local/share`). Это переносимость: на машине с переопределённым XDG doctor видит «недавние модели», а метрики/цены — нет | Один и тот же вопрос «где история opencode» получает разные ответы на разных поверхностях | implementer: один `xdg.xdg_data_home()` + общий `opencode_db_path()`; metrics перевести на него | высокая |
| CTX12 | SUGGESTION | `doctor.py:398-400` | Агрегатная строка WARN говорит `run awf_check_model_config`, но такой команды CLI нет: в `awf/cli.py` подкоманд с таким именем не объявлено (grep по `add_parser`), это имя MCP-инструмента | Оператор в терминале получает совет запустить то, чего у него нет | implementer: либо подкоманда `awf check-model-config`, либо текст «run awf doctor» | высокая |
| CTX13 | SUGGESTION | `api/dashboard._active_stage_model` (`:1049-1052`) | Резолвер вызывается со слагом стадии, когда имя не нашлось в списке стадий (докстринг это разрешает). С новым наследованием неизвестный слаг больше не даёт «—»: проба p5 при `supervisor=p/super`, `worker=p/work` и `stage_name="replan"` вернула `p/work`, хотя replan спавнится ролью supervisor и пойдёт на `p/super` | Чип модели может назвать модель, на которой стадия не работает; до A48 в этом случае был честный «—». Достижимость — только деградировавший путь (стадия не найдена в отображаемом пайплайне); воспроизведение через юнит-тест есть, живой сценарий не подтверждён | implementer: не подставлять worker-модель для слага, которого нет среди ролей (передавать роль из state или `None`) | средняя |

### Baseline (ctx-2)

| Проверка | Команда (cwd — корень проекта) | Результат |
|---|---|---|
| целевые тесты | `python -m pytest tests/unit/test_role_model_required.py tests/unit/test_model_check.py tests/unit/test_doctor.py tests/unit/test_doctor_audit_fixes.py -q --timeout=300 -p no:cacheprovider` | PASS — 97 passed |
| дашборд-модель | `python -m pytest tests/negative/test_reports31_dashboard_active_model.py -q` | PASS — 11 passed |
| линт | `ruff check .` | PASS — All checks passed |
| храповик | `sh scripts/ratchet.sh` | PASS — within baseline, rc=0 (138/138) |
| живой doctor | `python -m awf.cli doctor --project-dir .` | rc=0; 9 строк «роль X: модель opencode-go/deepseek-v4.1-flash», 1 provider, WARN нет |
| полный сьют | артефакт QA `.agentic/context/SUITE-17afddf…-4ea28cc4.json` | PASS — 7013 passed, exit 0 (23:55). Сам не перезапускал |
| пробы аудита | `python /tmp/opencode/probe_audit_0370b.py`, `probe_union.py`, `probe_dash.py` | p1/p2/p4/p5/p7 подтвердили CTX1, CTX5, CTX10–CTX13 |

### Ограничения проверки (ctx-2)

- Полный сьют не перезапускал: опираюсь на артефакт QA на том же дереве.
- Переносимость проверял чтением и пробами путей; на другой ОС/без XDG не запускал.
- Живую replan-сессию не поднимал (дорого). Косвенно: эта стадия идёт на `opencode-go/deepseek-v4.1-flash`, то есть спавн с явным `--model` работает.
- Достижимость CTX13 в живом дашборде не подтверждена — помечена как гипотеза с путём проверки, не как воспроизведённый дефект.
- Правки A47 (`opencode_db.py`, `__main__.py`) смотрел только в части моделей.

## Итог для следующего этапа

Код реализует договорённость: одна модель по правилу «своя → worker →
отказ», флаг всегда явный, doctor показывает модель каждой роли и знает
роли пайплайна. Блокеров нет. Правки аудита — только отчёт: дерево не
менял. Для владельца по убыванию пользы: CTX10 (падение валидатора на
кривой форме `models:`, одна строка), CTX5 (доки и роль-файл всё ещё
обещают модель по умолчанию), CTX1 (четыре обходных вызова супервизора
печатают «crashed» вместо отказа), CTX3 (свежий `awf init` не стартует без
ручной правки конфига). CTX11–CTX13 — гигиена на потом, не блокеры юнита.

## Ревизия ctx-3 (текущее дерево после QA-ревью)

Проверено дерево `59e798b` — тот же `tree-sha`, что в артефакте QA
`.agentic/context/SUITE-59e798b…-4ea28cc4.json` (exit 0, 7013 passed).
HEAD `6fa4f36`; рабочее дерево не менял. Ниже — только дельта к ctx-2.

### Baseline (ctx-3)

| Проверка | Команда (cwd — корень проекта) | Результат |
|---|---|---|
| целевые тесты | `python -m pytest tests/unit/test_role_model_required.py tests/unit/test_role_model_inheritance.py tests/unit/test_role_model_doctor.py tests/unit/test_model_check.py tests/unit/test_doctor.py tests/negative/test_reports31_dashboard_active_model.py -q --timeout=300 -p no:cacheprovider` | PASS — 95 passed |
| линт | `ruff check .` | PASS — All checks passed |
| храповик | `sh scripts/ratchet.sh` | PASS — 137/137, rc=0 (baseline возвращён) |
| живой doctor | `python -m awf.cli doctor --project-dir .` | rc=0; 9 строк «роль X: модель opencode-go/deepseek-v4.1-flash» + итог; WARN нет |
| полный сьют | артефакт QA на том же `59e798b` | PASS — 7013 passed, exit 0; сам не перезапускал |
| пробы аудита | `python /tmp/opencode/probe_audit_0370b.py`, `probe_union.py`, `probe_dash.py`, `probe_ctx14.py` | подтверждают CTX3, CTX5, CTX11, CTX13, CTX14; CTX10 и CTX2 закрыты |

### Статусы находок на текущем дереве

| ID | Статус ctx-3 | Подтверждение |
|---|---|---|
| CTX1 | понижен до SUGGESTION | `_stop_on_missing_model` ловит отказ в 5 ветках (`pipeline_engine.py:723, 1635, 1706, 2786, 3086`). Исключение одно и оно описано в докстринге: rollback-replan (`:1097`) намеренно продолжает работу (A34), пишет `replan_error` и PROGRESS. Сообщение «replan after rollback failed» всё ещё смешивает конфиг-отказ с транзиентным сбоем — это вопрос текста, не логики |
| CTX2 | закрыт | `model_check.py:190-201`: множество «ключи `models:` ∪ роли стадий». Проба `probe_union.py`: роль только из пайплайна попала в `missing_model_roles` |
| CTX3 | открыт | `_templates.py:38-43`: в шаблоне `models:` только `supervisor.description`, `models.worker.model` нет. Первый спавн свежего проекта отказывает (проба p5) |
| CTX4 | открыт | `doctor.py:366-372`: ветка «opencode не готов» печатает WARN о ролях и `ok models: модели не определены`, `problem` (нечитаемый `opencode.json`) не печатается |
| CTX5 | открыт | `roles.py:52` `UNASSIGNED_MODEL_TEXT` и `USAGE.ru.md:885-886` по-прежнему обещают «модель конфига по умолчанию». В этом репозитории `.agentic/roles/supervisor.md:5` несёт эту фразу при заданной модели в конфиге |
| CTX6 | открыт | `model_check.py:255-258` — английский совет; `supervisor.role_model_hint` — русский. Doctor вычитает английские по членству, MCP `awf_check_model_config` возвращает именно их |
| CTX7 | открыт | `AWF_DEBUG` не описан ни в одном `*.md` (grep: только отчёты и тесты) |
| CTX8 | открыт | `_stop_on_missing_model` только `_log`; `last_signal`/записи стадии нет, `orchestrator.py:331` зовёт `liveness.finish(rc)` без `reason` → `ENGINE-EXIT` с «(no exception)» |
| CTX9 | открыт | `tests/conftest.py:279-288`: `assert "\nmodels:\n" in text` + строковый replace. Связано с CTX3: шаблон и фикстура держатся за один текст |
| CTX10 | закрыт | `model_check.py:161-165`: `if not isinstance(models_config, dict): models_config = {}`. Проба p4: `check_model_config` на `models: [- worker]` возвращает отчёт, не падает |
| CTX11 | открыт | `metrics.py:254` игнорирует `XDG_DATA_HOME`, `model_check.py:167` и `opencode_config.py:50` — уважают. Проба p2: пути разные. В `awf/xdg.py` помощника для data-home нет — три копии знания |
| CTX12 | открыт | `doctor.py:401`: «run awf_check_model_config», подкоманды с таким именем в `awf/cli.py` нет (это MCP-инструмент) |
| CTX13 | открыт | `dashboard._active_stage_model` (`:1049`) на неизвестном слаге зовёт резолвер со слагом: проба `probe_dash.py` → `replan` получает модель worker, хотя стадия идёт ролью supervisor |

### Новые находки (ctx-3)

| ID | Важность | Файл / символ | Факт и доказательство | Влияние | Следующий шаг | Уверенность |
|---|---|---|---|---|---|---|
| CTX14 | MINOR | `api/roles._resolve_role_model` (`:99-122`); `api/model_check.py:360-370` | Второй резолвер модели: учитывает только `models.<role>.model`, наследование `models.worker.model` не знает. Следствия: (1) при заданной worker-модели роль-файл получает заглушку «не назначена…» — исходный CTX5 идёт из этого места; (2) сверка роль-файла сравнивает `file_model` с сырым `models.<role>.model`, а не с эффективной моделью. Проба `probe_ctx14.py`: файл `**Модель:** \`p/x\`` при `models.worker.model: p/x` даёт `match: False` и WARN «config models:supervisor.model is (default)» — ложный дрейф (QA-F6 воспроизведён на текущем дереве). Заглушка же (`file_model: None`) считается «match: True» и не проверяется никогда | Один и тот же вопрос «какая модель у роли» получает три ответа: движок/doctor (`get_role_model`), роль-файл и его сверка (своя логика). Ложный WARN учит игнорировать doctor; заглушка скрывает расхождение | implementer: `_resolve_role_model` и сверку перевести на `supervisor.get_role_model`; в `role_files` сравнивать с эффективной моделью | высокая |
| CTX15 | SUGGESTION | `awf/cmd_start` / `api/run.py` (нет вызова `check_model_config`) | В `awf start` предполётной проверки моделей нет: процесс запускается, отказ приходит из первой стадии (`pipeline_engine._stop_on_missing_model`). `grep -n "model" awf/cmd_start.py awf/api/run.py` — только импорт `git_model`. Это остаток открытого QA-F4. Требование TODO («отказ до спавна») выполнено: спавна нет | MCP `awf_start` и CLI отвечают успехом, ошибка видна только в логе/`status` позже; для проверки перед запуском оператору нужно помнить про `awf doctor` | implementer: `check_model_config(...).missing_model_roles` перед спавном движка, отказ синхронно | высокая |

### Поверхность × исход (анти-вакуум, проверка по дереву)

`grep -rn "get_role_model|require_role_model|--model" awf agent_workflow_ui/src`
на текущем дереве даёт:

- строители `--model`: ровно два — `agent_stage.py:264` и `supervisor.py:1658`;
  оба после `require_role_model`;
- читатели правила: `dashboard.py:1049`, `model_check.py:186`, оба через
  `supervisor.get_role_model`;
- второй резолвер (писатель роль-файлов): `roles._resolve_role_model`
  (`roles.py:99`, вызовы `:1059`, `:1206`, `:1428`) — не наследует worker
  (CTX14);
- проверка «где ещё спрашивают модель»: `opencode_db.available_models`,
  `metrics` модели ролей не резолвят; `_net.parse_model_from_cmd` читает уже
  собранный argv.

Итог: правило «своя → worker → отказ» живёт в одном месте для спавна и
показа, но у писателя роль-файлов и его сверки — своя копия (CTX14).
Других копий в дереве не нашлось.

### Ограничения проверки (ctx-3)

- Полный сьют не перезапускал: артефакт QA снят на том же `59e798b`
  (exit 0, 7013 passed); расхождения дерева с артефактом нет.
- Живую replan-сессию не поднимал (дорого, внешняя сессия). Косвенно: эта
  стадия идёт на `opencode-go/deepseek-v4.1-flash`.
- Переносимость (CTX11) проверена пробами путей, не запуском на другой ОС.
- Правки A47 (`opencode_db.py`, `__main__.py`) смотрел только в части
  моделей; это не мой scope (TODO-0369).
- Блокеров нет: все открытые находки — MINOR/SUGGESTION, ни одна не мешает
  приёмке A48.
