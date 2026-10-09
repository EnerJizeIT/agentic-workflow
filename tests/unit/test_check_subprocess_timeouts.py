"""check-subprocess-timeouts.py — alias resolution (scanner unit, 2026-10-09).

The gate used to hardcode the callee names ("subprocess", "_sp"). The
scanner now resolves, per file, the names that file binds to the
subprocess module or its gated members, from the file's own import
statements. These tests pin the contract: an aliased call without a
timeout is red, the same call with a timeout is green, the plain-import
detection is a regression shield, and names that are not subprocess
aliases are never counted.

The script's file name contains a dash, so it is loaded through
importlib instead of a package import.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check-subprocess-timeouts.py"


def _checker():
    spec = importlib.util.spec_from_file_location("check_subprocess_timeouts", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_sample(tmp_path: Path, code: str) -> Path:
    path = tmp_path / "sample.py"
    path.write_text(code, encoding="utf-8")
    return path


def test_module_alias_run_without_timeout_is_counted(tmp_path):
    mod = _checker()
    path = _write_sample(tmp_path, "import subprocess as _s\n_s.run(['true'])\n")
    assert mod.analyze_file(path) == (1, 1)


def test_module_alias_run_with_timeout_is_green(tmp_path):
    mod = _checker()
    path = _write_sample(
        tmp_path, "import subprocess as _s\n_s.run(['true'], timeout=5)\n"
    )
    assert mod.analyze_file(path) == (1, 0)


def test_plain_popen_without_timeout_is_counted(tmp_path):
    mod = _checker()
    path = _write_sample(tmp_path, "import subprocess\nsubprocess.Popen(['true'])\n")
    assert mod.analyze_file(path) == (1, 1)


def test_from_import_alias_without_timeout_is_counted(tmp_path):
    mod = _checker()
    path = _write_sample(tmp_path, "from subprocess import Popen as P\nP(['true'])\n")
    assert mod.analyze_file(path) == (1, 1)


def test_from_import_alias_with_timeout_is_green(tmp_path):
    mod = _checker()
    path = _write_sample(
        tmp_path, "from subprocess import run as r\nr(['true'], timeout=3)\n"
    )
    assert mod.analyze_file(path) == (1, 0)


def test_non_subprocess_names_are_never_counted(tmp_path):
    mod = _checker()
    code = (
        "import os\n"
        "import foo\n"
        "foo.run(['x'])\n"
        "os.Popen(['x'])\n"
        "run(['x'])\n"
    )
    path = _write_sample(tmp_path, code)
    assert mod.analyze_file(path) == (0, 0)


def test_resolve_subprocess_names_collects_all_import_forms():
    mod = _checker()
    code = (
        "import subprocess\n"
        "import subprocess as _s\n"
        "from subprocess import run\n"
        "from subprocess import Popen as P\n"
        "from os import path\n"
    )
    tree = ast.parse(code)
    module_aliases, member_aliases = mod.resolve_subprocess_names(tree)
    assert module_aliases == {"subprocess", "_s"}
    assert member_aliases == {"run": "run", "P": "Popen"}


# --- CLI contract through main(): exit codes are the gate's public behavior ---


def _sandbox_awf(tmp_path: Path) -> Path:
    awf = tmp_path / "awf"
    awf.mkdir()
    return awf


def test_main_flags_aliased_call_without_timeout(tmp_path, monkeypatch):
    mod = _checker()
    awf = _sandbox_awf(tmp_path)
    lines = ["import subprocess", "import subprocess as _s", ""]
    for _ in range(19):
        lines.append("subprocess.run(['true'], timeout=1)")
    lines.append("_s.run(['true'])")
    (awf / "sandbox.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert mod.main() == 1


def test_main_exit_zero_when_allowlist_matches(tmp_path, monkeypatch):
    mod = _checker()
    awf = _sandbox_awf(tmp_path)
    (awf / "api").mkdir()
    (awf / "_proc.py").write_text(
        "import subprocess\nsubprocess.Popen(['true'])\n", encoding="utf-8"
    )
    (awf / "api" / "_background.py").write_text(
        "import subprocess\nsubprocess.run(['true'])\n", encoding="utf-8"
    )
    (awf / "signal_watch.py").write_text(
        "import subprocess\nsubprocess.run(['true'])\n", encoding="utf-8"
    )
    lines = ["import subprocess"]
    for _ in range(17):
        lines.append("subprocess.run(['true'], timeout=1)")
    (awf / "rest.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert mod.main() == 0


def test_main_exit_two_on_syntax_error(tmp_path, monkeypatch):
    mod = _checker()
    awf = _sandbox_awf(tmp_path)
    (awf / "broken.py").write_text("def oops(:\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert mod.main() == 2


def test_main_exit_two_below_min_call_sites(tmp_path, monkeypatch):
    mod = _checker()
    awf = _sandbox_awf(tmp_path)
    (awf / "tiny.py").write_text(
        "import subprocess\nsubprocess.run(['true'], timeout=1)\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    assert mod.main() == 2
