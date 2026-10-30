# Контекст: экономия контекста стадии и сессия имплементатора на фикс-круге

Статус: REVIEW
Ревизия: ctx-2
Входы: TODO-0302; `context-efficiency.md@req-4`; `context-efficiency.requirements-review.md@rev-4` (FAIL); TODO-0301; `.agentic/done/TODO-0250/TODO.md` и `.agentic/done/TODO-0251/TODO.md` (требования владельца, архив 28.10)
База кода: commit `401680bbfd568ea03793a6fcbd107180fcc49da4`; в дереве только untracked `docs/spec/context-efficiency/`; opencode 1.19.4 в PATH

## 1. Итог для аналитика

- **Повторный заход.** TODO-0250 («пакет контекста стадии») и TODO-0251 («переиспользование сессии») лежали в архиве как stale, не реализованы. Тестов `tests/negative/test_stage_context_pack.py` и `tests/negative/test_session_reuse.py` в дереве нет. Требования пишутся поверх этих документов: там уже выбраны носители (`awf/context_pack.py`, `context/SESSION-<todo>-<stage>.json`, ключ `engine.reuse_session: off|fix-only|always`), состав секций пака и матрица «условие → исход».
- **Файла `BACKLOG-NEXT.md` нет**, модулей `awf/_session.py` и `awf/agent_stage_prompt.py` нет. Реальные адреса: спавн — `awf/agent_stage.py:171-177`, промпт — `awf/supervisor.py:283`, карточка — `awf/brief.py`.
- **Переиспользования сессии нет.** Аргументы спавна не содержат `--session` и `--continue`, id сессии нигде не сохраняется. Каждая попытка — холодный старт.
- **Фикс-круг стартует заново по конструкции.** Отказ дописывает `## REVIEW-фикс` (`awf/api/hygiene.py:1205`), движок перезапускает стадию тем же спавном. Замер: TODO-0296 — 6 секций и по 4 сессии на роль; TODO-0218 — 14 секций.
- **Цена входа стадии:** медиана токенов первого шага сессии — 48 236 (n=60, минимум 45 187, максимум 54 987). Доля файлов awf — около 9–10 тысяч токенов (роль + доктрина + TODO + handoffs + промпт ≈ 36–41 КБ).

**Что уточнил круг 2 (RR32–RR38):**

- **RR32:** тела секции не отдаёт ни одна функция; канон границы — `_apply_section_update` (`awf/api/hygiene.py:1263-1271`, до следующего `## `). В 297 архивных юнитах внутри блока нет вложенных `## `/`###` — граница однозначна.
- **RR33:** число ревью неверно (TODO-0250 — 41 строка, 0 секций). Конфликт доказан на других: TODO-0244 52+117=169, TODO-0284 97+77=174, TODO-0218 61+98=159. Потолок витков default 3, проект 6, предел 20.
- **RR34:** `_WORKER_TITLE_RE` кончается на `$` (`awf/metrics.py:120`), записи `-a<N>` невидимы; флаги `-s/--session`, `--fork`, `--format` есть.
- **RR36:** ключей `engine.*` нет; читатель `awf/config.py:29`.
- **Среда:** `/usr/bin/python3` не видит `agent_workflow_ui.tools`; venv и `python3` из PATH зелёные.

Главные ограничения:

- роль-аудитор в `readonly_roles`: `edit`/`write` запрещены, запись только через `bash` (`awf/_env.py:374-401`);
- полный сьют упирается в 300 с baseline-команды (раздел 6), опора — целевые тесты;
- `opencode run --session <id>` вместе с `--agent/--model/--file/--title` live не проверялся — Q3, архитектор.

## 2. Карта затронутых путей

| Пункт | Вход | Компоненты | Данные и эффекты | Источник |
|---|---|---|---|---|
| Спавн стадии | движок после перехода | сборка argv, очистка старых сигналов, env роли | процесс `opencode`, stage-log `logs/awf-<role>-<todo>.out` | `awf/agent_stage.py:100-350` |
| Промпт стадии | kind (plan/execute/verify), TODO | базовый текст, контекст пайплайна, задание стадии, идентичность стадии | текст промпта в stdin команды | `awf/supervisor.py:283-388`; `awf/agent_stage.py:24-58,122-137` |
| Вложения (`--file`) | файлы проекта | роль, доктрина, TODO, handoffs прошлых стадий | содержимое в контексте модели | `awf/agent_stage.py:171-200` |
| Handoff стадии | PROGRESS/DONE/DONE.json, git | `collect_handoff`: факты стадии, дайджест, дифф | `handoff/<stage>-<todo>.md` | `awf/agent_stage.py:371-570` |
| Ретраи и фикс-круг | отказ/тихий выход/сеть | счётчики попыток, заметка продолжения, потолок витков | новый спавн, `retry_note`, `BUDGET-<id>.md` на потолке | `awf/pipeline_engine.py:2160-2235`; `awf/budget.py` |
| Завершение юнита | verify/approve | архивация `.agentic/done/<todo>/` | handoffs и артефакты уезжают в архив | `awf/api/pipeline.py` (approve) |
| Наблюдаемость | state, логи | `awf status`, `awf brief`, RUN-REPORT, метрики | `state/current.yaml`, отчёт метрик | `awf/brief.py`; `awf/metrics.py` |

Точки входа: `awf start`/`awf continue` и MCP `awf_start`, `awf_run_next`, `awf_continue`.

## 3. Переиспользуемые компоненты и контракты

- **Спавн и ожидание сигнала.** `run_agent_stage` строит argv и ждёт сигнал; тестовая подмена — `tests/infra/opencode_shim/opencode` (трассирует argv) — опора для тестов «`--session` есть/нет».
- **Идентичность сессии.** Заголовок `awf-<role>-<todo>` (`awf/agent_stage.py:175`); поиск по заголовку — `awf/metrics.py:338-403`; `opencode session list --format json` отдаёт `id`, `title`, `directory`.
- **Флаги opencode 1.19.4.** `-s/--session <id>`, `--fork` (только вместе с `--continue`/`--session`), `--format json` — из `opencode run --help`.
- **Бюджет и обрезка текста.** `awf/brief.py`: `MAX_WORDS = 900`, `_clip_by_words`, `_clip_changelog_section` — готовый приём для лимита пака.
- **Парсер и граница секций `REVIEW-фикс`.** Заголовки — `awf/todos.py:17-24`; номер витка — `review_round`/`review_summary` (`awf/todos.py:27-57`); граница тела — `_apply_section_update` (`awf/api/hygiene.py:1263-1271`, до следующего `## `), пишет блок `review_append` (`:1205-1212`). Готового чтения тела нет — это и есть предмет RR32.
- **Потолок витков.** `max_review_passes` + `_at_cap` (`awf/budget.py:78-131`), на потолке движок останавливает юнит на входе в verify (`awf/brief.py:509`).
- **Чтение конфига.** `cfg_mod.get(config, "engine.<key>", default)` (`awf/config.py:29`), новых ключей в коде нет.
- **Артефакты юнита.** `.agentic/context/`: `BASELINE-*.sha`, `.untracked`, `GATES-*.md`, `CHECKLIST-*.json`, `CLOSER-*.json`, `PIPELINE-*.yaml`, `VERIFIED-*.sha` — пакет ссылается на них, не пересказывает.
- **Запись состояния.** `write_state(project_dir, **fields)` (`awf/pipeline_state.py:46-84`), `clear_state` (`:182`) — произвольные ключи; вызывается при reset (`awf/api/lifecycle.py:1619`) и чистом завершении (`awf/orchestrator.py:401`), но не в `continue_pipeline`.
- **Проверки spec-пакета.** `awf spec check <dir>` → `awf/spec_package.py`: шапка требует `Статус`, `Ревизия`, `Входы`, `База кода`; роли — context, requirements, requirements-review, architecture, test-spec, readiness (`awf/spec_package.py:36-66`); бюджеты 2200 слов на документ и 11000 на пакет.

## 4. Поведение, которое нельзя сломать

- **Контракт сигнала.** Стадия завершается только файлом `DONE-<todo>.ready` / `BLOCKED-<todo>.ready`; отсутствие сигнала — провал стадии.
- **Очистка старых сигналов** перед спавном и запрет симлинков на этом пути (`awf/agent_stage.py:155-170`).
- **Убийство только своей группы процессов.** Guard `proc.pid > 1` в `awf/_proc.py` и tripwire `_forbid_session_kill` в `tests/conftest.py` неприкосновенны (`AGENTS.md`).
- **Права ролей.** `readonly_roles` (`agent-qa-review`, `agent-project-auditor`) без `edit`/`write`; execute-стадии не видят управляющих MCP-инструментов (`awf/_env.py:297-345`).
- **Формат handoff.** Секция `## Stage facts`, строка `- model:`, дайджест DONE до 10 строк — из них дашборд читает модель стадии (`awf/agent_stage.py:466-500`).
- **Изоляция коммита.** Коммит содержит только дифф против baseline; `.agentic/` не коммитится.
- **Режим «выключено» байт-в-байт.** Требование владельца в 0251: при `engine.reuse_session: off` argv и поведение совпадают с текущими.
- **Дерево воркера не откатывать** (`git checkout`/`git stash` запрещены) — доктрина `02-worker-tree.md`.
- **Отказ при одновременном запуске** — noop с текстом, не исключение — доктрина `03-launch-lease.md`.

## 5. Тестовые средства

- `tests/infra/opencode_shim/opencode` — подмена CLI, режимы `silent-exit`, `crash`, `hang`, `do-step`, `write-blocked`; трассирует argv.
- `tests/unit/test_agent_stage_prompt.py`, `tests/unit/test_handoff_v2.py` — сборка промпта и handoff.
- `tests/negative/test_stage_assignment_outputs.py`, `tests/negative/test_stage_identity.py` — задание стадии и блок идентичности в DONE.json.
- `tests/unit/test_brief.py`, `tests/unit/test_stage_tool_profiles.py` — карточка супервизора и профили инструментов.
- `tests/unit/test_pipeline_engine.py`, `tests/unit/test_run.py` — переходы, объявленный выход, ретраи (в этом аудите не гонялись).
- Запуск — интерпретатор venv проекта (`/home/pklochkov/.local/share/awf-venv/bin/python`) или `python3` из PATH; `/usr/bin/python3` не видит `agent_workflow_ui.tools`.
- Ворота: `bash scripts/run-all.sh`.

## 6. Baseline

| Проверка | Команда | Каталог | Результат | Наблюдение |
|---|---|---|---|---|
| Целевые тесты стадии | `python3 -m pytest tests/unit/test_agent_stage_prompt.py tests/unit/test_handoff_v2.py tests/negative/test_stage_assignment_outputs.py -q -p no:cacheprovider` | корень проекта | PASS | 21 passed за 26.04 с |
| Смежные тесты | `python3 -m pytest tests/unit/test_brief.py tests/negative/test_stage_identity.py tests/negative/test_stage_tool_profiles.py tests/unit/test_agent_stage_exit_code.py -q -p no:cacheprovider` | корень проекта | PASS | 85 passed за 1.03 с |
| Те же смежные под `/usr/bin/python3` | то же | корень проекта | FAIL | 6 failed: `No module named 'agent_workflow_ui.tools'` — дефект среды, не кода |
| Spec-пакет | `python -m awf spec check docs/spec/context-efficiency` | корень проекта | FAIL | rc 1: нет документов ролей architecture, test-spec, readiness (context/requirements/requirements-review есть) |
| Полный сьют | `verification.test_cmd` из `.agentic/config.yaml` | корень проекта | NOT_RUN | не запускался: аудит его не требует, baseline-команда исторически упирается в 300 с |
| Дифф дерева | `git status --porcelain` | корень проекта | PASS | только untracked `docs/spec/context-efficiency/` |

## 7. Находки в scope

CTX1–CTX10 перепроверены на ctx-2: пути и строки кода прежние, кроме уточнений в разделе 1.

| ID | Важность | Файл / символ | Факт и доказательство | Влияние на фичу | Следующий шаг / владелец | Уверенность |
|---|---|---|---|---|---|---|
| CTX1 | MAJOR | TODO-0301; `awf/agent_stage.py:171-177`; `awf/supervisor.py:283` | В TODO указан несуществующий `BACKLOG-NEXT.md`, модулей `awf/_session.py` и `awf/agent_stage_prompt.py` нет. Реальные точки: спавн — `run_agent_stage`, промпт — `build_prompt`, карточка — `awf/brief.py`. | Ложные контракты и границы правок. | Аналитик: реальные файлы; архитектор: границы. | Высокая |
| CTX2 | MAJOR | `awf/agent_stage.py:171-200` | argv: `run --auto --print-logs --agent --title --file ... --model -- prompt`; `--session`/`--continue` нет нигде в `awf/`. | Нужен новый аргумент, захват id, политика отказа. | Архитектор: хранение id, условия reuse. | Высокая |
| CTX3 | MAJOR | TODO-0296, TODO-0218; opencode.db | TODO-0296: 6 секций, 4 сессии на роль; TODO-0218: 14 секций, 15 сессий. Фикс-круг всегда даёт новую сессию. | Каждый виток платит вход стадии. | Аналитик/архитектор: сценарий фикс-пасса. | Высокая |
| CTX4 | MAJOR | `.agentic/roles/*.md`; `DOCTRINE.md`; handoffs | Роли 15.6–16.2 КБ, доктрина 2 958 Б, TODO-0302 4 417 Б, handoffs 3.6–4.1 КБ; у стадии 7 файлов. | Постоянный вход ≈ 36 КБ. | Архитектор: что режется. | Высокая |
| CTX5 | MAJOR | `awf/supervisor.py:177-182` | `Prior completed TODOs:` перечисляет все 297 каталогов — 3 288 из 5 654 символов промпта, растёт линейно. | Диета промпта. | Аналитик: граница списка. | Высокая |
| CTX6 | MINOR | `awf/agent_stage.py:579-613`; `default.yaml` | Две стадии `default` с ролью `agent-implementer`; легаси-ветка `resolve_prev_handoffs` отдаёт один handoff дважды. | ≈4 КБ лишних, путаница авторства. | Архитектор: дедуп в паке или отдельно. | Высокая |
| CTX7 | MINOR | `awf/agent_stage.py:566,387` | Handoff `<stage>-<todo>.md` без номера попытки; у TODO-0296 4 файла на 4 прогона. | Нет артефакта «что я уже сделал». | Архитектор: per-attempt артефакт для дельты. | Высокая |
| CTX8 | MINOR | `awf/agent_stage.py:175`; `opencode session list` | Заголовок не отличает попытки (до 15 строк на роль); поведение `--title` с `--session` неизвестно. | Поиск id по заголовку неоднозначен. | Архитектор: суффикс и/или файл id. | Высокая |
| CTX9 | MINOR | `awf/pipeline_state.py:46-84`; `awf/metrics.py:338-403` | В state id сессии нет; метрики читают v1 `session`, на 2.x это `session_v2`. | Захват обязан пережить неизвестную схему. | Архитектор: проба схемы, мягкая деградация. | Высокая |
| CTX10 | MINOR | `awf/signal_watch.py`; `docs/design/opencode-v2-migration.md:61` | Убийство по группе процессов; на 2.x сессия переживает убийство клиента, на 1.19.4 локальна. | Reuse после timeout/kill — отдельное решение. | Архитектор: правило для обеих ситуаций. | Средняя |
| CTX11 | MAJOR | `awf/todos.py:22-24`; `awf/budget.py:99-101`; `awf/api/hygiene.py:1205-1212,1263-1271` | Носителя тела секции нет: `review_headings` отдаёт только строки заголовков, `awf/` не содержит ни одной функции «тело секции». Канон границы тела есть в `_apply_section_update` (до следующего `## `); в 297 архивных юнитах внутри блоков нет вложенных `## `/`###`. | Без носителя пакет понесёт голые заголовки, и AC5 это пропустит (RR32). | Аналитик: назвать носитель — новый helper рядом с `review_headings` на той же границе; AC5 — «дословный текст»; AC9 — источник красных тестов и хвоста падения. Носитель — архитектор. | Высокая |
| CTX12 | MAJOR | архив `.agentic/done/TODO-*/TODO.md`; `awf/budget.py:34-37,78-94` | Измерено: TODO-0244 — 52+117=169 строк, TODO-0284 — 97+77=174, TODO-0218 — 61+98=159, TODO-0296 — 76+57=133. Секции 2 и 5 сами превышают 150. Число ревью «TODO-0250 — 80 строк» неверно (41 строка, 0 секций). `max_review_passes`: default 3, проект 6, потолок 20. | Предел 150 и запрет резать 2/5 несовместимы; реализация выбирает молча (RR33). | Решение супервизора в TODO-0302: 150 — потолок секций 1/3/4/6/7, секции 2/5/8 не режутся; аналитик обязан записать это и не выдавать 150 за предел файла. | Высокая |
| CTX13 | MINOR | `awf/metrics.py:120-129`; `opencode run --help` | `_WORKER_TITLE_RE` кончается на `$` — записи `-a<N>` невидимы. Флаги `-s/--session`, `--fork`, `--format`, `-c/--continue` есть. На продолжении `--session <id>` новой строки с заголовком `-a2` может не появиться. | Буквальный «ищи по заголовку» на повторе ничего не находит (RR34); AC21 проходит только через файл пары. | Аналитик: разделить захват — свежий спавн ищет по `title`+`directory`, продолжение берёт id из файла пары и проверяет в БД. | Высокая |
| CTX14 | MINOR | `awf/config.py:29`; `.agentic/config.yaml` | Ключей `engine.context_pack` и `engine.reuse_session` нет ни в коде, ни в конфиге; белого списка ключей нет — читатель `get(config, dotted, default)`. `always` отличается от `fix-only` только случаем «файл жив, счётчик потерян, attempt=1»; свежий файл на входе пары отсутствует, поэтому на первом входе поведение совпадает. | Ключ может остаться мёртвым, а противоречие A4/A8 не поймает ни один AC (RR36). | Аналитик: переформулировать A4 и добавить AC на потерянный счётчик либо убрать `always`. | Высокая |
| CTX15 | SUGGESTION | `tests/unit/test_brief.py` под разными интерпретаторами | `/usr/bin/python3` — 6 failed (`No module named 'agent_workflow_ui.tools'`), venv проекта и `python3` из PATH — 85 passed. | Verify-команда на «не том» python даст ложный красный. | Аналитик/архитектор: verify юнитов — только venv проекта. | Высокая |
| CTX16 | SUGGESTION | `awf/spec_package.py:36-66`; `awf spec check` | Пакет требует шесть документов ролей; сейчас есть три (context, requirements, requirements-review). `spec check` → rc 1 с именами architecture, test-spec, readiness. | Приёмка «rc 0» достижима только после трёх следующих стадий этого забега. | Аналитик: не требовать rc 0 от промежуточного состояния. | Высокая |

## 8. Несвязанные находки

- В `inbox/` лежит `APPROVE-TODO-0257.ready` — остаток прошлого юнита, на эту спеку не влияет.
- В `build/lib/awf/` лежит устаревшая копия пакета; искать код только в `awf/`.

## 9. Вопросы и ответы

- Q1 (архитектор): id сессии — `context/SESSION-<todo>-<stage>.json` или поле `state/current.yaml`? От этого зависит, переживёт ли id рестарт движка.
- Q2 (архитектор): пакет — файл `context/STAGE-<todo>-<stage>.md` с `--file` или секция промпта?
- Q3 (архитектор): совместимость `--session` с `--agent/--model/--file/--title` на 1.19.4.
- Q4 (архитектор): «фикс-пасс» — `attempt > 1` или наличие секции `## REVIEW-фикс` (`awf/todos.py:12-40`)?
- Q5 (архитектор): продолжать после таймаута и убийства или всегда свежую (CTX10)?
- A1: замер «~22k токенов» не воспроизводится: медиана первого шага 48 236. Цифру в требованиях заменить на измеренную.
- A2: «3 компакции в 0215» проверить нельзя — событий компакции в `opencode.db` нет; оставляю как свидетельство владельца.
- A3: `opencode run --session` с остальными флагами live не проверялся.
- A4 (RR32): тело — на границе `## `, как в `_apply_section_update`; тест проверяет текст, не заголовок.
- A5 (RR33): конфликт доказан на трёх архивных юнитах, не на TODO-0250; AC7 — «потолок секций 1/3/4/6/7», не «файл ≤ 150».
- A6 (RR34): на продолжении id берётся из файла пары; поиск по заголовку — только свежий спавн.
