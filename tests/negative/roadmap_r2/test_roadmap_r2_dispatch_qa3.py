"""R2.2 QA, iter 3 (TODO-0221): the new dirty/isolate surfaces must stay
strict-UTF-8 encodable for ANY path bytes.

Файл КРАСНЫЙ на текущем дереве (находка стадии agent-qa-review, iter 3).

Путь git — ЛЮБАЯ байтовая строка, не только корректный UTF-8. R2.2 добавил
три НОВЫЕ пользовательские поверхности, которые печатают путь из модели R2.1:

* ``dispatch_policy.format_dirty_refusal`` — сообщение отказа require_clean;
* ``dispatch_policy.format_isolate_report`` — isolate-предупреждение и
  dependency-warning;
* ``dispatch._dirty_tree_classes`` -> ``_format_dirty_warning`` — dirty-warning.

Все они берут ``git_model.display_path`` (сырое ``surrogateescape``-декодирование)
вместо ``git_model.escape_for_ui``. Для имени с не-UTF8 байтом ``display_path``
даёт lone surrogate (U+DC80..U+DCFF), и строка перестаёт кодироваться строгим
UTF-8 sink'ом: ``json.dumps(..., ensure_ascii=False).encode("utf-8")`` (канал
вывода awf) бросает ``UnicodeEncodeError``, а MCP-клиент получает в JSON
escape ``\\udcFF``, который не декодируется в корректную UTF-8 строку.

Тот же класс, что TODO-0220 стадия QA чинила для ``escape_for_ui``/unmerged
(``test_roadmap_r2_git_model_qa2.py``): сообщение-поверхность обязана быть
strict-UTF-8 encodable. R2.2 не зовёт ``escape_for_ui`` на этих трёх
поверхностях.

Прод-код стадией QA не правится (правило итерации); фикс — следующим проходом
через REVIEW. Все репозитории — disposable в ``tmp_path``.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from awf import api
from awf.api._errors import AwfApiError

SRC = "TODO-0001"
NON_UTF8 = b"\xff\xfe.txt"  # invalid UTF-8 byte name


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
        project_name="R2QA3",
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


def _write_non_utf8(repo: Path) -> None:
    (repo / os.fsdecode(NON_UTF8)).write_bytes(b"wip\n")


def _safe(text: str) -> str:
    """ASCII-safe описание строки — чтобы сам прогон не падал на lone
    surrogate и красный был чистым утверждением."""
    return text.encode("utf-8", "backslashreplace").decode("ascii", "backslashreplace")


def _assert_strict_utf8(text: str, what: str) -> None:
    has_surrogate = any(0xDC80 <= ord(ch) <= 0xDCFF for ch in text)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        broken = True
    else:
        broken = False
    if has_surrogate or broken:
        pytest.fail(
            f"{what} несёт сырой high-байт пути (не-UTF8) и не кодируется "
            f"строгим UTF-8 sink'ом; ascii-safe={_safe(text)}"
        )


def test_require_clean_refusal_message_is_utf8_encodable_for_non_utf8_path(tmp_path):
    """Отказ require_clean с именем, содержащим не-UTF8 байт, обязан
    оставаться кодируемым в UTF-8 строгим sink'ом (JSON/лог/CLI stdout)."""
    repo = _project(tmp_path, policy="require_clean")
    _write_non_utf8(repo)

    with pytest.raises(AwfApiError) as exc:
        api.dispatch_todo(repo, f"# {SRC}\nTask\n", todo_id=SRC)
    _assert_strict_utf8(str(exc.value), "require_clean refusal")


def test_isolate_warnings_are_utf8_encodable_for_non_utf8_path(tmp_path):
    """isolate-прогон УСПЕШЕН, но несёт dirty-warning / isolate-warning с
    именем не-UTF8; результат обязан сериализоваться каналом awf
    (``json.dumps(..., ensure_ascii=False).encode("utf-8")``)."""
    repo = _project(tmp_path, policy="isolate")
    _write_non_utf8(repo)

    res = api.dispatch_todo(repo, f"# {SRC}\nTask\n", todo_id=SRC)
    for field in (
        "dirty_warning",
        "isolate_warning",
        "isolate_dependency_warning",
    ):
        _assert_strict_utf8(getattr(res, field, ""), f"dispatch result {field}")

    try:
        json.dumps(res.as_dict(), ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        pytest.fail(
            "dispatch result не сериализуется каналом awf "
            "(json.dumps(ensure_ascii=False).encode('utf-8')) — "
            "lone surrogate пути не-UTF8"
        )


def test_claim_plan_display_path_is_utf8_encodable_for_non_utf8_path(tmp_path):
    """План claim_changes печатается CLI (``awf claim-changes`` -> stdout со
    строгим error-обработчиком): путь не-UTF8 в плане обязан быть
    strict-UTF-8 encodable, иначе CLI/JSON-канал падает."""
    from awf import claim_changes as cc

    repo = _project(tmp_path, policy="require_clean")
    api.create_baseline(repo, SRC)
    name = b"bad\xff.py"
    path = repo / os.fsdecode(name)
    path.write_bytes(b"v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add non-utf8")
    path.write_bytes(b"v2\n")

    plan = cc.plan_claim_changes(repo, SRC)
    assert plan.files, "the modified non-UTF8 file must be a claimable change"
    for f in plan.files:
        _assert_strict_utf8(f.path, "claim plan path")
