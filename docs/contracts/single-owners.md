# Contract: Один факт — один владелец
Owns: чтение git-фактов (blob/mode, пути, снимок baseline и `.status`), публикационные факты, состояние операции; пробы — в `docs/design/single-owners.md`.
Path: реестр `docs/design/single-owners.md`; сканер `tests/negative/test_miniwave_m4_guards.py`
Never: вторая реализация факта; печатные чтения путей — только `-z` без декодирующих аргументов. Исключения — guard'ы в design-таблице. Снимок `BASELINE-*.untracked` — сырой NUL, один читатель `read_untracked_snapshot`. Путь — только `GitRepository`.
Gate: [ -f docs/design/single-owners.md ] && ! grep -rqn "_legacy_plan_split" awf --include='*.py' && python3 -m pytest tests/negative/test_miniwave_m4_guards.py -q -k m4 --timeout=120 -p no:cacheprovider

| Факт | Модуль-владелец | Маркер |
| --- | --- | --- |
| blob и mode изменения | `awf/infra/git_model.py` | `class ChangeRecord` |
| пути дерева и untracked | `awf/infra/git_model.py` | `def untracked_paths` |
| пути относительно baseline | `awf/infra/git_model.py` | `def changed_paths` |
| индекс против ревизии | `awf/infra/git_model.py` | `def staged_paths` |
| набор файлов без baseline | `awf/commit_delta.py` | `def worktree_file_set` |
| предсуществующие untracked базового снимка | `awf/infra/git_model.py` | `def read_untracked_snapshot` |
| базовый снимок дерева (запись) | `awf/api/pipeline.py` | `def create_baseline` |
| статус на baseline (`BASELINE-*.status`, печатный долг) | `awf/commit_delta.py` | `def staged_at_baseline` |
| публикационные факты | `awf/publish.py` | `class UnitReceipt` |
| состояние операции | `awf/infra/state_store/_ops.py` | `def write_operation` |
| печатные чтения путей — только в исключениях | `tests/negative/test_miniwave_m4_guards.py` | `def find_printed_git_path_reads` |
