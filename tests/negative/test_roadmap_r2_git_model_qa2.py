"""R2.1 QA, iter 5 (roadmap R2, unit R2.1): UI escaping of non-UTF-8 bytes.

Файл КРАСНЫЙ на текущем дереве (находка стадии agent-qa-review).

Путь git может быть ЛЮБОЙ байтовой строкой, не только корректным UTF-8.
Отказ модели — это UI-поверхность (AR-2: сообщение об unmerged-конфликте
обязано показывать control-символы экранированно, а не сырыми). Но
``escape_for_ui`` экранирует только C0/DEL: байт >= 0x80, который не
складывается в валидный UTF-8, декодируется в lone surrogate
(U+DC80..U+DCFF) через ``surrogateescape`` и уходит в сообщение КАК ЕСТЬ.

Следствие: строка отказа не кодируется в UTF-8 строгим sink'ом
(``str.encode("utf-8")`` бросает ``UnicodeEncodeError``), а awf печатает
JSON через ``json.dumps(..., ensure_ascii=False)`` и пишет его в UTF-8
строго — то есть честный типизированный отказ сам ломает вывод. Тот же
lone-surrogate роняет и канал отчёта pytest-xdist (INTERNALERROR), что
видно при полном прогоне.

Красный-до: тесты падают на текущем дереве. Прод-код стадией QA не
правится (правило итерации); фикс — следующим проходом через REVIEW.

Все репозитории — disposable в ``tmp_path``. Реальный git не трогается.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _model():
    """Ленивый импорт: на базовом дереве символа нет. Провал — утверждение
    «модуля нет», а не import-ошибка."""
    try:
        from awf.infra import git_model
    except ImportError as e:
        raise AssertionError(f"R2.1 git_model module is missing: {e}") from e
    return git_model


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, capture_output=True
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit(repo: Path, message: str = "c") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _safe(text: str) -> str:
    """ASCII-safe описание строки для сообщения об ошибке — чтобы сам прогон
    не падал на lone-surrogate и красный был чистым утверждением."""
    return text.encode("utf-8", "backslashreplace").decode("ascii", "backslashreplace")


def test_escape_for_ui_output_is_utf8_encodable_for_any_bytes():
    """Путь — произвольные байты; UI-экранирование обязано дать строку,
    которую строгий UTF-8 sink (log/JSON/file) примет без исключения.

    Байт ``0xff`` не является валидным UTF-8: ``surrogateescape`` даёт
    lone surrogate ``U+DCFF``. Сегодня он уходит в вывод сырым, и строка
    перестаёт кодироваться в UTF-8 — экранирование не выполняет свою
    работу для high-байтов.
    """
    gm = _model()
    escaped = gm.escape_for_ui(b"bad\xffname.txt")
    has_surrogate = any(0xDC80 <= ord(ch) <= 0xDCFF for ch in escaped)
    if has_surrogate or _cannot_encode(escaped):
        pytest.fail(
            "escape_for_ui оставил высокий (не-UTF8) байт сырым: "
            "строка не кодируется в UTF-8 строгим sink'ом; "
            f"ascii-safe={_safe(escaped)}"
        )


def test_unmerged_refusal_message_is_utf8_safe_for_non_utf8_name(tmp_path):
    """Integration той же находки: сообщение об unmerged-отказе с именем,
    содержащим не-UTF8 байт, обязано оставаться кодируемым в UTF-8.

    Сообщение строит ``preflight()`` через ``escape_for_ui``; awf публикует
    ошибки как ``json.dumps(..., ensure_ascii=False)`` в UTF-8 строго.
    """
    gm = _model()
    repo = _repo(tmp_path)
    name = b"conf\xff.txt"
    path = repo / os.fsdecode(name)
    path.write_bytes(b"base\n")
    _commit(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    path.write_bytes(b"side\n")
    _commit(repo, "side")
    _git(repo, "checkout", "-q", "-")
    path.write_bytes(b"main\n")
    _commit(repo, "main")
    _git(repo, "merge", "side", check=False)  # conflict

    with pytest.raises(gm.UnmergedIndexError) as exc:
        gm.GitRepository(repo).preflight()
    message = str(exc.value)
    has_surrogate = any(0xDC80 <= ord(ch) <= 0xDCFF for ch in message)
    if has_surrogate or _cannot_encode(message):
        pytest.fail(
            "refusal-сообщение об unmerged несёт сырой high-байт и не "
            "кодируется в UTF-8 строгим sink'ом; "
            f"ascii-safe={_safe(message)}"
        )


def _cannot_encode(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False
