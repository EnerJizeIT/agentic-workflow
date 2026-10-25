"""M7, виток 5 (TODO-0231): два HIGH-фикса и слои из аудита.

W1 — счётчик витков не читает ``iter`` (фикс в test_miniwave_m7_hygiene).
W2 — bare-форма аннотации (там же).
Здесь:

- **W3** — оба писателя TODO (``review_append`` / ``update_todo``)
  переживают не-UTF-8 файл: чтение И запись в surrogateescape, байты
  сохраняются, бэкап байт-точен;
- **W4** — изолированные git-index дельты и commit-гейта создаются в
  общем базисе ``paths.temp_base()`` (/tmp/opencode), не в системном
  temp;
- **W7** — ``awf init`` ставит чекпойнт-шаблон в
  ``.agentic/docs/checkpoint-template.md`` (только отсутствующий),
  RUN-REPORT печатает его путь; пользовательская копия не
  перезаписывается.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api, paths

T = "TODO-0001"

REPO = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _non_utf8_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for name in ("inbox", "outbox", "context", "logs", "done", "state",
                 "roles", "pipelines", "phases"):
        (project / ".agentic" / name).mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text(
        "project: demo\nmodels: {}\n", encoding="utf-8"
    )
    (project / ".agentic" / "roles" / "supervisor.md").write_text(
        "# supervisor\n", encoding="utf-8"
    )
    raw = b"# task\xff with raw \xff bytes\n"
    (project / ".agentic" / "inbox" / f"{T}.md").write_bytes(raw)
    (project / ".agentic" / "inbox" / f"{T}.ready").write_text("", encoding="utf-8")
    return project


# ─── W3: TODO writers survive a non-UTF-8 unit ──────────────────────────


def test_todo_writers_survive_non_utf8_bytes(tmp_path: Path) -> None:
    project = _non_utf8_project(tmp_path)
    todo = project / ".agentic" / "inbox" / f"{T}.md"
    original = todo.read_bytes()

    from awf.api import hygiene

    # Writer 1: update_todo while the unit is still queued (the plain
    # reword's append path)
    result1 = hygiene.update_todo(project, T, append="\n\n## Addendum\n")
    assert result1 is not None
    assert b"# task\xff with raw \xff bytes\n" in todo.read_bytes()

    # a STARTED unit: review_append belongs to a live unit only
    (project / ".agentic" / "outbox" / f"PROGRESS-{T}.md").write_text(
        "worker at it\n", encoding="utf-8"
    )

    # Writer 2: review_append on the STARTED unit
    result2 = hygiene.review_append(project, T, text="vitok body\n")
    assert result2.round == 1
    assert b"# task\xff with raw \xff bytes\n" in todo.read_bytes()

    # The backup keeps the pre-write bytes byte-faithful
    backups = sorted((project / ".agentic" / "context").glob("*.bak-*"))
    assert backups, "the append must leave a backup"
    assert backups[0].read_bytes() == original


def test_todo_write_version_check_survives_non_utf8(tmp_path: Path) -> None:
    """W3: the version-guard comparison ALSO reads surrogateescape — a
    concurrent-update refusal stays a refusal, never a UnicodeDecodeError;
    a matching write round-trips the raw bytes."""
    project = _non_utf8_project(tmp_path)
    md_path = project / ".agentic" / "inbox" / f"{T}.md"

    from awf.api import hygiene
    from awf.api._errors import AwfApiError

    expected = md_path.read_text(encoding="utf-8", errors="surrogateescape")

    with pytest.raises(AwfApiError) as err:
        hygiene._write_todo_locked(md_path, expected + " drifted", "new")
    assert "version conflict" in str(err.value)
    assert b"new" not in md_path.read_bytes(), (
        "a version conflict refuses the write; the file keeps its bytes"
    )

    hygiene._write_todo_locked(md_path, expected, expected + "\nmore\n")
    assert b"\xff" in md_path.read_bytes(), "surrogates round-trip the write"


# ─── W4: isolated git indexes live under paths.temp_base() ──────────────


def _seeded_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo, "-c", "user.email=t@t.t", "-c", "user.name=t",
        "commit", "-qm", "baseline",
    )
    return repo


def _mkstemp_spy(monkeypatch, marker: str):
    """ASA spy on the SHARED tempfile.mkstemp — the delta assembler imports
    tempfile inside its method, a module-local patch would miss it."""
    import tempfile

    seen: dict[str, object] = {}
    real = tempfile.mkstemp

    def spy(*args, **kwargs):
        if str(kwargs.get("prefix", "")).startswith(marker):
            seen["dir"] = kwargs.get("dir")
        return real(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", spy)
    return seen


@pytest.mark.parametrize("module_name,runner_name", [
    ("commit_delta", "candidate"), ("commit_gate", "commit-index"),
])
def test_isolated_index_mkdtemp_uses_temp_base(
    tmp_path: Path, monkeypatch, module_name: str, runner_name: str
) -> None:
    from awf import commit_delta, commit_gate, commit_plan

    repo = _seeded_repo(tmp_path)
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    base = tmp_path / "base"
    monkeypatch.setattr(paths, "TMP_BASE", str(base))
    module = {"commit_delta": commit_delta, "commit_gate": commit_gate}[module_name]
    seen = _mkstemp_spy(monkeypatch, f"awf-{runner_name}")

    if module_name == "commit_delta":
        assembler = commit_delta.DeltaAssembler(repo, T, logs_dir=tmp_path / "logs")
        oid, error = assembler._build_candidate_tree(["a.txt"], [])
        assert error == ""
        assert oid, "the staged isolated index must yield a tree oid"
    else:
        plan = commit_plan.CommitPlan(todo_id=T, generation=0, verified_sha=None,
                                      files=("a.txt",))
        commit_gate._commit_via_isolated_index(repo, plan, "unit commit")
    assert seen["dir"] == paths.temp_base(), (
        f"the {module_name} isolated index must be created in temp_base, "
        f"not the system temp: saw {seen['dir']}"
    )


# ─── W7: awf init installs the checkpoint template ──────────────────────


def _init_project(tmp_path: Path, name: str) -> Path:
    proj = tmp_path / name
    proj.mkdir()
    _git(proj, "init", "-q")
    api.init_project(proj, project_name=name.upper())
    return proj


def test_init_copies_the_checkpoint_template(tmp_path: Path) -> None:
    proj = _init_project(tmp_path, "p")
    dest = proj / ".agentic" / "docs" / "checkpoint-template.md"
    assert dest.is_file()
    body = (REPO / "awf" / "templates" / "docs" / "checkpoint-template.md").read_text(
        encoding="utf-8"
    )
    assert dest.read_text(encoding="utf-8") == body


def test_init_never_overwrites_the_user_copy(tmp_path: Path) -> None:
    proj = _init_project(tmp_path, "p")
    dest = proj / ".agentic" / "docs" / "checkpoint-template.md"
    dest.write_text("# the user tuned it\n", encoding="utf-8")
    api.init_project(proj, force=True)
    assert dest.read_text(encoding="utf-8") == "# the user tuned it\n"


def test_re_init_seeds_a_missing_checkpoint_template(tmp_path: Path) -> None:
    proj = _init_project(tmp_path, "p")
    dest = proj / ".agentic" / "docs" / "checkpoint-template.md"
    dest.unlink()
    api.init_project(proj)
    assert dest.is_file(), "a re-init seeds the missing copy again"
