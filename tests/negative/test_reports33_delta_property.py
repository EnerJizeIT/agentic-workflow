"""TODO-0212: сторож дельта-механизма — адверсариальный property-тест коммит-гейта.

Дельта-механизм коммит-гейта (pre-dirty файлы входят в коммит только
дельтой юнита: снапшот worktree на диспатче + временный индекс) принёс
класс «тихая порча коммита»: F1 (дубликат контекста перехватывает ханк),
F2 (exec-бит), F3 (дубликат в окне якоря), F4 (опустошённый файл),
F5 (пустой seq кладётся prepend-ом), F6 (устаревший снапшот на
re-диспатче), F7 (CRLF молча нормализуется в LF). Плюс F8 — найдено
самим сторожем: чистая вставка юнита (append/prepend-ханк без -/+
базовой стороны), чьё окно пересекает pre-dispatch-изменение,
кладётся в начало окна, а не в точку вставки — молча, без warning.

Инвариант (главный): после прогона коммит-гейта на дереве с
pre-dispatch-грязью committed-байты каждого файла равны ИЛИ ровно
дельте юнита поверх HEAD (D — ниже в FilePlan.delta), ИЛИ всему
worktree-контенту (W) при ГРОМКОМ предупреждении в логе
(«REPORTS33 delta mechanism for '<path>'»). Тихий коммит с иным
содержимым (гибрид) = провал теста. Байты worktree после коммита
не меняются нигде.

Сценарии: сид-детерминированный генератор (SEED, N_GENERATED) +
именованные регрессии F1–F8 + особые случаи. Сид-детерминизм:
повторный вызов генератора с тем же сидом — те же сценарии
(отдельный тест).
"""

from __future__ import annotations

import os
import random
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from awf import api, commit_gate

T = "TODO-0001"
SEED = 20261012
N_GENERATED = 64


# ── модель сценария ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class FilePlan:
    """Один файл сценария: три состояния контента + ожидаемая форма
    committed-контента.

    expect:
      delta   — committed == delta, и НЕТ REPORTS33-warning по файлу;
      degrade — committed == work (байты worktree) и ЕСТЬ громкий warning;
      either  — (committed == delta и нет warning) ИЛИ (committed == work
                и warning): инвариант не требует конкретного исхода;
      crlf    — как degrade, плюс warning обязан называть «line endings»
                и committed обязан быть целиком CRLF;
      absent  — файл вне коммита (юнит его не трогал), worktree чист от
                коммита;
      delete  — файл удалён из HEAD; loud=True — удаление громкое
                (pre-dirty путь), False — обычный путь;
      plain   — новый (untracked на диспатче) файл: committed == work,
                REPORTS33-warning по файлу нет.
    """

    path: str
    head: bytes | None  # None — файла нет в HEAD
    snap: bytes | None  # None — файла нет в снапшоте диспатча
    work: bytes | None  # None — файл удалён в worktree
    expect: str
    delta: bytes | None = None
    expect_mode: str | None = None  # mode в ls-tree HEAD после коммита
    loud: bool = False


@dataclass(frozen=True)
class Scenario:
    name: str
    files: tuple[FilePlan, ...]


def _lf(lines: list[str]) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


def _crlf(lines: list[str]) -> bytes:
    return ("\r\n".join(lines) + "\r\n").encode("utf-8") if lines else b""


# ── генератор (чистая функция от сида) ──────────────────────────────────


def _gen_clean(rng: random.Random, sfx: str) -> Scenario:
    """Pre-правки и правки юнита в разнесённых зонах (зазор ≥ 4 строки) —
    дельта юнита применима к HEAD однозначно; чистые вставки юнита
    (insert/append/prepend) всегда ≥ 4 строк от pre-зоны (иначе это уже
    F8-геометрия, отдельное семейство)."""
    n = rng.randint(12, 28)
    head = [f"{sfx}L{i}" for i in range(n)]
    unit_kind = rng.choice(["replace", "insert", "delete", "append", "prepend"])
    lo, hi = 0, n // 3
    if unit_kind == "prepend":
        lo = 4  # pre-зоны не ближе 4 строк к началу
    s = list(head)
    for k in range(rng.randint(1, 2)):
        kind = rng.choice(["replace", "insert", "delete"])
        idx = rng.randint(lo, hi)
        if kind == "replace":
            s[idx] = f"{sfx}P{k}"
        elif kind == "insert":
            s.insert(idx, f"{sfx}P{k}")
        else:
            s.pop(idx)

    # якорь — токен правой зоны (pre-зоны его не трогают): и в S, и в HEAD
    # он на месте, операции применяются по содержанию, а не по индексу
    delta_lines = list(head)
    s2 = list(s)
    if unit_kind in ("replace", "insert", "delete"):
        anchor_token = head[rng.randint(2 * n // 3, n - 1)]
        i, j = s2.index(anchor_token), delta_lines.index(anchor_token)
    if unit_kind == "replace":
        s2[i] = f"{sfx}U0"
        delta_lines[j] = f"{sfx}U0"
    elif unit_kind == "insert":
        s2.insert(i + 1, f"{sfx}U0")
        delta_lines.insert(j + 1, f"{sfx}U0")
    elif unit_kind == "delete":
        s2.pop(i)
        delta_lines.pop(j)
    elif unit_kind == "append":
        new = [f"{sfx}U{k}" for k in range(rng.randint(1, 2))]
        s2.extend(new)
        delta_lines.extend(new)
    else:  # prepend
        s2 = [f"{sfx}U0"] + s2
        delta_lines = [f"{sfx}U0"] + delta_lines
    return Scenario(
        name=f"gen-clean-{sfx}",
        files=(
            FilePlan(
                "core.py",
                _lf(head),
                _lf(s),
                _lf(s2),
                "either",
                delta=_lf(delta_lines),
            ),
        ),
    )


def _gen_defect_append(rng: random.Random, sfx: str) -> Scenario:
    """F8-класс: юнит дописывает в конец, a pre-dispatch-изменение
    пересекает окно append-ханка (последние 3 строки) либо сдвигает
    позиции строк в нём. Дельта к HEAD применима однозначно (D = HEAD +
    дописанное) — механизм обязан дать либо её, либо громкую деградацию;
    на дереве 12.10 часть геометрий кладёт строки молча не на то место."""
    n = rng.randint(4, 10)
    head = [f"{sfx}L{i}" for i in range(n)]
    added = [f"{sfx}U{k}" for k in range(rng.randint(1, 2))]
    sub = rng.randrange(5)
    s = list(head)
    if sub == 0:  # pre-замена S[-2]
        s[n - 2] = f"{sfx}P0"
    elif sub == 1:  # pre-замена S[-1]
        s[n - 1] = f"{sfx}P0"
    elif sub == 2:  # pre-вставка перед последней строкой
        s.insert(n - 1, f"{sfx}P0")
    elif sub == 3:  # pre-удаление последней строки
        s.pop()
    else:  # pre-удаление строки в начале (позиции в ханке сдвинуты)
        s.pop(rng.randint(0, max(0, n - 5)))
    s2 = s + added
    return Scenario(
        name=f"gen-f8append-{sfx}",
        files=(
            FilePlan(
                "core.py",
                _lf(head),
                _lf(s),
                _lf(s2),
                "either",
                delta=_lf(head + added),
            ),
        ),
    )


def _gen_safe_edge(rng: random.Random, sfx: str) -> Scenario:
    """Границы окна ханка: pre-изменение, которое fuzz не может сбросить
    (3-я строчка контекста) либо вне окна. Ожидаемый исход — громкая
    деградация либо чистая дельта; тихого гибрида быть не может."""
    sub = rng.randrange(4)
    sfx_ = sfx
    if sub == 0:  # pre-замена S[-3], append в конец
        n = rng.randint(7, 12)
        head = [f"{sfx_}L{i}" for i in range(n)]
        s = list(head)
        s[n - 4] = f"{sfx_}P0"
        return Scenario(
            name=f"gen-edge-{sfx_}",
            files=(
                FilePlan(
                    "core.py",
                    _lf(head),
                    _lf(s),
                    _lf(s + [f"{sfx_}U0"]),
                    "either",
                    delta=_lf(head + [f"{sfx_}U0"]),
                ),
            ),
        )
    if sub == 1:  # pre-замена p+2 (в окне вставки, не сбрасываемая)
        n = rng.randint(9, 14)
        head = [f"{sfx_}L{i}" for i in range(n)]
        p = rng.randint(2, n - 7)
        s = list(head)
        s[p + 2] = f"{sfx_}P0"
        anchor = head[p]
        i, j = s.index(anchor), head.index(anchor)
        s2 = list(s)
        s2.insert(i + 1, f"{sfx_}U0")
        d = list(head)
        d.insert(j + 1, f"{sfx_}U0")
        return Scenario(
            name=f"gen-edge-{sfx_}",
            files=(FilePlan("core.py", _lf(head), _lf(s), _lf(s2), "either", delta=_lf(d)),),
        )
    if sub == 2:  # pre-замена p-1 (предпоследняя ведущая контекстная)
        n = rng.randint(9, 14)
        head = [f"{sfx_}L{i}" for i in range(n)]
        p = rng.randint(3, n - 6)
        s = list(head)
        s[p - 1] = f"{sfx_}P0"
        anchor = head[p]
        i, j = s.index(anchor), head.index(anchor)
        s2 = list(s)
        s2.insert(i + 1, f"{sfx_}U0")
        d = list(head)
        d.insert(j + 1, f"{sfx_}U0")
        return Scenario(
            name=f"gen-edge-{sfx_}",
            files=(FilePlan("core.py", _lf(head), _lf(s), _lf(s2), "either", delta=_lf(d)),),
        )
    # sub == 3: pre-удаление строки в начале + append в конец
    n = rng.randint(8, 14)
    head = [f"{sfx_}L{i}" for i in range(n)]
    s = list(head)
    s.pop(rng.randint(0, max(0, n - 5)))
    return Scenario(
        name=f"gen-edge-{sfx_}",
        files=(
            FilePlan(
                "core.py",
                _lf(head),
                _lf(s),
                _lf(s + [f"{sfx_}U0"]),
                "either",
                delta=_lf(head + [f"{sfx_}U0"]),
            ),
        ),
    )


def _gen_crlf(rng: random.Random, sfx: str) -> Scenario:
    """CRLF-файл: текстовый merge нормализует переводы строк молча
    (F7-класс) — допустим только громкий legacy-исход с CRLF-байтами."""
    n = rng.randint(4, 16)
    head = [f"{sfx}L{i}" for i in range(n)]
    s = list(head)
    if rng.random() < 0.5:
        s[rng.randint(0, n - 1)] = f"{sfx}P0"
    else:
        s.insert(rng.randint(0, n), f"{sfx}P0")
    s2 = list(s)
    if rng.random() < 0.5:
        anchor = rng.choice([ln for ln in s2 if not ln.endswith("P0")])
        i = s2.index(anchor)
        s2[i] = f"{sfx}U0"
    else:
        s2.append(f"{sfx}U0")
    return Scenario(
        name=f"gen-crlf-{sfx}",
        files=(FilePlan("core.py", _crlf(head), _crlf(s), _crlf(s2), "crlf"),),
    )


def _gen_overlap(rng: random.Random, sfx: str) -> Scenario:
    """Юнит и pre-dispatch касаются одной строки: дельта либо выразима
    (W == D — оба исхода валидны), либо нет (только громкая деградация)."""
    n = rng.randint(5, 12)
    head = [f"{sfx}L{i}" for i in range(n)]
    i = rng.randint(1, n - 2)
    s = list(head)
    s[i] = f"{sfx}P0"
    sub = rng.randrange(3)
    if sub == 0:  # юнит перезаписывает pre-строку (W == D)
        s2 = list(s)
        s2[i] = f"{sfx}U0"
        d = list(head)
        d[i] = f"{sfx}U0"
    elif sub == 1:  # юнит удаляет pre-строку (W == D)
        s2 = list(s)
        s2.pop(i)
        d = list(head)
        d.pop(i)
    else:  # юнит вставляет строку после pre-строки (W != D)
        s2 = list(s)
        s2.insert(i + 1, f"{sfx}U0")
        d = list(head)
        d.insert(i + 1, f"{sfx}U0")
    return Scenario(
        name=f"gen-overlap-{sfx}",
        files=(FilePlan("core.py", _lf(head), _lf(s), _lf(s2), "either", delta=_lf(d)),),
    )


def _gen_special(rng: random.Random, sfx: str) -> Scenario:
    """Особые формы: опустошённый/удалённый до диспатча файл, удаление
    файла юнитом, новый файл, pre-dirty файл, которого юнит не трогал."""
    sub = rng.randrange(6)
    if sub == 0:  # F4: файл опустошён до диспатча, юнит пишет в него
        head = [f"{sfx}H{k}" for k in range(rng.randint(2, 4))]
        w = [f"{sfx}W{k}" for k in range(3)]
        return Scenario(
            name=f"gen-f4-{sfx}",
            files=(FilePlan("core.py", _lf(head), b"", _lf(w), "degrade"),),
        )
    if sub == 1:  # файл удалён до диспатча, юнит воссоздаёт
        head = [f"{sfx}H{k}" for k in range(rng.randint(2, 3))]
        w = [f"{sfx}W{k}" for k in range(rng.randint(2, 3))]
        return Scenario(
            name=f"gen-recreate-{sfx}",
            files=(FilePlan("core.py", _lf(head), None, _lf(w), "degrade"),),
        )
    if sub == 2:  # юнит удаляет pre-dirty файл (громкое удаление)
        n = rng.randint(3, 6)
        head = [f"{sfx}L{i}" for i in range(n)]
        s = list(head)
        s[0] = f"{sfx}P0"
        return Scenario(
            name=f"gen-del-dirty-{sfx}",
            files=(FilePlan("core.py", _lf(head), _lf(s), None, "delete", loud=True),),
        )
    if sub == 3:  # юнит удаляет чистый на baseline файл (обычный путь)
        n = rng.randint(3, 6)
        head = [f"{sfx}L{i}" for i in range(n)]
        return Scenario(
            name=f"gen-del-clean-{sfx}",
            files=(FilePlan("core.py", _lf(head), _lf(head), None, "delete"),),
        )
    if sub == 4:  # pre-dirty файл без правок юнита + новый файл юнита
        n = rng.randint(3, 5)
        a_head = [f"{sfx}A{i}" for i in range(n)]
        a_snap = list(a_head)
        a_snap[0] = f"{sfx}Apre"
        b_w = [f"{sfx}B{k}" for k in range(2)]
        return Scenario(
            name=f"gen-untouched-{sfx}",
            files=(
                FilePlan("keep.py", _lf(a_head), _lf(a_snap), _lf(a_snap), "absent"),
                FilePlan("new.py", None, None, _lf(b_w), "plain"),
            ),
        )
    # sub == 5: только новый файл (дерево чистое на диспатче)
    b_w = [f"{sfx}B{k}" for k in range(rng.randint(1, 3))]
    return Scenario(
        name=f"gen-newfile-{sfx}",
        files=(FilePlan("new.py", None, None, _lf(b_w), "plain"),),
    )


def _gen_exec(rng: random.Random, sfx: str) -> Scenario:
    """Исполняемый файл (100755 в HEAD): exec-бит обязан пережить
    дельта-коммит (F2-инвариант), контент — ровно дельта юнита."""
    n = rng.randint(6, 10)
    head = [f"#!{sfx}sh", *[f"{sfx}S{i}" for i in range(n)]]
    s = list(head)
    s[1] = f"{sfx}P0"
    s2 = list(s)
    s2[-1] = f"{sfx}U0"
    d = list(head)
    d[-1] = f"{sfx}U0"
    return Scenario(
        name=f"gen-exec-{sfx}",
        files=(
            FilePlan(
                "run.sh", _lf(head), _lf(s), _lf(s2), "delta", delta=_lf(d), expect_mode="100755"
            ),
        ),
    )


_FAMILIES = {
    "clean": (_gen_clean, 18),
    "f8append": (_gen_defect_append, 10),
    "edge": (_gen_safe_edge, 10),
    "crlf": (_gen_crlf, 8),
    "overlap": (_gen_overlap, 8),
    "special": (_gen_special, 8),
    "exec": (_gen_exec, 4),
}


def generate_scenarios(seed: int, n: int) -> list[Scenario]:
    """Сид-детерминированный набор сценариев: чистая функция от
    (seed, n) — повторный вызов даёт идентичный список."""
    rng = random.Random(seed)
    pool: list[str] = []
    for name, (_fn, weight) in _FAMILIES.items():
        pool.extend([name] * weight)
    rng.shuffle(pool)
    out: list[Scenario] = []
    for i in range(n):
        fam = pool[i % len(pool)]
        gen_fn = _FAMILIES[fam][0]
        out.append(gen_fn(rng, f"{fam}{i:02d}"))
    return out


# ── харнесс: tmp-репо + диспатч + прогон коммит-гейта ───────────────────


def _git(project: Path, *args: str, text: bool = True) -> str | bytes:
    return subprocess.run(
        ["git", *args],
        cwd=project,
        check=True,
        capture_output=True,
        text=text,
    ).stdout


def _git_out(project: Path, *args: str) -> str:
    return _git(project, *args, text=True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Git-репо с инициализированным .agentic/ и ЗАКОММИЧЕННЫМ init
    (дерево на диспатче под контролем сценария)."""
    proj = tmp_path / "repo"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=proj, check=True)
    api.init_project(
        proj,
        project_name="R33Prop",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "awf init")
    return proj


def _write(project: Path, path: str, data: bytes | None, mode: str | None):
    p = project / path
    if data is None:
        if p.exists():
            p.unlink()
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mode == "100755":
        os.chmod(p, 0o755)


def _dispatch(project: Path):
    return api.dispatch_todo(project, f"# {T}\nTask\n", todo_id=T)


def _file_warned(log_text: str, path: str) -> str | None:
    """REPORTS33-строка по файлу из лога (None — warning не было)."""
    for ln in log_text.splitlines():
        if f"REPORTS33 delta mechanism for '{path}'" in ln:
            return ln
    return None


def _assert_file(
    project: Path,
    fp: FilePlan,
    committed: set[str],
    log_text: str,
) -> None:
    if fp.expect == "absent":
        assert fp.path not in committed, (
            f"{fp.path}: файл, которого юнит не трогал, обязан выпасть "
            f"из плана (он в коммите: {sorted(committed)})"
        )
        return
    if fp.expect == "delete":
        # `git show --name-only` перечисляет и удаления, поэтому членство
        # в коммите проверяем по HEAD-дереву, а не по списку имён
        in_tree = _git_out(project, "ls-tree", "HEAD", "--", fp.path).strip() != ""
        assert not in_tree, (
            f"{fp.path}: юнит удалил файл — в HEAD-дереве он обязан "
            f"исчезнуть (в коммите: {sorted(committed)})"
        )
        warned = _file_warned(log_text, fp.path)
        if fp.loud:
            assert warned is not None, (
                f"{fp.path}: удаление pre-dirty файла — громкий путь "
                "( REPORTS33-строки по файлу в логе нет)"
            )
        return
    if fp.path not in committed:
        pytest.fail(
            f"{fp.path}: файл обязан войти в коммит (expect={fp.expect}), "
            f"но в коммите только {sorted(committed)}"
        )
    c = _git(project, "cat-file", "blob", f"HEAD:{fp.path}", text=False)
    w = fp.work
    assert w is not None
    warned = _file_warned(log_text, fp.path)
    if fp.expect == "delta":
        assert c == fp.delta, (
            f"{fp.path}: коммит обязан нести ровно дельту юнита\n"
            f"  delta={fp.delta!r}\n  committed={c!r}"
        )
        assert warned is None, (
            f"{fp.path}: чистая дельта не сопровождается REPORTS33-warning (в логе: {warned!r})"
        )
    elif fp.expect in ("degrade", "crlf"):
        assert c == w, (
            f"{fp.path}: деградация = legacy-коммит всего worktree-"
            f"контента\n  work={w!r}\n  committed={c!r}"
        )
        assert warned is not None, (
            f"{fp.path}: деградация обязана быть ГРОМКОЙ (REPORTS33-строки по файлу в логе нет)"
        )
        if fp.expect == "crlf":
            assert b"\r\n" in c, f"{fp.path}: CRLF-файл обязан сохраниться с CRLF (получено: {c!r})"
            assert "line endings" in warned, (
                f"{fp.path}: warning обязан называть причину «line endings» (получено: {warned!r})"
            )
    elif fp.expect == "either":
        assert (c == fp.delta and warned is None) or (c == w and warned), (
            f"{fp.path}: ТИХАЯ ПОРЧА — committed-контент не дельта юнита "
            f"и не worktree (и warning не было), либо worktree без "
            f"громкого warning:\n  delta={fp.delta!r}\n  work={w!r}\n"
            f"  committed={c!r}\n  warned={warned!r}"
        )
    elif fp.expect == "plain":
        assert c == w, (
            f"{fp.path}: новый (untracked) файл обязан войти в коммит "
            f"целиком\n  work={w!r}\n  committed={c!r}"
        )
        assert warned is None, (
            f"{fp.path}: у нового файла нет дельта-пути — REPORTS33-"
            f"warning по нему недопустим (получено: {warned!r})"
        )
    else:
        pytest.fail(f"{fp.path}: неизвестный expect={fp.expect!r}")
    if fp.expect_mode is not None:
        mode = _git_out(project, "ls-tree", "HEAD", "--", fp.path).split()
        assert mode and mode[0] == fp.expect_mode, (
            f"{fp.path}: mode в коммите {mode[:1]!r} ≠ HEAD-mode "
            f"{fp.expect_mode} (тихая смена mode = порча)"
        )


def _run_scenario(project: Path, sc: Scenario) -> None:
    # 1. HEAD: базовые файлы, один коммит
    for fp in sc.files:
        if fp.head is not None:
            _write(project, fp.path, fp.head, fp.expect_mode)
    if any(fp.head is not None for fp in sc.files):
        _git(project, "add", "-A")
        _git(project, "commit", "-qm", "head")
    # 2. pre-dispatch WIP (до диспатча)
    for fp in sc.files:
        if fp.head is not None:
            _write(project, fp.path, fp.snap, None)
    d = _dispatch(project)
    assert d.baseline_sha, "baseline_sha обязан быть зафиксирован"
    # 3. работа юнита (после диспатча)
    for fp in sc.files:
        _write(project, fp.path, fp.work, fp.expect_mode)
    # 4. прогон коммит-гейта
    outcome = commit_gate.maybe_commit(
        "worker",
        T,
        "commit_and_next",
        project,
        project / ".agentic" / "logs",
        auto=False,
        baseline_sha=d.baseline_sha,
    )
    assert outcome.status == "committed", (
        f"{sc.name}: коммит юнита обязан состояться (получено {outcome.status}: {outcome.reason})"
    )
    log_path = project / ".agentic" / "logs" / "orchestrator.log"
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    committed = set(
        ln
        for ln in _git_out(project, "show", "--name-only", "--format=", "HEAD").splitlines()
        if ln.strip()
    )
    for fp in sc.files:
        _assert_file(project, fp, committed, log_text)
    # worktree после коммита не тронут (байты в точности как до коммита)
    for fp in sc.files:
        p = project / fp.path
        if fp.work is None:
            assert not p.exists(), f"{fp.path}: файл должен быть удалён"
        else:
            assert p.read_bytes() == fp.work, (
                f"{fp.path}: коммит-гейт обязан не менять worktree "
                f"(было {fp.work!r}, стало {p.read_bytes()!r})"
            )


# ── сгенерированные сценарии ─────────────────────────────────────────────

SCENARIOS = generate_scenarios(SEED, N_GENERATED)


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_scenario(project: Path, sc: Scenario) -> None:
    """Инвариант на каждом сценарии: committed-байты == дельте юнита
    (без warning) или == всему worktree (с громким warning); тихого
    гибрида нет; worktree после коммита не тронут."""
    _run_scenario(project, sc)


def test_seed_determinism() -> None:
    """Повторный вызов генератора с тем же сидом — те же сценарии."""
    a = generate_scenarios(SEED, N_GENERATED)
    b = generate_scenarios(SEED, N_GENERATED)
    assert a == b
    assert len(a) == N_GENERATED
    c = generate_scenarios(SEED + 1, N_GENERATED)
    assert c != a, "другой сид обязан дать другой набор сценариев"


# ── именованные регрессии F1–F7 (отчёты QA/супервизора по F-блокам) ─────


def _scenario_case(project: Path, sc: Scenario) -> None:
    _run_scenario(project, sc)


def test_regression_f1_duplicated_context(project: Path) -> None:
    """F1: 7+ pre-dispatch-строк перед правкой юнита + дублирующийся
    контекст-паттерн (X,Y,Z в начале и в конце файла) — дельта юнита
    обязана лечь в конец, а не в место первого дубликата."""
    lines = ["X", "Y", "Z", "d1", "d2"] + [f"filler {i}" for i in range(40)] + ["X", "Y", "Z"]
    pre = [f"pre {i}" for i in range(10)]
    _scenario_case(
        project,
        Scenario(
            name="f1",
            files=(
                FilePlan(
                    "core.py",
                    _lf(lines),
                    _lf(lines[:45] + pre + lines[45:]),
                    _lf(lines[:45] + pre + lines[45:] + ["unit1", "unit2"]),
                    "delta",
                    delta=_lf(lines + ["unit1", "unit2"]),
                ),
            ),
        ),
    )


def test_regression_f2_executable_mode(project: Path) -> None:
    """F2: исполняемый файл (100755 в HEAD) обязан сохранить exec-бит
    в дельта-коммите."""
    _scenario_case(
        project,
        Scenario(
            name="f2",
            files=(
                FilePlan(
                    "run.sh",
                    _lf(["#!/bin/sh", "echo v1"]),
                    _lf(["#!/bin/sh", "echo v1", "echo pre"]),
                    _lf(["#!/bin/sh", "echo v1", "echo pre", "echo unit"]),
                    "delta",
                    delta=_lf(["#!/bin/sh", "echo v1", "echo unit"]),
                    expect_mode="100755",
                ),
            ),
        ),
    )


def test_regression_f3_duplicate_in_anchor_window(project: Path) -> None:
    """F3: копия 4-строчного контекст-паттерна в 4 строках ДО якоря —
    дельта юнита обязана лечь на точный якорь (конец файла), а не на
    первый дубликат."""
    head = ["x", "a", "b", "c", "x", "a", "b", "c"]
    _scenario_case(
        project,
        Scenario(
            name="f3",
            files=(
                FilePlan(
                    "core.py",
                    _lf(head),
                    _lf(["p"] + head),
                    _lf(["p"] + head[:-1] + ["C"]),
                    "delta",
                    delta=_lf(head[:-1] + ["C"]),
                ),
            ),
        ),
    )


def test_regression_f4_emptied_file(project: Path) -> None:
    """F4: файл опустошён до диспатча, юнит пишет в него содержимое —
    громкая деградация на legacy (весь worktree), строки HEAD в коммит
    не утекают."""
    _scenario_case(
        project,
        Scenario(
            name="f4",
            files=(
                FilePlan(
                    "core.py",
                    _lf(["h1", "h2"]),
                    b"",
                    _lf(["w1", "w2", "w3"]),
                    "degrade",
                ),
            ),
        ),
    )


def test_regression_f5_append_after_edited_context(project: Path) -> None:
    """F5: pre-dispatch меняет строку, которую юнит-ханк использует как
    контекст, юнит дописывает строку в конец — дельта обязана лечь ПОСЛЕ
    контекста (конец файла), а не prepend-ом в начало."""
    _scenario_case(
        project,
        Scenario(
            name="f5",
            files=(
                FilePlan(
                    "core.py",
                    _lf(["a", "b"]),
                    _lf(["a2", "b"]),
                    _lf(["a2", "b", "u"]),
                    "delta",
                    delta=_lf(["a", "b", "u"]),
                ),
            ),
        ),
    )


def test_regression_f6_stale_worktree_on_redispatch(project: Path, monkeypatch) -> None:
    """F6: re-диспатч того же id при сбое `git stash create` — снапшот
    первой попытки не переживает re-диспатч, warning честный
    («WILL ENTER the unit commit as-is»), коммит = legacy-контент
    worktree."""
    (project / "core.py").write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    _git(project, "add", "core.py")
    _git(project, "commit", "-qm", "core")
    (project / "core.py").write_text("a2\nb\nc\nd\ne\n", encoding="utf-8")
    _dispatch(project)
    snap_file = project / ".agentic" / "context" / f"BASELINE-{T}.worktree"
    assert snap_file.is_file()
    (project / ".agentic" / "inbox" / f"{T}.md").unlink()
    (project / "core.py").write_text("a2\nb\nc\nd\ne\nw2\n", encoding="utf-8")
    from awf import git_utils as awf_git_utils

    real_git_stdout = awf_git_utils.git_stdout

    def failing_stash(cwd, *args, check=True):
        if args == ("stash", "create"):
            return ""  # git failure, глотается семантикой check=False
        return real_git_stdout(cwd, *args, check=check)

    monkeypatch.setattr(awf_git_utils, "git_stdout", failing_stash)
    d2 = api.dispatch_todo(project, f"# {T}\nTask v2\n", todo_id=T)
    assert not snap_file.is_file(), (
        "re-диспатч без свежего снапшота обязан не оставлять снапшот первой попытки на месте"
    )
    assert "stay OUTSIDE the unit commit" not in d2.dirty_warning, (
        f"warning без свежего снапшота не обязан обещать исключение: {d2.dirty_warning!r}"
    )
    assert "WILL ENTER the unit commit as-is" in d2.dirty_warning, (
        f"деградация обязана быть обещана явно: {d2.dirty_warning!r}"
    )
    (project / "core.py").write_text("a2\nb\nc\nd\ne\nw2\nu\n", encoding="utf-8")
    outcome = commit_gate.maybe_commit(
        "worker",
        T,
        "commit_and_next",
        project,
        project / ".agentic" / "logs",
        auto=False,
        baseline_sha=d2.baseline_sha,
    )
    assert outcome.status == "committed", f"{outcome.status}: {outcome.reason}"
    committed = _git_out(project, "show", "HEAD:core.py").splitlines()
    assert committed == ["a2", "b", "c", "d", "e", "w2", "u"], (
        "честная деградация = legacy-коммит всего worktree-содержимого — "
        f"ровно то, что warning обещал (получено: {committed!r})"
    )


def test_regression_f7_crlf(project: Path) -> None:
    """F7: CRLF-файл в дельта-пути — тихая нормализация в LF
    недопустима: коммит несёт legacy-контент worktree с CRLF + громкий
    warning с причиной «line endings»."""
    _scenario_case(
        project,
        Scenario(
            name="f7",
            files=(
                FilePlan(
                    "core.py",
                    _crlf(["a", "b"]),
                    _crlf(["a2", "b"]),
                    _crlf(["a2", "b", "u"]),
                    "crlf",
                ),
            ),
        ),
    )
    # чистая функция: \r в HEAD-контенте ИЛИ в патче → None (бан merge)
    assert (
        commit_gate._apply_unit_delta(
            "a\r\nb\r\nc\r\n",
            "@@ -1,2 +1,2 @@\n a\r\n-b\r\n+B\r\n",
            [],
        )
        is None
    )
    assert (
        commit_gate._apply_unit_delta(
            "a\nb\nc\n",
            "@@ -1,2 +1,2 @@\n a\r\n-b\r\n+B\r\n",
            [],
        )
        is None
    )
    # контроль: LF-контент мержится по-прежнему (бан не перестраховочный)
    assert (
        commit_gate._apply_unit_delta(
            "a\nb\nc\n",
            "@@ -1,1 +1,1 @@\n-a\n+A\n",
            [],
        )
        == "A\nb\nc\n"
    )


# ── F8: найдено сторожем — тихая мисплейсмент чистых вставок ────────────
#
# Геометрия: юнит дописывает строки в КОНЕЦ файла (чанк без базовой
# -/+ стороны), a pre-dispatch-изменение пересекает окно чанка
# (последние 2 строки — сбрасывается fuzz-ом) либо сдвигает позиции
# строк окна в HEAD. Fuzz-вариант, сокращающий ХВОСТ контекста,
# совпадает в начале окна раньше, чем вариант с точным якорем, —
# строки юнита ложатся не на точку вставки. Ни D, ни W, без warning.
# Ожидаемое поведение (инвариант): ровно D либо громкая деградация.


def test_regression_f8a_append_pre_replace_s_minus2(project: Path) -> None:
    """F8a: HEAD=[a,b,c], pre: b→b2 (S[-2]), юнит дописывает u в конец —
    коммит обязан быть [a,b,c,u] (дельта) либо [a,b2,c,u] с громким
    warning; на дереве 12.10 получается [a,u,b,c] молча."""
    _scenario_case(
        project,
        Scenario(
            name="f8a",
            files=(
                FilePlan(
                    "core.py",
                    _lf(["a", "b", "c"]),
                    _lf(["a", "b2", "c"]),
                    _lf(["a", "b2", "c", "u"]),
                    "either",
                    delta=_lf(["a", "b", "c", "u"]),
                ),
            ),
        ),
    )


def test_regression_f8b_append_pre_replace_last_line(project: Path) -> None:
    """F8b: HEAD=[a,b,c], pre: c→c2 (S[-1]), юнит дописывает u в конец —
    коммит обязан быть [a,b,c,u] либо [a,b,c2,u] с громким warning;
    на дереве 12.10 получается [a,b,u,c] молча."""
    _scenario_case(
        project,
        Scenario(
            name="f8b",
            files=(
                FilePlan(
                    "core.py",
                    _lf(["a", "b", "c"]),
                    _lf(["a", "b", "c2"]),
                    _lf(["a", "b", "c2", "u"]),
                    "either",
                    delta=_lf(["a", "b", "c", "u"]),
                ),
            ),
        ),
    )


def test_regression_f8c_append_pre_deleted_last_line(project: Path) -> None:
    """F8c: HEAD=8 строк, pre: последняя строка удалена (S короче),
    юнит дописывает u в конец — коммит обязан быть HEAD+[u] либо S+[u]
    с громким warning; на дереве 12.10 получается u ПЕРЕД удалённой в
    worktree строкой (её HEAD-копия остаётся в коммите после u) —
    молча."""
    head = [f"L{i}" for i in range(8)]
    _scenario_case(
        project,
        Scenario(
            name="f8c",
            files=(
                FilePlan(
                    "core.py",
                    _lf(head),
                    _lf(head[:-1]),
                    _lf(head[:-1] + ["u"]),
                    "either",
                    delta=_lf(head + ["u"]),
                ),
            ),
        ),
    )
