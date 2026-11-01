"""A21: the selector must be conservative — a missed regression is worse.

Every rule is pinned here on a synthetic project: core forces the full suite,
a module change pulls its importers, its name match, its literal mentions and
its neighbour suite, and a selection larger than the threshold gives up and
runs everything.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from awf import test_selection


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".agentic").mkdir(parents=True)
    (project / "awf" / "api").mkdir(parents=True)
    (project / "tests" / "unit").mkdir(parents=True)
    (project / "tests" / "negative").mkdir(parents=True)

    files = {
        "awf/foo.py": "VALUE = 1\n",
        "awf/api/bar.py": "VALUE = 2\n",
        "awf/pipeline_engine.py": "CORE = True\n",
        "tests/unit/test_foo.py": "from awf.foo import VALUE\n\n\ndef test_x():\n    assert VALUE\n",
        "tests/unit/test_bar.py": "from awf.api.bar import VALUE\n\n\ndef test_y():\n    assert VALUE\n",
        "tests/unit/test_unrelated.py": "def test_z():\n    assert True\n",
        "tests/unit/test_name_match.py": "def test_q():\n    assert True\n",
        "tests/unit/test_version_sync.py": "def test_v():\n    assert True\n",
        "tests/unit/test_opencode2_only_guard.py": "def test_g():\n    assert True\n",
        "tests/negative/test_design_agentic_writers.py": "def test_d():\n    assert True\n",
        "tests/negative/test_docs_counters.py": "def test_c():\n    assert True\n",
    }
    for rel, body in files.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    # Enough unrelated files that a narrow change stays under FULL_RATIO.
    for i in range(20):
        (project / "tests" / "unit" / f"test_filler_{i}.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8"
        )

    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=project, check=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=project, check=True)
    return project


def test_no_diff_runs_everything(tmp_path):
    project = _project(tmp_path)
    result = test_selection.select(project)
    assert result.full is True
    assert "no diff" in result.reason


def test_a_core_change_forces_the_full_suite(tmp_path):
    project = _project(tmp_path)
    (project / "awf" / "pipeline_engine.py").write_text("CORE = False\n", encoding="utf-8")
    result = test_selection.select(project)
    assert result.full is True
    assert "core change" in result.reason


def test_a_module_change_pulls_importers_mentions_and_guards(tmp_path):
    project = _project(tmp_path)
    (project / "awf" / "foo.py").write_text("VALUE = 2\n", encoding="utf-8")

    result = test_selection.select(project)

    assert result.full is False
    assert "tests/unit/test_foo.py" in result.files          # import + name
    assert "tests/unit/test_version_sync.py" in result.files  # always-on guard
    assert "tests/unit/test_unrelated.py" not in result.files


def test_package_form_import_pulls_the_submodule_test(tmp_path):
    """`from awf.api import bar` imports awf.api.bar — the selector must see it.

    A file that imports the submodule only through its package used to be
    missed: no import edge, no literal mention of the module path.
    """
    project = _project(tmp_path)
    (project / "tests" / "unit" / "test_package_form.py").write_text(
        "from awf.api import bar as bar_mod\n\n\ndef test_p():\n    assert bar_mod.VALUE\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "package form"], cwd=project, check=True)

    (project / "awf" / "api" / "bar.py").write_text("VALUE = 3\n", encoding="utf-8")

    result = test_selection.select(project)
    assert "tests/unit/test_package_form.py" in result.files


def test_package_form_imports_behind_a_comment_still_count(tmp_path):
    """`from awf.api import bar  # noqa` keeps its edge; a lost edge is a
    missed test, which the selector must never do silently.

    Both comment shapes real tests use: end of line, and inside the
    parentheses of a multi-line import.
    """
    project = _project(tmp_path)
    (project / "tests" / "unit" / "test_comment_single.py").write_text(
        "from awf.api import bar  # noqa: F401\n\n\ndef test_s():\n    assert bar.VALUE\n",
        encoding="utf-8",
    )
    (project / "tests" / "unit" / "test_comment_paren.py").write_text(
        "from awf.api import (\n    baz,  # noqa: F401\n)\n\n\ndef test_p():\n    assert baz.VALUE\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "comment forms"], cwd=project, check=True)

    (project / "awf" / "api" / "bar.py").write_text("VALUE = 3\n", encoding="utf-8")
    (project / "awf" / "api" / "baz.py").write_text("VALUE = 4\n", encoding="utf-8")

    result = test_selection.select(project)
    assert "tests/unit/test_comment_single.py" in result.files
    assert "tests/unit/test_comment_paren.py" in result.files


def test_a_literal_mention_selects_its_test(tmp_path):
    project = _project(tmp_path)
    (project / "tests" / "unit" / "test_name_match.py").write_text(
        "PATH = 'awf/foo.py'\n\n\ndef test_q():\n    assert PATH\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "mention"], cwd=project, check=True)

    (project / "awf" / "foo.py").write_text("VALUE = 3\n", encoding="utf-8")

    result = test_selection.select(project)
    assert "tests/unit/test_name_match.py" in result.files


def test_the_changed_test_file_itself_is_selected(tmp_path):
    project = _project(tmp_path)
    (project / "tests" / "unit" / "test_unrelated.py").write_text(
        "def test_z():\n    assert 1\n", encoding="utf-8"
    )
    result = test_selection.select(project)
    assert "tests/unit/test_unrelated.py" in result.files


def test_a_too_large_selection_becomes_the_full_suite(tmp_path):
    project = _project(tmp_path)
    (project / "awf" / "foo.py").write_text("VALUE = 2\n", encoding="utf-8")
    monkeyed = test_selection.FULL_RATIO
    try:
        test_selection.FULL_RATIO = 0.0
        result = test_selection.select(project)
    finally:
        test_selection.FULL_RATIO = monkeyed
    assert result.full is True
    assert "selection reached" in result.reason


def test_force_full_is_honoured(tmp_path):
    project = _project(tmp_path)
    result = test_selection.select(project, force_full=True)
    assert result.full is True
    assert "forced" in result.reason


def test_the_cli_prints_paths_and_explain(tmp_path, capsys):
    project = _project(tmp_path)
    (project / "awf" / "foo.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert test_selection._main(["--project-dir", str(project), "--explain"]) == 0
    out = capsys.readouterr()
    assert "tests/unit/test_foo.py" in out.out
    assert "selected" in out.err
