"""W6.1 (волна 6): prove-red честен для плагинных тестов и реального красного.

Дефект 1 (editable-утечка плагина). Герметичный bootstrap снимал дубликат-
провайдеры только для топ-уровневых пакетов worktree; вложенный src-layout
подпроект (плагин agent_workflow_ui: ``<top>/src/<pkg>``) в ``own`` не
попадал, поэтому запись ``<repo>/agent_workflow_ui/src`` из editable-
установки оставалась в sys.path, и плагинные тесты видели рабочий
дерево-фикс на baseline — ``not-red`` вместо ``red-ok`` (подтверждено трижды
в волне 5b, юниты 0099/0100/0101 — красные доказывались вручную свапом).

Дефект 2 (классификатор). Любой ``has no attribute`` читался как
отсутствующий символ: ``AttributeError: 'list' object has no attribute
'get'`` — настоящий красный код под тестом — попадал в broken-runner
вместо real-red.

Тесты вызывают санитайз bootstrap в его baseline-форме (секция
``_BOOTSTRAP_SOURCE`` до передачи управлению pytest) в песочнице с
синтетическими meta_path/sys.path — без реального субпроцесса.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace

from awf.prove_red import _BOOTSTRAP_SOURCE, _classify_pytest

#: Маркер передачи управлению pytest — всё до него и есть санитайз.
_HANDOFF_MARKER = "import pytest  # noqa: E402"


def _run_bootstrap_sanitize(
    meta_path: list, sys_path: list[str]
) -> SimpleNamespace:
    """Exec секции санитайза bootstrap в песочнице с подменой ``sys``.

    Возвращает ``fake_sys`` с мутированными ``meta_path``/``path``.
    Секция берётся из ``_BOOTSTRAP_SOURCE`` (имя на момент baseline):
    строки импортов выкидываются, имена ``os``/``sys``/``Path`` связаны
    в песочнице напрямую — ``sys`` здесь синтетический.
    """
    assert _HANDOFF_MARKER in _BOOTSTRAP_SOURCE, (
        "bootstrap changed shape: the pytest hand-off marker moved"
    )
    section = _BOOTSTRAP_SOURCE.split(_HANDOFF_MARKER, 1)[0]
    code = "\n".join(
        line
        for line in section.splitlines()
        if not line.startswith(("import ", "from "))
    )
    fake_sys = SimpleNamespace(
        meta_path=list(meta_path), path=list(sys_path), argv=["pytest"]
    )
    sandbox: dict = {"os": os, "sys": fake_sys, "Path": Path}
    exec(compile(code, "_bootstrap_sanitize_section", "exec"), sandbox)
    return fake_sys


def _resolved(entries: list[str]) -> set[Path]:
    return {Path(e).resolve() for e in entries if e}


def _build_fake_worktree(tmp_path: Path) -> Path:
    """Фейковый baseline-worktree: ядро + вложенный src-layout плагин."""
    worktree = tmp_path / "worktree"
    (worktree / "awf").mkdir(parents=True)
    (worktree / "awf" / "__init__.py").write_text("", encoding="utf-8")
    plugin_pkg = worktree / "agent_workflow_ui" / "src" / "agent_workflow_ui"
    plugin_pkg.mkdir(parents=True)
    (plugin_pkg / "__init__.py").write_text("", encoding="utf-8")
    return worktree


class _EditableFinder:
    """PEP 660-style finder; имя несёт маркер "editable"."""


_EditableFinder.__module__ = "_editable_impl_agent_workflow_ui"


def test_plugin_editable_leak_is_dropped_in_hermetic_run(
    tmp_path: Path, monkeypatch
) -> None:
    """Утечка плагина (запись src в sys.path + editable-finder) снята,
    а плагин резолвится из самого worktree."""
    worktree = _build_fake_worktree(tmp_path)
    # Синтетическое «рабочее дерево»: editable-установка плагина, чей
    # src-каталог (<repo>/agent_workflow_ui/src) влез в sys.path.
    working_tree = tmp_path / "working_tree"
    leak_src = (
        working_tree / "agent_workflow_ui" / "src" / "agent_workflow_ui"
    )
    leak_src.mkdir(parents=True)
    (leak_src / "__init__.py").write_text("LEAK = True\n", encoding="utf-8")
    stdlib = tmp_path / "stdlib"
    stdlib.mkdir()

    monkeypatch.chdir(worktree)
    fake_sys = _run_bootstrap_sanitize(
        meta_path=[object(), _EditableFinder],
        sys_path=[str(worktree), str(leak_src.parent), str(stdlib)],
    )

    # Editable-finder снят с meta_path (маркер "editable").
    assert _EditableFinder not in fake_sys.meta_path
    # Запись рабочего дерева, провайдящая пакет плагина, снята с sys.path.
    assert Path(leak_src.parent).resolve() not in _resolved(fake_sys.path)
    # Провайдер плагина из САМОГО worktree добавлен: baseline импортирует
    # плагин из checkout, а не из установки.
    provider = (worktree / "agent_workflow_ui" / "src").resolve()
    assert provider in _resolved(fake_sys.path)
    # Не-проектные записи (stdlib) и корень worktree на месте.
    assert stdlib.resolve() in _resolved(fake_sys.path)
    assert worktree.resolve() in _resolved(fake_sys.path)


_OBJ_ATTR_OUTPUT = """\
collected 1 item

tests/test_list_get.py::test_get
    x = [1]
    x.get("k")
E   AttributeError: 'list' object has no attribute 'get'

=========================== short test summary info ============================
FAILED tests/test_list_get.py::test_get - AttributeError: 'list' object has no attribute 'get'
============================== 1 failed in 0.05s ===============================
"""


def test_attribute_error_on_object_is_real_red(tmp_path: Path) -> None:
    """``'list' object has no attribute 'get'`` — баг кода под тестом
    (тест исполнен, «1 failed»): real-red, а не broken-runner."""
    project = tmp_path / "proj"
    (project / "awf").mkdir(parents=True)
    (project / "awf" / "__init__.py").write_text("", encoding="utf-8")

    assert _classify_pytest(1, _OBJ_ATTR_OUTPUT, project) == "real-red"


_MOD_ATTR_OUTPUT = """\
collected 1 item

tests/test_new_symbol.py::test_new
    import awf.prove_red
    awf.prove_red.new_symbol()
E   AttributeError: module 'awf.prove_red' has no attribute 'new_symbol'

=========================== short test summary info ============================
FAILED tests/test_new_symbol.py::test_new - AttributeError: module 'awf.prove_red' has no attribute 'new_symbol'
============================== 1 failed in 0.05s ===============================
"""


def test_module_attribute_error_is_symbol_missing_red(tmp_path: Path) -> None:
    """Регресс (зелёный и на baseline): отсутствующий символ НОВОГО кода
    на существующем модуле — ``module 'awf.prove_red' has no attribute`` —
    остаётся symbol-missing-red, а не real-red."""
    project = tmp_path / "proj"
    (project / "awf").mkdir(parents=True)
    (project / "awf" / "__init__.py").write_text("", encoding="utf-8")
    (project / "awf" / "prove_red.py").write_text("", encoding="utf-8")

    assert (
        _classify_pytest(1, _MOD_ATTR_OUTPUT, project) == "symbol-missing-red"
    )


_TYPE_ATTR_OUTPUT = """\
collected 1 item

tests/test_foo_class.py::test_bar
    foo.Foo.bar
E   AttributeError: type object 'Foo' has no attribute 'bar'

=========================== short test summary info ============================
FAILED tests/test_foo_class.py::test_bar - AttributeError: type object 'Foo' has no attribute 'bar'
============================= 1 failed in 0.05s ===============================
"""


def test_type_object_attribute_error_is_real_red(tmp_path: Path) -> None:
    """``type object 'Foo' has no attribute 'bar'`` — рендеринг Python для
    ошибки доступа к атрибуту класса (тест исполнен, «1 failed»): real-red,
    а не symbol-missing-red → broken-runner (F-1: regex не знал форму,
    которую Python печатает для ошибок на классе)."""
    project = tmp_path / "proj"
    (project / "awf").mkdir(parents=True)
    (project / "awf" / "__init__.py").write_text("", encoding="utf-8")

    assert _classify_pytest(1, _TYPE_ATTR_OUTPUT, project) == "real-red"


def test_failed_summary_does_not_mask_as_collection_error(tmp_path: Path) -> None:
    """Сводка «1 failed, 1 error»: исполненный тест есть — не
    collection-error (тесты считаются исполненными при «N failed»)."""
    project = tmp_path / "proj"
    (project / "awf").mkdir(parents=True)
    (project / "awf" / "__init__.py").write_text("", encoding="utf-8")
    output = (
        "collected 2 items\n"
        "F E\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_a.py::test_a - KeyError: 'apply_status'\n"
        "ERROR tests/test_b.py::test_b\n"
        "========================= 1 failed, 1 error in 0.4s =========================\n"
    )

    assert _classify_pytest(1, output, project) == "real-red"


def test_bootstrap_handoff_marker_is_stable() -> None:
    """Секция санитайза стабильно вырезается маркером (защита теста 1)."""
    assert _BOOTSTRAP_SOURCE.count(_HANDOFF_MARKER) == 1
    section = _BOOTSTRAP_SOURCE.split(_HANDOFF_MARKER, 1)[0]
    assert "worktree = Path(os.getcwd()).resolve()" in section
    assert re.search(r"sys\.path\[:\]\s*=", section)
