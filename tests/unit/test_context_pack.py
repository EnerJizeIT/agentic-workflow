"""T3–T8: the stage context pack (R2/R5/R9, IMP2).

One pack file, eight sections in spec order, the same bytes on a rebuild.
The review bodies travel verbatim; the capped sections shrink with a
pointer line; an absent input is an ``n/a`` line with its reason.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from awf import api, cli
from awf.context_pack import SECTION_CAPS, TOTAL_CAP
from awf.todos import review_headings

STAGE = "agent-dev"
ROLE = "developer"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout


def _todo_text(reviews: int = 0, refs: int = 0, filler: int = 0) -> str:
    lines = [
        "---",
        "verify:",
        "  - pytest tests/unit/test_context_pack.py -q",
        "---",
        "# TODO — pack fixture",
        "",
        "## Что сделать",
        "1. Собрать пакет.",
        "",
        "## Границы",
        "- только `awf/context_pack.py`",
    ]
    for i in range(refs):
        lines.append(f"- ссылка `src/mod_{i}.py`")
    lines += ["", "## Приёмка", "- зелёный тест"]
    lines += [f"- строка приёмки {i}" for i in range(filler)]
    lines += ["", "## Чек-лист завершения", "- [ ] unit-01: тест", ""]
    for n in range(1, reviews + 1):
        lines += [
            f"## REVIEW-фикс (супервизор, виток {n})",
            f"- находка {n}: пакет теряет тело",
            f"- следствие {n}: правка в секции {n}",
            "",
        ]
    return "\n".join(lines) + "\n"


def _w_pipeline(proj: Path) -> None:
    (proj / ".agentic" / "pipelines").mkdir(parents=True)
    (proj / ".agentic" / "context" / "PIPELINE-TODO-0001.yaml").write_text(
        "name: default\nstages:\n"
        "- name: plan\n  role: supervisor\n  description: plan the unit\n"
        f"- name: {STAGE}\n  role: {ROLE}\n  task: build the pack\n"
        "- name: verify\n  role: supervisor\n",
        encoding="utf-8",
    )
    (proj / ".agentic" / "roles" / f"{ROLE}.md").write_text(
        "# Developer role\n", encoding="utf-8",
    )
    (proj / ".agentic" / "doctrine").mkdir(parents=True)
    (proj / ".agentic" / "doctrine" / "01-a.md").write_text(
        "# Правило\n\nСигнал в свою группу.\n", encoding="utf-8",
    )


def _project(tmp_path: Path, todo: str | None = None) -> Path:
    proj = tmp_path / "proj"
    for sub in ("inbox", "outbox", "context", "handoff", "logs", "roles"):
        (proj / ".agentic" / sub).mkdir(parents=True)
    todo = todo if todo is not None else _todo_text()
    (proj / ".agentic" / "inbox" / "TODO-0001.md").write_text(todo, encoding="utf-8")
    _w_pipeline(proj)
    (proj / ".agentic" / "context" / "BASELINE-TODO-0001.sha").write_text(
        _commit_all(proj) + "\n", encoding="utf-8",
    )
    (proj / ".agentic" / "context" / "BASELINE-TODO-0001.untracked").write_text(
        "", encoding="utf-8",
    )
    return proj


def _commit_all(proj: Path) -> str:
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "base")
    return _git(proj, "rev-parse", "HEAD").strip()


def _section(text: str, number: str) -> list[str]:
    m = re.search(rf"^## {number}\. .*$", text, re.MULTILINE)
    assert m, f"section {number} missing"
    rest = text[m.end():]
    nxt = re.search(r"^## \d+\. ", rest, re.MULTILINE)
    body = rest[: nxt.start()] if nxt else rest
    return body.strip("\n").split("\n")


def _capped_sum(text: str) -> int:
    total = 0
    for n in ("1", "3", "4", "6", "7", "9"):
        if re.search(rf"^## {n}\. ", text, re.MULTILINE):
            total += len(_section(text, n))
    return total


def test_sections_path_determinism(tmp_path):
    proj = _project(tmp_path)
    first = api.render_context_pack(proj, "TODO-0001", STAGE)
    second = api.render_context_pack(proj, "TODO-0001", STAGE)

    assert first == second
    assert first.endswith("\n") and not first.endswith("\n\n")
    assert "\r" not in first
    for n in range(1, 9):
        assert re.search(rf"^## {n}\. ", first, re.MULTILINE), n
    assert not re.search(r"^## 9\. ", first, re.MULTILINE)
    assert first.index("## 1. ") < first.index("## 8. ")

    assert "todo: TODO-0001" in first
    assert STAGE in _section(first, "1")[1]
    assert "- роль: developer" in _section(first, "1")
    assert "- задание: build the pack" in _section(first, "1")
    assert "baseline:" in _section(first, "3")[0]
    assert any("diff --stat" in line for line in _section(first, "3"))
    assert "проверяй утверждения пробами" in first
    shas = api.parse_input_shas(first)
    assert set(shas) == {"todo", "baseline", "role", "doctrine", "handoff"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", v) for k, v in shas.items() if k != "handoff")

    built = api.build_context_pack(proj, "TODO-0001", STAGE)
    assert built.path == proj / ".agentic" / "context" / f"STAGE-TODO-0001-{STAGE}.md"
    assert built.path.read_text(encoding="utf-8") == first


def test_review_bodies_verbatim_and_contract_uncut(tmp_path):
    todo = _todo_text(reviews=6, filler=120)
    proj = _project(tmp_path, todo)
    text = api.render_context_pack(proj, "TODO-0001", STAGE)

    assert len(review_headings(todo)) == 6
    five = "\n".join(_section(text, "5"))
    for n in range(1, 7):
        assert f"## REVIEW-фикс (супервизор, виток {n})" in five
        assert f"- находка {n}: пакет теряет тело" in five
        assert f"- следствие {n}: правка в секции {n}" in five
    assert five.index("виток 1") < five.index("виток 6")
    # The contract (section 2) is not cut either — all 120 filler lines stay.
    assert "строка приёмки 119" in "\n".join(_section(text, "2"))
    assert len(text.split("\n")) > TOTAL_CAP
    assert _capped_sum(text) <= TOTAL_CAP


def test_caps_shrink_with_pointer(tmp_path):
    proj = _project(tmp_path)
    handoff = proj / ".agentic" / "handoff" / "plan-TODO-0001.md"
    handoff.write_text("\n".join(f"handoff line {i}" for i in range(200)) + "\n",
                       encoding="utf-8")
    (proj / ".agentic" / "inbox" / "TODO-0001.md").write_text(
        _todo_text(refs=30), encoding="utf-8",
    )
    for i in range(30):
        (proj / f"changed_{i}.py").write_text(f"x = {i}\n", encoding="utf-8")

    text = api.render_context_pack(proj, "TODO-0001", STAGE)
    four = _section(text, "4")
    assert len(four) <= SECTION_CAPS["4"]
    assert any("сокращено" in line for line in four)
    six = _section(text, "6")
    assert len(six) <= SECTION_CAPS["6"]
    assert _capped_sum(text) <= TOTAL_CAP
    assert len(_section(text, "3")) <= SECTION_CAPS["3"]


def test_missing_inputs_are_explicit_and_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "top-secret-value")
    monkeypatch.setenv("OPENCODE_TOKEN", "another-secret-value")
    proj = _project(tmp_path, "# TODO — пустой\n\nНет блоков.\n")
    (proj / ".agentic" / "context" / "BASELINE-TODO-0001.sha").unlink()
    (proj / ".agentic" / "roles" / f"{ROLE}.md").write_text(
        "# Developer role\nROLE-BODY-MARKER\n", encoding="utf-8")

    text = api.render_context_pack(proj, "TODO-0001", STAGE)
    assert re.search(r"^## 4\. ", text, re.MULTILINE)
    for number in ("2", "4", "5", "6"):
        assert "n/a" in "\n".join(_section(text, number))
    assert "нет данных" in text
    assert "top-secret-value" not in text
    assert "another-secret-value" not in text
    assert "AWS_SECRET" not in text
    # The role body is an input by sha only — the pack never repeats it.
    assert "ROLE-BODY-MARKER" not in text


def test_cli_file_stdout_and_refusal(tmp_path, capsys):
    proj = _project(tmp_path)
    target = proj / ".agentic" / "context" / f"STAGE-TODO-0001-{STAGE}.md"

    assert cli.main(["context-pack", "TODO-0001", "--stage", STAGE,
                     "--project-dir", str(proj)]) == 0
    first = target.read_bytes()
    assert cli.main(["context-pack", "TODO-0001", "--stage", STAGE,
                     "--project-dir", str(proj)]) == 0
    assert target.read_bytes() == first

    target.unlink()
    capsys.readouterr()
    assert cli.main(["context-pack", "TODO-0001", "--stage", STAGE, "--stdout",
                     "--project-dir", str(proj)]) == 0
    printed = capsys.readouterr().out
    assert printed.encode("utf-8") == first
    assert not target.exists()

    assert cli.main(["context-pack", "TODO-9999", "--stage", STAGE,
                     "--project-dir", str(proj)]) == 2
    assert "TODO-9999" in capsys.readouterr().err
    assert cli.main(["context-pack", "TODO-0001", "--stage", "nope",
                     "--project-dir", str(proj)]) == 2
    assert "nope" in capsys.readouterr().err
    assert not target.exists()

    # A stage is required: no file named ``STAGE-<todo>-None.md`` appears.
    assert cli.main(["context-pack", "TODO-0001",
                     "--project-dir", str(proj)]) == 2
    assert "stage" in capsys.readouterr().err
    assert not list((proj / ".agentic" / "context").glob("STAGE-TODO-0001-*"))


def test_contract_heading_with_suffix_keeps_its_body(tmp_path):
    todo = _todo_text().replace("## Приёмка", "## Приёмка (AC)")
    proj = _project(tmp_path, todo)

    two = "\n".join(_section(api.render_context_pack(proj, "TODO-0001", STAGE), "2"))
    assert "## Приёмка (AC)" in two
    assert "- зелёный тест" in two


def test_cli_out_leaves_the_k2_path_untouched(tmp_path):
    proj = _project(tmp_path)
    target = proj / ".agentic" / "context" / f"STAGE-TODO-0001-{STAGE}.md"
    other = tmp_path / "elsewhere.md"

    assert cli.main(["context-pack", "TODO-0001", "--stage", STAGE,
                     "--out", str(other), "--project-dir", str(proj)]) == 0
    assert other.is_file()
    assert not target.exists()


def test_delta_only_on_continuation(tmp_path):
    proj = _project(tmp_path, _todo_text(reviews=3))
    (proj / "changed.py").write_text("y = 0\n", encoding="utf-8")
    sha = _commit_all(proj)
    (proj / ".agentic" / "context" / "BASELINE-TODO-0001.sha").write_text(
        sha + "\n", encoding="utf-8")
    session = proj / ".agentic" / "context" / f"SESSION-TODO-0001-{STAGE}.json"
    session.write_text("{}", encoding="utf-8")
    (proj / "changed.py").write_text("y = 1\n", encoding="utf-8")

    text = api.render_context_pack(proj, "TODO-0001", STAGE)
    nine = "\n".join(_section(text, "9"))
    assert "- виток: 3" in nine
    assert "changed.py" in nine
    assert "- новая секция: ## REVIEW-фикс (супервизор, виток 3); тело — в секции 5" in nine
    assert "прошлая сессия не является источником фактов" in nine
    # The body lives in section 5 only — section 9 points, never repeats it.
    assert "прошлая сессия не является источником фактов" not in "\n".join(
        _section(text, "5"))

    _commit_all(proj)

    quiet = _project(tmp_path / "quiet")
    (quiet / ".agentic" / "context" / f"SESSION-TODO-0001-{STAGE}.json").write_text(
        "{}", encoding="utf-8")
    text = api.render_context_pack(quiet, "TODO-0001", STAGE)
    assert "- нет изменений" in "\n".join(_section(text, "9"))

    (quiet / ".agentic" / "context" / f"SESSION-TODO-0001-{STAGE}.json").unlink()
    text = api.render_context_pack(quiet, "TODO-0001", STAGE)
    assert not re.search(r"^## 9\. ", text, re.MULTILINE)


def test_fenced_example_heading_is_not_the_contract(tmp_path):
    """AC5/AC9: a ``## Что сделать`` quoted in a fence is an example, not
    the contract — the real block must still reach section 2."""
    todo = (
        "# TODO — fence fixture\n\n"
        "```markdown\n## Что сделать\nэто пример, не контракт\n```\n\n"
        "## Что сделать\n1. Настоящий шаг.\n\n"
        "## Границы\n- `awf/context_pack.py`\n\n"
        "## Приёмка\n- зелёный тест\n\n"
        "## Чек-лист завершения\n- [ ] unit-01: тест\n"
    )
    proj = _project(tmp_path, todo)
    two = "\n".join(_section(api.render_context_pack(proj, "TODO-0001", STAGE), "2"))
    assert "1. Настоящий шаг." in two
    assert "это пример, не контракт" in two
    assert two.index("это пример, не контракт") < two.index("1. Настоящий шаг.")


def test_delta_round_follows_section_five_headings(tmp_path):
    """AC33/AC43: the delta names the round of the REAL review sections —
    a fenced ``## REVIEW-фикс`` example must not raise it, and the body
    stays in section 5 only."""
    todo = _todo_text() + (
        "\n```markdown\n## REVIEW-фикс (пример, виток 99)\n"
        "это пример формата\n```\n\n"
        "## REVIEW-фикс (супервизор, виток 3)\n- находка: настоящая\n"
    )
    proj = _project(tmp_path, todo)
    (proj / ".agentic" / "context" / f"SESSION-TODO-0001-{STAGE}.json").write_text(
        "{}", encoding="utf-8")

    text = api.render_context_pack(proj, "TODO-0001", STAGE)
    five = "\n".join(_section(text, "5"))
    nine = "\n".join(_section(text, "9"))
    assert "секций `## REVIEW-фикс`: 1" in five
    assert "- виток: 3" in nine
    assert "виток: 99" not in nine
    assert "- находка: настоящая" in five
    assert "- находка: настоящая" not in nine


def test_previous_handoff_found_under_legacy_role_name(tmp_path):
    """K2/AC5: the previous stage's handoff is the file the spawner would
    attach — the stage name first, then the legacy role name."""
    proj = _project(tmp_path)
    (proj / ".agentic" / "context" / "PIPELINE-TODO-0001.yaml").write_text(
        "name: default\nstages:\n"
        "- name: plan-dev\n  role: planner\n"
        f"- name: {STAGE}\n  role: {ROLE}\n",
        encoding="utf-8",
    )
    (proj / ".agentic" / "roles" / "planner.md").write_text("# Planner\n",
                                                            encoding="utf-8")
    (proj / ".agentic" / "handoff" / "planner-TODO-0001.md").write_text(
        "# Handoff\n\nlegacy role-named\n", encoding="utf-8")

    text = api.render_context_pack(proj, "TODO-0001", STAGE)
    four = "\n".join(_section(text, "4"))
    assert "planner-TODO-0001.md" in four
    assert "legacy role-named" in four
    assert re.fullmatch(r"[0-9a-f]{64}", api.parse_input_shas(text)["handoff"])
