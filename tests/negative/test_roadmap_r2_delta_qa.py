"""R2.3 (roadmap R2) — QA-проба границ (agent-qa-review).

Независимые тесты поверх ``tests/negative/test_roadmap_r2_delta.py``.
Зелёные фиксируют верное поведение, которого нет в приёмочной матрице
(канал ``.include``, содержимое candidate tree, «один дом» дельта-алгоритма).
Красные доказывают ОТКРЫТЫЕ точки R2.3 — их чинит следующий проход, не этот
сторож:

* ``test_claimed_tracked_predecessor_change_enters_the_candidate`` —
  требование 3 матрицы R2 / добор 0221: claim tracked-изменения
  предшественника обязан ВОЙТИ в кандидат. Сейчас ``claimed`` фиксируется
  в плане, ``owned`` ставится, но ``files`` (то, что реально коммитится)
  путь не содержит — legacy-проход выкидывает его как «no unit delta».
* ``test_pre_existing_untracked_is_not_owned`` — pre-existing untracked
  лежит в ``excluded`` с причиной и вне ``files``, но в ``operations``
  помечен ``owned=True``: один объект противоречит сам себе.
* ``test_unstaged_chmod_reports_the_mode_git_will_store`` и
  ``test_unstaged_symlink_typechange_is_a_symlink_operation`` —
  типизированная операция берёт ``index_mode``/``index_blob`` (для
  несценированной правки это факты baseline), а не то, что Git реально
  сохранит: chmod 0755 читается как 100644, typechange — как обычный файл.
  Требование 3: «проверка типа/inclusion совпадает с тем, что Git реально
  сохранит».

Ленивый импорт: на базовом дереве ``awf.commit_delta`` отсутствует.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

T = "TODO-0001"


def _delta():
    try:
        from awf import commit_delta
    except ImportError as e:  # pragma: no cover - baseline only
        raise AssertionError(f"awf.commit_delta module is missing: {e}") from e
    return commit_delta


def _git(proj: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=proj, check=check, capture_output=True, text=True
    )


def _write(proj: Path, path: str, data: str | bytes) -> None:
    p = proj / path
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        p.write_bytes(data)
    else:
        p.write_text(data, encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    proj = tmp_path / "repo"
    proj.mkdir()
    _git(proj, "init", "-q")
    _git(proj, "config", "user.email", "t@t.t")
    _git(proj, "config", "user.name", "tester")
    (proj / ".agentic" / "context").mkdir(parents=True, exist_ok=True)
    _write(proj, ".gitignore", ".agentic/\n")
    return proj


def _snapshot(
    proj: Path,
    *,
    untracked: str | None = None,
    with_worktree: bool = False,
) -> str:
    """Write BASELINE-{T}.sha/.status (+ optional .untracked/.worktree)."""
    sha = _git(proj, "rev-parse", "HEAD").stdout.strip()
    ctx = proj / ".agentic" / "context"
    (ctx / f"BASELINE-{T}.sha").write_text(sha + "\n")
    (ctx / f"BASELINE-{T}.status").write_text(
        _git(proj, "status", "--short", check=False).stdout
    )
    if untracked is not None:
        (ctx / f"BASELINE-{T}.untracked").write_text(untracked)
    if with_worktree:
        snap = _git(proj, "stash", "create").stdout.strip()
        (ctx / f"BASELINE-{T}.worktree").write_text(snap + "\n")
    return sha


# ─── зелёные: пробелы приёмочной матрицы ────────────────────────────────


def test_include_channel_adopts_pre_existing_untracked(tmp_path: Path) -> None:
    """``BASELINE-<todo>.include`` — явный include pre-existing untracked
    (RUN10 #4) — должен войти в кандидат и в ``claimed``; приёмочная
    матрица гоняет только untracked-snapshot-ветку, не сам ``.include``."""
    delta = _delta()
    from awf._path_link import encode_path_link

    proj = _repo(tmp_path)
    _write(proj, "a.txt", "A\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    _write(proj, "up.txt", "U\n")  # pre-existing untracked, explicitly included
    baseline = _snapshot(proj, untracked="")  # dispatch dropped it from the snapshot
    ctx = proj / ".agentic" / "context"
    (ctx / f"BASELINE-{T}.include").write_bytes(
        encode_path_link(["up.txt"]).encode()
    )
    _write(proj, "unit.txt", "N\n")

    plan = delta.assemble_candidate(proj, T, baseline)

    assert "up.txt" in plan.files, "явный include обязан войти в кандидат"
    assert "up.txt" in plan.claimed


def test_own_rename_candidate_tree_drops_the_source(tmp_path: Path) -> None:
    """B01 на уровне содержимого: в candidate tree нет старого пути и есть
    новый — приёмка проверяет только ``files``, не materialised tree."""
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "a.txt", "A\n")
    _write(proj, "keep.txt", "K\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    baseline = _snapshot(proj)
    _git(proj, "mv", "a.txt", "b.txt")

    plan = delta.assemble_candidate(proj, T, baseline, build_tree=True)
    tree_files = set(
        _git(proj, "ls-tree", "-r", "--name-only", plan.candidate_tree)
        .stdout.split()
    )
    assert "a.txt" not in tree_files, "старый путь не должен остаться в кандидате"
    assert "b.txt" in tree_files
    assert _git(proj, "cat-file", "-p", f"{plan.candidate_tree}:b.txt").stdout == "A\n"


def test_foreign_staged_rename_leaves_no_commit_content(tmp_path: Path) -> None:
    """S2-01: чужой staged rename — кандидат пуст, оба пути объяснены."""
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "keep.txt", "K\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    _git(proj, "mv", "keep.txt", "moved.txt")
    baseline = _snapshot(proj)

    plan = delta.assemble_candidate(proj, T, baseline, build_tree=True)

    assert plan.files == ()
    assert plan.candidate_tree == ""
    assert ("moved.txt", "staged-at-baseline") in plan.excluded


def test_delta_algorithm_has_one_home(tmp_path: Path) -> None:
    """REPORTS33 F1–F8 живут в одном модуле; гейт ре-экспортирует, а не
    держит вторую копию."""
    delta = _delta()
    from awf import commit_gate, commit_plan

    assert commit_gate._apply_unit_delta is delta.apply_unit_delta
    assert commit_gate._parse_delta_hunks is delta.parse_delta_hunks
    assert commit_gate._head_position is delta.head_position
    assert commit_gate._head_end_position is delta.head_end_position
    assert commit_plan.read_worktree_snapshot is delta.read_worktree_snapshot


# ─── красные: открытые точки R2.3 ──────────────────────────────────────


def test_claimed_tracked_predecessor_change_enters_the_candidate(
    tmp_path: Path,
) -> None:
    """RED (открыто): claim tracked-изменения предшественника обязан войти
    в кандидат (добор 0221 п.1, строка 3 матрицы R2 «с claim включает нужный
    blob»). Сейчас ``files`` его не содержит."""
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "f.txt", "A\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    _write(proj, "f.txt", "A foreign\n")  # pre-dispatch predecessor edit
    baseline = _snapshot(proj, untracked="", with_worktree=True)
    _write(proj, "unit.txt", "U\n")  # the unit does not touch f.txt

    without = delta.assemble_candidate(proj, T, baseline)
    assert "f.txt" not in without.files, "без claim чужое изменение не входит"
    assert ("f.txt", "pre-dispatch changes only (no unit delta)") in without.excluded

    claimed = delta.assemble_candidate(proj, T, baseline, claim=["f.txt"])
    assert claimed.claimed == ("f.txt",)
    assert "f.txt" in claimed.files, (
        "с claim tracked-изменение предшественника обязано войти в кандидат"
    )


def test_pre_existing_untracked_is_not_owned(tmp_path: Path) -> None:
    """RED (открыто): pre-existing untracked вне ``files`` и в ``excluded``,
    но ``operations`` помечает его ``owned=True`` — один CandidatePlan
    противоречит сам себе."""
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "a.txt", "A\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    _write(proj, "pre.txt", "P\n")  # pre-existing untracked
    baseline = _snapshot(proj, untracked="pre.txt\n")
    _write(proj, "unit.txt", "U\n")

    plan = delta.assemble_candidate(proj, T, baseline)

    assert "pre.txt" not in plan.files
    assert ("pre.txt", "pre-existing untracked") in plan.excluded
    ops = {op.raw_path: op for op in plan.operations}
    assert "pre.txt" not in ops or not ops["pre.txt"].owned, (
        "pre-existing untracked — чужое: typed-операция не может быть owned"
    )


def test_unstaged_chmod_reports_the_mode_git_will_store(tmp_path: Path) -> None:
    """RED (открыто): chmod 0644→0755 без staging — Git сохранит 100755;
    операция обязана отражать этот mode, а не baseline-факт индекса."""
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "f.txt", "A\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    baseline = _snapshot(proj, untracked="")
    os.chmod(proj / "f.txt", 0o755)

    plan = delta.assemble_candidate(proj, T, baseline)
    op = next(o for o in plan.operations if o.raw_path == "f.txt")
    assert op.kind == "modified"
    assert op.mode == "100755", "операция обязана давать mode, который сохранит Git"


def test_unstaged_symlink_typechange_is_a_symlink_operation(
    tmp_path: Path,
) -> None:
    """RED (открыто): regular→symlink без staging — Git сохранит ссылку
    (mode 120000, blob = строка цели), а операция отдаёт старый файл."""
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "target.txt", "T\n")
    _write(proj, "f.txt", "A\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    baseline = _snapshot(proj, untracked="")
    (proj / "f.txt").unlink()
    os.symlink("target.txt", proj / "f.txt")

    plan = delta.assemble_candidate(proj, T, baseline)
    op = next(o for o in plan.operations if o.raw_path == "f.txt")
    assert op.kind == "typechange"
    assert op.is_symlink, "Git сохранит симлинк — операция обязана это сказать"
    assert op.mode == "120000"
    want = subprocess.run(
        ["git", "hash-object", "--stdin"],
        cwd=proj,
        input=b"target.txt",
        capture_output=True,
    ).stdout.decode().strip()
    assert op.blob == want, "blob — строка цели, не старое содержимое файла"


def test_untracked_operation_reports_the_mode_git_will_store(
    tmp_path: Path,
) -> None:
    """QA (F3-adjacent): a NEW (untracked) symlink / executable is a user path
    whose stored type Git will decide — ``120000`` + the target string for a
    link, ``100755`` for an executable. The typed operation must say so, not
    ``None`` (the F3 fix covered only the TRACKED case; an untracked record
    carries no index/work modes)."""
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "a.txt", "A\n")
    _git(proj, "add", "a.txt")
    _git(proj, "commit", "-qm", "init")
    baseline = _snapshot(proj, untracked="")
    os.symlink("a.txt", proj / "lnk")  # untracked symlink
    _write(proj, "run.sh", "#!/bin/sh\n")
    os.chmod(proj / "run.sh", 0o755)  # untracked executable

    plan = delta.assemble_candidate(proj, T, baseline)
    ops = {op.raw_path: op for op in plan.operations}

    assert ops["lnk"].mode == "120000", "untracked symlink stores 120000"
    assert ops["lnk"].is_symlink
    want = subprocess.run(
        ["git", "hash-object", "--stdin"],
        cwd=proj,
        input=b"a.txt",
        capture_output=True,
    ).stdout.decode().strip()
    assert ops["lnk"].blob == want, "blob is the link TARGET STRING"
    assert ops["run.sh"].mode == "100755", "untracked executable stores 100755"


# ─── REVIEW iter 3: не-ASCII идентичность, неглухой claim, одна константа ─


def test_foreign_staged_rename_non_ascii_is_excluded_whole(tmp_path: Path) -> None:
    """RED (H1): при ``core.quotePath=true`` (дефолт) baseline-снапшот хранит
    C-квотированное имя (``"\\321\\204..."``), а модель отдаёт raw
    (``файл.txt``). До моста предикат владения не находил совпадения и
    признавал чужой rename СВОИМ — source-удаление и destination уходили в
    ``files`` (нарушение S2-01 на не-ASCII имени)."""
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "keep.txt", "K\n")
    _write(proj, "файл.txt", "A\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    _git(proj, "mv", "файл.txt", "файл2.txt")  # FOREIGN staged rename
    baseline = _snapshot(proj)
    # sanity: git really C-quoted the name in the snapshot
    status = (proj / ".agentic" / "context" / f"BASELINE-{T}.status").read_text()
    assert "\\321" in status or 'файл' in status

    plan = delta.assemble_candidate(proj, T, baseline)

    assert plan.files == (), "чужой rename не должен попасть в files (S2-01)"
    assert "файл.txt" not in plan.files
    assert "файл2.txt" not in plan.files
    assert not plan.conflicts, "оба пути чужие — это не смешанная пара"


def test_own_rename_non_ascii_includes_both_members(tmp_path: Path) -> None:
    """B01 на не-ASCII: собственный rename после baseline включает destination
    и deletion source (проверка на уровне ``files`` — план-оракул матрицы;
    содержимое candidate tree ограничено печатными парсерами, их снос
    супервизор осознанно отложил в мини-волну)."""
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "keep.txt", "K\n")
    _write(proj, "файл.txt", "A\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    baseline = _snapshot(proj)
    _git(proj, "mv", "файл.txt", "файл2.txt")  # the unit's OWN rename

    plan = delta.assemble_candidate(proj, T, baseline)

    assert "файл2.txt" in plan.files, "destination обязан войти в коммит"
    assert "файл.txt" in plan.files, "source обязан войти как deletion"
    assert not plan.conflicts, "собственный rename — не конфликт"


def test_claim_channel_failure_is_not_silent(
    tmp_path: Path, monkeypatch
) -> None:
    """REVIEW-M1: сбой разрешения claim-канала не должен гасить его МОЛЧА —
    иначе принятое предшественником изменение тихо выпадает из коммита
    (класс, который закрывал F1)."""
    import awf.api.hygiene as hygiene
    from awf import commit_plan

    proj = _repo(tmp_path)
    (proj / ".agentic" / "logs").mkdir(parents=True, exist_ok=True)

    def _boom(*_a, **_k):
        raise OSError("claim link unreadable")

    monkeypatch.setattr(hygiene, "transfer_exclusions", _boom)

    assert commit_plan._claimed_paths(proj, T) == []
    log_file = proj / ".agentic" / "logs" / "orchestrator.log"
    assert log_file.exists(), "сбой обязан оставить строку в логе"
    assert "claim-channel resolution failed" in log_file.read_text(encoding="utf-8")


def test_git_timeout_is_one_constant() -> None:
    """REVIEW-M2: один таймаут git — импорт из ``git_model``, без twins и
    без оставшихся литералов ``timeout=30`` в модулях commit-пути.

    R2.3 architect-review: ``commit_plan`` больше не зовёт git (сырой
    file-set переехал в ``commit_delta``) — инвариант держат модули,
    которые реально запускают git."""
    from awf import commit_delta, commit_gate, git_utils
    from awf.infra import git_model

    for mod in (commit_delta, commit_gate, git_utils):
        assert mod.GIT_TIMEOUT is git_model.GIT_TIMEOUT, (
            f"{mod.__name__} must import GIT_TIMEOUT, not define a twin"
        )
    for mod in (commit_delta, commit_gate, git_utils):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "timeout=30" not in src, (
            f"{mod.__name__} still hardcodes the git timeout literal"
        )


# ─── REVIEW iter 4: сепаратор rename-строки внутри C-квотированного имени ──


def test_tracked_dirty_name_with_arrow_substring_is_foreign(tmp_path: Path) -> None:
    """RED (F-arrow): ``git status --short`` C-квотирует ЛЕГАЛЬНОЕ имя,
    содержащее подстроку ``" -> "`` (в имени есть пробелы — потому кавычки),
    а ``_status_pair`` / ``_status_path`` режут строку по ``" -> "``
    безусловно: `` M "a -> b.txt"`` распадается на ``"a`` и ``b.txt"``.
    Настоящий путь не попадает ни в ``baseline_dirty_paths`` (предикат
    владения), ни в ``worktree_dirty_at_baseline`` (delta-механизм), поэтому
    ЧУЖОЕ преддиспатчное изменение этого tracked-файла читается как СВОЁ и
    входит в кандидат целиком (``files``, без excluded и без delta) — тот же
    класс, что H1/S2-01, но на имени с подстрокой-сепаратором. Мост H1
    починил квотирование, но не разбор стрелки."""
    delta = _delta()
    proj = _repo(tmp_path)
    name = "a -> b.txt"
    _write(proj, name, "A\n")
    _write(proj, "keep.txt", "K\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    _write(proj, name, "A foreign\n")  # foreign pre-dispatch edit
    baseline = _snapshot(proj, untracked="", with_worktree=True)
    _write(proj, "unit.txt", "U\n")  # the unit does not touch it

    # sanity: git really C-quoted the row (the arrow would otherwise be real)
    status = (proj / ".agentic" / "context" / f"BASELINE-{T}.status").read_text()
    assert '"a -> b.txt"' in status

    assert delta.baseline_dirty_paths(proj, T) == {name}, (
        "C-квотированное имя с ' -> ' обязано свестись к сырой идентичности"
    )
    plan = delta.assemble_candidate(proj, T, baseline)
    assert name not in plan.files, (
        "чужое преддиспатчное изменение не входит в кандидат (S2-01)"
    )


# ─── QA iter 5: F-arrow на ПАРЕ (не одиночном файле) ─────────────────────


def test_own_rename_destination_with_arrow_substring_is_a_pair(
    tmp_path: Path,
) -> None:
    """F-arrow на rename-ПАРЕ: destination содержит подстроку ``" -> "``
    (git C-квотирует его), источник ASCII. Разбор стрелки обязан найти РОВНО
    одну границу пары (вне кавычек), и собственный rename включает оба члена,
    а candidate tree не оставляет старый путь. Существующие тесты гоняют
    стрелку только на ОДИНОЧНОМ `` M``-файле и не-ASCII пару без стрелки."""
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "src.txt", "A\n")
    _write(proj, "keep.txt", "K\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    baseline = _snapshot(proj)
    _git(proj, "mv", "src.txt", "dst -> x.txt")  # own rename, arrow in dest

    plan = delta.assemble_candidate(proj, T, baseline, build_tree=True)

    assert "dst -> x.txt" in plan.files, "destination обязан войти"
    assert "src.txt" in plan.files, "source обязан войти как deletion"
    assert not plan.conflicts, "собственный rename — не конфликт"
    tree = {
        line.strip()
        for line in _git(
            proj, "ls-tree", "-r", "--name-only", plan.candidate_tree
        ).stdout.splitlines()
        if line.strip()
    }
    assert "dst -> x.txt" in tree, "новое имя обязано быть в candidate tree"
    assert "src.txt" not in tree, "старое имя не должно остаться"


def test_foreign_staged_rename_destination_with_arrow_is_excluded(
    tmp_path: Path,
) -> None:
    """S2-01 на паре с подстрокой-сепаратором в destination: ЧУЖОЙ staged
    rename ``src2.txt -> "dst -> y.txt"`` до диспатча не должен ни удалить
    source, ни добавить destination; собственный файл юнита остаётся."""
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "src2.txt", "A\n")
    _write(proj, "keep.txt", "K\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    _git(proj, "mv", "src2.txt", "dst -> y.txt")  # FOREIGN staged rename
    baseline = _snapshot(proj)
    _write(proj, "unit.txt", "U\n")

    plan = delta.assemble_candidate(proj, T, baseline)

    assert "dst -> y.txt" not in plan.files
    assert "src2.txt" not in plan.files, "source чужого rename не удаляется"
    assert "unit.txt" in plan.files, "работа юнита обязана остаться"
    assert not plan.conflicts, "оба пути чужие — это не смешанная пара"


# ─── REVIEW iter 5 (аудитор M3): один mode+blob-правило, не третья копия ──


def test_untracked_directory_is_refused_like_the_model(tmp_path: Path) -> None:
    """REVIEW-M3: ``_untracked_mode_blob`` больше не выводит mode+blob
    руками, а делегирует в R2.1-модель. Директория (git не хранит её как
    content blob) обязана получить ТУ ЖЕ отказ, что и модель — старая копия
    фабриковала executable-файл (``st_mode & 0o100`` у директории всегда
    выставлен) вместо отказа (класс F-QA2/QA4)."""
    from awf.infra.git_model import GitRepository, UnsupportedChangeError

    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "d/inner.txt", "x\n")  # an untracked directory

    with pytest.raises(UnsupportedChangeError):
        GitRepository(proj).worktree_mode_blob(b"d")

    with pytest.raises(UnsupportedChangeError):
        delta.DeltaAssembler(proj, T)._untracked_mode_blob(b"d")


def test_untracked_symlink_mode_blob_matches_the_model(tmp_path: Path) -> None:
    """REVIEW-M3 (парная зелёная): symlink-случай делегирования сохранён —
    ``120000`` и blob = строка цели, и дельта-путь совпадает с моделью."""
    from awf.infra.git_model import GitRepository

    delta = _delta()
    proj = _repo(tmp_path)
    os.symlink("nowhere-target", proj / "lnk")

    want = GitRepository(proj).worktree_mode_blob(b"lnk")
    got = delta.DeltaAssembler(proj, T)._untracked_mode_blob(b"lnk")
    assert got == want
    assert got[0] == "120000"
    assert got[1] == subprocess.run(
        ["git", "hash-object", "--stdin"],
        cwd=proj,
        input=b"nowhere-target",
        capture_output=True,
    ).stdout.decode().strip()


# ─── REVIEW iter 6 (QA): чужая rename-пара после правки destination ───────


def test_foreign_staged_rename_with_edited_destination_keeps_the_source(
    tmp_path: Path,
) -> None:
    """RED (S2-01, iter 6): чужой staged rename, у которого destination
    доправлен ПОСЛЕ staging.

    `git diff --name-only <baseline>` (рабочее дерево vs HEAD) теряет
    rename-детекцию, когда правка destination уводит similarity ниже порога:
    он выдаёт `new.txt` + `old.txt` (add+delete), тогда как
    `git status --short` (index vs HEAD) всё ещё видит пару
    `RM old.txt -> new.txt`. Legacy-проход строит `foreign_paths` по снапшоту
    status (там только destination), поэтому source `old.txt` НЕ отсекается и
    уходит в `files` как удаление — коммит юнита снёс бы чужой исходник из
    HEAD (ровно запрет строки S2-01 «Source не удаляется из HEAD»).

    Модель уже знает, что это ПАРА и что оба члена чужие: assembler обязан
    исключить пару целиком, не полагаясь на то, что legacy-проход случайно
    отсеет source.
    """
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "old.txt", "A\n")
    _write(proj, "keep.txt", "K\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    baseline = _git(proj, "rev-parse", "HEAD").stdout.strip()

    # FOREIGN pre-dispatch work: staged rename + heavy edit of the destination.
    _git(proj, "mv", "old.txt", "new.txt")
    _write(proj, "new.txt", "\n".join(f"line {i}" for i in range(50)) + "\n")
    _snapshot(proj, untracked="")

    # precondition: status sees the pair, the worktree diff does not.
    status = (proj / ".agentic" / "context" / f"BASELINE-{T}.status").read_text()
    assert " -> " in status, f"ожидалась rename-пара в снапшоте: {status!r}"
    assert set(_git(proj, "diff", "--name-only", baseline).stdout.split()) == {
        "old.txt",
        "new.txt",
    }, "предусловие: worktree-diff теряет rename-детекцию"

    _write(proj, "unit.txt", "U\n")  # the unit touches neither pair member

    plan = delta.assemble_candidate(proj, T, baseline, build_tree=True)

    assert "old.txt" not in plan.files, (
        "S2-01: source чужого rename не должен удаляться из HEAD"
    )
    assert "new.txt" not in plan.files, "destination чужого rename не входит"
    assert "unit.txt" in plan.files, "работа юнита обязана остаться"
    assert not plan.conflicts, "оба члена чужие — это не смешанная пара"
    if plan.candidate_tree:
        assert _git(
            proj, "cat-file", "-e", f"{plan.candidate_tree}:old.txt", check=False
        ).returncode == 0, "в candidate tree HEAD-версия source обязана остаться"


@pytest.mark.parametrize("klass", ["own", "foreign", "foreign_edited", "mixed"])
def test_pair_decision_table(tmp_path: Path, klass: str) -> None:
    """Контроль-инвентарь iter 6: таблица решений ассемблера по классам
    rename-пары — own (оба члена свои) включается парой с удалением source;
    foreign (оба чужие) исключается целиком; foreign после правки destination
    (similarity ниже порога) тоже исключается целиком, source объяснён в
    ``excluded``; mixed (один член чужой) уходит в ``conflicts``."""
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "a.txt", "A\n")
    _write(proj, "keep.txt", "K\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")

    if klass in ("foreign", "foreign_edited"):
        _git(proj, "mv", "keep.txt", "moved.txt")
    if klass == "foreign_edited":
        _write(proj, "moved.txt", "\n".join(f"l{i}" for i in range(50)) + "\n")
    if klass == "mixed":
        _write(proj, "keep.txt", "K foreign\n")  # source pre-dirty (foreign)

    baseline = _snapshot(proj, untracked="")

    if klass == "own":
        _git(proj, "mv", "a.txt", "b.txt")
    if klass == "mixed":
        _git(proj, "mv", "keep.txt", "moved.txt")
    _write(proj, "unit.txt", "U\n")  # the unit never touches a pair member

    plan = delta.assemble_candidate(proj, T, baseline)

    if klass == "own":
        assert {"a.txt", "b.txt"} <= set(plan.files)
        assert not plan.conflicts
    elif klass == "foreign":
        assert "keep.txt" not in plan.files and "moved.txt" not in plan.files
        assert not plan.conflicts
    elif klass == "foreign_edited":
        assert "keep.txt" not in plan.files and "moved.txt" not in plan.files
        assert "unit.txt" in plan.files
        assert not plan.conflicts
        assert ("keep.txt", "foreign rename pair (staged-at-baseline)") in (
            plan.excluded
        )
    elif klass == "mixed":
        assert plan.conflicts
        assert "keep.txt" not in plan.files and "moved.txt" not in plan.files


# ─── REVIEW iter 7 (QA): стыки прошлых HIGH-классов ──────────────────────


def test_foreign_staged_rename_arrow_in_destination_with_edited_destination(
    tmp_path: Path,
) -> None:
    """S2-01 на СТЫКЕ двух прошлых HIGH: destination содержит подстроку
    ``" -> "`` (F-arrow: git C-квотирует имя) И доправлен после staging так,
    что worktree-diff теряет rename-детекцию (iter 6). Оба класса чинились
    порознь и покрыты порознь; их пересечение — нет. Идентичность пары
    берётся из модели (там кавычки и стрелка уже разведены), поэтому
    ассемблер обязан снять пару целиком и сохранить HEAD-версию source."""
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "src2.txt", "A\n")
    _write(proj, "keep.txt", "K\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    baseline = _git(proj, "rev-parse", "HEAD").stdout.strip()
    dest = "dst -> y.txt"
    _git(proj, "mv", "src2.txt", dest)
    _write(proj, dest, "\n".join(f"line {i}" for i in range(50)) + "\n")
    _snapshot(proj, untracked="")
    _write(proj, "unit.txt", "U\n")

    plan = delta.assemble_candidate(proj, T, baseline, build_tree=True)

    assert dest not in plan.files, "destination чужого rename не входит"
    assert "src2.txt" not in plan.files, "source чужого rename не удаляется"
    assert "unit.txt" in plan.files, "работа юнита обязана остаться"
    assert not plan.conflicts, "оба члена чужие — это не смешанная пара"
    if plan.candidate_tree:
        assert _git(
            proj, "cat-file", "-e", f"{plan.candidate_tree}:src2.txt", check=False
        ).returncode == 0, "HEAD-версия source обязана остаться в candidate tree"


def test_own_rename_and_foreign_pair_coexist(tmp_path: Path) -> None:
    """Собственный rename и чужая staged-пара в ОДНОМ плане: ассемблер
    обрабатывает их независимо — своя пара включается (destination +
    deletion source), чужая снимается целиком, HEAD-версия чужого source
    остаётся в candidate tree (S2-01), свой source из дерева уходит (B01).
    Существующие тесты гоняют эти классы только по отдельности."""
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "own.txt", "O\n")
    _write(proj, "fro.txt", "F\n")
    _write(proj, "keep.txt", "K\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    baseline = _git(proj, "rev-parse", "HEAD").stdout.strip()
    _git(proj, "mv", "fro.txt", "fro2.txt")  # foreign, staged before dispatch
    _snapshot(proj, untracked="")
    _git(proj, "mv", "own.txt", "own2.txt")  # the unit's own rename
    _write(proj, "unit.txt", "U\n")

    plan = delta.assemble_candidate(proj, T, baseline, build_tree=True)

    assert {"own.txt", "own2.txt", "unit.txt"} <= set(plan.files)
    assert "fro.txt" not in plan.files and "fro2.txt" not in plan.files
    assert not plan.conflicts
    tree = set(
        _git(proj, "ls-tree", "-r", "--name-only", plan.candidate_tree).stdout.split()
    )
    assert "own2.txt" in tree, "destination своей пары обязан быть в дереве"
    assert "own.txt" not in tree, "source своей пары уходит из дерева (B01)"
    assert "fro.txt" in tree, "HEAD-версия source чужой пары обязана остаться"
    assert "fro2.txt" not in tree, "destination чужой пары не входит"
