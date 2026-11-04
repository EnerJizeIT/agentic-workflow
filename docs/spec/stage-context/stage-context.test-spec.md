# Тестовая спецификация: контракт контекста стадии

Статус: REVIEW
Ревизия: ts-4 (req-9, arch-4, rd-2: закрыт FR8; AC41, пятистрочная таблица пределов, файл лога, формат `закрыто: N`)
Входы: `stage-context.md@req-9`, `requirements-review@rr-9`, `architecture@arch-4`, `context@ctx-3`, `readiness@rd-2`, TODO-0374/0377, `.agentic/pipelines/*.yaml`, `.agentic/context/PIPELINE-*.yaml`, `.agentic/roles/`, `.agentic/done/`.
База кода: `main` @ `c523183`; проверки спроектированы, не запускались.

## 1. Scope

Покрыты R1–R14, AC1–AC41, INV1–INV11, C1–C8, IMP1–IMP4. ts-4: FR8 — T13 перечисляет юниты; AC41 — T5; AC12 — T40; AC36 — T15/T16; пределы — T14; лог — T42. Измерением и ручной сверкой — T47–T50, T55. Вне scope: Q1, выбор владельца по Q3/Q5/Q6, живой прогон.

## 2. Ожидаемые значения

| Величина | Значение |
|---|---|
| Потолок | 9 000 символов стартового текста на пять семейств; `checklist` наследует `reviewer` |
| Семейства | `plan`, `execute`, `reviewer`, `supervisor`, `spec`; `reviewer/checklist` — строка `reviewer` |
| Носитель типа | `stage_types.<имя>` в `.agentic/config.yaml`; 16 пар R14/AC33 |
| Суммы | `plan` 5 600; `execute`/`supervisor`/`spec` 9 000; `reviewer` 7 900; `checklist` 5 150; `CHECK` 800, `PHASES` 700, `VISION` 500, `BLOCKED` 100 — вне матрицы |
| Обрезка | P5 → P4 → P3 → P2 → P1; P0 не режется; `REVIEW` последним из P1, `BLOCKER` последним в `REVIEW` |
| Файл стадии | `.agentic/context/STAGE-<todo>-<stage>.md` равен тексту `opencode run`, путь в `file=` |
| Строка CTX | `type=<family> cap=9000 total= file= role=<взято>/<всего> role_pct= blocks= cut= missing= duration=` в `.agentic/logs/orchestrator.log` |
| Покрытие роли | `floor(taken × 100 / total)`; ниже 70 % — `CTX-ROLE-LOW`; 69,5 % считается как 69 % |
| База качества | `.agentic/done/TODO-0320…TODO-0370/handoff/*.md`: 26 находок `BLOCKER`/`MAJOR` в 13 юнитах, максимум 4; окно 0338–0347 — 1 блок, медиана 12,12 мин |
| Стоп | сумма находок ≥ 4 за три юнита; любой блок `## REVIEW-фикс`; медиана > 14,5 мин; числа — `QUALITY_STOPS` (D9) |
| Останавливают | `CTX-FAIL`, `CTX-TYPE-MISSING`, `CTX-TYPE-STRICT`; не останавливают — `CTX-TYPE-DEFAULT`, `CTX-ROLE-LOW`, `CTX-CONFLICT`, `CTX-SNAPSHOT` |
| Секрет | `<redacted: имя>`; `[redacted: файл-секрет]`; блок остаётся |

Наборы блоков по типам — arch-4 §5 (шесть строк); тест сверяет точный список и порядок.

## 3. Покрытие

R1–R14 и INV1–INV11 покрыты карточками: тип — T1–T10, T13, T38; словарь — T11, T23, T45, T46; бюджет — T13–T22, T26, T29; роль — T30–T33; нет данных — T23–T28; путь — T12; снимок — T6, T38, T39; фикс-круг — T40; reuse — T41; один путь — T18, T25, T34–T37; замер — T33, T42–T44; эффект — T47–T50; файл — T25, T36; генерация — T2, T3, T5. INV1/INV11 — T51, INV2/3/5 — T52, INV4 — T39, INV6/7 — T37 и T53, INV8 — T54, INV9 — T38, INV10 — T37. Непокрытых AC нет; AC18, AC20, AC37 — измерением (T47–T50, T55).

## 4. Карточки T#

Общее: фикстуры в `tmp_path`, `$HOME` не читается, реальные роли и снимки только читаются.

**T1 — набор по типу.** AC1, AC22; IMP2. Шесть фикстур, `blocks[].id` в порядке arch-4 §5; `checklist` без `ROLE`, `DOCTRINE`, `REVIEW`, `PREV`; нет `AUDIT`, `METRICS`, `MISSING`.

**T2 — 16 пар `stage_types`.** AC33, AC2; C4; IMP1. Копия пайплайнов, конфиг без ключа: `init` даёт 16 пар R14, повторный — тот же конфиг, `doctor` rc 0; роль вне карты — отказ.

**T3 — `doctor` не пишет.** AC33, RR58; C4; IMP1. Без имени — rc≠0 и имя; устаревшее значение (`agent-qa-review: execute`) — rc≠0 и пара; sha256 конфига до и после совпадает.

**T4 — один носитель.** AC39, AC14; C4, C6; IMP1. Фикстуры `agent-qa-review: execute` и `reviewer`: наборы разные, sha256 модулей тот же, карты имя→семья в рантайме нет.

**T5 — нет, пустой и битый ключ.** AC40, AC41, AC21, RR52; C4, C6. Три копии: нет ключа и `stage_types: {}` — `CTX-TYPE-MISSING` с путём и `awf init`; `agent-qa-review: audit` — с именем пары и путём; `reviewer/checklist` допустим, `audit` и `reviewer/unknown` — нет; подпроцесса нет.

**T6 — имя вне списка.** AC32, AC11; C4, C6. Снимок `implementer-close` (30 из 213), роль `agent-implementer`: старт, `CTX-TYPE-DEFAULT`, семейство по роли, снимок не изменён.

**T7 — `strict`.** AC32, AC14, A10; C4. Та же фикстура с `strict: true`: отказ `CTX-TYPE-STRICT`, подпроцесс не запущен; без `strict` — работа.

**T8 — `spec` на нулевой позиции.** AC21, AC1; C5. `spec-context`, индекс 0, значение `spec`: набор `spec`, нет «создай TODO», `OUTPUT` на месте.

**T9 — контракт вывода.** AC22; C1–C5; IMP2. Шесть текстов T1: фразы DONE/BLOCKED/REVIEW и «молчание: провал» литералами, `OUTPUT` последний.

**T10 — новая стадия и роль данными.** AC2; C4. Пайплайн с `extra-review` и строка конфига; роль `agent-extra` файлом: сборки успешны, код сборки не менялся.

**T11 — противоречие владельцев.** AC8; C2. TODO `status: done` против DONE.json `blocked`; `GATES` PASS против `gates: ["failed"]`: по строке `CTX-CONFLICT` на пару, сборка успешна.

**T12 — абсолютный путь.** AC24, R6; C2. Проект в `tmp_path/«проект с пробелами»/repo`, `$HOME` другой: во всех шести текстах `str(project_root)`.

**T13 — потолок на трёх юнитах.** AC3; IMP2. 15 пар «семейство × юнит» (FR8) плюс 3 сборки строки `checklist` на юнитах `reviewer` — 18 сборок: `execute` — TODO-0370/0357/0367; `reviewer` — 0374/0367/0357; `checklist` — те же 0374/0367/0357; `supervisor` — verify 0370/0359/0365; `spec` — 0357/0374/0376; `plan` — 0377/0376/0374. У каждой `total ≤ 9 000`, `CTX-FAIL` нет, `total = len(text)`.

**T14 — сумма пределов ≤ 9 000.** AC35; IMP2. Тест разбирает таблицу §6 и `FAMILIES`: `plan` 5 600, `execute` 9 000, `reviewer` 7 900, `checklist` 5 150, `supervisor` 9 000, `spec` 9 000; каждая сумма ≤ 9 000; `checklist` — производная строка `reviewer`, отдельной строки в таблице нет; падение при 9 001.

**T15 — крупный TODO-0374.** AC36; C1; IMP2. `UNIT` 1 569 + `CRIT` 2 374 + `STAGE` 400 + `RULES` 350 + `OUTPUT` 800 = P0 5 493 при пределах 5 850 (RR61): текст ≤ 9 000, P0 без пометки среза, `CTX-FAIL` нет.

**T16 — verify TODO-0370.** AC36, RR56, RR57; C4; IMP2. Входы 11 983 (`A42`): `DIFF` 3 827, `GATES` 76, сумма 8 773 ≤ 9 000, оба целиком, срез — в `CTX`.

**T17 — P0 не влезает.** AC3; C8; IMP2. Синтетический `CRIT` 10 000: `failed` непуст, `CTX-FAIL` с числом, воркер не стартует.

**T18 — один аргумент.** AC3, AC14; C1; IMP2. Стаб `opencode` пишет argv: нет `--file`, один текст после `--`, длина равна `total`.

**T19 — позиции блоков.** AC4, AC1; IMP2. Шесть текстов T1 (arch-4 §5): `STAGE` первый, `UNIT` за ним (в `plan` — `PHASES`), `CRIT` после материала, `OUTPUT` последний.

**T20 — `REVIEW` перед ролью.** AC4, AC34; C1, C8. `execute` с `## REVIEW-фикс` и длинной ролью: `REVIEW` до `ROLE` и `DOCTRINE`.

**T21 — порядок не зависит от витка.** AC4, AC5, AC21; C6. Первый виток без `REVIEW`/`GATES`/`PREV`, второй со всеми; стадия на индексах 0 и 3 — порядок общих блоков совпадает.

**T22 — `AC#` дословно.** AC18, AC4; C1. TODO-0374 и фикстура AC21–AC41: каждый `AC#` найден подстрокой, `verify:` в `CRIT`.

**T23 — нет артефакта.** AC9; C1, C2, C5, C6. По одному отсутствующему входу (TODO, DONE, REVIEW, GATES, дифф, handoff, снимок): строка «нет данных: <причина> — <путь>», rc 0.

**T24 — пустой и отсутствующий.** AC9, AC25; C1. `GATES` 0 байт → «блок пуст», файла нет → «нет данных»; причины в `missing` разные.

**T25 — нет `.agentic/context/`.** AC31, A9, D16; C6. Сборка даёт `CTX-FAIL` с причиной, каталог не создан, подпроцесс не стартовал.

**T26 — порядок реза.** AC10; C6, C8. Сумма на 2 000 больше потолка: уходят `PREVLINK`, P4, P3, P2, P1; P0 цел; у среза «ещё N: см. файл» с числом.

**T27 — `REVIEW` не раньше `ROLE`.** AC34; C8. `REVIEW` 1 200 и роль 6 000, места хватает на P0+P1: `REVIEW` целиком, роль в ссылке; обратный порядок — провал.

**T28 — `BLOCKER` последним.** AC10, AC34; C8. `REVIEW` 30 строк, `BLOCKER R-2` в середине: `BLOCKER` в тексте, прочее в ссылке, `UNIT`/`CRIT` целы.

**T29 — handoff-ы.** AC5, AC10; C1, C2. Handoff-ы 400/900/3 000/2 000: `PREV` — последний, остальные — `PREVLINK` с путями.

**T30 — три фразы на роль.** AC6; C1, C2. 27 фраз (supervisor и восемь `agent-*`): все в тексте, не только в TOC.

**T31 — правка роли.** AC6; IMP2. Копия QA с маркером «СПЕЦ-РЕЖИМ-МАРКЕР»: маркер в тексте, sha модулей цел.

**T32 — ядро и оглавление.** AC6; IMP2. `agent-system-analyst.md`: шапка ≤500, ядро ≤ предела, разделы вне маркеров — TOC; файлов `*.core.md` нет.

**T33 — покрытие роли.** AC38, R11; C1, C8. Ядро 1 000/предел 700 → 70 %, 1 450/1 000 → 68 %, 1 000/695 → 69,5 %: `role=700/1000` и `role_pct`, WARN на 69 и 68, нет на 70.

**T34 — одна точка сборки.** AC14; C1–C5; IMP2. Четыре входа (`run_agent_stage`, `plan`, `verify`, `replan`): сборщик вызван один раз, `resolve_prev_handoffs` — ноль раз, текст воркера равен возврату.

**T35 — нет флага.** AC14, AC16. Grep `context_mode`, `legacy_context`, `AWF_CONTEXT*` пуст; argparse и конфиг не дают выбора; `context-pack --stdout` файла не пишет.

**T36 — файл стадии.** AC31, AC7, R13; C8; IMP2. После сборки файл есть, равен argv побайтово, `CTX` содержит `file=`; повторный прогон перезаписывает, дублей с суффиксами нет.

**T37 — AC16 механизирован.** AC16, INV7, INV8, INV10. Один сборщик, нет флага, пайплайны и роли не изменены; удаление `STAGE-*.md` не меняет вердикт.

**T38 — старый снимок.** AC11, INV9; C4, C6; IMP2. Первый по имени `PIPELINE-*.yaml` и снимок `implementer-close`: набор по имени, `sha256` до и после совпадает, отказов нет.

**T39 — форма снимка.** AC15, INV4; C6. Наборы ключей нового и старого снимков равны; `type`/`kind` нет; 12 пайплайнов грузятся, `<action:>` отвергается.

**T40 — находки при удалённом сигнале.** AC12; C7. Две секции: `R-1` закрыта, `R-2` новая, `.ready` нет: `R-2` в тексте, тело `R-1` не пересказано, строка `закрыто: 1 (R-1)` дословно.

**T41 — продолжение.** AC13; C7. TODO изменён, добавлена `R-2`: различие — строка о сессии, изменения видны, `total` не больше свежего.

**T42 — `CTX` на каждый тип.** AC25, AC19, AC3; IMP2. 18 сборок T13: одна запись в `.agentic/logs/orchestrator.log` (arch-4 §12), `type` объявленный, `cap` 9 000, `total = len(text)`, `file=`, `role=`.

**T43 — отчёт равен замеру.** AC19; IMP3. Три юнита с известными `CTX`: `awf report` отдаёт те же тип, `total/cap`, `file=`, покрытие, блоки, срез, пропуски, длительность, витки.

**T44 — инциденты и длительность.** AC26, AC29; IMP3. `STAGE-DIFF` и `COMMIT-AUDIT`, контрольный юнит без них: `incidents=2`; 10:00:00–10:07:30 → 450 с.

**T45 — владелец и sha256.** AC23; C2; IMP3. У блока `owner`; файловый — путь и sha256, `DIFF`/`STATE` — «на лету» и источник; реестр — раздел отчёта.

**T46 — правило секрета.** AC27, R2, RR17; C2; IMP2. Дифф с `token=abc123` и `.env`: значения нет, есть `<redacted: token>` и `[redacted: файл-секрет]`; `worker_tree` импортирует `redact`.

**T47 — контрольная точка.** AC37; IMP3–IMP4. Три юнита подряд: находки `BLOCKER`/`MAJOR`, блоки `## REVIEW-фикс`, медиана длительности против базы `.agentic/done/…/handoff/*.md`, числа из `QUALITY_STOPS`; стоп по порогу с причиной.

**T48 — окно 10 и три spec-рана.** AC17, AC28, AC29, AC30; IMP4. Окно 0338–0347: 1 блок, 55 стадий, медиана 12,12; QA 16 запусков, медиана 21,06; spec-раны 5/2/2: медиана блоков ≤1, spec ≤2, инцидентов 0, QA ≤25.

**T49 — ручная сверка.** AC18; IMP4. Требования, архитектура, тест-спека, план IMP, три spec-рана: ID юнита и все `AC#` дословно; расхождение — `BLOCKED`.

**T50 — план IMP.** AC20, AC14; IMP1–IMP4. Четыре IMP с точками замера; `init` пишет ключ до включения отказа; `_compute_kind` удалён, запасной ветки нет.

**T51 — сигналы и WARN.** INV1, INV11, AC32, AC38. Тесты сигналов зелёные; сборка с `CTX-TYPE-DEFAULT` и `CTX-ROLE-LOW` не меняет сигнал; стадию останавливают только `CTX-FAIL`/`CTX-TYPE-MISSING`/`CTX-TYPE-STRICT`.

**T52 — гейт и формат.** INV2, INV3, INV5; IMP1–IMP4. Стадия не коммитит; `DONE.json` — `files_changed`, `tests_run` объектами `{cmd, result}`, `gates`, `notes`, `stage`; allowlist и `STAGE-DIFF` не ослаблены.

**T53 — процессы и права.** INV6, INV7. Guard `proc.pid > 1` и tripwire `_forbid_session_kill` на месте; `check-contracts.sh` зелёный по `process-safety`; `readonly_roles` без `edit`/`write`.

**T54 — доктрина и handoff.** INV8; IMP2. Доктрина — один блок; имена и место handoff те же; `PREV`/`PREVLINK` читают те же файлы.

**T55 — полный регресс.** INV1–INV11, AC18; IMP1–IMP4. `check-contracts.sh` и `run-all.sh` зелёные, полный сьют — один канонический прогон, `ruff check .` чистый; удалённых и ослабленных тестов нет.

## 5. Файлы, фикстуры, команды

Тесты (планируются): `test_stage_context_types.py` — T1–T8, T10, T39; `test_stage_context_blocks.py` — T9, T11, T19–T22, T29, T40, T54; `test_stage_context_budget.py` — T14–T17, T26–T28; `test_stage_context_roles.py` — T30–T32; `test_stage_context_switch.py` — T12, T35, T37, T41; `test_stage_context_measure.py` — T33, T42, T44, T45; `test_stage_context_secrets.py` — T46; `integration/` — T6, T13, T18, T34, T36, T38; `negative/` — T17, T23–T25; существующие — `test_doctor.py` (T3), `test_reports_archive.py` (T43), `test_signals.py` и `test_commit_gate.py` (T51, T52).

Фикстуры: `stage_project`, `six_types`, `real_fixtures` (18 сборок T13, 213 снимков, 9 ролей), `stage_artifacts`, `role_phrases` (27 фраз), `old_snapshot`, `secret_diff`, `quality_base`.

Команды: `pytest tests/unit/test_stage_context_*.py -q`, `ruff check .`, `bash scripts/check-contracts.sh`, `bash scripts/run-all.sh`, `scripts/suite-canonical.sh`.

## 6. Условия приёмки

1. T1–T46, T49, T51–T54 зелёные; у каждой карточки тест с меткой `T<номер>`.
2. T47, T48, T50, T55 — измерением или ручной сверкой с числами; живой прогон только по запросу.
3. TDD: сначала красный тест по отсутствующему поведению; ошибка установки красной фазой не считается.
4. Пакеты зелёные: контракты, храповики, тесты, линт. INV1–INV11 проверены T51–T55; расхождение — `BLOCKED`.

## 7. Findings

| ID | Важность | Проблема | Владелец | Состояние |
|---|---|---|---|---|
| QA20 | SUGGESTION | Q3/Q5/Q6 меняют только числа AC28/AC17/AC30; T48 идёт по условным критериям и приёмку не блокирует | владелец | OPEN |
| QA24 | SUGGESTION | AC33 проверяет отсутствие имени, а arch §4/RR58 — ещё и устаревшее значение; AC на сравнение значений нет | аналитик | OPEN |

FR8–FR17 закрыты в ts-4 (T13, T15, T14, T42, T5, T46, T40); QA14 закрыт D13; QA15–QA19/QA23 = FR9–FR14/FR17; QA21/QA22 устарели. Открытых MAJOR/BLOCKER нет.
