"""S12 (TODO-0267): a slug-specific spec pipeline end to end (no model).

``awf spec init <slug>`` writes ``.agentic/pipelines/spec-<slug>.yaml``;
the six author stages of that pipeline write ``docs/spec/<slug>/<slug>.*``
(mock agents fill the documents from the IMP1 fixture). The resulting
package passes ``awf spec check`` and no path mentions ``spec-pipeline``.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from awf import cmd_spec_check
from awf import spec_package as sp
from awf.api import setup
from awf.pipeline import load_stages
from tests.spec_support import init_project_repo
from tests.unit.conftest import _spec_documents

SLUG = "carry-cd"


def _write_roles(repo: Path) -> None:
    roles = repo / ".agentic" / "roles"
    roles.mkdir(parents=True, exist_ok=True)
    for role in setup._stage_roles(setup._variant_composition("spec")[0]):
        (roles / f"{role}.md").write_text(f"# {role}\n", encoding="utf-8")


def _materialize_package(repo: Path, stages) -> Path:
    """Mock agents: every author stage writes its declared document."""
    root = repo / "docs" / "spec" / SLUG
    docs = _spec_documents(SLUG)
    for stage in stages:
        if not stage.output:
            continue
        name = Path(stage.output).name
        path = repo / stage.output
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(docs[name], encoding="utf-8")
    sha = sp.package_fingerprint(root)
    readiness = root / f"{SLUG}.readiness.md"
    readiness.write_text(
        readiness.read_text(encoding="utf-8").replace("__PACKAGE_SHA__", sha),
        encoding="utf-8",
    )
    return root


def test_spec_init_package_end_to_end(tmp_path: Path) -> None:
    repo = init_project_repo(tmp_path)
    _write_roles(repo)

    result, unresolved = setup.write_spec_pipeline(repo, SLUG)
    assert unresolved == []
    pipeline = Path(result.file)
    assert pipeline.name == f"spec-{SLUG}.yaml"
    text = pipeline.read_text(encoding="utf-8")
    assert "spec-pipeline" not in text

    stages = load_stages(pipeline)
    assert stages[0].name == "plan" and stages[0].kind == "plan"
    assert stages[1].kind == "execute"
    author = [stage for stage in stages if stage.output]
    assert len(author) == 6

    root = _materialize_package(repo, stages)
    for stage in author:
        assert stage.output.startswith(f"docs/spec/{SLUG}/{SLUG}.")
        assert (repo / stage.output).is_file()
        assert "spec-pipeline" not in stage.output

    gate = sp.spec_gate(repo, root)
    assert gate.ok, gate.reason + " / " + " / ".join(gate.details)
    rc = cmd_spec_check.run(
        SimpleNamespace(path=f"docs/spec/{SLUG}", project_dir=str(repo))
    )
    assert rc == 0


def test_spec_init_does_not_touch_existing_agentic_files(tmp_path: Path) -> None:
    repo = init_project_repo(tmp_path)
    _write_roles(repo)
    default = repo / ".agentic" / "pipelines" / "default.yaml"
    default.write_text("# user-owned default pipeline\n", encoding="utf-8")
    before = default.read_text(encoding="utf-8")

    setup.write_spec_pipeline(repo, SLUG)

    assert default.read_text(encoding="utf-8") == before
    assert not (repo / ".agentic" / "pipelines" / "spec.yaml").exists()


def test_spec_init_runs_through_the_cli(tmp_path: Path, capsys) -> None:
    from awf import cli

    repo = init_project_repo(tmp_path)
    _write_roles(repo)
    rc = cli.main(["spec", "init", SLUG, "--project", str(repo)])
    assert rc == 0
    assert (repo / ".agentic" / "pipelines" / f"spec-{SLUG}.yaml").is_file()
    capsys.readouterr()


def test_spec_init_refuses_an_escaping_slug_and_a_missing_project(
    tmp_path: Path, capsys
) -> None:
    from awf import cli

    repo = init_project_repo(tmp_path)
    _write_roles(repo)
    rc = cli.main(["spec", "init", "../escape", "--project", str(repo)])
    assert rc == 1
    assert "Invalid spec slug" in capsys.readouterr().out
    assert list((repo / ".agentic" / "pipelines").glob("spec-*.yaml")) == []

    plain = tmp_path / "plain"
    plain.mkdir()
    rc = cli.main(["spec", "init", "pkg", "--project", str(plain)])
    assert rc == 1
    assert "No .agentic/" in capsys.readouterr().out


def test_spec_init_warning_names_the_add_role_cure(tmp_path: Path, capsys) -> None:
    """AUD2 (0288): an unresolved stage role is curable in one command.

    A clean project has the packaged role TEMPLATES but no role files, so
    ``write_spec_pipeline`` reports the roles as unresolved. The warning must
    name ``awf add-role`` — otherwise the reader is told "fails at runtime"
    with no path forward.
    """
    from awf import cli

    repo = init_project_repo(tmp_path)

    rc = cli.main(["spec", "init", "pkg", "--project", str(repo)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "awf add-role" in out, out
    assert "agent-requirements-reviewer" in out, out
