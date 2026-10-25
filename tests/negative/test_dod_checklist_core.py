"""TODO-0244: the Definition-of-done checklist core.

Proves, on a real project in ``tmp_path``, the whole surface of the unit:

(a) composition — base + declared classes + wave + acceptance, declared-wins,
    unknown class refusal, duplicate-id refusal;
(b) structural probes — a shell string is refused, the allowlist holds, the
    timeout is capped;
(c) the gate — a full set is ok, one false item is missing, DONE claims
    never override the run;
(d) cache — the same tree reuses the record, a changed tree recomputes;
(e) ``checklist apply`` — idempotent, backs up, refuses started/archived;
(f) verify_pack — the checklist section passes, fails, or is ``none``;
(g) DONE proposals become a warning, never an automatic change;
(h) the design doc carries the four owned sections.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf import api, cmd_checklist, verify_pack
from awf.checklist_gate import run as run_gate
from awf.checklists import (
    CHECKLIST_HEADING,
    MAX_PROBE_TIMEOUT,
    ChecklistError,
    Item,
    argv_refusal,
    block_of,
    compose,
    load_base,
    load_classes,
    parse_block,
    parse_item,
    render_block,
    render_item,
)
from awf.unit_contract import parse_todo_contract

REPO = Path(__file__).resolve().parents[2]


# ─── helpers ────────────────────────────────────────────────────────────


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="DoD")
    return tmp_git_repo


def _dispatch(proj: Path, text: str) -> tuple[str, Path]:
    result = api.dispatch_todo(proj, text)
    path = proj / ".agentic" / "inbox" / f"{result.todo_id}.md"
    return result.todo_id, path


def _todo(body: str) -> str:
    return f"# TODO — checklist unit\n\n{body}\n"


def _wave(proj: Path, name: str, items: list[dict]) -> None:
    path = proj / ".agentic" / "checklists" / "waves" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"name: {name}", "items:"]
    for item in items:
        lines.append(f"  - id: {item['id']}")
        lines.append(f"    text: {item['text']}")
        if "path" in item:
            lines.append("    probe:")
            lines.append("      type: file_exists")
            lines.append(f"      path: {item['path']}")
        elif "argv" in item:
            lines.append("    probe:")
            lines.append("      type: command_rc")
            lines.append(f"      argv: {json.dumps(item['argv'])}")
            lines.append(f"    expect: {item.get('expect', 0)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _apply(proj: Path, todo_id: str) -> tuple[int, str]:
    args = SimpleNamespace(
        action="apply", todo_id=todo_id, project_dir=str(proj), json=False
    )
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cmd_checklist.run(args)
    return rc, buf.getvalue()


def _done_json(proj: Path, todo_id: str, payload: dict) -> None:
    path = proj / ".agentic" / "outbox" / f"DONE-{todo_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# ─── (a) composition ────────────────────────────────────────────────────


def test_composition_base_class_wave_acceptance(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    _wave(
        proj,
        "w1",
        [
            {"id": "wave-one", "text": "Пункт волны", "path": "tests"},
        ],
    )
    text = _todo("## Приёмка\n- Первый критерий\n- Второй критерий\n")
    contract, _ = parse_todo_contract(
        "---\nchecklist_classes: [packaging]\nwave: w1\n---\n" + text
    )
    composition = compose(proj, text, contract)
    ids = [item.id for item in composition.items]
    assert ids[: len(load_base())] == [i.id for i in load_base()]
    packaging = [i.id for i in load_classes()["packaging"]]
    assert packaging == ids[len(load_base()) : len(load_base()) + len(packaging)]
    assert "wave-one" in ids
    assert ids[-2:] == ["unit-01", "unit-02"]
    assert composition.wave == "w1"


def test_composition_only_declared_classes(tmp_git_repo: Path) -> None:
    """Boundary hints are printed, never merged: a class not declared is out."""
    proj = _project(tmp_git_repo)
    text = _todo("## Границы\n`awf/cli.py`, `pyproject.toml`\n\n## Приёмка\n- критерий\n")
    composition = compose(proj, text, {})
    assert "cli-parity" in composition.suggested
    ids = [item.id for item in composition.items]
    assert "cli-help-command" not in ids
    assert "packaging-pyproject" not in ids


def test_a_declared_class_is_not_a_hint(tmp_git_repo: Path) -> None:
    """`suggested` names only classes the unit has NOT declared.

    One meaning for both writers: dispatch prints the line, `checklist
    apply` prints the same line — a declared class is not an invitation.
    """
    proj = _project(tmp_git_repo)
    text = _todo(
        "## Границы\n`awf/cli.py`, `pyproject.toml`\n\n## Приёмка\n- критерий\n"
    )
    composition = compose(proj, text, {"checklist_classes": ["cli-parity"]})
    assert "cli-parity" not in composition.suggested
    assert "packaging" in composition.suggested


def test_a_hint_never_names_a_class_the_catalog_does_not_have(
    tmp_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hint table names classes as strings; the catalog is the vocabulary.

    A renamed/removed class must not leave a hint that tells the supervisor
    to declare a class ``merge_items`` would refuse.
    """
    from awf.checklists import _compose

    proj = _project(tmp_git_repo)
    monkeypatch.setattr(
        _compose,
        "_SUGGESTIONS",
        (*_compose._SUGGESTIONS, ("ghost-class", ("pyproject",))),
    )
    text = _todo("## Границы\n`pyproject.toml`\n\n## Приёмка\n- критерий\n")
    composition = compose(proj, text, {})
    assert "packaging" in composition.suggested
    assert "ghost-class" not in composition.suggested


def test_composition_unknown_class_refused(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    with pytest.raises(ChecklistError, match="unknown checklist class"):
        compose(proj, _todo("## Приёмка\n- x\n"), {"checklist_classes": ["nope"]})


def test_composition_duplicate_id_refused(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    _wave(proj, "dup", [{"id": "unit-01", "text": "столкновение", "path": "tests"}])
    with pytest.raises(ChecklistError, match="duplicate checklist id"):
        compose(proj, _todo("## Приёмка\n- x\n"), {"wave": "dup"})


def test_dispatch_freezes_the_block(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- критерий\n"))
    text = path.read_text(encoding="utf-8")
    assert CHECKLIST_HEADING in text
    items = parse_block(text)
    assert items is not None
    assert [i.id for i in items][: len(load_base())] == [i.id for i in load_base()]
    assert items[-1].id == "unit-01"
    assert items[-1].manual
    # The block is frozen: a later wave edit cannot reach the live unit.
    _wave(proj, "late", [{"id": "late-item", "text": "поздно", "path": "tests"}])
    assert block_of(path.read_text(encoding="utf-8")) == block_of(text)


def test_dispatch_refuses_unknown_class(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    with pytest.raises(api.AwfApiError, match="checklist: unknown checklist class"):
        api.dispatch_todo(
            proj,
            "---\nchecklist_classes: [ghost]\n---\n" + _todo("## Приёмка\n- x\n"),
        )
    assert not list((proj / ".agentic" / "inbox").glob("TODO-*.md"))


# ─── (b) probes are structural ──────────────────────────────────────────


def _item(**probe) -> dict:
    return {"id": "p", "text": "проба", "probe": probe}


@pytest.mark.parametrize(
    "argv",
    [
        ["sh", "-c", "echo hi"],
        ["bash", "-c", "true"],
        ["grep", "x", "|", "y"],
        ["curl", "https://example.invalid"],
        ["git", "push"],
        ["python3", "-c", "print(1)"],
        "git status",
    ],
)
def test_probe_shell_and_allowlist_refused(argv) -> None:
    with pytest.raises(ChecklistError):
        parse_item(_item(type="command_rc", argv=argv), "t[0]", source="test")


def test_probe_allowlisted_command_accepted() -> None:
    item = parse_item(
        {**_item(type="command_rc", argv=["git", "status", "--porcelain"]), "expect": 0},
        "t[0]",
        source="test",
    )
    assert item.probe["timeout"] == 120


def test_probe_timeout_capped() -> None:
    with pytest.raises(ChecklistError, match="timeout"):
        parse_item(
            {
                **_item(type="command_rc", argv=["git", "status"], timeout=121),
                "expect": 0,
            },
            "t[0]",
            source="test",
        )


def test_block_round_trip_keeps_a_custom_timeout() -> None:
    """The frozen block is what the gate runs: a bound inside the cap survives.

    A non-default timeout used to be dropped at freeze and silently widened
    to the cap — the block (not the YAML) is the source of a run, so the
    declared bound has to travel with it.
    """
    item = parse_item(
        {
            **_item(type="command_rc", argv=["git", "status"], timeout=30),
            "expect": 0,
        },
        "t[0]",
        source="test",
    )
    block = render_block([item])
    assert "timeout=30" in block
    parsed = parse_block(block)
    assert parsed is not None
    assert parsed[0].probe["timeout"] == 30

    default_item = parse_item(
        {**_item(type="command_rc", argv=["git", "status"]), "expect": 0},
        "t[0]",
        source="test",
    )
    default_block = render_block([default_item])
    assert "timeout=" not in default_block
    default_parsed = parse_block(default_block)
    assert default_parsed is not None
    assert default_parsed[0].probe["timeout"] == MAX_PROBE_TIMEOUT


def test_probe_unknown_key_refused() -> None:
    with pytest.raises(ChecklistError, match="unknown probe key"):
        parse_item(
            {"id": "p", "text": "t", "probe": {"type": "file_exists", "path": "x", "sh": "y"}},
            "t[0]",
            source="test",
        )


def test_block_with_shell_probe_is_refused_on_parse() -> None:
    """The frozen block is what the gate EXECUTES — the same allowlist holds.

    A hand-edited TODO is not re-composed at dispatch (the block is frozen),
    so the parse step is the only guard left between a `bash -c` line and a
    probe run at verify time.
    """
    block = (
        "## Чек-лист завершения\n\n"
        '- [ ] sneaky: обход — probe(command_rc "bash" "-c" "true") expect rc=0\n'
    )
    with pytest.raises(ChecklistError, match="not allowed"):
        parse_block(block)


# ─── (c) the gate ───────────────────────────────────────────────────────


def test_gate_full_set_ok(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    (proj / "tests").mkdir()
    _wave(proj, "ok", [{"id": "wave-ok", "text": "тесты", "path": "tests"}])
    todo_id, _ = _dispatch(
        proj,
        "---\nwave: ok\n---\n" + _todo("## Приёмка\n- критерий\n"),
    )
    result = run_gate(proj, todo_id)
    assert result["ok"] is True
    assert result["missing"] == []
    assert result["manual"] == ["unit-01"]


def test_gate_false_item_is_missing(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    _wave(proj, "bad", [{"id": "wave-bad", "text": "нет файла", "path": "nope.txt"}])
    todo_id, _ = _dispatch(proj, "---\nwave: bad\n---\n" + _todo("## Приёмка\n- x\n"))
    result = run_gate(proj, todo_id)
    assert result["ok"] is False
    assert result["missing"] == ["wave-bad"]
    detail = next(i for i in result["items"] if i["id"] == "wave-bad")["detail"]
    assert "missing" in detail


def test_gate_done_claim_is_not_truth(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    _wave(proj, "lie", [{"id": "wave-lie", "text": "ложь", "path": "nope.txt"}])
    todo_id, _ = _dispatch(proj, "---\nwave: lie\n---\n" + _todo("## Приёмка\n- x\n"))
    _done_json(proj, todo_id, {"checklist": {"wave-lie": True}})
    result = run_gate(proj, todo_id)
    assert result["ok"] is False
    assert "wave-lie" in result["missing"]
    assert any("failed" in w and "wave-lie" in w for w in result["warnings"])


# ─── (d) tree-sha cache ─────────────────────────────────────────────────


def test_gate_cache_reused_and_recomputed(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    todo_id, _ = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    first = run_gate(proj, todo_id)
    assert first["cached"] is False
    second = run_gate(proj, todo_id)
    assert second["cached"] is True
    assert second["tree_sha"] == first["tree_sha"]

    (proj / "new-file.txt").write_text("change\n", encoding="utf-8")
    third = run_gate(proj, todo_id)
    assert third["cached"] is False
    assert third["tree_sha"] != first["tree_sha"]


def test_cached_run_still_sees_new_done_proposals(tmp_git_repo: Path) -> None:
    """DONE facts live outside the tree fingerprint — a reuse must re-read them."""
    proj = _project(tmp_git_repo)
    todo_id, _ = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    first = run_gate(proj, todo_id)
    assert first["cached"] is False
    assert first["proposals"] == 0

    done = proj / ".agentic" / "outbox" / f"DONE-{todo_id}.md"
    done.parent.mkdir(parents=True, exist_ok=True)
    done.write_text("## Предложения по чек-листу\n- добавить X\n", encoding="utf-8")

    second = run_gate(proj, todo_id)
    assert second["cached"] is True
    assert second["proposals"] == 1
    assert any(w.startswith("proposals: 1") for w in second["warnings"])


def test_done_facts_are_read_from_the_legacy_short_name(
    tmp_git_repo: Path,
) -> None:
    """The gate uses the handoff's finder, so `DONE-0001.*` counts too.

    A worker may still write the legacy short signal name. Reading the
    canonical path directly would skip its claims silently — the gate must
    see exactly what the handoff sees.
    """
    proj = _project(tmp_git_repo)
    todo_id, _ = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    short = todo_id.rsplit("-", 1)[-1]
    done = proj / ".agentic" / "outbox" / f"DONE-{short}.json"
    done.parent.mkdir(parents=True, exist_ok=True)
    done.write_text(json.dumps({"checklist": ["ghost-item"]}), encoding="utf-8")

    result = run_gate(proj, todo_id)
    assert any("ghost-item" in w for w in result["warnings"])


# ─── (e) apply ──────────────────────────────────────────────────────────


def test_apply_is_idempotent_with_backup(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    text = path.read_text(encoding="utf-8")
    assert "packaging-pyproject" not in text

    # The unit later declares a class: apply re-composes the frozen block.
    path.write_text(
        "---\nchecklist_classes: [packaging]\n---\n" + text, encoding="utf-8"
    )
    rc, out = _apply(proj, todo_id)
    assert rc == 0 and "applied" in out
    new_text = path.read_text(encoding="utf-8")
    assert "packaging-pyproject" in new_text
    backups = list((proj / ".agentic" / "context").glob(f"{todo_id}.md.bak-*"))
    assert len(backups) == 1

    rc2, out2 = _apply(proj, todo_id)
    assert rc2 == 0 and "up to date" in out2
    backups2 = list((proj / ".agentic" / "context").glob(f"{todo_id}.md.bak-*"))
    assert len(backups2) == 1


def test_apply_refuses_started_or_archived(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))

    progress = proj / ".agentic" / "outbox" / f"PROGRESS-{todo_id}.md"
    progress.parent.mkdir(parents=True, exist_ok=True)
    progress.write_text("started\n", encoding="utf-8")
    rc, _ = _apply(proj, todo_id)
    assert rc == 2
    progress.unlink()

    archived = proj / ".agentic" / "done" / todo_id
    archived.mkdir(parents=True, exist_ok=True)
    (archived / "TODO.md").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    rc, _ = _apply(proj, todo_id)
    assert rc == 2


# ─── (f) verify_pack section ────────────────────────────────────────────


def _pack(proj: Path, todo_id: str):
    return verify_pack.verify_pack(proj, todo_id, write_report=False)


def test_verify_pack_section_pass(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    todo_id, _ = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    result = _pack(proj, todo_id)
    assert result.sections["checklist"] == "pass"


def test_verify_pack_section_fail(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    _wave(proj, "fail", [{"id": "wave-fail", "text": "нет", "path": "nope.txt"}])
    todo_id, _ = _dispatch(proj, "---\nwave: fail\n---\n" + _todo("## Приёмка\n- x\n"))
    result = _pack(proj, todo_id)
    assert result.sections["checklist"] == "fail"
    assert result.exit_code == 1


def test_verify_pack_section_none(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    text = block_of(path.read_text(encoding="utf-8"))
    assert text is not None
    path.write_text(path.read_text(encoding="utf-8").replace(text, "").rstrip() + "\n",
                    encoding="utf-8")
    result = _pack(proj, todo_id)
    assert result.sections["checklist"] == "skipped"
    assert result.details["checklist"] == "none"


# ─── (g) proposals ──────────────────────────────────────────────────────


def test_done_proposals_are_a_warning(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    todo_id, _ = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    done = proj / ".agentic" / "outbox" / f"DONE-{todo_id}.md"
    done.parent.mkdir(parents=True, exist_ok=True)
    done.write_text(
        "## Предложения по чек-листу\n- добавить проверку X\n- убрать проверку Y\n",
        encoding="utf-8",
    )
    result = run_gate(proj, todo_id)
    assert result["proposals"] == 2
    assert any(w.startswith("proposals: 2") for w in result["warnings"])


# ─── (h) design doc ─────────────────────────────────────────────────────


def test_design_doc_has_four_owned_sections() -> None:
    text = (REPO / "docs" / "design" / "dod-gate.md").read_text(encoding="utf-8")
    for heading in ("## Владельцы", "## Правила обновления", "## Pruning", "## Метрики"):
        assert heading in text


# ─── (R1) the allowlist is a CLOSED flag list ───────────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "diff", "--output=qa-probe-write.txt", "HEAD"],
        ["git", "diff", "--output", "qa-probe-write.txt"],
        ["git", "show", "--output=qa-probe-write.txt", "HEAD"],
        ["git", "diff", "--ext-diff"],
        ["git", "status", "-c", "core.pager=cat"],
        ["rg", "--pre", "sh -c 'touch qa-pwned.txt'", "pattern", "."],
        ["rg", "--hostname-bin", "id", "pattern", "."],
        ["rg", "--exec", "touch", "pattern", "."],
        ["grep", "-f", "/etc/passwd", "x"],
        ["grep", "--file=/etc/passwd", "x"],
        ["ls", "/"],
        ["grep", "-q", "x", "/etc/passwd"],
        ["test", "-e", "../outside"],
        ["find", ".", "-exec", "touch", "{}", ";"],
        ["env", "FOO=1", "ls"],
        ["xargs", "ls"],
        ["make"],
    ],
)
def test_allowlist_refuses_write_exec_and_escape(argv) -> None:
    assert argv_refusal(argv), f"{argv!r} must be refused by the closed allowlist"


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "status", "--porcelain"],
        ["git", "status", "--porcelain=v1"],
        ["git", "diff", "--stat", "HEAD"],
        ["git", "diff", "--unified=3", "--", "awf/x.py"],
        ["git", "diff", "--name-only", "--", "awf"],
        ["git", "ls-files", "--error-unmatch", "pyproject.toml"],
        ["git", "show", "--stat", "HEAD"],
        ["grep", "-q", "-e", "pattern", "--", "awf/x.py"],
        ["rg", "-n", "pattern", "awf"],
        ["ls", "-l", "awf"],
        ["test", "-f", "pyproject.toml"],
        ["python3", "-m", "pytest", "--collect-only", "tests"],
    ],
)
def test_allowlist_accepts_legal_forms(argv) -> None:
    assert argv_refusal(argv) == "", f"{argv!r} is a legal read-only probe"


def test_wave_with_a_write_flag_is_refused_at_dispatch(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    _wave(
        proj,
        "bad",
        [
            {
                "id": "bad-write",
                "text": "пишет",
                "argv": ["git", "diff", "--output=qa-probe-write.txt", "HEAD"],
            }
        ],
    )
    with pytest.raises(api.AwfApiError, match="checklist: .*--output"):
        api.dispatch_todo(proj, "---\nwave: bad\n---\n" + _todo("## Приёмка\n- x\n"))


def test_frozen_block_with_a_write_flag_is_refused_on_parse() -> None:
    block = (
        "## Чек-лист завершения\n\n"
        '- [ ] sneaky: пишет — probe(command_rc "git" "diff" '
        '"--output=qa-probe-write.txt" "HEAD") expect rc=0\n'
    )
    with pytest.raises(ChecklistError, match="--output"):
        parse_block(block)


# ─── (R2) a broken block is a section verdict ───────────────────────────


def test_verify_pack_malformed_block_is_a_failed_section(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- критерий\n"))
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("- [ ] unit-01: критерий — manual", "- [ ] unit-01: x — broken"),
        encoding="utf-8",
    )
    result = verify_pack.verify_pack(proj, todo_id, write_report=False)
    assert result.sections["checklist"] == "fail"


def test_verify_pack_refused_probe_argv_is_a_failed_section(
    tmp_git_repo: Path,
) -> None:
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- критерий\n"))
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            'probe(command_rc ["git", "status", "--porcelain"]) expect rc=0',
            'probe(command_rc ["git", "diff", "--output=q.txt", "HEAD"]) expect rc=0',
        ),
        encoding="utf-8",
    )
    result = verify_pack.verify_pack(proj, todo_id, write_report=False)
    assert result.sections["checklist"] == "fail"


# ─── (R3) a carried block still answers to its declaration ─────────────


def test_carried_block_with_declared_class_round_trips(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    _, path = _dispatch(
        proj,
        "---\nchecklist_classes: [packaging]\n---\n" + _todo("## Приёмка\n- x\n"),
    )
    carried = path.read_text(encoding="utf-8")
    second = api.dispatch_todo(proj, carried)
    second_path = proj / ".agentic" / "inbox" / f"{second.todo_id}.md"
    assert block_of(second_path.read_text(encoding="utf-8")) == block_of(carried)


def test_carried_block_missing_a_declared_class_item_is_refused(
    tmp_git_repo: Path,
) -> None:
    proj = _project(tmp_git_repo)
    _, path = _dispatch(
        proj,
        "---\nchecklist_classes: [packaging]\n---\n" + _todo("## Приёмка\n- x\n"),
    )
    carried = path.read_text(encoding="utf-8").replace(
        "checklist_classes: [packaging]", "checklist_classes: [packaging, scripts]"
    )
    with pytest.raises(api.AwfApiError, match="misses declared class item"):
        api.dispatch_todo(proj, carried)


def test_carried_block_with_a_missing_wave_file_is_refused(
    tmp_git_repo: Path,
) -> None:
    proj = _project(tmp_git_repo)
    _, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    carried = path.read_text(encoding="utf-8").replace(
        "# TODO — checklist unit", "---\nwave: nothere\n---\n\n# TODO — checklist unit"
    )
    with pytest.raises(api.AwfApiError, match="wave 'nothere' not found"):
        api.dispatch_todo(proj, carried)


# ─── (S1) the cache key sees gitignored probe targets ───────────────────


def test_gate_cache_masks_a_broken_gitignored_probe(tmp_git_repo: Path) -> None:
    """The tree fingerprint excludes `.agentic/`, but the base probes read it.

    A green run followed by a deleted baseline file must NOT reuse the
    cached PASS: the key carries the probe targets themselves.
    """
    proj = _project(tmp_git_repo)
    todo_id, _ = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    first = run_gate(proj, todo_id)
    assert first["ok"] is True, first["missing"]

    baseline = proj / ".agentic" / "context" / f"BASELINE-{todo_id}.sha"
    assert baseline.is_file()
    baseline.unlink()

    second = run_gate(proj, todo_id)
    assert second["cached"] is False
    assert second["ok"] is False
    assert "base-baseline-sha" in second["missing"]

    # The same data again is a reuse — the key is stable, not permanently
    # invalidated by the gate's own cache write.
    third = run_gate(proj, todo_id)
    assert third["cached"] is True


# ─── (S2) render → parse is one grammar ─────────────────────────────────


def test_block_round_trip_keeps_a_control_char_in_argv() -> None:
    """One JSON codec: a tab/newline is itself, not a literal ``\\t``/``\\n``."""
    pattern = "^\tvalue:\nnext"
    item = Item(
        id="tab",
        text="tab-indented value",
        probe={"type": "grep_present", "file": "a.yaml", "pattern": pattern},
        expect=True,
        source="base",
    )
    block = "# TODO\n\n## Чек-лист завершения\n\n" + render_item(item) + "\n"
    parsed = parse_block(block)
    assert parsed is not None
    assert parsed[0].probe["pattern"] == pattern
    assert parsed[0].text == item.text

    multiline = Item(
        id="ml",
        text="first line\nsecond line",
        probe={"type": "file_exists", "path": "README.md"},
        expect=True,
        source="base",
    )
    parsed_ml = parse_block(
        "# TODO\n\n## Чек-лист завершения\n\n" + render_item(multiline) + "\n"
    )
    assert parsed_ml is not None
    assert parsed_ml[0].text == multiline.text


def test_a_non_utf8_todo_does_not_crash_the_gate(tmp_git_repo: Path) -> None:
    """The TODO reader keeps non-UTF-8 bytes; the hashes must too.

    A strict ``encode("utf-8")`` raised ``UnicodeEncodeError`` out of the
    gate (and out of verify-pack) on a file the reader was built to keep.
    """
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    path.write_bytes(path.read_bytes() + b"\nnote: \xff latin-1\n")
    result = run_gate(proj, todo_id)
    assert result["status"] == "checked"
    assert result["block_sha"]
    assert result["ok"] is True, result["missing"]


# ─── (S3) dispatch refuses to freeze an unreadable block ────────────────


def test_dispatch_never_writes_an_unreadable_block(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    wave = proj / ".agentic" / "checklists" / "waves" / "multi.yaml"
    wave.parent.mkdir(parents=True, exist_ok=True)
    wave.write_text(
        "name: multi\n"
        "items:\n"
        "  - id: multi-text\n"
        "    text: |\n"
        "      first line\n"
        "      second line\n"
        "    probe:\n"
        "      type: file_exists\n"
        "      path: README.md\n",
        encoding="utf-8",
    )
    todo_id, path = _dispatch(proj, "---\nwave: multi\n---\n\n# TODO — multi\n")
    parsed = parse_block(path.read_text(encoding="utf-8"))
    assert parsed is not None
    item = next(i for i in parsed if i.id == "multi-text")
    assert item.text == "first line\nsecond line"


def test_apply_never_writes_an_unreadable_block(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """``apply`` is the second writer of the frozen block — same guard.

    The dispatch path is covered above; this proves the invariant lives in
    the shared composer, not in one caller. A renderer that drifts must
    refuse the apply before the TODO is touched (no write, no backup).
    """
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    text = path.read_text(encoding="utf-8")
    without_block = text.split("## Чек-лист завершения")[0]
    path.write_text(without_block, encoding="utf-8")

    from awf.checklists import _compose

    monkeypatch.setattr(
        _compose,
        "render_block",
        lambda items: "## Чек-лист завершения\n\n- [ ] broken: no probe\n",
    )
    rc, _out = _apply(proj, todo_id)
    assert rc == 2
    assert path.read_text(encoding="utf-8") == without_block
    assert not list((proj / ".agentic" / "context").glob(f"{todo_id}.md.bak-*"))


# ─── (S4) a full reword keeps the frozen block ──────────────────────────


def test_full_reword_does_not_silently_drop_the_frozen_block(
    tmp_git_repo: Path,
) -> None:
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(
        proj,
        "---\nchecklist_classes: [packaging]\n---\n" + _todo("## Приёмка\n- x\n"),
    )
    assert block_of(path.read_text(encoding="utf-8")) is not None

    api.update_todo(
        proj, todo_id, content="# TODO — reworded\n\n## Приёмка\n- x\n"
    )
    text = path.read_text(encoding="utf-8")
    assert block_of(text) is not None
    assert "packaging-pyproject" in text
    assert _pack(proj, todo_id).sections["checklist"] != "skipped"

    with pytest.raises(api.AwfApiError, match="frozen"):
        api.update_todo(
            proj,
            todo_id,
            section_updates={CHECKLIST_HEADING[3:]: "- [ ] x: y — manual\n"},
        )


def test_all_block_writers_end_the_file_with_one_newline(
    tmp_git_repo: Path,
) -> None:
    """Dispatch, the reword carry and ``apply`` place the block the same way.

    The carry used to join the block itself and dropped the trailing
    newline, so the same file ended differently depending on which writer
    touched it last.
    """
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    dispatched = path.read_text(encoding="utf-8")
    assert dispatched.endswith("\n") and not dispatched.endswith("\n\n")

    api.update_todo(
        proj, todo_id, content="# TODO — reworded\n\n## Приёмка\n- x\n"
    )
    reworded = path.read_text(encoding="utf-8")
    assert block_of(reworded) is not None
    assert reworded.endswith("\n") and not reworded.endswith("\n\n")

    rc, out = _apply(proj, todo_id)
    assert rc == 0, out
    applied = path.read_text(encoding="utf-8")
    assert applied.endswith("\n") and not applied.endswith("\n\n")


# ─── (Q) the parse/gate surface, round 4 ────────────────────────────────


@pytest.mark.parametrize("bullet", ["  - [ ] ", "* [ ] ", "+ [ ] ", "[ ] "])
def test_a_checkbox_line_is_never_silently_dropped(
    tmp_git_repo: Path, bullet: str
) -> None:
    """Any bullet style and indentation reads; nothing vanishes silently."""
    proj = _project(tmp_git_repo)
    _wave(
        proj,
        "q1",
        [{"id": "q1-miss", "text": "файла нет", "path": "nope.txt"}],
    )
    todo_id, path = _dispatch(
        proj, "---\nwave: q1\n---\n" + _todo("## Приёмка\n- x\n")
    )
    text = path.read_text(encoding="utf-8")
    assert "- [ ] q1-miss:" in text
    path.write_text(
        text.replace("- [ ] q1-miss:", f"{bullet}q1-miss:"), encoding="utf-8"
    )

    result = run_gate(proj, todo_id)
    assert "q1-miss" in result["missing"]


def test_an_unreadable_checkbox_line_is_refused() -> None:
    """A line that TRIES to be a checkbox and still does not parse is an error."""
    block = "## Чек-лист завершения\n\n- [x] done-already: сделано\n"
    with pytest.raises(ChecklistError, match="unreadable checklist line"):
        parse_block(block)


def test_an_empty_block_is_not_a_green_checklist(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    text = path.read_text(encoding="utf-8")
    block = block_of(text)
    assert block is not None
    path.write_text(
        text.replace(block, CHECKLIST_HEADING + "\n"), encoding="utf-8"
    )

    result = run_gate(proj, todo_id, use_cache=False)
    assert result["status"] == "checked"
    assert result["ok"] is False
    assert result["missing"] == ["(empty block)"]
    assert _pack(proj, todo_id).sections["checklist"] == "fail"


def test_a_reword_with_a_heading_only_block_keeps_the_real_items(
    tmp_git_repo: Path,
) -> None:
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(
        proj, "---\nchecklist_classes: [packaging]\n---\n" + _todo("## Приёмка\n- x\n")
    )
    assert "- [ ]" in (block_of(path.read_text(encoding="utf-8")) or "")

    api.update_todo(
        proj,
        todo_id,
        content=(
            "# TODO — reworded\n\n"
            f"{CHECKLIST_HEADING}\n\n"
            "## Приёмка\n- x\n"
        ),
    )
    text = path.read_text(encoding="utf-8")
    assert "packaging-pyproject" in (block_of(text) or "")


def test_a_reword_that_brings_its_own_block_is_validated(
    tmp_git_repo: Path,
) -> None:
    """F14/F15 on the reword surface: the new text's OWN block is checked.

    The carry branch validated only the block it re-attached. A reword that
    brings its own block is a writer too — a doubled or unreadable block
    would slip in exactly where dispatch refuses it.
    """
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    before = path.read_text(encoding="utf-8")
    two_blocks = (
        "# TODO — reworded\n\n"
        f"{CHECKLIST_HEADING}\n\n"
        "- [ ] own: one — manual\n\n"
        f"{CHECKLIST_HEADING}\n\n"
        "- [ ] own-2: two — manual\n"
    )
    with pytest.raises(api.AwfApiError, match="checklist"):
        api.update_todo(proj, todo_id, content=two_blocks)
    assert path.read_text(encoding="utf-8") == before

    unreadable = (
        "# TODO — reworded\n\n"
        f"{CHECKLIST_HEADING}\n\n"
        "- [x] own: already checked\n"
    )
    with pytest.raises(api.AwfApiError, match="checklist"):
        api.update_todo(proj, todo_id, content=unreadable)
    assert path.read_text(encoding="utf-8") == before

    # A readable block the new text brings stays authoritative and is written.
    api.update_todo(
        proj,
        todo_id,
        content=(
            "# TODO — reworded\n\n"
            f"{CHECKLIST_HEADING}\n\n"
            "- [ ] own: one — manual\n"
        ),
    )
    assert "- [ ] own: one — manual" in path.read_text(encoding="utf-8")


def test_a_data_probe_path_escape_is_refused_at_dispatch(
    tmp_git_repo: Path,
) -> None:
    proj = _project(tmp_git_repo)
    _wave(
        proj,
        "q4",
        [{"id": "q4-escape", "text": "вне проекта", "path": "../outside.txt"}],
    )
    with pytest.raises(api.AwfApiError, match="leaves the project"):
        api.dispatch_todo(proj, "---\nwave: q4\n---\n" + _todo("## Приёмка\n- x\n"))
    assert not list((proj / ".agentic" / "inbox").glob("TODO-*.md"))


def test_an_escaping_probe_does_not_abort_the_whole_gate(
    tmp_git_repo: Path,
) -> None:
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    text = path.read_text(encoding="utf-8")
    injected = (
        '- [ ] q4-escape: outside — probe(file_exists ["../outside.txt"]) '
        "expect present\n- [ ] unit-01: x — manual"
    )
    path.write_text(
        text.replace("- [ ] unit-01: x — manual", injected), encoding="utf-8"
    )

    result = run_gate(proj, todo_id, use_cache=False)
    assert result["ok"] is False
    assert "q4-escape" in result["missing"]


def test_grep_absent_on_a_missing_target_is_not_a_pass(tmp_path: Path) -> None:
    from awf.checklist_gate import run_probe

    proj = tmp_path / "proj"
    proj.mkdir()
    item = Item(
        id="q5-absent",
        text="шаблона нет",
        probe={"type": "grep_absent", "file": "gone.txt", "pattern": "FORBIDDEN"},
        expect=False,
        source="test",
    )
    ok, detail = run_probe(proj, item, todo_id="TODO-0001")
    assert ok is False
    assert "cannot read" in detail


def test_a_block_expect_token_must_be_present_or_absent() -> None:
    block = (
        "## Чек-лист завершения\n\n"
        '- [ ] q6: опечатка — probe(grep_present ["a.txt", "p"]) expect nonsense\n'
    )
    with pytest.raises(ChecklistError, match="present.*absent"):
        parse_block(block)


# ─── (A) architect review: the block is replaced by its EXACT text ──────


def test_apply_rewrites_a_heading_that_differs_only_in_trailing_spaces(
    tmp_git_repo: Path,
) -> None:
    """``block_of`` returns the heading verbatim, so ``apply`` replaces it.

    A rebuilt heading (``## <name>``) missed a hand-edited line with trailing
    spaces: the exact-substring replace became a silent no-op while the CLI
    still printed «applied».
    """
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    text = path.read_text(encoding="utf-8")
    assert CHECKLIST_HEADING + "\n" in text
    text = text.replace(CHECKLIST_HEADING + "\n", CHECKLIST_HEADING + "   \n", 1)
    path.write_text(
        "---\nchecklist_classes: [packaging]\n---\n" + text, encoding="utf-8"
    )

    rc, out = _apply(proj, todo_id)
    assert rc == 0 and "applied" in out
    new_text = path.read_text(encoding="utf-8")
    assert "packaging-pyproject" in new_text, "apply must replace the block"
    assert new_text.count(CHECKLIST_HEADING) == 1, "no duplicated heading"


def test_a_broken_block_fail_is_a_measured_section(tmp_git_repo: Path) -> None:
    """A failure the section OBSERVED counts as measured, as every other
    failing section does; only «no block / could not read the unit» stays
    unmeasured (skipped)."""
    from awf.verify_pack import _checklist_section

    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- критерий\n"))
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("- [ ] unit-01: критерий — manual", "- [ ] unit-01: x — broken"),
        encoding="utf-8",
    )
    section = _checklist_section(proj, todo_id)
    assert section.status == "fail"
    assert section.measured is True


# ─── (F11) a filesystem error on a probe target is a verdict ────────────


def test_a_symlink_loop_on_a_probe_target_does_not_kill_the_gate(
    tmp_git_repo: Path,
) -> None:
    """``Path.resolve()`` raises ``RuntimeError`` (not ``ChecklistError``) on
    a symlink loop; if it escapes, verify_pack dies with a traceback."""
    proj = _project(tmp_git_repo)
    loop = proj / "loop"
    loop.symlink_to(loop)
    _wave(
        proj,
        "f11-loop",
        [{"id": "f11-loop-1", "text": "петля", "path": "loop"}],
    )
    todo_id, _ = _dispatch(
        proj, "---\nwave: f11-loop\n---\n" + _todo("## Приёмка\n- x\n")
    )

    result = run_gate(proj, todo_id)  # must not raise RuntimeError
    assert result["status"] == "checked"
    assert "f11-loop-1" in result["missing"]


def test_a_permission_denied_parent_directory_is_a_verdict_not_a_gate_crash(
    tmp_git_repo: Path,
) -> None:
    """F20: a mode-000 *directory* in the path makes ``exists``/``is_dir``
    raise EACCES (Python swallows only ENOENT/ENOTDIR). The cache key and
    the probe must both answer ``missing``; the gate must still run the
    other items and verify_pack must not see a traceback.
    """
    import os

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root bypasses file modes — the unreadable-parent setup is void")

    proj = _project(tmp_git_repo)
    (proj / "ok.txt").write_text("ok\n", encoding="utf-8")
    secret = proj / "secret"
    secret.mkdir()
    (secret / "target.txt").write_text("hi\n", encoding="utf-8")
    wave = proj / ".agentic" / "checklists" / "waves" / "locked.yaml"
    wave.parent.mkdir(parents=True, exist_ok=True)
    wave.write_text(
        "name: locked\nitems:\n"
        "  - id: locked-file\n    text: file under a locked dir\n"
        "    probe:\n      type: file_exists\n      path: secret/target.txt\n"
        "  - id: locked-grep\n    text: grep under a locked dir\n"
        "    probe:\n      type: grep_present\n      file: secret/target.txt\n"
        "      pattern: hi\n"
        "  - id: fine\n    text: a readable target\n"
        "    probe:\n      type: file_exists\n      path: ok.txt\n",
        encoding="utf-8",
    )
    todo_id, _ = _dispatch(
        proj, "---\nwave: locked\n---\n" + _todo("## Приёмка\n- x\n")
    )
    os.chmod(secret, 0)
    try:
        result = run_gate(proj, todo_id)
    finally:
        os.chmod(secret, 0o755)
    assert result["status"] == "checked"
    states = {item["id"]: item["status"] for item in result["items"]}
    assert states["fine"] == "ok"
    assert states["locked-file"] == "missing"
    assert states["locked-grep"] == "missing"


def test_a_legal_git_probe_ignores_an_ambient_external_diff_hook(
    tmp_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F21: the allowlist refuses ``--ext-diff``, but ``GIT_EXTERNAL_DIFF``
    from the ambient environment reaches a plain ``git diff`` probe and
    launches an arbitrary program. The sanitized env must strip it.
    """
    proj = _project(tmp_git_repo)
    (proj / "README.md").write_text("seed\nchanged\n", encoding="utf-8")
    _wave(
        proj,
        "gitenv",
        [{"id": "git-read", "text": "read-only diff", "argv": ["git", "diff"]}],
    )
    todo_id, _ = _dispatch(
        proj, "---\nwave: gitenv\n---\n" + _todo("## Приёмка\n- x\n")
    )
    marker = proj / "PWNED.txt"
    hook = proj.parent / "external-diff.sh"
    hook.write_text(f"#!/bin/sh\necho pwned > {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(hook))

    result = run_gate(proj, todo_id)
    states = {item["id"]: item["status"] for item in result["items"]}
    assert states["git-read"] == "ok"
    assert not marker.exists()


def test_a_pytest_probe_does_not_import_an_ambient_plugin(
    tmp_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F22: the allowlist refuses ``-p <module>`` because importing a module
    runs its top-level code, but ``PYTHONPATH`` plus ``PYTEST_PLUGINS``
    reaches the same exec inside an allowlisted ``python3 -m pytest
    --collect-only`` probe. The sanitized env must strip that family too.
    """
    proj = _project(tmp_git_repo)
    (proj / "test_seed.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    _wave(
        proj,
        "pyenv",
        [
            {
                "id": "pytest-collect",
                "text": "collection only",
                "argv": ["python3", "-m", "pytest", "--collect-only", "-q"],
            }
        ],
    )
    todo_id, _ = _dispatch(
        proj, "---\nwave: pyenv\n---\n" + _todo("## Приёмка\n- x\n")
    )
    marker = tmp_git_repo.parent / f"{tmp_git_repo.name}-PWNED-pytest.txt"
    pkgdir = tmp_git_repo.parent / f"{tmp_git_repo.name}-ambient-plugins"
    pkgdir.mkdir()
    (pkgdir / "evilplugin.py").write_text(
        "import pathlib\n"
        f"pathlib.Path({str(marker)!r}).write_text('x')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(pkgdir))
    monkeypatch.setenv("PYTEST_PLUGINS", "evilplugin")

    result = run_gate(proj, todo_id)
    states = {item["id"]: item["status"] for item in result["items"]}
    assert states["pytest-collect"] == "ok"
    assert not marker.exists()


def test_a_filesystem_error_is_a_checklist_section_fail(
    tmp_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any future FS error out of the gate must be a section, not a pack
    traceback: the section is the only thing verify-pack must rely on."""
    from awf import checklist_gate
    from awf.verify_pack import _checklist_section

    proj = _project(tmp_git_repo)
    todo_id, _ = _dispatch(proj, _todo("## Приёмка\n- x\n"))

    def _boom(*args, **kwargs):
        raise PermissionError(13, "Permission denied", "secret.txt")

    monkeypatch.setattr(checklist_gate, "run", _boom)
    section = _checklist_section(proj, todo_id)
    assert section.status == "fail"
    assert section.measured is True
    assert "PermissionError" in section.lines[0]


# ─── (A) a second writer cannot make the frozen block ambiguous ─────────


def test_section_updates_refuses_a_case_variant_of_the_frozen_heading(
    tmp_git_repo: Path,
) -> None:
    """The block reader matches headings case-insensitively; the guard must.

    An exact-case compare would let ``"чек-лист завершения"`` through, and
    the section updater (which matches case-sensitively) would append a
    SECOND heading at the end instead of the refusal the freeze promises.
    """
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    before = path.read_text(encoding="utf-8")
    with pytest.raises(api.AwfApiError, match="frozen"):
        api.update_todo(
            proj,
            todo_id,
            section_updates={CHECKLIST_HEADING[3:].casefold(): "- [ ] evil: x\n"},
        )
    assert path.read_text(encoding="utf-8") == before
    assert before.count(CHECKLIST_HEADING) == 1


def test_append_cannot_add_a_second_frozen_block(tmp_git_repo: Path) -> None:
    """``append`` is the neighbouring writer of the freeze: a second block
    would be read by a human while the gate executes the first one."""
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    before = path.read_text(encoding="utf-8")
    with pytest.raises(api.AwfApiError, match="second"):
        api.update_todo(
            proj,
            todo_id,
            append=f"{CHECKLIST_HEADING}\n\n- [ ] evil: x — manual\n",
        )
    assert path.read_text(encoding="utf-8") == before


def test_section_body_cannot_swallow_the_frozen_block(tmp_git_repo: Path) -> None:
    """A section body is free text: an unterminated fence before the block
    would make ``block_of`` see no block and the gate answer ``none``."""
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    before = path.read_text(encoding="utf-8")
    with pytest.raises(api.AwfApiError, match="checklist"):
        api.update_todo(
            proj, todo_id, section_updates={"Приёмка": "```\nunterminated"}
        )
    assert path.read_text(encoding="utf-8") == before


def test_section_body_cannot_inject_a_second_block(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    before = path.read_text(encoding="utf-8")
    with pytest.raises(api.AwfApiError, match="checklist"):
        api.update_todo(
            proj,
            todo_id,
            section_updates={
                "Приёмка": "- x\n\n" + CHECKLIST_HEADING + "\n\n- [ ] fake: y — manual"
            },
        )
    assert path.read_text(encoding="utf-8") == before


def test_section_updates_cannot_add_a_block_to_a_legacy_unit(
    tmp_git_repo: Path,
) -> None:
    """A unit dispatched before the DoD feature has no block; a section body
    must not become a back door for one (the block is composed, not typed)."""
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    text = path.read_text(encoding="utf-8")
    block = block_of(text)
    assert block is not None
    path.write_text(text.replace(block, "", 1).rstrip("\n") + "\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(api.AwfApiError, match="cannot add"):
        api.update_todo(
            proj,
            todo_id,
            section_updates={
                "Приёмка": "- x\n\n" + CHECKLIST_HEADING + "\n\n- [ ] fake: y — manual"
            },
        )
    assert path.read_text(encoding="utf-8") == before


# ─── (F) the writers refuse a path-shaped id and an empty carried block ─


def test_checklist_cli_refuses_an_id_that_is_not_a_todo_id(
    tmp_git_repo: Path,
) -> None:
    """F12: ``checklist`` takes a TODO id, never a path.

    ``Path / token`` drops the inbox prefix for an absolute token, so an
    unchecked id makes ``show/check/hash/apply`` read or write outside the
    project — the one declared write surface is ``.agentic/inbox``.
    """
    proj = _project(tmp_git_repo)
    target = tmp_git_repo.parent / "outside-f12.md"
    target.write_text("# not a todo\n\nkeep me\n", encoding="utf-8")
    original = target.read_text(encoding="utf-8")
    bad_id = str(target)[: -len(".md")]

    for action in ("show", "check", "hash", "apply"):
        rc = cmd_checklist.run(
            SimpleNamespace(
                action=action, todo_id=bad_id, project_dir=str(proj), json=False
            )
        )
        assert rc == 2, f"`checklist {action}` accepted {bad_id!r} as a TODO id"
        assert target.read_text(encoding="utf-8") == original, (
            f"`checklist {action}` touched a file outside .agentic/inbox"
        )


def test_dispatch_refuses_a_carried_heading_only_block(
    tmp_git_repo: Path,
) -> None:
    """F13: a heading without items is not the frozen block.

    Composing over it would append a SECOND heading and leave the empty one
    authoritative, so the unit would be born failing (Q2/Q3 on the writer).
    """
    proj = _project(tmp_git_repo)
    content = _todo(
        f"{CHECKLIST_HEADING}\n\nпока без пунктов\n\n## Приёмка\n- x\n"
    )
    with pytest.raises(api.AwfApiError, match="checklist"):
        api.dispatch_todo(proj, content)
    inbox = proj / ".agentic" / "inbox"
    assert not list(inbox.glob("TODO-*.md")), "a refused dispatch wrote the TODO"
    assert not list(inbox.glob("TODO-*.ready")), (
        "a refused dispatch wrote the signal"
    )


# ─── (F14/F15/F16) the final text is what the gate reads ────────────────


def test_dispatch_never_freezes_a_block_the_final_todo_cannot_read(
    tmp_git_repo: Path,
) -> None:
    """F14: a block appended after an unterminated fence is invisible.

    The writer used to validate the block it COMPOSED, never the final text
    it wrote: content ending with an open ```` ``` ```` swallowed the
    appended heading, the gate answered ``status="none"`` and a unit with a
    DoD block was silently green with zero checks. Refusing is a valid
    answer; accepting is only valid when the block reads back.
    """
    proj = _project(tmp_git_repo)
    content = (
        "# TODO — pasted example\n\n"
        "```\n"
        "snippet pasted without its closing fence\n"
    )
    try:
        result = api.dispatch_todo(proj, content)
    except api.AwfApiError as exc:
        assert "checklist" in str(exc)
        return
    path = proj / ".agentic" / "inbox" / f"{result.todo_id}.md"
    text = path.read_text(encoding="utf-8")
    assert block_of(text) is not None, (
        "dispatch accepted content whose appended block sits inside an "
        "unterminated code fence — the gate sees no block"
    )
    assert run_gate(proj, result.todo_id, use_cache=False)["status"] == "checked"


def test_a_reword_never_freezes_a_block_the_final_todo_cannot_read(
    tmp_git_repo: Path,
) -> None:
    """F14: the second writer carries the same guard (hygiene reword)."""
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    before = path.read_text(encoding="utf-8")
    with pytest.raises(api.AwfApiError, match="checklist"):
        api.update_todo(
            proj,
            todo_id,
            content="# TODO — reworded\n\n```\nunterminated snippet\n",
        )
    assert path.read_text(encoding="utf-8") == before


def test_dispatch_refuses_a_second_frozen_block(tmp_git_repo: Path) -> None:
    """F15: two frozen blocks = the gate runs one, a human reads two."""
    proj = _project(tmp_git_repo)
    content = _todo(
        "## Приёмка\n- x\n\n"
        f"{CHECKLIST_HEADING}\n\n"
        "- [ ] evil: фейковая проверка — manual\n\n"
        f"{CHECKLIST_HEADING}\n\n"
        "- [ ] unit-01: x — manual\n"
    )
    with pytest.raises(api.AwfApiError, match="checklist"):
        api.dispatch_todo(proj, content)
    inbox = proj / ".agentic" / "inbox"
    assert not list(inbox.glob("TODO-*.md")), "a refused dispatch wrote the TODO"


def test_a_horizontal_rule_under_the_title_is_not_a_broken_contract() -> None:
    """F16: an ``---`` directly under the title is a horizontal rule."""
    contract, unknown = parse_todo_contract("# TODO — task\n\n---\n\n## Приёмка\n- x\n")
    assert contract is None
    assert unknown == []


def test_apply_never_appends_a_block_the_final_todo_cannot_read(
    tmp_git_repo: Path,
) -> None:
    """F14 on the third writer: ``checklist apply`` appends, same guard."""
    proj = _project(tmp_git_repo)
    todo_id, path = _dispatch(proj, _todo("## Приёмка\n- x\n"))
    text = path.read_text(encoding="utf-8")
    block = block_of(text)
    assert block is not None
    without_block = text.replace(block, "", 1).rstrip("\n")
    without_block += "\n\n```\nunterminated snippet\n"
    path.write_text(without_block, encoding="utf-8")

    rc, out = _apply(proj, todo_id)
    assert rc == 2, out
    assert path.read_text(encoding="utf-8") == without_block
    assert not list((proj / ".agentic" / "context").glob(f"{todo_id}.md.bak-*"))


def test_importing_the_checklist_package_does_not_pull_the_api_layer() -> None:
    """The data package must not depend on the application layer.

    TODO-0244: ``checklists`` is read by the gate; reaching the shared
    markdown reader through ``awf.api`` executed dispatch, hygiene and
    verify_pack just to read one heading. ``awf._markdown`` lives at the
    package root for that reason — this test keeps the seam.
    """
    code = (
        "import sys, awf.checklists; "
        "pulled = [m for m in ('awf.api', 'awf.verify_pack', 'awf.brief', "
        "'awf.prove_red') if m in sys.modules]; "
        "print(pulled); "
        "raise SystemExit(1 if pulled else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
