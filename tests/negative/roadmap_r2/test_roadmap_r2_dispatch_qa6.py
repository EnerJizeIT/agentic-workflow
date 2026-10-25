"""R2.2 QA (TODO-0221, iter 8): the transfer LINK codec is not byte-faithful.

Файл КРАСНЫЙ на текущем дереве (находка стадии agent-qa-review, iter 8).

Модель R2.1 читает пути git в NUL-протоколе и держит их как БАЙТЫ; её
доктрина прямо запрещает ``strip``/снятие C-кавычек с имени файла, а
``tree_fingerprint`` специально переписан, потому что «a newline in a name
shredded the list» (``ls-files`` + ``splitlines()``).

Но переносные ссылки R2.2 (finding-канал ``BASELINE-*.include`` и
``REJECT-*.files``) хранят RAW-пути как текст, склеенный ``"\\n"``, и читаются
обратно через ``splitlines()`` + ``.strip()``:

* ``read_include_list`` / ``read_reject_files`` → ``.splitlines()`` режет имя
  с переводом строки (``0x0a``) и с ASCII-управляющими ``0x0b/0x0c/0x1c/0x1d/
  0x1e`` (Python ``str.splitlines`` считает их границами строки);
* ``.strip()`` съедает ведущие/хвостовые пробелы имени (законные в git).

Имя ``"a\\nb.txt"`` (или ``"a\\x0cb.txt"``, или ``" leading.txt"``) поэтому
round-trip'ится в ДРУГОЙ путь:

* ``include``: на dispatch исключение берётся из in-memory списка (ещё верно),
  но на re-baseline ``transfer_exclusions`` читает испорченную ссылку →
  исключение теряется → файл возвращается в ``BASELINE-*.untracked`` →
  commit-gate сочтёт его pre-existing и молча выбросит из коммита юнита
  (ровно класс silent loss, который чинил M4 для не-UTF-8 — там байт
  0xDC80..0xDCFF, здесь ASCII-управляющий/пробел);
* ``carry_over``: ``REJECT-*.files`` — единственный источник, поэтому путь
  портится уже на dispatch → finding не исключается из снапшота retry.

M4 (REVIEW-фикс 7) закрыл только суррогаты (не-UTF-8); ASCII-управляющие и
краевые пробелы остались. Прод-код стадией QA не правится (правило
итерации); фикс — следующим проходом через REVIEW. Все репозитории —
disposable в ``tmp_path``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from awf import api, include_untracked, paths, reject_files

SRC = "TODO-0001"
RETRY = "TODO-0002"

#: Legal git names the newline-joined text codec cannot represent.
NEWLINE_NAME = os.fsdecode(b"a\nb.txt")
CONTROL_NAME = os.fsdecode(b"a\x0cb.txt")
SPACE_NAME = " leading.txt"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _project(tmp_path: Path, *, policy: str = "isolate") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    api.init_project(
        repo,
        project_name="R2QA6",
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


def _make(repo: Path, name: str) -> None:
    """Create an untracked file whose name carries the exact bytes."""
    fs_path = os.path.join(os.fsencode(repo), os.fsencode(name))
    with open(fs_path, "wb") as f:
        f.write(b"payload\n")


def test_include_link_roundtrips_a_newline_name(tmp_path):
    """The ``BASELINE-*.include`` link must round-trip the exact raw path — the
    same byte-faithfulness M4 delivered for a non-UTF-8 name. A newline (and
    an ASCII control byte / a leading space) is shredded by ``splitlines()`` +
    ``strip()``."""
    repo = _project(tmp_path)
    _make(repo, NEWLINE_NAME)

    api.dispatch_todo(
        repo, f"# {SRC}\norigin\n", todo_id=SRC, include_untracked=[NEWLINE_NAME]
    )
    link = repo / ".agentic" / "context" / f"BASELINE-{SRC}.include"
    # The new NUL-terminated link form: each entry followed by a NUL (the
    # newline separator is what shredded the name, so it is gone).
    assert link.read_bytes() == os.fsencode(NEWLINE_NAME) + b"\x00"
    assert include_untracked.read_include_list(repo, SRC) == [NEWLINE_NAME], (
        "the include link shredded the newline name — the reader returns "
        f"{include_untracked.read_include_list(repo, SRC)!r}, not the raw path"
    )


def test_included_newline_name_survives_a_rebaseline(tmp_path):
    """Silent loss: an adopted newline-named file must stay OUT of
    ``BASELINE-*.untracked`` on a re-baseline, or the commit gate drops it."""
    repo = _project(tmp_path)
    _make(repo, NEWLINE_NAME)
    api.dispatch_todo(
        repo, f"# {SRC}\norigin\n", todo_id=SRC, include_untracked=[NEWLINE_NAME]
    )
    snap = repo / ".agentic" / "context" / f"BASELINE-{SRC}.untracked"
    assert snap.read_text(encoding="utf-8").strip() == ""  # dispatch is correct

    from awf.api.hygiene import transfer_exclusions

    carry_over, include, claim = transfer_exclusions(repo, SRC)
    assert include == {NEWLINE_NAME}, (
        f"re-baseline loses the include exclusion: include={include!r}"
    )
    api.create_baseline(
        repo, SRC,
        carry_over=carry_over or None,
        include=include or None,
        claim=claim or None,
    )
    assert snap.read_text(encoding="utf-8").strip() == "", (
        "the adopted newline-named file returned to BASELINE-*.untracked after "
        f"re-baseline — the commit gate will silently drop it: {snap.read_text()!r}"
    )


def test_carry_over_control_byte_finding_is_adopted(tmp_path):
    """The ``REJECT-*.files`` link is the carry-over channel's single source:
    a control-byte name must round-trip, or the retry's snapshot keeps the
    finding as pre-existing and the commit gate drops it."""
    repo = _project(tmp_path)
    api.dispatch_todo(repo, f"# {SRC}\norigin\n", todo_id=SRC)
    _make(repo, CONTROL_NAME)

    recorded = reject_files.snapshot_rejected_files(repo, SRC, paths.logs_dir(repo))
    assert recorded == [CONTROL_NAME], f"snapshot recorded {recorded!r}"
    assert reject_files.resolve_carry_over(repo, SRC) == [CONTROL_NAME], (
        "REJECT-*.files shredded the control-byte name: "
        f"{reject_files.resolve_carry_over(repo, SRC)!r}"
    )

    api.dispatch_todo(
        repo, f"# {RETRY}\nretry\n", todo_id=RETRY, carry_over_from=SRC
    )
    snap = repo / ".agentic" / "context" / f"BASELINE-{RETRY}.untracked"
    assert snap.read_text(encoding="utf-8").strip() == "", (
        "carried-over control-byte finding is NOT excluded from the snapshot: "
        f"{snap.read_text()!r}"
    )


def test_include_link_keeps_an_edge_space_name(tmp_path):
    """A leading/trailing space is legal in a git name; ``.strip()`` on read
    silently rewrites the path, so the exclusion no longer matches."""
    repo = _project(tmp_path)
    _make(repo, SPACE_NAME)
    api.dispatch_todo(
        repo, f"# {SRC}\norigin\n", todo_id=SRC, include_untracked=[SPACE_NAME]
    )
    assert include_untracked.read_include_list(repo, SRC) == [SPACE_NAME], (
        "the include link stripped the edge space from the name"
    )
