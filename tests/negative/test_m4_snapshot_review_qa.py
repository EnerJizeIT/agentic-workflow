"""QA-0228 review findings — RED on the reviewed tree.

Two behavioural gaps the M4 unit left behind. Production code is NOT fixed
here (project rule for the awf self-modification wave): each finding is a red
test; the fix goes through the next review pass.

* F-QA-1 — the LEGACY (pre-migration) ``BASELINE-*.untracked`` reader still
  ``strip()``-s each line, so a pre-existing untracked file whose name starts
  with a space reads as this unit's work (F1b on an OLD snapshot, the case the
  TODO's R1 names explicitly): ``detect_work_evidence`` is True and the file
  enters the commit plan.
* F-QA-2 — the verify-pack report is a strict UTF-8 sink while the diff /
  untracked-excluded sections carry RAW identities (surrogates). A non-UTF-8
  path raises ``UnicodeEncodeError`` out of the public ``verify_pack`` — the
  acceptance item "verify surfaces: quotepath=false + non-UTF-8 → no raw
  crash" is not met.

Every case builds a real git repository and calls a public surface.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "qa@example.com")
    _git(repo, "config", "user.name", "QA")
    (repo / "core.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / ".agentic" / "context").mkdir(parents=True)
    # A real awf project gitignores ``.agentic``; hide the runtime files.
    info = repo / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "exclude").write_text(".agentic/\n", encoding="utf-8")
    return repo


def _non_utf8_untracked(repo: Path) -> str:
    _git(repo, "config", "core.quotepath", "false")
    name = os.fsdecode(b"\xff\xfe.txt")
    (repo / name).write_bytes(b"data\n")
    return name


def _legacy_snapshot(repo: Path, todo_id: str) -> str:
    """Write the PRE-migration snapshot form: git's PRINTED ``ls-files``
    listing, one path per line (no NUL). This is what a project baselined
    before the raw writer holds."""
    sha = _git(repo, "rev-parse", "HEAD").strip()
    printed = _git(repo, "ls-files", "--others", "--exclude-standard")
    ctx = repo / ".agentic" / "context"
    (ctx / f"BASELINE-{todo_id}.sha").write_text(sha + "\n", encoding="utf-8")
    (ctx / f"BASELINE-{todo_id}.untracked").write_text(printed, encoding="utf-8")
    return sha


def test_legacy_printed_snapshot_keeps_an_edge_space_path(tmp_path: Path) -> None:
    """A pre-migration snapshot lists a leading-space name unquoted (git's
    ``core.quotePath`` does not touch a space). The legacy reader must keep
    the space: stripping it makes a pre-existing untracked file look like
    this unit's work and pulls it into the commit plan (TODO-0228 R1: F1b on
    an OLD snapshot)."""
    from awf import commit_plan, verify
    from awf.infra import git_model

    assert hasattr(git_model, "read_untracked_snapshot"), (
        "no canonical snapshot reader — the raw NUL writer has no reader"
    )
    repo = _repo(tmp_path)
    pre_existing = " note.txt"
    (repo / pre_existing).write_text("pre-existing\n", encoding="utf-8")
    sha = _legacy_snapshot(repo, "TODO-0001")

    assert verify.detect_work_evidence(repo, sha, "TODO-0001") is False, (
        "a pre-existing untracked file (leading space in the name) counted as "
        "this unit's work"
    )

    (repo / "core.py").write_text("v2\n", encoding="utf-8")
    split = commit_plan._plan_split(repo, sha, "TODO-0001")
    assert split.files == ["core.py"], split.files

    assert git_model.read_untracked_snapshot(repo, "TODO-0001") == {pre_existing}


def test_verify_pack_writes_a_report_with_a_non_utf8_path(tmp_path: Path) -> None:
    """The diff/excluded sections carry the RAW identity (a surrogate). The
    report writer is a strict UTF-8 sink, so a non-UTF-8 changed path raises
    ``UnicodeEncodeError`` out of the public ``verify_pack`` (TODO-0228
    acceptance d: no raw crash on a non-UTF-8 verify surface)."""
    from awf import verify_pack
    from awf.api.pipeline import _untracked_snapshot

    repo = _repo(tmp_path)
    _non_utf8_untracked(repo)
    sha = _git(repo, "rev-parse", "HEAD").strip()
    ctx = repo / ".agentic" / "context"
    (ctx / "BASELINE-TODO-0001.sha").write_text(sha + "\n", encoding="utf-8")
    (ctx / "BASELINE-TODO-0001.untracked").write_text(
        _untracked_snapshot(repo, set()), encoding="utf-8", errors="surrogateescape"
    )
    (repo / "core.py").write_text("v2\n", encoding="utf-8")

    result = verify_pack.verify_pack(repo, "TODO-0001")

    report = Path(result.report_path)
    assert report.is_file()
    assert report.read_text(encoding="utf-8", errors="backslashreplace").strip()


def test_tree_fingerprint_tolerates_a_broken_exclude(tmp_path: Path) -> None:
    """F-QA-3 [BLOCKER]: ``git_utils.tree_fingerprint`` used to read the
    untracked names with ``check=False`` — a git that refuses the repository
    (an unwritable ``.git/info/exclude``, the M1 scenario) still produced a
    fingerprint. The unit routed the names through ``GitRepository``
    (``check=True``), so the same refusal now RAISES and ``awf tree-sha`` /
    ``awf approve`` fail. ``tests/negative/test_miniwave_m1_suite.py::
    test_unwritable_exclude_warns_instead_of_silence`` is RED on this tree
    and GREEN at the baseline — the rest of the read must degrade, not abort.
    """
    from awf import git_utils

    repo = _repo(tmp_path)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.unlink()
    exclude.mkdir()

    fingerprint = git_utils.tree_fingerprint(repo)

    assert isinstance(fingerprint, str) and len(fingerprint) == 64, fingerprint


def test_legacy_snapshot_does_not_split_on_a_unicode_line_separator(
    tmp_path: Path,
) -> None:
    """F-QA-4 [LOW]: the legacy printed snapshot is ONE PATH PER LINE (git
    separates with ``\\n`` only), but the reader uses ``splitlines()``, which
    also breaks on U+2028/U+2029/U+0085. Under ``core.quotePath=false`` such a
    name is printed raw, so it is shredded into two fake paths — the same
    F1b class the unit closed for the edge space. The fix is one character:
    ``text.split("\\n")``.
    """
    from awf import verify
    from awf.infra import git_model

    repo = _repo(tmp_path)
    _git(repo, "config", "core.quotepath", "false")
    name = "a\u2028b.txt"
    (repo / name).write_text("pre-existing\n", encoding="utf-8")
    sha = _legacy_snapshot(repo, "TODO-0001")

    assert git_model.read_untracked_snapshot(repo, "TODO-0001") == {name}
    assert verify.detect_work_evidence(repo, sha, "TODO-0001") is False
