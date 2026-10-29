"""CARRY IMP5 (TODO-0285): R3/R4/R8 — документы и паритет ``pipeline-write``.

Две группы проверок:

- T13 — статический текст: README, ``_lock.py``, ``design.md``,
  ``practices.md``, оба USAGE, докстринги ролей/шаблонов, релизные заметки
  не обещают того, чего код не делает.
- T11 — паритет CLI и формы: ``--variant`` пишет ровно текст превью,
  роли не материализует, код возврата не зависит от неразрешённых ролей.

Ни одна проверка не трогает ``dashboard.py``/``phase.py``/``doctrine.py``:
по AC11 эти модули остаются как есть.

AUD-0285-1: тест сверяет список живых вызывающих ``locked()`` из
``_lock.py`` с ``design.md`` и ``design/agentic-writers.md`` — третий
вызывающий (``pipeline_state.py``) не должен выпасть из документов молча.
AR-0285-6: обратное направление — обход дерева исходников против докстринга.
Список в ``_lock.py`` обязан покрывать ВСЕХ прямых импортёров ``locked``;
новый четвёртый вызывающий краснит guard, а не проскальзывает в живую
win32-ветку молча.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

from awf import api, cli
from awf.pipeline import load_stages

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_LIVE_CALLERS_RE = re.compile(
    r"via\s+``locked\(\)``\s*\((?P<body>[^)]*)\)", re.S
)
_DIRECT_LOCK_IMPORT_RE = re.compile(
    r"from\s+\.+_lock\s+import\s+([A-Za-z0-9_ ,]+)"
)


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _paragraph(text: str, needle: str) -> str:
    """Абзац, в котором встречается *needle* (до пустой строки)."""
    start = text.find(needle)
    assert start >= 0, f"{needle!r} not found"
    end = text.find("\n\n", start)
    return text[start:] if end < 0 else text[start:end]


def _direct_locked_importers() -> set[str]:
    """Files under ``awf/`` importing ``locked`` from ``_lock`` (paths
    relative to ``awf/``, as the docstring spells them)."""
    awf_root = REPO_ROOT / "awf"
    found: set[str] = set()
    for path in awf_root.rglob("*.py"):
        if path == awf_root / "_lock.py":
            continue  # the module that defines ``locked``
        for names in _DIRECT_LOCK_IMPORT_RE.findall(path.read_text(encoding="utf-8")):
            if "locked" in {n.strip() for n in names.split(",")}:
                found.add(path.relative_to(awf_root).as_posix())
    return found


def _warning_lines(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.startswith("WARNING: role ")]


# ─── T13: платформа и документация совпадают с кодом ────────────────────


class TestPlatformDocs:
    """R3/AC6–AC7: обещания о платформе совпадают с кодом."""

    def test_readme_does_not_promise_windows(self):
        text = _read("README.md")
        assert "Windows (untested)" not in text
        os_row = next(
            line for line in text.splitlines() if line.startswith("| **OS**")
        )
        assert "unsupported" in os_row.lower()
        # The destructive names must be real commands: `awf force-init`
        # does not exist, the CLI spells it `awf init --force`.
        assert "init --force" in os_row
        assert "`force-init`" not in os_row

    def test_usage_and_readme_agree_on_windows(self):
        """AUD-0285-2: USAGE must not call Windows "untested" when it refuses."""
        os_row = next(
            line
            for line in _read("README.md").splitlines()
            if line.startswith("| **OS**")
        )
        usage = _paragraph(_read("USAGE.md"), "**Linux-first:**")
        for text in (os_row, usage):
            assert "unsupported" in text.lower()
            assert "init --force" in text
        assert "untested" not in usage.lower()
        # The RU usage has no platform-limitations paragraph; if one is
        # added it must not smuggle the old promise back in.
        assert "untested" not in _read("USAGE.ru.md").lower()

    def test_lock_docstring_separates_live_and_dead_msvcrt(self):
        text = _read("awf/_lock.py")
        assert "msvcrt" in text
        assert "locked()" in text
        assert "dead" in text or "reserve" in text
        assert "kept for Windows support" not in text
        # The live-caller list is the fact the docstring claims: the direct
        # locked() user in pipeline_state must not be silently dropped.
        assert "pipeline_state" in text

    def test_design_names_the_inventory_and_refuses_non_posix(self):
        text = _read("docs/design.md")
        assert "design/agentic-writers.md" in text
        assert "_transact.py" not in text
        platform = _paragraph(text, "**Платформа:**")
        assert "не-POSIX" in platform
        assert "ОТКАЗ" in platform

    def test_design_states_what_survives_on_windows(self):
        platform = _paragraph(_read("docs/design.md"), "**Платформа:**")
        # AUD-0285-1: the managed writer refuses the publish, not the whole
        # read-modify-write: the lock, the read and the merge happen first.
        assert "lock/read/merge успевают" in platform
        assert "read→merge→write на Windows не выполняется" not in platform

    def test_live_locked_callers_match_the_design_docs(self):
        """AUD-0285-1: every name in the `_lock.py` list is documented.

        The list is the fact the fail-closed policy leans on. A third direct
        caller (`pipeline_state.py`) that reaches the win32 branch must be
        named in both documents — otherwise the docs claim a dead reserve
        the code no longer has.
        """
        docstring = _read("awf/_lock.py")
        match = _LIVE_CALLERS_RE.search(docstring)
        assert match, "the `_lock.py` docstring must list the direct `locked()` callers"
        names = re.findall(r"``([\w/]+\.py)``", match.group("body"))
        assert names, match.group("body")
        assert "pipeline_state.py" in names

        design_platform = _paragraph(_read("docs/design.md"), "**Платформа:**")
        writers = _read("docs/design/agentic-writers.md")
        policy_start = writers.index("## Политика платформы")
        policy_end = writers.find("\n## ", policy_start + 1)
        policy = writers[policy_start:] if policy_end < 0 else writers[policy_start:policy_end]

        for name in names:
            assert name in design_platform, (
                f"{name} is a live `locked()` caller but is missing from the "
                "`design.md` platform paragraph"
            )
            assert name in policy, (
                f"{name} is a live `locked()` caller but is missing from the "
                "`agentic-writers.md` platform policy"
            )
            # Anti-vacuum: the name must be a real import site, not a stale
            # label kept alive only by the documentation.
            src = _read("awf/" + name)
            assert "_lock import" in src and "locked" in src, name

    def test_direct_locked_callers_are_all_documented(self):
        """AR-0285-6: source → docstring — no undocumented direct caller.

        The sibling test walks docstring → docs and cannot see a NEW
        caller. Here every importer of ``locked`` is either the live list
        or the documented dead-reserve ``run_state.py``.
        """
        text = _read("awf/_lock.py")
        match = _LIVE_CALLERS_RE.search(text)
        assert match, "`_lock.py` must list the direct `locked()` callers"
        live = set(re.findall(r"``([\w/]+\.py)``", match.group("body")))
        assert "run_state" in text
        assert _direct_locked_importers() == live | {"run_state.py"}


class TestReferenceDocs:
    """R4/AC8–AC11: справочники и докстринги без ложных обещаний."""

    def test_practices_does_not_promise_a_template_symlink(self):
        para = _paragraph(_read("docs/practices.md"), "awf/templates/")
        assert "симлинк" not in para.lower()
        # TODO-0230 removed the root copy for good (test_template_sync
        # guards its absence): the replacement text must not invent a
        # second on-disk copy either.
        assert "рабочая копия" not in para
        assert "удалён" in para

    @pytest.mark.parametrize("rel", ["USAGE.md", "USAGE.ru.md"])
    def test_usage_config_get_knows_code_5_and_env(self, rel):
        para = _paragraph(_read(rel), "config-get")
        assert "5" in para
        assert "AWF_SUITE_CMD" in para
        assert "AWF_PYTHON" in para
        # TODO-0277 moved the helper from config-get to awf suite-command:
        # the paragraph must not keep describing the old 3/4 mapping.
        assert "3/4" not in para
        assert "suite-command" in para

    def test_add_role_docstring_names_five_templates(self):
        text = _read("awf/api/roles.py")
        start = text.index("def add_role(")
        doc = text[start:]
        for name in (
            "agent-qa-review",
            "agent-implementer",
            "agent-architect-review",
            "agent-project-auditor",
            "agent-refactoring-specialist",
        ):
            assert name in doc

    def test_templates_dir_does_not_claim_all_copies(self):
        text = _read("awf/api/_templates.py")
        start = text.index("def awf_templates_dir(")
        doc = text[start:]
        assert "every copy" not in doc
        assert "ONLY source" not in doc
        assert "canonical builder" in doc

    def test_release_notes_name_the_new_default(self):
        notes = _read("docs/RELEASE-NOTES.md")
        para = _paragraph(notes, "silent_work_attempts")
        assert "default 2" in para
        assert "1 → 2" in para or "1 to 2" in para
        # AC10 is file-wide: the paragraph may not promise the old default,
        # and no other section may smuggle it back in.
        assert not re.search(r"default\s+1\b", notes)


# ─── T11: паритет `pipeline-write --variant` и код возврата ──────────────


@pytest.fixture
def project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="CarryDocs")
    return tmp_git_repo


@pytest.fixture
def no_resolvable_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every role resolves nowhere — the warning list is deterministic.

    Without this the warnings depend on the developer's global awf roles
    dir (`~/.local/share/awf/roles`), and the tests would pass or fail by
    machine instead of by code.
    """
    monkeypatch.setattr(
        "awf.api.setup._role_resolves", lambda role, project_dir: False
    )


def _role_files(project: Path) -> list[str]:
    return sorted(p.name for p in (project / ".agentic" / "roles").iterdir())


def _pipeline_text(project: Path, name: str = "default") -> str:
    return (project / ".agentic" / "pipelines" / f"{name}.yaml").read_text(
        encoding="utf-8"
    )


def test_variant_help_names_the_difference(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["pipeline-write", "--help"])
    assert exc.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "Role files are NOT created here" in out
    assert "awf add-role" in out


def test_role_help_names_the_closer_gate(capsys):
    """TODO-0290: --help names the closer gate for a role composition."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["pipeline-write", "--help"])
    assert exc.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "checklist-close/check pair" in out


def test_variant_write_matches_preview_and_touches_only_pipeline(
    project, capsys, no_resolvable_roles
):
    config_before = (project / ".agentic" / "config.yaml").read_bytes()
    supervisor_before = (
        project / ".agentic" / "roles" / "supervisor.md"
    ).read_bytes()
    roles_before = _role_files(project)
    capsys.readouterr()

    rc = cli.main(
        [
            "pipeline-write", "default", "--variant", "full",
            "--project-dir", str(project),
        ]
    )

    out = capsys.readouterr().out
    assert rc == 0, out
    assert _pipeline_text(project) == api.setup.pipeline_variant_yaml_text(
        "full", name="default"
    )
    assert load_stages(project / ".agentic" / "pipelines" / "default.yaml")

    # INV3: роли не материализуются; конфиг и supervisor не тронуты.
    assert _role_files(project) == roles_before
    assert (project / ".agentic" / "config.yaml").read_bytes() == config_before
    assert (
        project / ".agentic" / "roles" / "supervisor.md"
    ).read_bytes() == supervisor_before

    # AC22–AC23: вывод называет разницу и маршрут; неразрешённые роли —
    # WARNING, а код возврата остаётся 0.
    normalised = " ".join(out.split())
    assert "role files were not created" in normalised.lower()
    assert "awf add-role" in normalised
    assert "WARNING" in normalised
    assert "has no role file" in normalised


def test_variant_existing_name_without_force_keeps_bytes(project, capsys):
    args = [
        "pipeline-write", "default", "--variant", "full",
        "--project-dir", str(project),
    ]
    assert cli.main(args) == 0
    capsys.readouterr()
    before = _pipeline_text(project)

    rc = cli.main(args)

    captured = capsys.readouterr()
    assert rc == 1, captured.out
    assert _pipeline_text(project) == before


def test_role_and_variant_paths_print_one_warning_text(
    project, capsys, no_resolvable_roles
):
    """Both write paths share one warning text, slug and route (docstring
    of ``_warn_unresolved``). Unresolved stays a warning: rc 0."""
    base = ["--project-dir", str(project)]
    assert cli.main(["pipeline-write", "via-variant", "--variant", "full"] + base) == 0
    variant_warnings = _warning_lines(capsys.readouterr().out)
    assert variant_warnings, "the variant path must warn about a missing role"
    slug = re.search(r"role '([^']+)'", variant_warnings[0]).group(1)

    assert cli.main(["pipeline-write", "via-role", "--role", slug] + base) == 0
    role_out = capsys.readouterr().out

    assert _warning_lines(role_out) == [variant_warnings[0]], role_out
    assert f"`awf add-role {slug}`" in role_out


def test_unknown_variant_is_argparse_rc_2(project):
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "pipeline-write", "default", "--variant", "nope",
                "--project-dir", str(project),
            ]
        )
    assert exc.value.code == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_write_error_is_rc_1_without_a_file(project, capsys):
    pipelines = project / ".agentic" / "pipelines"
    pipelines.mkdir(exist_ok=True)
    pipelines.chmod(0o500)
    try:
        rc = cli.main(
            [
                "pipeline-write", "new-pipe", "--variant", "full",
                "--project-dir", str(project),
            ]
        )
    finally:
        pipelines.chmod(0o700)

    assert rc == 1, capsys.readouterr().out
    assert not (pipelines / "new-pipe.yaml").exists()


def test_form_materializes_roles_with_the_same_yaml(project):
    result = api.apply_project_setup(project, team=[], flow_variant="full")

    assert result.pipeline_file is not None
    assert _pipeline_text(project) == api.setup.pipeline_variant_yaml_text("full")
    data = yaml.safe_load(_pipeline_text(project))
    roles = {stage["role"] for stage in data["stages"]}
    for role in roles - {"supervisor"}:
        assert (project / ".agentic" / "roles" / f"{role}.md").is_file(), role
