"""W6.5 (TODO-0106): release hygiene — CI wheel smoke + docs/registry parity.

A-18 (audit layer 13): CI only ever exercised editable installs, so the
``agent-workflow-ui`` wheel was built but never installed. This pins two
guards:
  * the CI workflow invokes ``scripts/wheel-smoke.sh`` (which builds BOTH
    wheels and smoke-installs them in a clean venv), and the script exists and
    is executable;
  * the tool counts quoted in ``USAGE.md`` / ``USAGE.ru.md`` still match the
    live MCP registry — the registry is the single source of truth.

REVIEW fixes (attempt 2):
  * F1 — the smoke must not prove a stale wheel left in dist/ by a version
    bump (``TestWheelSmokeStaleArtefacts`` exercises the picker via the
    offline ``--pick-wheel`` mode);
  * F2 — the CI wiring check parses the workflow YAML and requires the
    invocation in a step's ``run:``, not a comment.

A-18 supplement (TODO-0111):
  * ``TestWheelSmokeMatrix`` — the CI package job runs wheel-smoke on the
    full ``requires-python`` range (3.10 lower bound + 3.12) via a
    ``python-version`` matrix, while the test job deliberately stays
    single-version (3.12).
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOWS_DIR = _REPO / ".github" / "workflows"
_WHEEL_SMOKE = _REPO / "scripts" / "wheel-smoke.sh"

# The summary line both docs carry under the tools reference, e.g.
#   "47 tools: 42 `awf_*` workflow + 5 UI (forms)."
#   "47 инструментов: 42 `awf_*` workflow + 5 UI (формы)."
# Groups: (total, awf_*, UI). Backticks around ``awf_*`` are optional so a
# cosmetic re-format does not mask a real count drift.
_COUNT_RE = re.compile(
    r"(\d+)\s+(?:tools|инструмент(?:ов|ы|а))\s*:\s*(\d+)\s+`?awf_\*`?\s+workflow\s*\+\s*(\d+)\s+UI"
)


def _registry_counts() -> dict[str, int]:
    """Count tools from the registry — the single source of truth.

    R-06 (TODO-0112): reads ``tools/registry.py`` directly — ``create_server()``
    registers exactly this list, and the registry↔``list_tools()`` parity is
    pinned by ``test_audit25_r06_registry.py``."""
    pytest.importorskip("mcp.server.fastmcp")
    from agent_workflow_ui.tools.registry import TOOLS

    names = [t.name for t in TOOLS]
    awf = [n for n in names if n.startswith("awf_")]
    ui = [n for n in names if not n.startswith("awf_")]
    return {
        "total": len(names),
        "awf": len(awf),
        "ui": len(ui),
        "ui_names": sorted(ui),
        "all_names": sorted(names),
    }


@pytest.fixture(scope="module")
def registry_counts() -> dict[str, int]:
    return _registry_counts()


class TestWheelSmokeInCI:
    """A-18: the wheel smoke is wired into CI and the script is usable."""

    def test_workflow_invokes_wheel_smoke(self):
        # F2 (TODO-0106 attempt 2): parse the YAML and require the path in a
        # step's `run:` field. A mention in a comment is not an invocation —
        # the old substring check passed on comment-only references.
        workflows = sorted(_WORKFLOWS_DIR.glob("*.y*ml"))
        assert workflows, f"no workflow files in {_WORKFLOWS_DIR}"
        found = []
        for path in workflows:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for job_name, job in (doc.get("jobs") or {}).items():
                for step in (job or {}).get("steps") or []:
                    if "scripts/wheel-smoke.sh" in (step.get("run") or ""):
                        found.append(f"{path.name} / {job_name} / {step.get('name', '<unnamed>')}")
        assert found, (
            "no workflow step's `run:` invokes scripts/wheel-smoke.sh "
            "(a mention in a YAML comment does not count)"
        )

    def test_wheel_smoke_script_exists_and_is_executable(self):
        assert _WHEEL_SMOKE.is_file(), f"missing {_WHEEL_SMOKE}"
        assert os.access(_WHEEL_SMOKE, os.X_OK), f"{_WHEEL_SMOKE} is not executable (chmod +x)"


class TestWheelSmokeStaleArtefacts:
    """F1 (TODO-0106 attempt 2): a stale wheel left in dist/ after a version
    bump must not be the one the smoke proves. The old selection
    (``ls dist/*.whl | head -n1``, alphabetical) picked the OLDEST wheel —
    false green exactly at release. The picker the smoke uses after the
    build (exposed offline as ``--pick-wheel``) must refuse ambiguity
    instead of guessing."""

    def _pick(self, tmp_path: Path, *wheels: str) -> subprocess.CompletedProcess:
        dist = tmp_path / "dist"
        dist.mkdir()
        for name in wheels:
            (dist / name).write_bytes(b"")
        return subprocess.run(
            ["bash", str(_WHEEL_SMOKE), "--pick-wheel", str(dist), "awf-*.whl"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_ambiguous_old_and_new_fails_clearly(self, tmp_path):
        # The release scenario: the previous release's wheel sits next to the
        # new one in dist/. The smoke must refuse with a readable error, not
        # silently prove the old artefact.
        proc = self._pick(tmp_path, "awf-1.3.0-py3-none-any.whl", "awf-1.4.0-py3-none-any.whl")
        assert proc.returncode != 0, f"ambiguous dist/ must fail, got: {proc.stdout!r}"
        assert "ambiguous" in proc.stderr.lower(), f"no refusal message: {proc.stderr!r}"
        assert "awf-1.3.0" in proc.stderr and "awf-1.4.0" in proc.stderr

    def test_unique_wheel_is_returned(self, tmp_path):
        # After the build-step wipe, exactly one artefact per package: the
        # picker returns it and the smoke proves it.
        proc = self._pick(tmp_path, "awf-1.4.0-py3-none-any.whl")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == str(tmp_path / "dist" / "awf-1.4.0-py3-none-any.whl")

    def test_missing_wheel_fails(self, tmp_path):
        # Build produced nothing (broken runner) — fail, do not install a
        # stale artefact or pass with no wheel at all.
        proc = self._pick(tmp_path)
        assert proc.returncode != 0
        assert "no wheel" in proc.stderr.lower(), f"no readable message: {proc.stderr!r}"


class TestWheelSmokeMatrix:
    """A-18 supplement (TODO-0111): the package job proves the wheels on the
    full ``requires-python`` range — a 3.10-only breakage (syntax, stdlib,
    dependency floor) must not hide behind a single-version green. The test
    job stays single-version (3.12): the matrix lives on the cheap package
    job, not the full suite."""

    def _ci(self) -> dict:
        path = _WORKFLOWS_DIR / "ci.yml"
        assert path.is_file(), f"missing {path}"
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return doc

    def test_package_job_matrix_covers_310_and_312(self):
        doc = self._ci()
        job = (doc.get("jobs") or {}).get("package")
        assert job is not None, "ci.yml: no 'package' job"
        matrix = ((job.get("strategy") or {}).get("matrix") or {}).get("python-version")
        assert matrix, (
            "package job must run wheel-smoke on a python-version matrix "
            "(requires-python >=3.10 — one version cannot prove the range)"
        )
        for needed in ("3.10", "3.12"):
            assert needed in matrix, f"package matrix lacks python {needed}: {matrix}"

    def test_package_job_runs_smoke_under_matrix_python(self):
        # Both halves: a step actually invokes the smoke, and the
        # setup-python step consumes the matrix value (a hardcoded
        # python-version alongside the matrix would run the smoke on the
        # wrong interpreter).
        doc = self._ci()
        job = (doc.get("jobs") or {}).get("package")
        assert job is not None, "ci.yml: no 'package' job"
        steps = (job or {}).get("steps") or []
        assert any("scripts/wheel-smoke.sh" in (s.get("run") or "") for s in steps), (
            "no package-job step's `run:` invokes scripts/wheel-smoke.sh"
        )
        setups = [s for s in steps if "setup-python" in (s.get("uses") or "")]
        assert setups, "package job must set up python before building wheels"
        versions = [(s.get("with") or {}).get("python-version") for s in setups]
        assert any(
            isinstance(v, str) and "matrix.python-version" in v for v in versions
        ), f"setup-python must consume the matrix value, got: {versions}"

    def test_test_job_stays_single_python_312(self):
        doc = self._ci()
        job = (doc.get("jobs") or {}).get("test")
        assert job is not None, "ci.yml: no 'test' job"
        assert "strategy" not in (job or {}), (
            "test job must stay single-version (version coverage belongs to "
            "the package matrix, not the full suite)"
        )
        steps = (job or {}).get("steps") or []
        setups = [s for s in steps if "setup-python" in (s.get("uses") or "")]
        versions = [(s.get("with") or {}).get("python-version") for s in setups]
        assert versions == ["3.12"], f"test job must stay on Python 3.12, got: {versions}"


class TestDocsMatchRegistry:
    """A-08: the documented tool counts track the live MCP registry."""

    @pytest.mark.parametrize("doc", ["USAGE.md", "USAGE.ru.md"], ids=["en", "ru"])
    def test_tool_counts_match_registry(self, registry_counts: dict[str, int], doc: str):
        path = _REPO / doc
        assert path.is_file(), f"missing {path}"
        match = _COUNT_RE.search(path.read_text(encoding="utf-8"))
        assert match is not None, (
            f"{doc}: could not find the '<total> tools: <awf> awf_* workflow + <ui> UI' "
            "summary line — the count drifted out of the recognized format"
        )
        total, awf, ui = (int(match.group(i)) for i in (1, 2, 3))
        got = (total, awf, ui)
        want = (registry_counts["total"], registry_counts["awf"], registry_counts["ui"])
        assert got == want, (
            f"{doc} claims {total} total / {awf} awf_* / {ui} UI, "
            f"but the registry has {want[0]} / {want[1]} / {want[2]} "
            f"(UI: {', '.join(registry_counts['ui_names'])})"
        )

    @pytest.mark.parametrize("doc", ["USAGE.md", "USAGE.ru.md"], ids=["en", "ru"])
    def test_registry_tools_listed_in_tables(self, registry_counts: dict[str, int], doc: str):
        # The reference tables must stay a complete reference: every registry
        # tool has a row. The count line alone would still pass while rows
        # silently drop out of the tables.
        text = (_REPO / doc).read_text(encoding="utf-8")
        listed = set(re.findall(r"^\|\s*`([a-z0-9_]+)`", text, re.M))
        missing = [n for n in registry_counts["all_names"] if n not in listed]
        assert not missing, (
            f"{doc}: registry tools missing from the reference tables: {missing} — "
            "add a row for each so the tables stay complete"
        )
