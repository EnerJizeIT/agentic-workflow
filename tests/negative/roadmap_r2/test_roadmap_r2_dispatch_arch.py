"""R2.2 architect-review (TODO-0221, iter 5): the REJECT write/read surface is
NOT byte-faithful, though the R2.2 invariant claims it is.

Файл КРАСНЫЙ на дереве до фикса стадии agent-architect-review.

R2.2 перевело канал переноса finding (``reject_files``) на raw-модель R2.1:
``snapshot_rejected_files`` теперь строит имена через ``display_path``
(``surrogateescape``), а ``read_reject_files`` / ``resolve_carry_over``
возвращают их же. Инвариант «путь — это сырые байты» объявлен общим (таблица
«поверхность × исход» в DONE: «REJECT-*.files (писатель) | RAW | переведено»).

Но поверхность записи осталась через СТРОГИЙ UTF-8:
``Path.write_text(..., encoding="utf-8")``. Имя с байтом, не складывающимся в
UTF-8, приходит как lone surrogate (U+DC80..U+DCFF) и ``encode("utf-8")``
падает ``UnicodeEncodeError``. Это НЕ ``OSError``, а ``ValueError`` — поэтому
документированный контракт ``snapshot_rejected_files`` («never raises — the
reject must not fail on it») рвётся: исключение вылетает в ``reject_commit`` и
валит сам отказ. Класс ровно тот, что QA iter 4 (F3) закрыл на печатных
поверхностях (``b"\\xff\\xfe.txt"``), но в самой REJECT-записи остался разрыв.

Независимо от исключения, файл обязан round-trip'ить raw-байты: запись и
чтение идут в одной форме (``surrogateescape``), иначе carry_over не совпадёт
с adopted-set — тот же разрыв F2/F3, только на байтах вне UTF-8.

Все репозитории — disposable в ``tmp_path``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from awf import api, paths, reject_files

SRC = "TODO-0001"
RETRY = "TODO-0002"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _project(tmp_path: Path, *, policy: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    api.init_project(
        repo,
        project_name="R2Arch",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    cfg = repo / ".agentic" / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    data["config_version"] = 1
    data["discipline"] = {"dirty_policy": policy}
    cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "awf init")
    return repo


def _create_non_utf8_untracked(repo: Path) -> str:
    """An untracked file whose name is NOT valid UTF-8; returns its raw
    display form (surrogateescape), the identity the R2.1 model reports."""
    name = b"\xff\xfe.txt"
    fd = os.open(os.fsencode(repo) + b"/" + name, os.O_CREAT | os.O_WRONLY, 0o644)
    os.write(fd, b"finding\n")
    os.close(fd)
    from awf.infra.git_model import GitRepository, display_path

    raws = GitRepository(repo).untracked_paths()
    match = [p for p in raws if os.fsencode(name) in p or p.endswith(name)]
    assert match, f"the model must list the non-UTF-8 untracked file; got {raws!r}"
    return display_path(match[0])


def test_reject_snapshot_survives_non_utf8_finding(tmp_path):
    """``snapshot_rejected_files`` must NEVER raise on a non-UTF-8 name: its
    contract is best-effort (the reject must not fail on the snapshot), and it
    must record the raw path it saw."""
    repo = _project(tmp_path, policy="isolate")
    api.dispatch_todo(repo, f"# {SRC}\norigin\n", todo_id=SRC)
    raw = _create_non_utf8_untracked(repo)

    recorded = reject_files.snapshot_rejected_files(repo, SRC, paths.logs_dir(repo))
    assert recorded == [raw], (
        "the non-UTF-8 finding must be recorded byte-faithfully "
        f"(expected {raw!r}, got {recorded!r})"
    )


def test_reject_read_round_trips_non_utf8_finding(tmp_path):
    """Write → read must round-trip the raw identity: a lone surrogate name
    survives the REJECT file so carry_over matches the adopted raw set."""
    repo = _project(tmp_path, policy="isolate")
    api.dispatch_todo(repo, f"# {SRC}\norigin\n", todo_id=SRC)
    raw = _create_non_utf8_untracked(repo)
    reject_files.snapshot_rejected_files(repo, SRC, paths.logs_dir(repo))

    again = reject_files.read_reject_files(repo, SRC)
    assert again == [raw], (
        f"read_reject_files must round-trip the raw path (got {again!r})"
    )


def test_orphaned_reject_files_survives_non_utf8_finding(tmp_path):
    """The failsafe reader over the same REJECT file must not raise either."""
    repo = _project(tmp_path, policy="isolate")
    api.dispatch_todo(repo, f"# {SRC}\norigin\n", todo_id=SRC)
    _create_non_utf8_untracked(repo)
    reject_files.snapshot_rejected_files(repo, SRC, paths.logs_dir(repo))
    # a fresh unit baselined while the file is still untracked → it is in
    # BASELINE-<retry>.untracked, the exact triple the failsafe exists for.
    api.dispatch_todo(repo, f"# {RETRY}\nretry\n", todo_id=RETRY)

    result = reject_files.orphaned_reject_files(repo, RETRY)
    assert result and any(SRC == origin for origin, _ in result), (
        f"the orphan failsafe must still measure the non-UTF-8 finding; got {result!r}"
    )
