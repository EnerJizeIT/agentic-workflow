# Один факт — один владелец: реестр и пробы

Документ отвечает на вопрос «кто читает каждый git-факт» и даёт по каждой
строке команду-пробу с ожидаемым результатом. Тест
`tests/negative/test_miniwave_m4_guards.py` разбирает обе таблицы: владелец
обязан существовать, маркер — присутствовать в его файле, проба — печатать
ровно ожидаемый токен (или ничего, токен `∅`).

Машинный реестр — `docs/contracts/single-owners.md`: те же факты и владельцы,
колонка «Маркер» вместо команды. Расхождение двух таблиц роняет тест.

Граница скана «печатных чтений»: `awf/**/*.py`. Сканер — белый список:
разрешён только байтовый протокол `-z` без декодирующих аргументов
(`text=True`, `universal_newlines`, `encoding`). Всё остальное, что просит у
git пути, — красный: голая подкоманда, печатающий флаг
(`--name-only`/`--name-status`/`--porcelain`/`--short`/`--others`/
`--ignored`/`--raw`) и `--stat` (он печатает путь на строку и падает на
не-UTF-8 имени так же, как листинг). Подкоманды `log`/`show` проверяются
только при печатающем флаге: `log -1 --pretty=…` путей не перечисляет.

Граница сканера — литеральные аргументы. Список флагов, собранный в
переменную, и обёртка вроде `git_utils.git_stdout` мимо сети проходят;
такие места держит ревью, а не тест.

## Реестр фактов

| Факт | Модуль-владелец | Проба | Ожидание |
| --- | --- | --- | --- |
| blob и mode изменения | `awf/infra/git_model.py` | `grep -n "class ChangeRecord" awf/infra/git_model.py` | `class ChangeRecord` |
| пути дерева и untracked | `awf/infra/git_model.py` | `grep -n "def untracked_paths" awf/infra/git_model.py` | `def untracked_paths` |
| пути относительно baseline | `awf/infra/git_model.py` | `grep -n "def changed_paths" awf/infra/git_model.py` | `def changed_paths` |
| индекс против ревизии | `awf/infra/git_model.py` | `grep -n "def staged_paths" awf/infra/git_model.py` | `def staged_paths` |
| набор файлов без baseline | `awf/commit_delta.py` | `grep -n "def worktree_file_set" awf/commit_delta.py` | `def worktree_file_set` |
| предсуществующие untracked базового снимка | `awf/infra/git_model.py` | `grep -n "def read_untracked_snapshot" awf/infra/git_model.py` | `def read_untracked_snapshot` |
| базовый снимок дерева (запись) | `awf/api/pipeline.py` | `grep -n "def create_baseline" awf/api/pipeline.py` | `def create_baseline` |
| статус на baseline (`BASELINE-*.status`, печатный долг) | `awf/commit_delta.py` | `grep -n "def staged_at_baseline" awf/commit_delta.py` | `def staged_at_baseline` |
| публикационные факты | `awf/publish.py` | `grep -n "class UnitReceipt" awf/publish.py` | `class UnitReceipt` |
| состояние операции | `awf/infra/state_store/_ops.py` | `grep -n "def write_operation" awf/infra/state_store/_ops.py` | `def write_operation` |
| печатные чтения путей — только в исключениях | `tests/negative/test_miniwave_m4_guards.py` | `grep -n "def find_printed_git_path_reads" tests/negative/test_miniwave_m4_guards.py` | `def find_printed_git_path_reads` |

## Исключения (осознанный долг)

Печатные чтения вне модели остались только там, где печатает сам git:
человекочитаемая сводка `--stat`, `status --porcelain` mutation-гейта и
вход хеша, печатный формат `.status` baseline (писатель — `api/pipeline.py`,
читатель — `commit_delta.py`) и ленивый запасной писатель снимка, когда
модель не прочитала дерево (его разбор идёт под C-quote-мостом).
Строка исключения обязательна: без неё сканер краснит тест, и новая вторая
реализация не проскочит.

| Модуль | Причина | Долг |
| --- | --- | --- |
| `awf/git_utils.py` | печатные хелперы: `status_porcelain` (mutation-гейт: непустой статус = грязно, недекодируемое имя — непустая метка) и `diff_stat` (`--stat` сводка для `awf_brief`/dashboard). В `tree_fingerprint` статус — это ВХОД хеша, а не разбор путей (декод `surrogateescape`), а имена untracked читает модель (`untracked_paths`): отдельного печатного читателя путей больше нет — `untracked_files` удалён | формат `.status` и отображение вне модели; старые `VERIFIED-*.sha` на дереве с именем, которое печатный `ls-files` C-quote'ил (не-ASCII или ASCII с табом, кавычкой, обратным слэшем, control), отвергаются как tree changed — осознанно, пере-верификация (см. «Долг: два отпечатка») |
| `awf/api/pipeline.py` | писатель снапшотов baseline: `status --short` для `.status` и печатный запасной путь `_untracked_snapshot`, когда модель не прочитала дерево; чтение под тройным guard'ом | формат `.status` baseline |
| `awf/verify.py` | `git_utils.diff_stat` — человекочитаемая сводка для dashboard/wait_event, не разбор путей; `git diff --quiet` путей не печатает; декод `errors`/`UnicodeDecodeError` в guard'ах | отображение вне модели |
| `awf/api/context.py` | `git diff --stat` — человекочитаемая сводка для `awf_brief`, не разбор путей; декод `errors="replace"` | отображение вне модели |
| `awf/api/lifecycle.py` | `git diff --stat` — сводка отчёта, не разбор путей; декод `errors="replace"` | отображение вне модели |
| `awf/pipeline_engine.py` | `git diff --stat` — сводка в промпт стадии, не разбор путей; декод `errors="replace"` | отображение вне модели |
| `awf/agent_stage.py` | `git diff --stat` — сводка handoff; декод `errors="replace"`; `git log -1 --pretty` путей не перечисляет | отображение вне модели |

## Решения

**Почему модель, а не guard на печатном чтении.** Guard закрывает падение,
но не убирает разбор печатных путей: краевой пробел срезается `strip()`,
C-quote отдаёт другую идентичность, чем сырые байты, и печатный текст всё
равно приходится собирать обратно в путь. NUL-протокол отдаёт байты как
есть, поэтому разбор исчезает вместе с классом ошибки.

**Почему исключения честные.** `BASELINE-*.untracked` теперь пишется в
сыром виде (NUL-протокол `_path_link`), а старые файлы читаются лениво:
`git_model.read_untracked_snapshot` распознаёт оба формата и сводит печатный
C-quote к той же сырой идентичности. Печатные чтения остались только там,
где формат `.status` ещё печатный, где `--stat` — человекочитаемая сводка, и
в запасном писателе снимка (ленивый C-quote-мост).
Каждое исключение держит guard на уровне модуля (`surrogateescape` /
`UnicodeDecodeError` / `errors="replace"`), и это проверяет тест — не у
каждого вызова: `git diff --stat` rollback'а (`api/pipeline.py`)
декодируется строго, поэтому не-UTF-8 имя в сводке — отказ операции
(`AwfApiError`, до `git reset`), а не молчаливый алиас пути. Git-log-читатели (handoff, wait_event, dashboard,
metrics) после добора 0239 говорят `surrogateescape`.

**Снос легаси.** `commit_delta._legacy_plan_split` (печатный разбор
`--name-only`) удалён: набор файлов считается моделью
(`changed_paths`/`staged_paths`/`untracked_paths`). `_all_changed_files`
переименован в `worktree_file_set` и тоже читает модель. `raw_lines` удалён:
единственный читатель untracked-снимка — `git_model.read_untracked_snapshot`.
`reject_files._untracked_view` больше не делает второй печатный скан (его
`raw -> printed` map никто не использовал), поэтому `git_utils.untracked_files`
удалён: читатель untracked один — модель. `verify.work_fingerprint` и
`verify.diff_stat_for_todo` тоже читают untracked моделью; печатным остался
только `--stat` как сводка. `api/workflow_commit.uncommitted_workflow_files`
читает свой pathspec через модель (`changed_paths(HEAD, paths)` /
`untracked_paths(paths)`): прежний `-z` через строгий `git_stdout` молча
терял не-UTF-8 имя вместе со всей пачкой. Прод-вызовов удалённых имён нет —
проверка в DONE.

**Долг: два отпечатка.** «Отпечаток состояния дерева» существует в двух
реализациях с РАЗНЫМ digest: `infra.git_model.tree_fingerprint` (его читает
`claim_changes`) и `git_utils.tree_fingerprint` (`awf tree-sha`, approve,
`commit_plan`). Это не рефакторинг: сведение к одному digest меняет формат
`VERIFIED-*.sha` и должно идти отдельным юнитом с миграцией уже записанных
отпечатков. Сейчас обе реализации читают пути через модель; расхождение
digest зафиксировано как долг, а не спрятано.

**Долг: печатный `.status` baseline.** Факт «статус индекса и рабочего
дерева на baseline» пока живёт в печатном виде. Писатель —
`api/pipeline.py` (`git status --short`), читатели — `commit_delta.py`
(`staged_at_baseline`, `worktree_dirty_at_baseline`, `baseline_dirty_paths`)
разбирают строки через `_short_sides` и сводят C-quote к сырой
идентичности (`git_model.raw_identity`). Сканер этого не видит: git-вызова
в читателе нет, он разбирает уже записанный файл. Это единственный
оставшийся печатный разбор ПУТЕЙ из снимка; миграция на машиночитаемый
`.status` (porcelain v2 `-z`, как `changes()`) меняет формат файла и требует
ленивого чтения старых снимков — отдельный юнит, адрес в бэклоге. Здесь факт
зарегистрирован как долг с одним владельцем, а не спрятан.

**Долг: отказ модели в наборе без baseline.** `worktree_file_set` (fallback
без `BASELINE-*.sha`) ловит `GitModelError` и отдаёт `[]`, тогда как
baseline-путь (`DeltaAssembler`) тот же отказ превращает в блокировку плана.
Для gitlink/unmerged/sparse это значит «коммитить нечего» вместо явного
отказа: fail-closed, но без объяснения. Развести два исхода — отдельный юнит
(сигнатура `worktree_file_set` и обработка в вызывающих), здесь зафиксировано
как долг.
