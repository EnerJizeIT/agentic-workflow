"""M7 acceptance: ratchet debt markers, auto numbers, prove-red new-api,
checkpoint template, iteration counter, test package, downtime, PA-2/RE-2,
non-pytest suite counts.

14 red / 6 passed / 1 skipped on the baseline (awf prove_red TODO-0231);
(f) downtime and (g) RE-2 pin behaviour that already existed. The fixtures
build their own tiny git repos; no real run is started.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf import api, paths, run_state, unit_contract
from awf import candidate_verify as cv
from awf import verify_pack as vp
from awf.prove_red import prove_red

REPO_ROOT = Path(__file__).resolve().parents[2]
TODO = "TODO-0001"
#: Built by concatenation so this file itself carries no debt marker.
_D_TODO = "TODO" + ":"
_D_FIXME = "FIX" + "ME"
_D_XXX = "X" + "XX"


# ─── helpers ─────────────────────────────────────────────────────────────


def _todos_counter_command() -> str:
    for line in (REPO_ROOT / ".ratchet-counters").read_text(encoding="utf-8").splitlines():
        if line.startswith("todos\t"):
            return line.split("\t", 1)[1]
    raise AssertionError("no 'todos' counter in .ratchet-counters")


def _count_todos(cwd: Path) -> int:
    out = subprocess.run(
        ["sh", "-c", _todos_counter_command()],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _fake_prove(verdict: str, warnings: list[str] | None = None,
                new_code_acceptable: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        todo_id=TODO,
        verdict=verdict,
        exit_code=2 if verdict == "broken-runner" else 1,
        baseline_sha="a" * 40,
        tests=[],
        copied_files=[],
        baseline_output="",
        current_output="",
        message=f"verdict {verdict}",
        warnings=list(warnings or []),
        new_code_acceptable=new_code_acceptable,
    )


# ─── (a) debt markers, not unit ids ──────────────────────────────────────


def test_todos_counter_counts_debt_markers_not_unit_ids(tmp_path: Path) -> None:
    repo = tmp_path / "counted"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "mod.py").write_text(
        f"def f():\n    # {_D_TODO} real debt\n    # {_D_FIXME}: later\n"
        f"    # {_D_XXX} unsafe\n    return 1\n",
        encoding="utf-8",
    )
    (repo / "test_mod.py").write_text(
        'UNIT = "TODO-0001"\nCASES = {"TODO-0002": 1}\n', encoding="utf-8"
    )

    assert _count_todos(repo) == 3, "TODO:/FIXME/XXX comment markers are debt"
    # The committed repo's own count must equal the recorded baseline.
    baseline = dict(
        line.split() for line in
        (REPO_ROOT / ".ratchet-baseline").read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    assert _count_todos(REPO_ROOT) == int(baseline["todos"])


def test_ratchet_update_rewrites_line_count_in_comment(tmp_path: Path) -> None:
    counters = tmp_path / "counters"
    baseline = tmp_path / "baseline"
    fixture = tmp_path / "fixture.py"
    fixture.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    counters.write_text(
        "# REPORTS33 (TODO-0209): @lines fixture.py (1 lines) — raised\n"
        "demo\tprintf 1\n",
        encoding="utf-8",
    )
    baseline.write_text("demo 1\n", encoding="utf-8")
    env = {
        **os.environ,
        "GT_RATCHET_COUNTERS": str(counters),
        "GT_RATCHET_BASELINE": str(baseline),
    }
    res = subprocess.run(
        ["sh", str(REPO_ROOT / "scripts" / "ratchet.sh"), "--update"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    written = counters.read_text(encoding="utf-8")
    assert "(3 lines)" in written, (
        "the annotated line count must be regenerated from the file"
    )
    assert "(TODO-0209)" in written, (
        "prose parentheses before @lines belong to the comment, not the "
        f"count; got: {written!r}"
    )


def test_ratchet_update_survives_a_bare_count_annotation(tmp_path: Path) -> None:
    """W2 (vitok 5): ``# @lines f.py (12)`` has no unit word after the count.

    Under ``set -u`` the unset ``$2`` used to blank the whole line and the
    script still exited 0. The bare form stays bare: the number refreshes,
    no ``lines`` word is invented, the neighbours survive.
    """
    counters = tmp_path / "counters"
    baseline = tmp_path / "baseline"
    fixture = tmp_path / "fixture.py"
    fixture.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    counters.write_text(
        "# @lines fixture.py (12)\n"
        "next\tprintf 2\n",
        encoding="utf-8",
    )
    baseline.write_text("next 2\n", encoding="utf-8")
    env = {
        **os.environ,
        "GT_RATCHET_COUNTERS": str(counters),
        "GT_RATCHET_BASELINE": str(baseline),
    }
    res = subprocess.run(
        ["sh", str(REPO_ROOT / "scripts" / "ratchet.sh"), "--update"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    written = counters.read_text(encoding="utf-8")
    assert "# @lines fixture.py (3)\n" in written, (
        "the bare form refreshes the count and invents no unit word: "
        f"got: {written!r}"
    )
    assert "next\tprintf 2" in written, (
        "a neighbour line must not be blanked by a malformed annotation: "
        f"got: {written!r}"
    )


# ─── (c) prove_red_kind: new-api ─────────────────────────────────────────


def test_contract_accepts_new_api_kind() -> None:
    content = (
        "---\nprove_red:\n  - tests/test_x.py\n"
        "prove_red_kind: new-api\nprove_red_note: new symbol\n---\n# Task\n"
    )
    data, unknown = unit_contract.parse_todo_contract(content)
    assert unknown == []
    assert data is not None and data["prove_red_kind"] == "new-api"


def test_unknown_prove_red_kind_still_rejected() -> None:
    content = (
        "---\nprove_red:\n  - tests/test_x.py\n"
        "prove_red_kind: nope\nprove_red_note: x\n---\n# Task\n"
    )
    with pytest.raises(ValueError):
        unit_contract.parse_todo_contract(content)


def test_new_api_missing_symbol_is_a_pass(tmp_path: Path, monkeypatch) -> None:
    contract = {
        "prove_red": ["tests/test_x.py"],
        "prove_red_kind": "new-api",
        "prove_red_note": "the module is new",
    }
    fake = _fake_prove(
        "broken-runner",
        ["acceptable for NEW code: the test falls because the symbol does "
         "not exist on the baseline"],
        new_code_acceptable=True,
    )
    monkeypatch.setattr(vp, "prove_red", lambda *a, **kw: fake)
    section = vp._prove_red_section(tmp_path, TODO, contract, None)
    assert section.status == "pass", section.lines
    assert section.detail == "new-api-ok"
    assert "new-api-ok" in "\n".join(section.lines)


def test_new_api_real_broken_runner_stays_fail(tmp_path: Path, monkeypatch) -> None:
    contract = {
        "prove_red": ["tests/test_x.py"],
        "prove_red_kind": "new-api",
        "prove_red_note": "the module is new",
    }
    monkeypatch.setattr(
        vp, "prove_red", lambda *a, **kw: _fake_prove("broken-runner")
    )
    section = vp._prove_red_section(tmp_path, TODO, contract, None)
    assert section.status == "fail"


def test_new_api_prose_warning_alone_is_not_enough(tmp_path: Path, monkeypatch) -> None:
    """The typed fact is the interface: the warning text must not grant a pass."""
    contract = {
        "prove_red": ["tests/test_x.py"],
        "prove_red_kind": "new-api",
        "prove_red_note": "the module is new",
    }
    prose_only = _fake_prove(
        "broken-runner",
        ["acceptable for NEW code: the symbol does not exist on the baseline"],
    )
    monkeypatch.setattr(vp, "prove_red", lambda *a, **kw: prose_only)
    section = vp._prove_red_section(tmp_path, TODO, contract, None)
    assert section.status == "fail"


def test_real_prove_red_producer_sets_the_typed_fact(tmp_path: Path) -> None:
    """`new_code_acceptable` comes from the real producer, not a fake.

    Two baseline shapes raise it (a missing symbol, a missing project module);
    an unrelated broken runner must leave it False.
    """

    def _repo(name: str, body: str) -> Path:
        repo = tmp_path / name
        repo.mkdir()
        _git(repo, "init", "-q")
        (repo / "base.py").write_text("def old():\n    return 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-qm", "b")
        ctx = repo / ".agentic" / "context"
        ctx.mkdir(parents=True)
        (ctx / f"BASELINE-{TODO}.sha").write_text(_git(repo, "rev-parse", "HEAD"))
        (repo / "tests").mkdir()
        (repo / "tests" / "test_x.py").write_text(body, encoding="utf-8")
        return repo

    def _fact(repo: Path, name: str) -> bool:
        got = prove_red(repo, TODO, tests=["tests/test_x.py"], tmp_base=tmp_path / name)
        return got.new_code_acceptable

    symbol = _repo("symbol", "def test_new():\n    from base import new\n    assert new()\n")
    (symbol / "base.py").write_text("def old():\n    return 1\n\ndef new():\n    return 1\n")
    assert _fact(symbol, "wt-symbol") is True

    fresh = _repo("fresh", "from newmod import new\n")
    (fresh / "newmod.py").write_text("def new():\n    return 1\n", encoding="utf-8")
    assert _fact(fresh, "wt-fresh") is True

    broken = _repo("broken", "import absent_third_party\n")
    assert _fact(broken, "wt-broken") is False


# ─── (d) RUN-REPORT: checkpoint template + iteration counter ─────────────


def _report_with_review_headings(tmp_git_repo: Path, headings: str) -> str:
    from awf.api import run as run_mod

    api.init_project(tmp_git_repo, project_name="M7Report")
    checkpoint = tmp_git_repo / ".agentic" / "docs" / "checkpoint-template.md"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.touch()
    done = tmp_git_repo / ".agentic" / "done" / TODO
    done.mkdir(parents=True)
    (done / "TODO.md").write_text("# Task\n\n" + headings, encoding="utf-8")
    state = {"active": False, "queue": [{"todo_id": TODO}], "completed": [TODO],
             "outcomes": {}, "rejects": {}}
    report = run_mod._write_report(tmp_git_repo, state, "unit test")
    assert report is not None
    return report.read_text(encoding="utf-8")


def test_run_report_surfaces_template_and_iterations(tmp_git_repo: Path) -> None:
    text = _report_with_review_headings(
        tmp_git_repo, "## REVIEW-фикс (супервизор, виток 2 — first)\n\n"
        "## REVIEW-фикс (супервизор, виток 3 — second)\n")
    assert ".agentic/docs/checkpoint-template.md" in text
    assert "iters: 3 (REVIEW-фикс 2 раз)" in text


def test_run_report_iters_counts_rounds_not_attempt_counters(
    tmp_git_repo: Path,
) -> None:
    """W1 (vitok 5): ``iter N`` is the pipeline's attempt counter, not a
    review round. A legacy heading's round is its leading number; mixing
    both compared unrelated numbers (TODO-0218 read ``iter 17`` for 14
    rounds)."""
    text = _report_with_review_headings(
        tmp_git_repo, "## REVIEW-фикс 2 (verify, iter 4)\n\n"
        "## REVIEW-фикс (супервизор, виток 3)\n")
    assert "iters: 3 (REVIEW-фикс 2 раз)" in text


def test_run_report_iters_falls_back_to_the_section_count(
    tmp_git_repo: Path,
) -> None:
    """W1 (vitok 5): headings without a round number — each section is the
    round it sits in, so the count of sections is the count."""
    text = _report_with_review_headings(
        tmp_git_repo, "## REVIEW-фикс (verify, iter 2)\n\n"
        "## REVIEW-фикс (verify, iter 3)\n")
    assert "iters: 2 (REVIEW-фикс 2 раз)" in text


def test_run_report_iters_on_archived_todo_0218() -> None:
    """W1 (vitok 5): the real archive — 14 legacy sections numbered 2..14
    with ``iter 2..17`` alongside; the report reads rounds, not attempts."""
    done = REPO_ROOT / ".agentic" / "done" / "TODO-0218"
    if not (done / "TODO.md").is_file():
        pytest.skip("TODO-0218 is not archived in this checkout")
    from awf.api import run as run_mod

    lines = run_mod._iteration_lines(REPO_ROOT, ["TODO-0218"])
    assert "- TODO-0218: iters: 14 (REVIEW-фикс 14 раз)" in lines

# ─── (e) roadmap_r2 test package ─────────────────────────────────────────


def test_roadmap_r2_tests_live_in_a_package() -> None:
    pkg = REPO_ROOT / "tests" / "negative" / "roadmap_r2"
    assert (pkg / "__init__.py").is_file(), "the package needs __init__.py"
    leftover = sorted((REPO_ROOT / "tests" / "negative").glob("test_roadmap_r2_*.py"))
    assert leftover == [], f"files still spread in the negative root: {leftover}"
    for name in ("publish", "delta", "candidate", "git_model", "dispatch"):
        assert (pkg / f"test_roadmap_r2_{name}.py").is_file(), f"missing {name}"


# ─── (f) downtime: an active supervisor is not downtime ──────────────────

def _active_run_after_idle(proj: Path, idle: float) -> None:
    now = datetime.now(timezone.utc)

    def stamp(minutes: float) -> str:
        return (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

    run_state.write_run(proj, started_at=stamp(60), last_supervisor_beat=stamp(idle))
    run_state.supervisor_beat(proj, now=now)


@pytest.mark.parametrize("idle,expected", [(5, 0), (40, 40)])
def test_supervisor_presence_is_not_downtime(
    tmp_git_repo: Path, idle: float, expected: int
) -> None:
    api.init_project(tmp_git_repo, project_name="M7Downtime")
    inbox = tmp_git_repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{TODO}.md").write_text("# T\n")
    api.run_start(tmp_git_repo, queue=[TODO], budget_minutes=240)
    _active_run_after_idle(tmp_git_repo, idle)
    assert api.run_status(tmp_git_repo).downtime_minutes == expected


# ─── (g) PA-2 temp base + RE-2 token match ───────────────────────────────


def test_candidate_temp_files_live_under_opencode(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-qm", "b")
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()

    seen: dict[str, object] = {}
    real = cv.tempfile.mkstemp

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(cv.tempfile, "mkstemp", spy)
    err = cv._checkout_tree(repo, tree, tmp_path / "out")
    assert err == ""
    assert seen.get("dir") == paths.temp_base(), (
        "the candidate index temp file must sit in the candidate temp base"
    )


def test_missing_dependency_binds_only_whole_path_tokens() -> None:
    assert not cv._path_referenced("x", "max(")
    assert not cv._path_referenced("lib.py", "mylib.py")
    assert not cv._path_referenced("src/foo.py", "/tmp/x/src/foobar.py")
    assert cv._path_referenced("src/foo.py", "/tmp/x/src/foo.py")


# ─── (h) non-pytest suite counts ─────────────────────────────────────────


def _suite_project(tmp_path: Path, script: str, tool: str = "npm") -> tuple[Path, Path]:
    proj = tmp_path / "suite"
    proj.mkdir()
    _git(proj, "init", "-q")
    (proj / "README").write_text("suite\n", encoding="utf-8")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-qm", "b")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / tool
    exe.write_text(script, encoding="utf-8")
    exe.chmod(0o755)
    return proj, bindir


def _run_canonical(proj: Path, bindir: Path, command: str) -> dict:
    env = {**os.environ, "AWF_SUITE_CMD": command, "AWF_PYTHON": sys.executable,
           "PATH": f"{Path(sys.executable).parent}:{bindir}:{os.environ['PATH']}"}
    res = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "suite-canonical.sh"),
         "--project", str(proj)],
        env=env, capture_output=True, text=True, timeout=180,
    )
    artifacts = sorted((proj / ".agentic" / "context").glob("SUITE-*.json"))
    assert artifacts, res.stdout + res.stderr
    return json.loads(artifacts[-1].read_text(encoding="utf-8"))


def test_npm_suite_counts_are_extracted(tmp_path: Path) -> None:
    proj, bindir = _suite_project(
        tmp_path,
        "#!/bin/sh\necho 'Tests:       2 failed, 3 passed, 5 total'\nexit 1\n",
    )
    payload = _run_canonical(proj, bindir, "npm test")
    assert payload["runner"] == "npm"
    assert payload["counts"]["passed"] == 3
    assert payload["counts"]["failed"] == 2


def test_unknown_runner_reports_null_counts(tmp_path: Path) -> None:
    proj, bindir = _suite_project(
        tmp_path,
        "#!/bin/sh\necho 'all good'\nexit 0\n",
        tool="suiter",
    )
    payload = _run_canonical(proj, bindir, "suiter test")
    # An unknown runner has no known count format — null, never fake zeros.
    assert payload["counts"] is None
    assert payload["runner"] == "suiter"


def test_runner_is_detected_behind_one_wrapper(tmp_path: Path) -> None:
    """W6 (vitok 5): the payload of a quoted wrapper hides the runner word
    (the boundary quote glues to it — ``pytest'`` is not ``pytest``) and a
    ``timeout``/``env`` prefix carries it away. One level is unwrapped; a
    wrapped KNOWN runner still finds its counts."""
    (tmp_path / "wrap1").mkdir()
    proj1, bindir1 = _suite_project(
        tmp_path / "wrap1",
        "#!/bin/sh\necho '1 passed, 1 skipped'\nexit 0\n",
        tool="pytest",
    )
    payload = _run_canonical(
        proj1, bindir1, f"bash -c 'timeout 600 {bindir1 / 'pytest'}'"
    )
    assert payload["runner"] == "pytest", (
        "the runner word must survive one wrapper level with the quote glue"
    )
    assert payload["counts"]["passed"] == 1

    (tmp_path / "wrap2").mkdir()
    proj2, bindir2 = _suite_project(
        tmp_path / "wrap2",
        "#!/bin/sh\necho '1 passed'\nexit 0\n",
        tool="shrunner",
    )
    ws = _run_canonical(
        proj2, bindir2,
        f"bash -c 'env GATE=1 timeout 5 {bindir2 / 'shrunner'} x'",
    )
    assert ws["counts"] is None, (
        "two wrapper levels stay unknown — the counts stay null with a hint"
    )
    assert ws["counts_hint"], "an unknown wrapped command names the unwrap limit"
