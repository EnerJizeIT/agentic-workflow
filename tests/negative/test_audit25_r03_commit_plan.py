"""R-03 (аудит 2026-09-25, слой 11): коммит как проверяемый объект результата.

Дефект (связывает A-01/A-04/A-14/A-15): approve писал verdict и сигнал
отдельно, `_handle_next` игнорировал булев отказ commit gate, а гейт сам
выбирал файлы через глобальный Git index — чужие staged записи либо
попадали в коммит юнита, либо снимались при откате, либо (A-01
refuse-first) блокировали коммит вовсе.

Инварианты:
1. CommitPlan — явный объект: TODO ID, поколение забега, проверенный
   отпечаток, список файлов, ожидаемый verdict. Строится на
   approve/verify и передаётся в гейт.
2. Коммит идёт через изолированный временный index (GIT_INDEX_FILE):
   пользовательский index не меняется ни при успехе, ни при отказе; в
   коммит попадают ровно файлы плана.
3. Результат гейта типизирован: committed/skipped/refused/error с
   причиной — execute и verify стадии обрабатывают его одинаково.
4. Сверка отпечатка (A-15) и verdict (A-04) — часть плана, без
   отдельных дублирующих проверок.

До фикса красный: `test_commit_uses_isolated_index` — гейт отказывал на
чужих staged (A-01 refuse-first), коммита не было.

REVIEW-0087 (попытка 2):
- P2: approve приходит в окно ожидания гейта — VERIFIED-файл, записанный
  в окне, перечитывается в план, и A-15 применяется к нему тоже
  (`test_approve_in_wait_window_is_fingerprint_checked`; в prove_red не
  добавлен — на pre-R-03 baseline окно было закрыто и тест зелёный).
- P3: rename/copy-запись в porcelain -z — два токена; источник (не путь)
  теперь потребляется вместе с целевым (`test_rename_source_not_parsed_as_path`).
"""
from __future__ import annotations

import subprocess
import time
import types
from pathlib import Path

import awf.commit_gate as gate
from awf import api, git_utils

try:
    from awf import commit_plan
except ImportError:  # baseline (pre-R-03): the module does not exist yet
    commit_plan = None

from awf.commit_gate import maybe_commit

TODO = "TODO-0001"


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _status(repo: Path) -> list[str]:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.splitlines()


def _staged(repo: Path) -> list[str]:
    return subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.splitlines()


def _index_entries(repo: Path) -> str:
    """The user's index itself (paths + blob SHAs) — not relative to HEAD."""
    return subprocess.run(
        ["git", "ls-files", "--stage"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _committed_files(repo: Path) -> list[str]:
    return subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.splitlines()


def _index_snapshot(repo: Path) -> str:
    """Deterministic user-index snapshot: entries + staged diff."""
    ls = subprocess.run(
        ["git", "ls-files", "--stage"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    diff = subprocess.run(
        ["git", "diff", "--cached"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    return ls + diff


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="R03CommitPlan")
    proj = tmp_git_repo
    (proj / ".agentic" / "inbox" / f"{TODO}.md").write_text("# Task\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=proj, check=True)
    return proj


def _logs(proj: Path) -> Path:
    logs = proj / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs


def test_commit_uses_isolated_index(tmp_git_repo: Path):
    """Посторонний файл staged; план — на файл юнита. Коммит идёт через
    изолированный index: содержит ровно файл плана, посторонний staged
    остаётся staged, пользовательский index не тронут.

    До фикса: гейт отказывал на чужих staged (A-01 refuse-first) — коммита
    не было: первый ассерт (HEAD сдвинулся) красный."""
    proj = _project(tmp_git_repo)
    baseline_sha = _head(proj)

    (proj / "foreign.txt").write_text("user WIP\n", encoding="utf-8")
    subprocess.run(["git", "add", "foreign.txt"], cwd=proj, check=True)
    (proj / "README.md").write_text("worker change\n", encoding="utf-8")
    index_before = _index_entries(proj)

    outcome = maybe_commit(
        "verify", TODO, "commit_and_next", proj, _logs(proj),
        auto=False, baseline_sha=baseline_sha,
    )

    assert _head(proj) != baseline_sha, (
        f"R-03: foreign staged entries must not block the unit commit — "
        f"the gate produced no commit (refused: {getattr(outcome, 'reason', outcome)!r})"
    )
    assert _committed_files(proj) == ["README.md"], (
        "the commit must contain exactly the plan's files"
    )
    assert "A  foreign.txt" in _status(proj), (
        "the user's foreign file must stay staged exactly as before"
    )
    assert _index_entries(proj) == index_before, (
        "the user's index must be byte-identical: the commit ran on the "
        "isolated index, the unit file was never staged into the user's one"
    )
    # Typed result (R-03, invariant 3):
    assert outcome.status == "committed", (
        f"the committed gate run reports committed — got {outcome.status}"
    )
    assert outcome.sha, "a committed outcome carries the commit sha"


def test_commit_plan_typed_results(tmp_git_repo: Path):
    """committed/skipped/refused наблюдаемы как типизированный результат;
    план несёт свои поля (инвариант 1)."""
    assert commit_plan is not None, "R-03 module missing (pre-fix baseline)"
    proj = _project(tmp_git_repo)
    baseline_sha = _head(proj)
    logs = _logs(proj)

    # skipped: clean tree — nothing changed since baseline
    outcome = maybe_commit(
        "verify", TODO, "commit_and_next", proj, logs,
        auto=False, baseline_sha=baseline_sha,
    )
    assert outcome.status == "skipped", (
        f"clean tree must skip — got {outcome.status}: {outcome.reason}"
    )
    assert _head(proj) == baseline_sha

    # committed: the unit change
    (proj / "file.txt").write_text("v1\n", encoding="utf-8")
    outcome = maybe_commit(
        "verify", TODO, "commit_and_next", proj, logs,
        auto=False, baseline_sha=baseline_sha,
    )
    assert outcome.status == "committed", (
        f"the unit change must commit — got {outcome.status}: {outcome.reason}"
    )
    assert outcome.sha and _head(proj) != baseline_sha

    # refused: the active run's diary says rejected (A-04, via the plan)
    (proj / "file.txt").write_text("v2\n", encoding="utf-8")
    api.run_start(proj, queue=[TODO])
    api.reject_commit(proj, TODO, "defect: the gate plan")
    head_before = _head(proj)
    index_before = _index_entries(proj)

    plan = commit_plan.build_commit_plan(proj, TODO, baseline_sha)
    assert plan.todo_id == TODO
    assert plan.generation == 1, "the plan carries the active run's generation"
    assert "file.txt" in plan.files
    assert plan.expected_verdict == "approved"

    outcome = maybe_commit(
        "verify", TODO, "commit_and_next", proj, logs,
        auto=False, baseline_sha=baseline_sha,
    )
    assert outcome.status == "refused", (
        f"the rejected verdict must refuse — got {outcome.status}: {outcome.reason}"
    )
    assert outcome.reason, "a refused outcome carries the reason"
    assert _head(proj) == head_before, "nothing may be committed"
    assert _index_entries(proj) == index_before, (
        "nothing may be staged into the user's index"
    )


def test_index_untouched_on_refused_plan(tmp_git_repo: Path):
    """Отказ по плану (расхождение проверенного отпечатка, A-15) не меняет
    index пользователя: до и после — один и тот же снимок."""
    assert commit_plan is not None, "R-03 module missing (pre-fix baseline)"
    proj = _project(tmp_git_repo)
    baseline_sha = _head(proj)

    (proj / "foreign.txt").write_text("user WIP\n", encoding="utf-8")
    subprocess.run(["git", "add", "foreign.txt"], cwd=proj, check=True)
    (proj / "file.txt").write_text("unit work\n", encoding="utf-8")
    index_before = _index_snapshot(proj)

    # approve с проверенным отпечаток — а затем дерево сместилось:
    # отпечаток в плане больше не сходится
    fp = git_utils.tree_fingerprint(proj)
    api.approve_commit(proj, TODO, verified_sha=fp)
    assert (proj / ".agentic" / "context" / f"VERIFIED-{TODO}.sha").is_file()
    (proj / "file.txt").write_text("moved after approve\n", encoding="utf-8")

    outcome = maybe_commit(
        "verify", TODO, "commit_and_next", proj, _logs(proj),
        auto=True, baseline_sha=baseline_sha,
    )

    assert outcome.status == "refused", (
        f"fingerprint drift must refuse — got {outcome.status}: {outcome.reason}"
    )
    assert _head(proj) == baseline_sha, "no commit may appear"
    assert _index_snapshot(proj) == index_before, (
        "R-03: a refused plan must leave the user's index byte-identical"
    )
    assert "A  foreign.txt" in _status(proj), "the foreign file stays staged"


def test_approve_in_wait_window_is_fingerprint_checked(tmp_git_repo: Path, monkeypatch):
    """REVIEW-0087 P2: approve приходит, пока гейт ждёт сигнала. План
    строится ДО ожидания, поэтому VERIFIED-файл, записанный в окне, в нём
    отсутствует (verified_sha="") и A-15 молча пропускается — гейт
    коммитит дерево, сместившееся после верификации.

    Детерминированно: polling-хелпер гейта играет роль супервизора — он
    записывает отпечаток и APPROVE-сигнал, затем сдвигает файл. После
    ожидания гейт обязан перечитать отпечаток в план и отказать. Патчится
    только привязка time самого гейта (не глобальный модуль — он же поллит
    subprocess.run(timeout=...), и «approve» срабатывал бы изнутри прогона
    git: сдвиг становился идемпотентным, отпечаток сходился).

    Красный до фикса: план построен до ожидания, сверка отпечатка
    пропускается, результат committed вместо refused. В prove_red не
    добавлен: на pre-R-03 baseline окно было закрыто (старый код читал
    VERIFIED после ожидания) и тест зелёный.
    """
    assert commit_plan is not None, "R-03 module missing (pre-fix baseline)"
    proj = _project(tmp_git_repo)
    baseline_sha = _head(proj)
    logs = _logs(proj)
    (proj / "file.txt").write_text("unit work\n", encoding="utf-8")

    calls: list[float] = []

    def fake_sleep(_seconds: float) -> None:
        # the supervisor approves in the wait window, then the tree moves
        calls.append(_seconds)
        fp = git_utils.tree_fingerprint(proj)
        (proj / ".agentic" / "context" / f"VERIFIED-{TODO}.sha").write_text(
            fp + "\n", encoding="utf-8"
        )
        (proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready").write_text(
            "", encoding="utf-8"
        )
        (proj / "file.txt").write_text("moved after approve\n", encoding="utf-8")

    # Patch the GATE's own time binding, not the global time module:
    # subprocess.run(timeout=...) polls internally via time.sleep, and a
    # global patch made those polls fake-approve the window (the "move"
    # ran idempotently and the fingerprint matched — flake under load).
    gate_time = types.SimpleNamespace(monotonic=time.monotonic, sleep=fake_sleep)
    monkeypatch.setattr(gate, "time", gate_time)

    outcome = maybe_commit(
        "verify", TODO, "commit_and_next", proj, logs,
        auto=True, baseline_sha=baseline_sha,
    )

    assert outcome.status == "refused", (
        f"the approve arrived in the wait window and the tree moved after "
        f"its fingerprint was stored — the gate must refuse; got "
        f"{outcome.status}: {outcome.reason}"
    )
    assert calls, "the gate must have entered the signal wait"
    assert len(calls) == 1, (
        f"the supervisor acts exactly once, in the wait window — "
        f"the gate's sleep was called {len(calls)} times"
    )
    assert "tree changed after verification" in outcome.reason
    assert _head(proj) == baseline_sha, "no commit may appear"


def test_rename_source_not_parsed_as_path(tmp_git_repo: Path):
    """REVIEW-0087 P3: staged rename с именем источника, похожим на
    статус-префикс (`git mv '?a b' ...`). Porcelain -z выдаёт rename как
    два токена — "R  <target>" и ГОЛОЕ имя источника; источник — не путь.
    До фикса сиротский токен источника парсился как запись, и его хвост
    ("b") попадал в план как чужой path (ложный error на git add). Сам
    rename — staged WIP пользователя — в план не попадает.
    """
    assert commit_plan is not None, "R-03 module missing (pre-fix baseline)"
    proj = _project(tmp_git_repo)

    # the rename source must be in HEAD for git to emit an "R" record
    # (otherwise git shows a plain addition — no two-token record)
    (proj / "?a b").write_text("rename source\n", encoding="utf-8")
    subprocess.run(["git", "add", "?a b"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "add rename source"], cwd=proj, check=True)

    # the unit change + the user's staged rename with a status-like source
    (proj / "file.txt").write_text("unit work\n", encoding="utf-8")
    subprocess.run(["git", "mv", "?a b", "renamed.txt"], cwd=proj, check=True)

    plan = commit_plan.build_commit_plan(proj, TODO, baseline_sha="")

    assert "b" not in plan.files, (
        "the orphan rename-source token must not be parsed as a path — "
        f"the plan picked up a phantom entry: {plan.files}"
    )
    assert "renamed.txt" not in plan.files and "?a b" not in plan.files, (
        "a staged rename is the user's index WIP — it stays out of the plan"
    )
    assert plan.files == ("file.txt",), (
        f"the plan must contain exactly the unit's change, got {plan.files}"
    )
