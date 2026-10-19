"""R2.2 QA (TODO-0221, iter 14): regression-lock for the ARCH-13-1 fix.

The architect-review fix (iter 13, ARCH-13-1) made every transfer-link reader
degrade a non-UTF-8 blob instead of letting ``UnicodeDecodeError`` (a
``ValueError``, NOT an ``OSError``) escape the documented best-effort
contract. The arch7 test covers three of the four surfaces of that family:

* ``reject_files.read_carry_over_link`` (``BASELINE-<todo>.carry_over``);
* ``claim_changes.read_claim_link`` (``BASELINE-<todo>.claim``);
* ``hygiene.transfer_exclusions`` routing through the above.

The FOURTH surface — ``claim_changes._source_baseline_sha``
(``BASELINE-<src>.sha``), reached by ``plan_claim_changes`` — has no test. It
is locked here so a revert of the guard is caught (red-before: the pre-fix
reader caught only ``OSError`` and raised ``UnicodeDecodeError``).

The positive control guards the other direction: the widened ``except`` must
not swallow a perfectly readable baseline.

Все репозитории — disposable в ``tmp_path``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from awf import api
from awf._errors import AwfApiError

SRC = "TODO-0001"
#: A blob no UTF-8 decoder accepts (a raw 0xff starter) — the NEG-3 input.
BINARY = b"\xff\xfe not a utf8 sha \x80"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _project(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    api.init_project(
        repo,
        project_name="R2QA8",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    cfg = repo / ".agentic" / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    data["config_version"] = 1
    data["discipline"] = {"dirty_policy": "isolate"}
    cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def test_source_baseline_sha_binary_blob_is_awf_error(tmp_path):
    """A non-UTF-8 ``BASELINE-<src>.sha`` must degrade to a clean ``AwfApiError``
    (the documented refusal), not raise ``UnicodeDecodeError`` past
    ``plan_claim_changes``."""
    from awf import claim_changes as cc

    repo = _project(tmp_path)
    ctx = repo / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"BASELINE-{SRC}.sha").write_bytes(BINARY)

    with pytest.raises(AwfApiError, match="cannot read the source baseline"):
        cc.plan_claim_changes(repo, SRC)


def test_source_baseline_sha_valid_blob_is_read(tmp_path):
    """Positive control: the widened ``except`` must not swallow a readable
    baseline — a valid ``.sha`` flows through into the plan unchanged."""
    from awf import claim_changes as cc

    repo = _project(tmp_path)
    ctx = repo / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"BASELINE-{SRC}.sha").write_text("deadbeef\n", encoding="utf-8")

    plan = cc.plan_claim_changes(repo, SRC)
    assert plan.baseline_sha == "deadbeef"
