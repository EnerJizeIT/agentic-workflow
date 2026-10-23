"""R2.2 QA (TODO-0221, iter 10): the transfer-link codec regressed NEG-3.

Файл КРАСНЫЙ на текущем дереве (находка стадии agent-qa-review).

Fix-8 (:mod:`awf._path_link`) заменил прежний strict-UTF-8 ридер
``read_include_list`` на общий кодек. Для NUL-формы это верно, но ЛЕГАСИ
ветка (файл без NUL) декодирует через ``surrogateescape`` и на невалидном
UTF-8 возвращает суррогатный путь ВМЕСТО ``None``.

Контракт NEG-3 (``tests/negative/test_neg3_broken_inputs.py`` (g))
прямо говорит: бинарный мусор в ссылке — это ОТСУТСТВУЮЩАЯ ссылка, не
краш. Дословно из докстроки кодека: «a binary blob → None (NEG-3: the
file is not a path list)». Прежний ``read_include_list`` ловил
``UnicodeDecodeError`` и возвращал ``None`` (это было зафиксировано
прямо в комментарии кода). Новая ветка этого не делает.

Существующий NEG-3 фикстура использует ``b"\\xff\\xfe\\x00binary garbage"``
— она содержит NUL и попадает в НУЛЕВУЮ ветку (``b"\\x00" in data`` →
``None``), поэтому регрессию легаси-ветки не ловит. Блоб БЕЗ NUL
(``b"\\xff\\xfe garbage"``) невалиден как UTF-8 и обязан дать ``None``.

Прод-код стадией QA не правится (правило итерации): фикс — следующим
проходом через REVIEW. Репозиторий — disposable в ``tmp_path``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from awf import api, include_untracked, reject_files

SRC = "TODO-0001"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _project(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    api.init_project(
        repo,
        project_name="R2QA7",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    cfg = repo / ".agentic" / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    data["config_version"] = 1
    data["discipline"] = {"dirty_policy": "isolate"}
    cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "awf init")
    return repo


#: Binary garbage WITHOUT a NUL byte — the legal-UTF-8 boundary the legacy
#: reader used to refuse (NEG-3), now decoded into a bogus surrogate path.
NO_NUL_BINARY = b"\xff\xfe garbage"


def test_legacy_include_link_binary_without_nul_degrades_to_none(tmp_path):
    """A no-NUL binary ``BASELINE-*.include`` must read as ``None`` (NEG-3).

    The pre-unit reader caught ``UnicodeDecodeError`` here. The shared codec's
    legacy branch uses ``surrogateescape``, so it returns a surrogate path and
    the caller can no longer tell a corrupt link from a real one."""
    repo = _project(tmp_path)
    ctx = repo / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    inc = ctx / f"BASELINE-{SRC}.include"
    inc.write_bytes(NO_NUL_BINARY)
    got = include_untracked.read_include_list(repo, SRC)
    assert got is None, (
        "a non-NUL binary include link must degrade to None (NEG-3): the "
        f"pre-unit reader returned None, the codec returned {got!r}"
    )


def test_legacy_reject_link_binary_without_nul_degrades_to_none(tmp_path):
    """Same NEG-3 contract for ``REJECT-*.files`` (the carry-over channel).

    A corrupt reject link must read as ``None`` so ``resolve_carry_over``
    refuses loudly instead of adopting bogus surrogate paths."""
    repo = _project(tmp_path)
    ctx = repo / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"REJECT-{SRC}.files").write_bytes(NO_NUL_BINARY)
    got = reject_files.read_reject_files(repo, SRC)
    assert got is None, (
        "a non-NUL binary reject link must degrade to None (NEG-3): the "
        f"codec returned {got!r}"
    )
