"""M7 REVIEW-виток 4 acceptance (TODO-0231): non-UTF-8 TODOs are legitimate
input for all three TODO readers (verify-pack, commit gate, step marker),
``paths.temp_base`` falls back to the system temp when the base exists but is
unwritable, the RUN-REPORT never prints a template link that the project does
not have, and the ratchet ``@lines`` annotations are real lines refreshed by
``--update`` (the fifth canonical "(8 lines)" mention is a debt-marker count,
stays manual). No live run is started.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

from awf import paths
from awf.commit_gate import _todo_title
from awf.plan_progress import extract_step_id_from_todo
from awf.verify_pack import _load_todo_contract

REPO_ROOT = Path(__file__).resolve().parents[2]
TODO = "TODO-0001"

#: A non-UTF-8 TODO: an ASCII contract block first, an invalid byte below.
_NON_UTF8_TODO = (
    b"---\nverify:\n  - echo ok\n---\n\n"
    b"# \xff streaky title\n\n"
    b"some prose with a raw 0xff byte below the heading\n"
)


# ─── T1: the three TODO readers survive non-UTF-8 content ────────────────


def _project_with_non_utf8_todo(tmp_path: Path) -> Path:
    (tmp_path / ".agentic" / "inbox").mkdir(parents=True)
    (tmp_path / ".agentic" / "inbox" / f"{TODO}.md").write_bytes(_NON_UTF8_TODO)
    logs = tmp_path / "logs"
    logs.mkdir()
    return tmp_path


def test_contract_loads_from_non_utf8_todo(tmp_path: Path) -> None:
    project = _project_with_non_utf8_todo(tmp_path)
    contract = _load_todo_contract(project, TODO, tmp_path / "logs")
    assert contract is not None and "verify" in contract, (
        "a byte-faithful TODO must still yield its contract block"
    )


def test_title_reader_tolerates_non_utf8_todo(tmp_path: Path) -> None:
    project = _project_with_non_utf8_todo(tmp_path)
    title = _todo_title(project, TODO)
    assert title.endswith("streaky title"), (
        "the title reader must degrade the invalid byte, not crash"
    )


def test_step_marker_reader_tolerates_non_utf8_todo(tmp_path: Path) -> None:
    step = tmp_path / "TODO-0002.md"
    step.write_bytes(
        b"---\nstep_id: 2\n---\n\n\xff\n- [ ] \xc0 Number 2: done\n"
    )
    assert extract_step_id_from_todo(step) == 2
    assert extract_step_id_from_todo(tmp_path / "absent.md") is None


def test_verify_pack_completes_on_a_non_utf8_todo(tmp_git_repo: Path) -> None:
    """T1 end-to-end: the whole pack survives, not only the three readers."""
    from awf.verify_pack import verify_pack

    repo = tmp_git_repo
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", "commit", "-qm", "b"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    context = repo / ".agentic" / "context"
    context.mkdir(parents=True)
    (context / f"BASELINE-{TODO}.sha").write_text(head, encoding="utf-8")
    inbox = repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / f"{TODO}.md").write_bytes(_NON_UTF8_TODO)
    outbox = repo / ".agentic" / "outbox"
    outbox.mkdir(parents=True)
    (outbox / f"DONE-{TODO}.json").write_text(
        '{"files_changed": ["a.txt"], "tests_run": [], "gates": [], '
        '"notes": "n"}',
        encoding="utf-8",
    )

    result = verify_pack(
        repo, TODO, write_report=True, tmp_base=repo.parent / "prove-red"
    )

    assert result.verdict == "ok", result.as_dict()
    assert result.report_path and Path(result.report_path).is_file(), (
        "the GATES report must be written even when the TODO is not UTF-8"
    )
    assert result.sections.get("done_json") == "pass", result.as_dict()


# ─── T2: temp_base falls back when the base exists but is unwritable ─────


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores the chmod the test relies on",
)
def test_unwritable_base_falls_back_to_system_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    monkeypatch.setattr(paths, "TMP_BASE", str(locked))
    try:
        assert paths.temp_base() == Path(tempfile.gettempdir()), (
            "an existing-but-unwritable base must fall back, not be returned"
        )
    finally:
        locked.chmod(0o700)
    monkeypatch.setattr(paths, "TMP_BASE", str(tmp_path / "fresh"))
    assert paths.temp_base() == tmp_path / "fresh", (
        "a missing base is still created and returned once it is usable"
    )


# ─── T3: the RUN-REPORT never prints a dead template link ────────────────


def _report_text(project: Path) -> str:
    from awf.api import run as run_mod

    state = {"active": False, "queue": [], "completed": [], "outcomes": {}, "rejects": {}}
    report = run_mod._write_report(project, state, "unit test")
    assert report is not None
    return report.read_text(encoding="utf-8")


def test_report_has_no_template_link_without_the_file(tmp_git_repo: Path) -> None:
    text = _report_text(tmp_git_repo)
    assert "Checkpoint template" not in text, (
        "a project without the template must not get a dead link"
    )
    # W7: the report points at the project copy (init-installed), not the
    # vendor doc in the installed package.
    checkpoint = tmp_git_repo / ".agentic" / "docs" / "checkpoint-template.md"
    checkpoint.parent.mkdir()
    checkpoint.touch()
    assert ".agentic/docs/checkpoint-template.md" in _report_text(tmp_git_repo)


# ─── T4: real @lines annotations, refreshed by --update ──────────────────


def _annotation_lines(counters_text: str) -> list[str]:
    out = []
    for line in counters_text.splitlines():
        if "@lines " not in line:
            continue
        token = line.split("@lines ", 1)[1].split(" ", 1)[0]
        if (REPO_ROOT / token).is_file():
            out.append(line)
    return out


def _annotation_count(line: str) -> str | None:
    """The annotation's RIGHT-side count (W5's property object).

    All three spelled forms the counters file carries: the arrow
    ``(a -> b lines)``, the unit form ``(b lines)`` and the bare ``(b)``.
    None when the annotation spells no number at all.
    """
    inside = line[line.index("@lines ") + len("@lines "):]
    if "(" not in inside:
        return None
    inside = inside[inside.index("(") + 1: inside.index(")")]
    words = inside.replace(" lines", "").split()
    if not words:
        return None
    if len(words) >= 2 and words[1] == "->":
        return words[2] if len(words) > 2 else None
    return words[0]


def test_counters_carry_real_annotations() -> None:
    """W5 (vitok 5): pin the PROPERTY, not the history — every ``@lines``
    annotation names an EXISTING file and its right side matches that
    file's ``wc -l``. Legit comment cleanups in the counters must not turn
    this red; a stale or dangling annotation must."""
    lines = _annotation_lines(
        (REPO_ROOT / ".ratchet-counters").read_text(encoding="utf-8")
    )
    assert lines, "the counters file must still carry real @lines annotations"
    for line in lines:
        token = line.split("@lines ", 1)[1].split(" ", 1)[0]
        real = int((REPO_ROOT / token).read_bytes().count(b"\n"))
        got = _annotation_count(line)
        assert got is not None, f"annotation without a count: {line!r}"
        assert int(got) == real, (
            f"{token}: annotated {got} lines, the file carries {real}"
        )


def test_update_rewrites_a_stale_annotation_to_the_real_length(tmp_path: Path) -> None:
    counters = REPO_ROOT / ".ratchet-counters"
    src = counters.read_text(encoding="utf-8")
    target = _annotation_lines(src)[0]
    m = re.search(r"\(([^()]*)\)", target.split("@lines ", 1)[1])
    assert m, "fixture premise: the annotation carries a count parenthesis"
    right = m.group(1).strip()
    if "->" in right:
        stale = target.replace(f"({m.group(1)})", f"({right.split('->')[0].strip()} -> 0 lines)", 1)
    else:
        stale = target.replace(f"({m.group(1)})", "(0 lines)", 1)
    assert stale != target, "fixture premise: the annotation is rewritable"
    tmp_counters = tmp_path / "counters"
    tmp_counters.write_text(src.replace(target, stale), encoding="utf-8")
    tmp_baseline = tmp_path / "baseline"
    tmp_baseline.write_text("files_over_400 999\ntodos 999\n")
    token = target.split("@lines ", 1)[1].split(" ", 1)[0]
    real_lines = (REPO_ROOT / token).read_bytes().count(b"\n")
    env = {
        **os.environ,
        "GT_RATCHET_COUNTERS": str(tmp_counters),
        "GT_RATCHET_BASELINE": str(tmp_baseline),
    }
    run = subprocess.run(
        ["sh", "scripts/ratchet.sh", "--update"],
        cwd=REPO_ROOT, capture_output=True, text=True, env=env, timeout=300,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    rewritten = tmp_counters.read_text(encoding="utf-8")
    line = next(
        snippet
        for snippet in rewritten.splitlines()
        if "@lines" in snippet and token in snippet
    )
    if "->" in right:
        assert f"-> {real_lines} lines)" in line, (
            "--update must rewrite the stale right side to the file's real wc -l"
        )
    else:
        assert f"({real_lines})" in line, (
            "--update must rewrite the stale bare count to the file's real wc -l"
        )
    assert "0 lines)" not in line
    other = [
        snippet
        for snippet in rewritten.splitlines()
        if "@lines" in snippet and token not in snippet
    ]
    sibling = other[0]
    assert "@lines" in sibling, "a sibling annotation line stays an annotation"
