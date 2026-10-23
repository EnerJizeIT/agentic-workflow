"""R2.2 architect-review, iter 13 (TODO-0221): the transfer-link readers guard
only ``OSError`` — a non-UTF-8 link blob raises ``UnicodeDecodeError`` (a
``ValueError``) that escapes the documented best-effort contract.

Файл КРАСНЫЙ на дереве до фикса стадии agent-architect-review.

The last fix (QA/iter 12, QA7-1) made the ``_path_link`` codec treat a
legacy (no-NUL) blob with an invalid byte as ``None`` (NEG-3). The two OTHER
link surfaces of the same transfer family never got that treatment:

* ``hygiene.transfer_exclusions`` reads ``BASELINE-<todo>.carry_over`` INLINE
  with ``read_text(encoding="utf-8")`` under ``except OSError`` — the channel
  owner (``reject_files``) never owned the link, unlike its ``.include`` /
  ``.claim`` siblings;
* ``claim_changes.read_claim_link`` reads ``BASELINE-<todo>.claim`` the same
  way, ``except OSError`` only.

A corrupt/tampered link whose bytes are not valid UTF-8 therefore raises
``UnicodeDecodeError`` out of ``transfer_exclusions``. Its callers document
the read as best-effort (``run.py`` ``awf run_next`` re-baseline: "Link-read
failure must not kill the re-baseline") but the except tuple
(``AwfApiError, RuntimeError, OSError, SubprocessError``) does NOT include
``ValueError`` — so the crash escapes the best-effort guard entirely.

Fix: the carry-over link lifecycle moves into its owning module
(``reject_files``: ``write``/``read``/``clear`` helpers, the sibling pattern),
and every link reader degrades a non-UTF-8 blob to None alongside ``OSError``.

Все репозитории — disposable в ``tmp_path``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from awf import api

SRC = "TODO-0001"
#: A blob no UTF-8 decoder accepts (a raw 0xff starter) — the NEG-3 input.
BINARY = b"\xff\xfe not a utf8 link \x80"


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
        project_name="R2ARCH7",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    return repo


def _ctx(repo: Path) -> Path:
    ctx = repo / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    return ctx


def test_transfer_exclusions_survives_binary_carry_over_link(tmp_path):
    """A non-UTF-8 ``.carry_over`` link must read as "no carry-over", not
    raise out of ``transfer_exclusions`` (the ``run_next`` re-baseline call)."""
    from awf.api import hygiene

    repo = _project(tmp_path)
    (_ctx(repo) / f"BASELINE-{SRC}.carry_over").write_bytes(BINARY)

    carry_over, include, claim = hygiene.transfer_exclusions(repo, SRC)
    assert (carry_over, include, claim) == (set(), set(), set())


def test_transfer_exclusions_survives_binary_claim_link(tmp_path):
    """The same for a non-UTF-8 ``.claim`` link (the claim sibling)."""
    from awf.api import hygiene

    repo = _project(tmp_path)
    (_ctx(repo) / f"BASELINE-{SRC}.claim").write_bytes(BINARY)

    carry_over, include, claim = hygiene.transfer_exclusions(repo, SRC)
    assert (carry_over, include, claim) == (set(), set(), set())


def test_read_claim_link_binary_blob_is_none(tmp_path):
    """``read_claim_link`` promises None for an unreadable link; a non-UTF-8
    blob is unreadable, not a crash."""
    from awf import claim_changes as cc

    repo = _project(tmp_path)
    (_ctx(repo) / f"BASELINE-{SRC}.claim").write_bytes(BINARY)

    assert cc.read_claim_link(repo, SRC) is None


def test_carry_over_link_lifecycle_is_owned_by_reject_files(tmp_path):
    """DRIFT: the carry-over link is the sibling of ``.include`` / ``.claim``,
    so its lifecycle lives in its channel module (``reject_files``) with
    write/read/clear helpers — not re-spelled inline in dispatch/hygiene."""
    from awf import reject_files as rf

    repo = _project(tmp_path)
    assert rf.read_carry_over_link(repo, SRC) is None

    rf.write_carry_over_link(repo, SRC, "TODO-0042")
    assert rf.read_carry_over_link(repo, SRC) == "TODO-0042"
    assert (
        (_ctx(repo) / f"BASELINE-{SRC}.carry_over").read_text(encoding="utf-8").strip()
        == "TODO-0042"
    )

    rf.clear_carry_over_link(repo, SRC)
    assert rf.read_carry_over_link(repo, SRC) is None
