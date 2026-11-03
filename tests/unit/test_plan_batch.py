"""A25 (TODO-0353): ``awf plan-batch`` — inbox drafts + ordered plan.

A batch is drafts only: no ``.ready``, no baseline. Every refusal (a
scope-less item, a dependency cycle) happens before the first file lands.
"""
from __future__ import annotations

import json

import pytest

from awf import api, cli

BACKLOG = """# Backlog

### A37 — First item

Do the first thing.

after: A36
refs: docs/spec.md#a37

### A38 — Second item

Do the second thing.

after: A37

### A39 — Third item

Do the third thing.

### A42 — No scope here
"""


@pytest.fixture
def project(tmp_git_repo):
    """Minimal initialized project: .agentic/config.yaml + docs/backlog.md."""
    return _make_project(tmp_git_repo)


def _make_project(repo):
    repo.mkdir(parents=True, exist_ok=True)
    agentic = repo / ".agentic"
    agentic.mkdir()
    (agentic / "config.yaml").write_text("project:\n  name: test\n", encoding="utf-8")
    docs = repo / "docs"
    docs.mkdir()
    (docs / "backlog.md").write_text(BACKLOG, encoding="utf-8")
    return repo


def _draft_files(project):
    return sorted((project / ".agentic" / "inbox").glob("TODO-*.md"))


def _sections(text: str) -> dict[str, str]:
    """``## Heading`` → body, for the draft's three required sections."""
    out: dict[str, str] = {}
    current = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                out[current] = "\n".join(body).strip()
            current = line[3:].strip()
            body = []
        elif current is not None:
            body.append(line)
    if current is not None:
        out[current] = "\n".join(body).strip()
    return out


def test_three_items_become_three_drafts_without_ready(project):
    result = api.plan_batch(project, ["A37", "A38", "A39"], goal="batch goal")

    assert len(result.drafts) == 3
    assert [d["id"] for d in result.plan] == ["A37", "A38", "A39"]
    files = _draft_files(project)
    assert len(files) == 3
    assert not list((project / ".agentic" / "inbox").glob("*.ready"))

    for draft in result.drafts:
        path = project / ".agentic" / "inbox" / f"{draft['todo_id']}.md"
        assert path.is_file()
        assert draft["path"] == str(path)
        assert draft["title"].startswith(draft["id"])
        sections = _sections(path.read_text(encoding="utf-8"))
        for name in ("Что сделать", "Приёмка", "Границы"):
            assert sections.get(name, "").strip(), f"{draft['id']}: empty {name}"
        assert "batch goal" in path.read_text(encoding="utf-8")


def test_scope_less_item_refuses_by_name_and_writes_nothing(project):
    with pytest.raises(api.AwfApiError, match="A42"):
        api.plan_batch(project, ["A37", "A42"])

    assert _draft_files(project) == []
    assert not list((project / ".agentic" / "inbox").glob("*.ready"))


def test_item_absent_from_backlog_refuses_by_name(project):
    with pytest.raises(api.AwfApiError, match="A99"):
        api.plan_batch(project, ["A37", "A99"])
    assert _draft_files(project) == []


def test_plan_follows_after_dependencies(project):
    # Input order is deliberately wrong: A38 needs A37 first.
    result = api.plan_batch(project, ["A38", "A37", "A39"])

    ids = [item["id"] for item in result.plan]
    assert ids == ["A37", "A38", "A39"]
    assert result.plan[0]["after"] == ["A36"]
    assert result.plan[1]["after"] == ["A37"]


def test_dependency_cycle_refuses_and_writes_nothing(project):
    items = [
        {"id": "A40", "scope": "one", "after": ["A41"]},
        {"id": "A41", "scope": "two", "after": ["A40"]},
    ]
    with pytest.raises(api.AwfApiError, match="цикл"):
        api.plan_batch(project, items)
    assert _draft_files(project) == []


def test_config_names_the_backlog_file(project):
    other = project / "docs" / "other.md"
    other.write_text("### A50 — From config\n\nScope from config.\n", encoding="utf-8")
    config = project / ".agentic" / "config.yaml"
    config.write_text(
        "project:\n  name: test\nplan:\n  batch_backlog_path: docs/other.md\n",
        encoding="utf-8",
    )

    result = api.plan_batch(project, ["A50"])
    assert result.backlog_path.endswith("docs/other.md")
    assert "Scope from config." in (
        project / ".agentic" / "inbox" / f"{result.drafts[0]['todo_id']}.md"
    ).read_text(encoding="utf-8")


def test_cli_output_is_the_tool_plan(project, tmp_path, capsys):
    tool_plan = api.plan_batch(project, ["A37", "A38", "A39"]).plan

    cli_project = _make_project(tmp_path / "cli-project")
    rc = cli.main(
        ["plan-batch", "A37", "A38", "A39", "--project-dir", str(cli_project)]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == tool_plan


# ── QA (TODO-0353): promises that had no test ───────────────────────────


def test_drafts_are_not_active_units_and_not_orphans(project):
    """The invariant «черновик не запускается»: no reader sees a draft."""
    from awf import paths, todos

    api.plan_batch(project, ["A37", "A38", "A39"])

    inbox, outbox = paths.inbox(project), paths.outbox(project)
    assert todos.list_active_todos(inbox, outbox) == []
    assert todos.newest_active(project) == ""
    assert api.list_orphans(project) == []
    # Nothing was started: no baseline, no closure files.
    assert not list((project / ".agentic" / "context").glob("BASELINE-*"))
    assert not list(outbox.glob("*.ready"))


def test_bad_input_refuses_before_writing(project):
    """An empty batch and a duplicate id are refused with nothing on disk."""
    with pytest.raises(api.AwfApiError, match="non-empty"):
        api.plan_batch(project, [])
    with pytest.raises(api.AwfApiError, match="дважды"):
        api.plan_batch(project, ["A37", "A37"])

    assert _draft_files(project) == []


def test_second_batch_does_not_reuse_draft_numbers(project):
    """A draft reserves its number: a later batch continues after it."""
    first = api.plan_batch(project, ["A37"])
    second = api.plan_batch(project, ["A38"])

    assert first.drafts[0]["todo_id"] != second.drafts[0]["todo_id"]
    assert len(_draft_files(project)) == 2


def test_mid_batch_collision_rolls_back_reservations(project, monkeypatch):
    """A taken number mid-batch refuses the WHOLE batch, keeps the foreign file."""
    inbox = project / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    foreign = inbox / "TODO-0002.md"
    foreign.write_text("someone else's unit", encoding="utf-8")
    import importlib

    module = importlib.import_module("awf.api.plan_batch")
    monkeypatch.setattr(module, "_next_todo_id", lambda _project: "TODO-0001")

    with pytest.raises(api.AwfApiError, match="уже существует"):
        api.plan_batch(project, ["A37", "A38", "A39"])

    assert [p.name for p in _draft_files(project)] == ["TODO-0002.md"]
    assert foreign.read_text(encoding="utf-8") == "someone else's unit"


def test_write_failure_removes_every_draft(project, monkeypatch):
    """All-or-nothing on a write error: the inbox is left as it was found."""
    import importlib

    module = importlib.import_module("awf.api.plan_batch")
    real_write = module.managed_write_api_text
    calls = {"n": 0}

    def failing_write(path, content, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        real_write(path, content, **kwargs)

    monkeypatch.setattr(module, "managed_write_api_text", failing_write)

    with pytest.raises(OSError, match="disk full"):
        api.plan_batch(project, ["A37", "A38", "A39"])

    assert _draft_files(project) == []


def test_call_overrides_and_backlog_use_one_separator_rule(project):
    """`accept`/`bounds` split on ``;`` in the call exactly as in the backlog."""
    extra = project / "docs" / "extra.md"
    extra.write_text(
        "### A60 — From file\n\nScope.\n\n"
        "accept: file check one; file check two\n"
        "bounds: file bound\n",
        encoding="utf-8",
    )

    call = api.plan_batch(
        project,
        [
            {
                "id": "A37",
                "accept": "call check one; call check two",
                "bounds": "call bound one; call bound two",
            }
        ],
    )
    from_file = api.plan_batch(project, ["A60"], backlog_path="docs/extra.md")

    def section(draft, name):
        path = project / ".agentic" / "inbox" / f"{draft['todo_id']}.md"
        return _sections(path.read_text(encoding="utf-8"))[name]

    assert [ln for ln in section(call.drafts[0], "Приёмка").splitlines() if ln] == [
        "- call check one",
        "- call check two",
    ]
    assert [ln for ln in section(call.drafts[0], "Границы").splitlines() if ln] == [
        "- call bound one",
        "- call bound two",
    ]
    assert [ln for ln in section(from_file.drafts[0], "Приёмка").splitlines() if ln] == [
        "- file check one",
        "- file check two",
    ]
    assert [ln for ln in section(from_file.drafts[0], "Границы").splitlines() if ln] == [
        "- file bound",
    ]


def test_refined_draft_dispatches_with_its_own_id(project):
    """The documented next step works: dispatch the draft text with force=True."""
    draft = api.plan_batch(project, ["A37"]).drafts[0]
    path = project / ".agentic" / "inbox" / f"{draft['todo_id']}.md"
    content = path.read_text(encoding="utf-8")

    with pytest.raises(api.AwfApiError, match="already|уже|exists"):
        api.dispatch_todo(project, content=content, todo_id=draft["todo_id"])

    result = api.dispatch_todo(
        project, content=content, todo_id=draft["todo_id"], force=True
    )
    assert result.todo_id == draft["todo_id"]
    assert (project / ".agentic" / "inbox" / f"{draft['todo_id']}.ready").is_file()


# ── architect-review (TODO-0353): structure and rollback surfaces ────────


def test_fenced_lines_are_scope_not_structure(project):
    """A ``###``/``after:`` line inside a fence is text — one heading walker."""
    fenced = project / "docs" / "fenced.md"
    fenced.write_text(
        "### A70 — Fenced\n\n"
        "Scope before the fence.\n\n"
        "```\n"
        "### A71 — Not an entry\n"
        "after: A71\n"
        "## not a heading either\n"
        "```\n\n"
        "Scope after the fence.\n",
        encoding="utf-8",
    )

    result = api.plan_batch(project, ["A70"], backlog_path="docs/fenced.md")

    assert [item["id"] for item in result.plan] == ["A70"]
    assert result.plan[0]["after"] == []
    text = (
        project / ".agentic" / "inbox" / f"{result.drafts[0]['todo_id']}.md"
    ).read_text(encoding="utf-8")
    assert "### A71 — Not an entry" in text
    assert "Scope after the fence." in text


def test_missing_backlog_file_is_named_as_such(project):
    with pytest.raises(api.AwfApiError, match="не найден"):
        api.plan_batch(project, ["A37"], backlog_path="docs/nope.md")
    assert _draft_files(project) == []


def test_item_absent_from_backlog_says_absent_not_scope_less(project):
    with pytest.raises(api.AwfApiError, match="нет в docs/backlog.md"):
        api.plan_batch(project, ["A99"])
    assert _draft_files(project) == []


def test_rollback_never_unlinks_through_a_swapped_inbox(project, monkeypatch):
    """A symlinked ``inbox`` mid-batch: the rollback skips, never follows."""
    import importlib
    import shutil

    module = importlib.import_module("awf.api.plan_batch")
    outside = project.parent / "outside-inbox"
    outside.mkdir()
    victim = outside / "TODO-0001.md"
    victim.write_text("not ours", encoding="utf-8")
    inbox = project / ".agentic" / "inbox"

    def swapping_write(path, content, **kwargs):
        if not inbox.is_symlink():
            shutil.rmtree(inbox)
            inbox.symlink_to(outside, target_is_directory=True)
        raise OSError("disk full")

    monkeypatch.setattr(module, "managed_write_api_text", swapping_write)
    with pytest.raises(OSError, match="disk full"):
        api.plan_batch(project, ["A37"])

    assert victim.read_text(encoding="utf-8") == "not ours"


# ── QA (виток 2, TODO-0353): CLI-путь отказа и сквозные опции ────────────


def test_cli_refusal_exits_1_and_names_the_item(tmp_path, capsys):
    """The CLI refusal is a clean rc=1 with the item's name, and no files."""
    cli_project = _make_project(tmp_path / "cli-refuse")

    rc = cli.main(["plan-batch", "A42", "--project-dir", str(cli_project)])

    assert rc == 1
    assert "A42" in capsys.readouterr().err
    assert _draft_files(cli_project) == []


def test_cli_goal_and_backlog_reach_the_draft(tmp_path, capsys):
    """``--goal`` and ``--backlog`` are not print-only options in the CLI."""
    cli_project = _make_project(tmp_path / "cli-goal")
    (cli_project / "docs" / "extra.md").write_text(
        "### A60 — From extra\n\nScope from extra.\n", encoding="utf-8"
    )

    rc = cli.main(
        [
            "plan-batch",
            "A60",
            "--backlog",
            "docs/extra.md",
            "--goal",
            "cli goal",
            "--project-dir",
            str(cli_project),
        ]
    )
    assert rc == 0
    capsys.readouterr()

    [draft] = _draft_files(cli_project)
    text = draft.read_text(encoding="utf-8")
    assert "cli goal" in text
    assert "Scope from extra." in text
