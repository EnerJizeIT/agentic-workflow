"""M7, виток 6 (TODO-0231): force-init префлайт четвёртой поверхности docs
и счётчик долга с owner-скопом в маркере.

Z1 — ``awf init --force`` на проекте, где ``.agentic/docs`` — симлинк,
отказывается ДО ``_clean_runtime``: ``docs`` входит в единый план префлайта,
когда чекпойнт-сид правда будет писать. До фикса wipe сносил
inbox/logs/state и только потом упирался в managed-барьер. Тот же класс,
что ``test_force_init_refuses_symlinked_*_before_wipe`` в
``test_miniwave_m3c_writers.py``.

Z2 — ``TODO(owner):`` считается долгом: скобочный скоуп между маркером и
двоеточием не меняет смысла. Регулярка счётчика читает
``TODO[[:space:]]*(\\([^)]*\\))?[[:space:]]*:``; ``TODO-0001`` и
``FIXMEuser`` по-прежнему не долг.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from awf import api
from awf.api._errors import AwfApiError

T = "TODO-0001"

REPO = Path(__file__).resolve().parents[2]

D_TODOSCOPE = "TODO" + ("(user):")


def _todos_counter_command() -> str:
    for line in (REPO / ".ratchet-counters").read_text(
        encoding="utf-8"
    ).splitlines():
        if line.startswith("todos\t"):
            return line.split("\t", 1)[1]
    raise AssertionError("no 'todos' counter in .ratchet-counters")


def _count_todos(repo: Path) -> int:
    out = subprocess.run(
        ["sh", "-c", _todos_counter_command()],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def test_debt_counter_reads_owner_scoped_marker(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    repo_code = (
        "def f():\n"
        "    # " + D_TODOSCOPE + " wire retry\n"
        "    pass\n"
        "# " + "FIXME" + "(user): odd naming\n"
        "# " + "TODO" + ": real debt\n"
        "# " + "TODO-0001 not debt\n"
        "# " + "FIXME" + "user not debt\n"
    )
    (repo / "code.py").write_text(repo_code, encoding="utf-8")
    _git(repo, "add", "-A")

    assert _count_todos(repo) == 3


def test_force_init_refuses_before_wiping_when_the_docs_surface_is_a_symlink(
    tmp_path: Path,
) -> None:
    if os.name != "posix":
        pytest.skip("symlinks are POSIX-only here")
    project = tmp_path / "proj"
    project.mkdir()
    _git(project, "init", "-q")
    api.init_project(project, project_name="Z1")

    inbox = project / ".agentic" / "inbox"
    (inbox / f"{T}.md").write_text("# live task\n", encoding="utf-8")
    (inbox / f"{T}.ready").write_text("", encoding="utf-8")
    (project / ".agentic" / "logs" / "run.log").write_text(
        "log\n", encoding="utf-8"
    )

    outside = tmp_path / "outside_docs"
    outside.mkdir()
    docs = project / ".agentic" / "docs"
    shutil.rmtree(docs)
    os.symlink(outside, docs)

    with pytest.raises(AwfApiError):
        api.init_project(project, project_name="Z1b", force=True)

    assert (inbox / f"{T}.md").is_file(), (
        "the refusal must come BEFORE the force wipe: the docs surface is a "
        "write target of the checkpoint seed, so it belongs in the one init "
        "plan checked before the first effect"
    )
    assert not list(outside.iterdir()), "nothing may be written outside"
