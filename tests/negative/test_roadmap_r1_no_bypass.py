"""R1.5 (roadmap R1): state-файлы пишет только Store — архитектурный страж.

На исходной ревизии файл КРАСНЫЙ: прямые записи ``state/run.yaml`` /
``state/current.yaml`` (и родственных артефактов состояния) разбросаны по
ядру вне пакета ``awf/infra/state_store`` — страж их находит статическим
анализом (AST), без запуска кода:

1. **запрет обходов** — ни один модуль вне пакета Store не пишет файлы
   контроля состояния (``run.yaml``, ``current.yaml``, ``service-run.yaml``,
   ``control.json``, ``migration-manifest.json``, ``state/records/``,
   ``state/operations/``). Исключения — явный список с причиной.
2. **детектор не пустой** — он обязан краснеть на синтетической прямой
   записи (анти-вакуум).
3. **reads не мутируют** — серия чтений не меняет байты/mtime состояния;
   heartbeat — ЯВНАЯ отдельная операция.

Границы: core (``awf/``) и плагин (``agent_workflow_ui/src/``).

**Что страж НЕ ловит** (документированные остатки, не повод считать его
вакуумным):

- запись по пути, пришедшему ПАРАМЕТРОМ функции (``def f(run_path):
  atomic_write_text(run_path, ...)``) — статически не отследить без
  межпроцедурного анализа. На 17.10 такой формы в дереве нет:
  ``run._rollback_projection`` переведён на Store-writers; появление
  новой формы — ручная проверка сплошным grep (см. DONE-отчёт юнита);
- снос содержимого ``state/`` в ``lifecycle._clean_state_dir`` идёт через
  Store-фасад ``delete_state_tree`` (REVIEW A5) — не обход: получатель
  внутри Store-пакета. RESET-цикл в ``lifecycle._reset_runtime_impl``
  сносит runtime-артефакты (inbox/outbox/context/logs), НЕ состояние —
  вне remit Store;
- запись через ОТКРЫТИЕ по флагам (``os.open(path, O_WRONLY|O_CREAT)``,
  ``Path.open("w")``, ``io.open``): на 17.10 писателей состояния этой формы
  в дереве нет — новая форма требует ручной проверки, как и параметр-путь.
  Класс записи через НАЗНАЧЕНИЕ ``os.replace``/``os.rename``/``shutil.move``
  /``shutil.copy*`` закрыт ниже (``WRITE_FUNC_DST_ARG1``).
"""
from __future__ import annotations

import ast
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Единственный разрешённый писатель файлов состояния — пакет Store.
STORE_PACKAGE = REPO_ROOT / "awf" / "infra" / "state_store"

#: Сканируемые корни: ядро и плагин.
SCAN_ROOTS = [REPO_ROOT / "awf", REPO_ROOT / "agent_workflow_ui" / "src"]

#: Артефакты контроля состояния (пути-владельцы — ``awf.paths`` / Store).
CONTROL_LEAF_NAMES = {
    "run.yaml",
    "service-run.yaml",
    "current.yaml",
    "control.json",
    "migration-manifest.json",
}
CONTROL_CHILD_NAMES = {"records", "operations"}

#: Функции-владельцы пути состояния. Любая ЗАПИСЬ по такому пути — обход Store.
STATE_PATH_PRODUCERS = {
    "run_file",  # awf.run_state — run.yaml / service-run.yaml
    "_state_file",  # awf.pipeline_state — current.yaml
    "control_path",
    "records_dir",
    "operations_dir",
    "manifest_path",
    "op_path",
    "_operation_record",  # awf.api.hygiene — журнал state/operations/
    "state_control_file",
    "state_records_dir",
    "state_operations_dir",
    "state_migration_manifest",
}

#: Примитивы записи: вызов (полное имя через точку) → индекс аргумента-пути.
WRITE_FUNC_ARG0 = {
    "awf._atomic.atomic_write_text",
    "atomic_write_text",
    "atomic_write_json",
    "atomic_write_bytes",
    "os.replace",
    "os.rename",
    "os.remove",
    "os.unlink",
    "shutil.move",
    "shutil.rmtree",
    "shutil.copy",
    "shutil.copy2",
    "shutil.copyfile",
    "open",
}

#: Примитивы, у которых ПУТЬ-НАЗНАЧЕНИЕ — второй аргумент: ``os.replace(src,
#: dst)`` / ``shutil.move(src, dst)`` и родня. Без этого множества страж
#: проверял только ``src`` (arg0) и СЛЕП к записи в контрольный путь через
#: назначение: ``os.replace(tmp, run_state.run_file(p))`` проходил мимо.
WRITE_FUNC_DST_ARG1 = {
    "os.replace",
    "os.rename",
    "shutil.move",
    "shutil.copy",
    "shutil.copy2",
    "shutil.copyfile",
}

#: Методы-записи: получатель — путь. ``mkdir`` не входит (создание каталога
#: состояния — не запись файла состояния).
METHOD_WRITE_NAMES = {"write_text", "write_bytes", "unlink", "touch"}
METHOD_PATH_ARG_METHODS = {"rename", "replace"}  # receiver ИЛИ аргумент

#: Явные исключения: файл → причина. Сейчас пусто: снос ``state/`` ушёл в
#: Store-фасад ``delete_state_tree`` (REVIEW A5), а RESET-цикл трогает
#: runtime-артефакты (inbox/outbox/...), не состояние.
ALLOWLIST: dict[str, str] = {}


def _dotted(node: ast.AST) -> str:
    """Dotted name of an Attribute/Name expression, or ''."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    else:
        return ""
    return ".".join(reversed(parts))


def _call_producer(expr: ast.AST, names: set[str]) -> bool:
    for n in ast.walk(expr):
        if isinstance(n, ast.Call):
            dotted = _dotted(n.func)
            if dotted and dotted.split(".")[-1] in names:
                return True
    return False


def _string_literals(expr: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(expr):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.add(n.value)
    return out


def _is_control_path(expr: ast.AST) -> bool:
    """Является ли выражение путём контроля состояния?"""
    if _call_producer(expr, STATE_PATH_PRODUCERS):
        return True
    lits = _string_literals(expr)
    if lits & CONTROL_LEAF_NAMES:
        return True
    # ``state_dir(...) / "records"`` / ``"operations"``
    if _call_producer(expr, {"state_dir"}) and (lits & CONTROL_CHILD_NAMES):
        return True
    return False


def _collect_aliases(tree: ast.AST) -> dict[str, list[ast.AST]]:
    """Простая карта ``имя → выражения`` из присваиваний.

    Ловит обход через локальную переменную: ``state_path = _state_file(p);
    atomic_write_text(state_path, ...)`` — путь приходит по имени, а не
    литералом в самой записи. Одна карта на файл (без переприсваиваний —
    «любое связывание имени путём состояния»); для стража консервативно.
    """
    aliases: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if value is None:
                continue
            for t in targets:
                if isinstance(t, ast.Name):
                    aliases.setdefault(t.id, []).append(value)
        elif isinstance(node, ast.NamedExpr):
            if isinstance(node.target, ast.Name):
                aliases.setdefault(node.target.id, []).append(node.value)
    return aliases


def _collect_import_aliases(tree: ast.AST) -> dict[str, str]:
    """Локальное имя примитива записи → его канонический dotted-путь.

    Закрывает обход через импорт-алиас: ``from awf._atomic import
    atomic_write_text as _atomic`` и ``_atomic(state_file, ...)`` раньше
    проходили мимо детектора (звался ``_atomic``, а в списке примитивов —
    ``atomic_write_text``) — а именно так писал ``current.yaml``
    ``awf/api/pipeline.py``. Импорт без алиаса тоже попадает в карту
    (``atomic_write_text`` → ``awf._atomic.atomic_write_text``).
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # Relative imports (``from .._atomic import ...``) resolve to a
            # bare name: the guard matches write primitives by module too
            # (``awf._atomic.atomic_write_text``), and the package root is
            # not known from the file. The bare name is in WRITE_FUNC_ARG0.
            module = node.module or ""
            prefix = module if (module and not node.level) else ""
            for a in node.names:
                local = a.asname or a.name
                out[local] = f"{prefix}.{a.name}" if prefix else a.name
        elif isinstance(node, ast.Import):
            for a in node.names:
                local = a.asname or a.name.split(".")[0]
                out[local] = a.name
    return out


def _resolve_name(dotted: str, import_aliases: dict[str, str]) -> str:
    """Разрешить dotted-имя вызова через карту импорт-алиасов."""
    if not dotted:
        return ""
    if dotted in import_aliases:
        return import_aliases[dotted]
    head, _, tail = dotted.partition(".")
    if tail and head in import_aliases:
        return f"{import_aliases[head]}.{tail}"
    return dotted



def _is_control_path_alias(
    expr: ast.AST, aliases: dict[str, list[ast.AST]], depth: int = 3
) -> bool:
    """``_is_control_path`` с разыменованием имён-алиасов (глубина ``depth``)."""
    if _is_control_path(expr):
        return True
    if depth <= 0:
        return False
    for n in ast.walk(expr):
        if isinstance(n, ast.Name) and n.id in aliases:
            for bound in aliases[n.id]:
                if _is_control_path_alias(bound, aliases, depth - 1):
                    return True
    return False


def _write_targets(
    tree: ast.AST, import_aliases: dict[str, str]
) -> list[tuple[int, ast.AST]]:
    """(lineno, target-expression) для каждой ЗАПИСИ файла в дереве.

    Имя вызванной функции-записи разрешается через ``import_aliases``:
    ``_atomic(path, ...)`` при ``... import atomic_write_text as _atomic``
    распознаётся как ``awf._atomic.atomic_write_text``.
    """
    out: list[tuple[int, ast.AST]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted(node.func)
        resolved = _resolve_name(dotted, import_aliases) if dotted else ""
        # Функция-запись с путём в позиции 0.
        if resolved in WRITE_FUNC_ARG0 and node.args:
            out.append((node.lineno, node.args[0]))
            # Двухпутевые примитивы пишут в НАЗНАЧЕНИЕ (arg1) — проверяем и
            # его, иначе обход через ``os.replace(shadow, control_path)``
            # остаётся незамеченным.
            if resolved in WRITE_FUNC_DST_ARG1 and len(node.args) > 1:
                out.append((node.lineno, node.args[1]))
            continue
        # Метод-запись: получатель — путь.
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in METHOD_WRITE_NAMES:
                out.append((node.lineno, node.func.value))
            elif attr in METHOD_PATH_ARG_METHODS:
                out.append((node.lineno, node.func.value))
                if node.args:
                    out.append((node.lineno, node.args[0]))
    return out


def find_bypasses(source: str, filename: str) -> list[str]:
    """Прямые записи состояния в исходнике → человекочитаемые находки."""
    tree = ast.parse(source, filename=filename)
    aliases = _collect_aliases(tree)
    import_aliases = _collect_import_aliases(tree)
    findings: list[str] = []
    for lineno, target in _write_targets(tree, import_aliases):
        if _is_control_path(target) or _is_control_path_alias(target, aliases):
            snippet = ast.unparse(target)
            findings.append(f"{filename}:{lineno}: {snippet}")
    return findings


def _iter_sources() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if STORE_PACKAGE in path.parents:
                continue
            if str(path) in ALLOWLIST:
                continue
            files.append(path)
    return sorted(files)


# ─── 1. Страж: обходов вне Store нет ─────────────────────────────────────


def test_no_direct_state_writers_outside_store():
    offenders: list[str] = []
    for path in _iter_sources():
        offenders.extend(
            find_bypasses(path.read_text(encoding="utf-8"), str(path.relative_to(REPO_ROOT)))
        )
    assert not offenders, (
        "R1.5: прямые записи файлов состояния вне awf/infra/state_store — "
        "переведи на Store (write_state_file / remove_state_file / "
        "transact) или добавь явное исключение с причиной:\n  "
        + "\n  ".join(offenders)
    )


def test_allowlist_entries_have_reason():
    for path, reason in ALLOWLIST.items():
        assert reason.strip(), f"{path}: исключение без причины"


# ─── 2. Мутационная проверка: детектор не пуст ───────────────────────────


def test_detector_flags_synthetic_direct_write():
    """Искусственная прямая запись обязана быть поймана — иначе страж
    зелёный ни на чём (анти-вакуум)."""
    bad = (
        "from awf import run_state\n"
        "from awf._atomic import atomic_write_text\n"
        "def f(project_dir):\n"
        "    atomic_write_text(run_state.run_file(project_dir), 'x')\n"
    )
    found = find_bypasses(bad, "synthetic_bypass.py")
    assert found, "детектор пропустил искусственную прямую запись run.yaml"

    bad2 = "def g(p):\n    (p / 'current.yaml').write_text('x')\n"
    assert find_bypasses(bad2, "synthetic_bypass.py"), "пропущена запись current.yaml"

    bad3 = (
        "def h(p):\n"
        "    (state_dir(p) / 'records' / 'r.json').write_text('x')\n"
    )
    assert find_bypasses(bad3, "synthetic_bypass.py"), "пропущена запись records/"

    # Обход через локальный алиас: путь приходит по имени.
    bad4 = (
        "from awf.pipeline_state import _state_file\n"
        "from awf._atomic import atomic_write_text\n"
        "def k(p):\n"
        "    state_path = _state_file(p)\n"
        "    atomic_write_text(state_path, 'x')\n"
    )
    assert find_bypasses(bad4, "synthetic_bypass.py"), "пропущен обход через алиас"

    # Обход через ИМПОРТ-алиас примитива записи (класс: current.yaml писался
    # как ``_atomic(state_file, ...)`` в awf/api/pipeline.py).
    bad5 = (
        "from awf.paths import state_dir\n"
        "from awf._atomic import atomic_write_text as _atomic\n"
        "def m(p):\n"
        "    state_file = state_dir(p) / 'current.yaml'\n"
        "    _atomic(state_file, 'x')\n"
    )
    assert find_bypasses(bad5, "synthetic_bypass.py"), (
        "пропущен обход через импорт-алиас примитива записи"
    )

    # Обход через НАЗНАЧЕНИЕ двухпутевого примитива: запись идёт в arg1,
    # src — безобидный temp. Класс: ``os.replace(tmp, current.yaml)``.
    bad6 = (
        "import os\n"
        "from awf import run_state\n"
        "def n(p):\n"
        "    os.replace('/tmp/x', run_state.run_file(p))\n"
    )
    assert find_bypasses(bad6, "synthetic_bypass.py"), (
        "пропущена запись в контрольный путь через назначение os.replace"
    )

    bad7 = (
        "import shutil\n"
        "from awf import run_state\n"
        "def q(p):\n"
        "    shutil.move('/tmp/x', run_state.run_file(p))\n"
    )
    assert find_bypasses(bad7, "synthetic_bypass.py"), (
        "пропущена запись в контрольный путь через назначение shutil.move"
    )


def test_detector_allows_store_and_reads():
    """Зелёные случаи: сам Store и обычные (не-)записи не ловятся."""
    ok = (
        "from pathlib import Path\n"
        "def read_state_step(p):\n"
        "    p.joinpath('state', 'control.json').read_text()\n"
        "def unrelated(p):\n"
        "    (p / 'logs' / 'out.log').write_text('x')\n"
        "    (p / 'state' / 'dashboard_port').write_text('1')\n"
    )
    assert not find_bypasses(ok, "synthetic_ok.py"), "ложное срабатывание на чтении/чужих путях"


# ─── 3. Reads не мутируют state; heartbeat явный ─────────────────────────


def _state_fingerprint(project_dir: Path) -> dict[str, tuple[int, bytes]]:
    fp: dict[str, tuple[int, bytes]] = {}
    state_dir = project_dir / ".agentic" / "state"
    if not state_dir.exists():
        return fp
    for path in sorted(state_dir.rglob("*")):
        if path.is_file():
            st = path.stat()
            fp[str(path.relative_to(project_dir))] = (st.st_mtime_ns, path.read_bytes())
    return fp


def test_reads_do_not_mutate_state(tmp_path):
    import awf.api as api
    from awf import pipeline_state, run_state

    proj = tmp_path / "proj"
    (proj / ".agentic" / "state").mkdir(parents=True)

    # Заводим активный run (валидная форма: непустая очередь) и состояние.
    run_state.write_run(
        proj,
        active=True,
        queue=[{"todo_id": "TODO-0001", "pipeline": ""}],
        index=0,
        current="TODO-0001",
        goal="g",
    )
    pipeline_state.write_state(proj, stage_name="plan", todo_id="TODO-0001")

    before = _state_fingerprint(proj)
    assert before, "состояние не создано — тест бессмыслен"

    # Серия чтений: фасады + верхнеуровневые read-API/mutators чтения.
    for _ in range(3):
        run_state.read_run(proj)
        pipeline_state.read_state(proj)
        run_state.run_is_active(proj)
        api.run_status(proj)
        try:
            api.brief(proj)
        except TypeError:
            pass

    after = _state_fingerprint(proj)
    assert before == after, "чтение изменило файлы состояния (байты или mtime)"


# ─── 4. dispatch: запись через ManagedFS, симлинк-инбокс → отказ ──────────


def test_dispatch_refuses_symlinked_inbox(tmp_git_repo):
    """R1.5 (добор 0215): ``.agentic/inbox`` — симлинк наружу → отказ до
    записи; внешний canary не тронут (S9-01 класс)."""
    from awf import api

    proj = tmp_git_repo
    api.init_project(proj, project_name="DispatchSymlink")

    agentic = proj / ".agentic"
    shutil.rmtree(agentic / "inbox", ignore_errors=True)
    outside = tmp_git_repo.parent / "outside"
    outside.mkdir()
    canary = outside / "TODO-0001.md"
    canary.write_text("synthetic disposable file\n", encoding="utf-8")
    (agentic / "inbox").symlink_to(outside, target_is_directory=True)

    with pytest.raises(Exception):
        api.dispatch_todo(proj, content="# TODO — x\n", todo_id="TODO-0001")

    assert canary.read_text(encoding="utf-8") == "synthetic disposable file\n", (
        "dispatch записал TODO через симлинкнутый inbox наружу проекта"
    )


def test_heartbeat_is_explicit_and_separate(tmp_path):
    from awf import run_state

    proj = tmp_path / "proj"
    (proj / ".agentic" / "state").mkdir(parents=True)
    run_state.write_run(
        proj,
        active=True,
        queue=[{"todo_id": "TODO-0001", "pipeline": ""}],
        index=0,
        current="TODO-0001",
        generation=1,
        last_supervisor_beat=None,
    )

    before = _state_fingerprint(proj)

    # Чтение НЕ дёргает heartbeat.
    run_state.read_run(proj)
    assert _state_fingerprint(proj) == before, "чтение незаметно обновило heartbeat"

    # Явный вызов heartbeat — единственный, кто пишет последний отпечаток.
    run_state.supervisor_beat(proj)
    after = _state_fingerprint(proj)
    assert after != before, "явный heartbeat ничего не записал"
    assert run_state.read_run(proj).get("last_supervisor_beat"), (
        "heartbeat не отразился в состоянии"
    )
