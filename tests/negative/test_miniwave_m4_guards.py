"""M4 (TODO-0228): single-owner registry, printed-path guard, legacy removal.

The registry is data in ``docs/contracts/single-owners.md`` and
``docs/design/single-owners.md``; this module executes the probes, checks
the markers, scans ``awf/**`` for a second printed git-path parser and
proves the legacy parser is gone. Names start with ``test_m4_`` for ``-k m4``.
"""
from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "docs" / "contracts" / "single-owners.md"
DESIGN = REPO / "docs" / "design" / "single-owners.md"
OWNER = "awf/infra/git_model.py"

CONTRACT_COLUMNS = ("Факт", "Модуль-владелец", "Маркер")
DESIGN_COLUMNS = ("Факт", "Модуль-владелец", "Проба", "Ожидание")
EXCEPTION_COLUMNS = ("Модуль", "Причина", "Долг")
EMPTY = "∅"
PROBE_TIMEOUT_SEC = 120

#: The probe is a read-only command; anything that could write is refused so
#: a row cannot turn the guard into a mutator.
ALLOWED_COMMANDS = {"grep", "test", "ls"}
FORBIDDEN_IN_PROBE = (">", "<", "|", "&", ";", "$(", "`", "\n", "\r")
SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")

#: Printed listings of paths: a `-z` on the same call is the machine
#: protocol and is allowed (raw bytes, no path split). The list is a
#: WHITELIST, not a blacklist of flags (M4 review): a bare listing, a
#: `--stat` summary, `log/show --name-only` all emit paths and are red
#: outside the model.
GIT_SUBCOMMANDS = {"diff", "status", "ls-files", "ls-tree", "log", "show",
                   "diff-tree"}
#: Bare subcommands that emit a path listing even without a flag.
PATH_LIST_SUBCOMMANDS = {"ls-files", "ls-tree", "status", "diff-tree"}
PRINTED_FLAGS = {"--name-only", "--name-status", "--porcelain", "--short",
                 "--others", "--ignored", "--raw", "--stat"}
#: Decoding kwargs: a ``-z`` listing decoded this way is NOT the byte
#: protocol — a non-UTF-8 name raises exactly like a printed listing.
DECODE_KWARGS = {"text", "universal_newlines", "encoding"}

#: A declared exception is honest only while the module carries a guard that
#: turns the printed decode failure into a degradation.
GUARD_TOKENS = ("surrogateescape", "UnicodeDecodeError", 'errors="replace"')


class DocError(AssertionError):
    pass


# ─── table parsing ───────────────────────────────────────────────────────


def _cells(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    return [cell.strip().strip("`").strip() for cell in stripped.strip("|").split("|")]


def _table(text: str, columns: tuple[str, ...]) -> list[dict[str, str]]:
    """The first table whose header is exactly ``columns``, as dict rows."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        cells = _cells(line)
        if cells is None or tuple(cells) != columns:
            continue
        rows: list[dict[str, str]] = []
        for line_no, row_line in enumerate(lines[index + 1:], start=index + 2):
            row = _cells(row_line)
            if row is None:
                break
            if all(SEPARATOR_RE.fullmatch(cell) for cell in row):
                continue
            if len(row) != len(columns):
                raise DocError(f"line {line_no}: {len(row)} columns, expected {len(columns)}")
            for column, cell in zip(columns, row):
                if not cell:
                    raise DocError(f"line {line_no}: empty {column!r} cell")
            rows.append(dict(zip(columns, row)))
        return rows
    raise DocError("no table with columns " + " | ".join(columns))


def _design_rows() -> list[dict[str, str]]:
    return _table(DESIGN.read_text(encoding="utf-8"), DESIGN_COLUMNS)


def _contract_rows() -> list[dict[str, str]]:
    return _table(CONTRACT.read_text(encoding="utf-8"), CONTRACT_COLUMNS)


def _exception_rows() -> list[dict[str, str]]:
    return _table(DESIGN.read_text(encoding="utf-8"), EXCEPTION_COLUMNS)


def _owner_file(row: dict[str, str]) -> Path:
    owner = row["Модуль-владелец"]
    path = REPO / owner
    if not path.is_file():
        raise DocError(f"owner {owner!r} does not exist")
    return path


# ─── printed-path scanner (the guard itself) ─────────────────────────────


def _string_constants(node: ast.AST) -> list[str]:
    return [
        n.value for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _decodes_text(node: ast.Call) -> bool:
    """``-z`` is byte-safe only while the bytes stay bytes: decoding kwargs
    (``text``, ``universal_newlines``, ``encoding``) break that."""
    return any(kw.arg in DECODE_KWARGS for kw in node.keywords)


def find_printed_git_path_reads(source: str, filename: str = "<source>") -> list[str]:
    """Every printed git path read in ``source``, one line per call site.

    A call is a path read when it asks git for paths in a form that is NOT
    the NUL machine protocol: ``-z`` is the only allowed form (and only while
    the bytes stay bytes). ``--stat`` is included: it prints a path per line
    and crashes on a non-UTF-8 name exactly like the listing it replaced.
    """
    tree = ast.parse(source)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        constants = _string_constants(node)
        func = getattr(node.func, "attr", None) or getattr(node.func, "id", "") or ""
        # A dict key or an argparse subparser named ``status`` is not a git
        # call: only a ``git`` literal (``subprocess.run(["git", ...])``) or a
        # git-named helper (``_git``/``git_stdout``/``_git_isolated``) counts.
        if "git" not in constants and "git" not in func.lower():
            continue
        subcommands = GIT_SUBCOMMANDS.intersection(constants)
        if not subcommands:
            continue
        if "-z" in constants:
            if _decodes_text(node):
                hits.append(
                    f"{filename}:{node.lineno}: decoded machine listing "
                    "(-z with text decoding is not byte-safe)"
                )
            continue
        flags = sorted(PRINTED_FLAGS.intersection(constants))
        bare = sorted(subcommands & PATH_LIST_SUBCOMMANDS)
        if flags or bare:
            hits.append(
                f"{filename}:{node.lineno}: printed git path read "
                f"{sorted(set(flags) | set(bare))}"
            )
    return hits


def _scan_tree() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted((REPO / "awf").rglob("*.py")):
        hits = find_printed_git_path_reads(
            path.read_text(encoding="utf-8"), str(path.relative_to(REPO))
        )
        if hits:
            found[str(path.relative_to(REPO))] = hits
    return found


# ─── registry checks ─────────────────────────────────────────────────────


def test_m4_registry_and_design_agree() -> None:
    contract = _contract_rows()
    design = _design_rows()
    if not contract or not design:
        raise DocError("the registry or the design table is empty")
    contract_pairs = {(row["Факт"], row["Модуль-владелец"]) for row in contract}
    design_pairs = {(row["Факт"], row["Модуль-владелец"]) for row in design}
    assert contract_pairs == design_pairs, (
        "contract and design disagree: "
        f"only in contract {sorted(contract_pairs - design_pairs)}, "
        f"only in design {sorted(design_pairs - contract_pairs)}"
    )


def test_m4_every_owner_exists_and_carries_its_marker() -> None:
    for row in _contract_rows():
        owner_file = _owner_file(row)
        marker = row["Маркер"]
        if marker not in owner_file.read_text(encoding="utf-8", errors="replace"):
            raise DocError(
                f"{row['Факт']!r}: marker {marker!r} is not in {row['Модуль-владелец']}"
            )


def _run_probe(probe: str) -> str:
    if not probe.strip():
        raise DocError("empty probe — the row promises nothing")
    for token in FORBIDDEN_IN_PROBE:
        if token in probe:
            raise DocError(f"probe contains forbidden {token!r}: {probe}")
    argv = shlex.split(probe)
    if not argv or Path(argv[0]).name not in ALLOWED_COMMANDS:
        raise DocError(f"probe command {argv[0] if argv else ''!r} is not read-only")
    result = subprocess.run(
        argv, cwd=REPO, capture_output=True, text=True,
        errors="surrogateescape", timeout=PROBE_TIMEOUT_SEC, check=False,
    )
    if result.returncode not in (0, 1):
        raise DocError(f"probe failed with rc={result.returncode}: {result.stderr[:300]}")
    return result.stdout


def test_m4_every_probe_prints_its_expectation() -> None:
    for row in _design_rows():
        _owner_file(row)
        output = _run_probe(row["Проба"])
        expected = row["Ожидание"].strip()
        if expected == EMPTY:
            assert output == "", (
                f"{row['Факт']!r}: expected no printed read, got:\n{output}"
            )
            continue
        assert expected in output, (
            f"{row['Факт']!r}: probe {row['Проба']!r} never printed "
            f"{expected!r}; got:\n{output[:500]}"
        )


def test_m4_printed_reads_only_in_registered_exceptions() -> None:
    exceptions = {row["Модуль"] for row in _exception_rows()}
    for module in exceptions:
        if not (REPO / module).is_file():
            raise DocError(f"exception module {module!r} does not exist")
    hits = _scan_tree()
    unexpected = sorted(module for module in hits if module not in exceptions | {OWNER})
    assert not unexpected, (
        "printed git path reads outside the model and the registry:\n"
        + "\n".join(f"- {module}: {hits[module]}" for module in unexpected)
    )


def test_m4_registered_exceptions_are_guarded() -> None:
    for row in _exception_rows():
        module = REPO / row["Модуль"]
        source = module.read_text(encoding="utf-8", errors="replace")
        assert any(token in source for token in GUARD_TOKENS), (
            f"{row['Модуль']}: an exception without a decode guard is a lie — "
            "add surrogateescape/UnicodeDecodeError handling or migrate to the model"
        )
        assert row["Долг"].strip(), f"{row['Модуль']}: the debt cell is empty"


def test_m4_scanner_catches_an_artificial_second_parser() -> None:
    second = (
        "import subprocess\n"
        "def changed(root):\n"
        "    return subprocess.run(['git', 'diff', '--name-only', 'HEAD'], text=True).stdout\n"
    )
    hits = find_printed_git_path_reads(second, "awf/second.py")
    assert hits and "awf/second.py" in hits[0]

    status = (
        "import subprocess\n"
        "def dirty(root):\n"
        "    return subprocess.run(['git', 'status', '--porcelain'], text=True).stdout\n"
    )
    assert find_printed_git_path_reads(status)

    machine = (
        "import subprocess\n"
        "def dirty(root):\n"
        "    return subprocess.run(['git', 'status', '--porcelain', '-z']).stdout\n"
    )
    assert find_printed_git_path_reads(machine) == []


def test_m4_scanner_catches_a_decoded_machine_listing() -> None:
    """Decoding the NUL protocol raises on a non-UTF-8 name exactly like the
    printed listing it replaced (M4 review)."""
    decoded = (
        "import subprocess\n"
        "def changed(root):\n"
        "    return subprocess.run(['git', 'ls-files', '-z'], text=True).stdout\n"
    )
    hits = find_printed_git_path_reads(decoded, "awf/second.py")
    assert hits, "a -z listing decoded with text=True must be flagged"
    assert "text decoding" in hits[0], hits[0]
    encoded = (
        "import subprocess\n"
        "def changed(root):\n"
        "    return subprocess.run(['git', 'ls-files', '-z'], encoding='utf-8').stdout\n"
    )
    assert find_printed_git_path_reads(encoded), "explicit encoding must be flagged"

    raw = (
        "import subprocess\n"
        "def changed(root):\n"
        "    return subprocess.run(['git', 'ls-files', '-z']).stdout\n"
    )
    assert find_printed_git_path_reads(raw) == []


def test_m4_scanner_catches_bare_and_summary_listings() -> None:
    """U4 (TODO-0228): the whitelist edge cases that the old blacklist let
    through — a bare ``ls-files``, a ``--stat`` summary and ``log``/``show``
    with ``--name-only`` are all printed path reads."""
    cases = {
        "bare ls-files": "subprocess.run(['git', 'ls-files'], text=True).stdout",
        "diff --stat": "subprocess.run(['git', 'diff', '--stat'], text=True).stdout",
        "log --name-only": (
            "subprocess.run(['git', 'log', '--name-only'], text=True).stdout"
        ),
        "show --name-only": (
            "subprocess.run(['git', 'show', '--name-only'], text=True).stdout"
        ),
    }
    for label, call in cases.items():
        source = f"import subprocess\ndef changed():\n    return {call}\n"
        hits = find_printed_git_path_reads(source, "awf/second.py")
        assert hits, f"{label} must be flagged as a printed path read"


# ─── legacy parser removal ───────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "core.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    (repo / ".agentic" / "context").mkdir(parents=True)
    return repo


def test_m4_legacy_printed_parser_is_removed() -> None:
    from awf import commit_plan

    for module in ("awf/commit_delta.py", "awf/commit_plan.py"):
        source = (REPO / module).read_text(encoding="utf-8")
        for name in ("_legacy_plan_split", "_all_changed_files"):
            assert name not in source, f"{module} still defines or references {name}"
    assert not hasattr(commit_plan, "_legacy_plan_split")
    assert not hasattr(commit_plan, "_all_changed_files")


def test_m4_plan_split_reads_the_model_and_sees_the_unit_work(tmp_path: Path) -> None:
    from awf import commit_plan

    repo = _repo(tmp_path)
    baseline = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "core.py").write_text("v2\n", encoding="utf-8")
    (repo / "new_file.py").write_text("x\n", encoding="utf-8")

    split = commit_plan._plan_split(repo, baseline, "TODO-0001")

    assert "core.py" in split.files
    assert "new_file.py" in split.files


# ─── verify-surface guards (добор 0221) ──────────────────────────────────


def _non_utf8_untracked(repo: Path) -> str:
    _git(repo, "config", "core.quotepath", "false")
    name = os.fsdecode(b"\xff\xfe.txt")
    (repo / name).write_bytes(b"data\n")
    return name


def test_m4_verify_diff_section_survives_non_utf8(tmp_path: Path) -> None:
    from awf import verify_pack

    repo = _repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD").strip()
    name = _non_utf8_untracked(repo)
    (repo / ".agentic" / "context" / "BASELINE-TODO-0001.sha").write_text(
        sha + "\n", encoding="utf-8"
    )

    section = verify_pack._diff_section(repo, "TODO-0001", None)

    assert section.name == "diff"
    assert any(name in line for line in section.lines), section.lines
    assert verify_pack._changed_paths(repo, "TODO-0001") is not None


def test_m4_detect_work_evidence_survives_non_utf8(tmp_path: Path) -> None:
    from awf import verify

    repo = _repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD").strip()
    _non_utf8_untracked(repo)

    assert verify.detect_work_evidence(repo, sha, "TODO-0001") is True


def test_m4_prove_red_test_listing_survives_non_utf8(tmp_path: Path) -> None:
    from awf import prove_red

    repo = _repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD").strip()
    name = _non_utf8_untracked(repo)
    (repo / f"test_{name}.py").write_bytes(b"def test_x(): pass\n")

    listed = prove_red._diff_test_files(repo, sha)

    assert f"test_{name}.py" in listed, listed


def test_m4_status_porcelain_never_reports_unknown_as_clean(tmp_path: Path) -> None:
    from awf import git_utils

    repo = _repo(tmp_path)
    _non_utf8_untracked(repo)
    status = git_utils.status_porcelain(repo)

    assert status.strip(), (
        "an undecodable listing must not read as a clean tree — the mutation "
        "dirty-gate refuses on a non-empty status"
    )


def test_m4_legacy_tree_fingerprint_reads_paths_through_the_model(
    tmp_path: Path,
) -> None:
    """``awf tree-sha`` keeps its own digest, but its untracked NAMES must
    come from the model: the printed ``ls-files`` read raised
    ``UnicodeDecodeError`` on a non-UTF-8 name under
    ``core.quotePath=false`` and aborted the whole fingerprint."""
    from awf import git_utils

    repo = _repo(tmp_path)
    name = _non_utf8_untracked(repo)
    first = git_utils.tree_fingerprint(repo)
    (repo / name).write_bytes(b"other bytes\n")
    assert git_utils.tree_fingerprint(repo) != first, (
        "a non-UTF-8 untracked file must be readable and its content hashed"
    )


# ─── REVIEW iter 5 (U1–U4): the raw identity reaches digest and baseline ──


def test_m4_files_digest_round_trips_a_non_utf8_name() -> None:
    """U1: the plan carries RAW path identities (surrogateescape), so the
    approve/commit digest must encode them the same way — a non-UTF-8 name
    used to raise ``UnicodeEncodeError`` inside
    ``candidate_verify``/``api.pipeline``/``commit_plan``."""
    import re

    from awf.commit_plan import files_digest

    name = os.fsdecode(b"\xff\xfe.txt")
    digest = files_digest([name, "core.py"])

    assert re.fullmatch(r"[0-9a-f]{64}", digest), digest
    assert digest == files_digest(["core.py", name]), "digest is order-insensitive"


def test_m4_baseline_status_is_unknown_not_clean_on_decode_failure(
    tmp_path: Path,
) -> None:
    """U2: an undecodable ``status --short`` must leave NO ``.status`` file
    (unknown → the legacy rule subtracts the whole staged set). An EMPTY file
    reads as "the index was clean" and smuggles a pre-staged non-UTF-8 file
    into the unit plan."""
    from awf import commit_delta, commit_plan
    from awf.api import pipeline

    repo = _repo(tmp_path)
    (repo / ".agentic" / "config.yaml").write_text(
        'verification:\n  test_cmd: ""\n', encoding="utf-8"
    )
    _git(repo, "config", "core.quotepath", "false")
    name = os.fsdecode(b"\xff\xfe.txt")
    (repo / name).write_bytes(b"pre-staged\n")
    _git(repo, "add", "--", name)
    baseline = _git(repo, "rev-parse", "HEAD").strip()

    pipeline.create_baseline(repo, "TODO-0001")

    status_file = repo / ".agentic" / "context" / "BASELINE-TODO-0001.status"
    assert not status_file.exists(), (
        "an undecodable status snapshot must be absent (unknown), not empty (clean)"
    )
    assert commit_delta.staged_at_baseline(repo, "TODO-0001") is None
    assert name not in commit_plan._plan_split(repo, baseline, "TODO-0001").files


def test_m4_design_tells_the_truth_about_the_fingerprint_migration() -> None:
    """U3: for a tree with a non-ASCII untracked name the new digest differs,
    so an older ``VERIFIED-*.sha`` is rejected as tree changed — deliberately,
    with a re-verify. The design must say so instead of promising survival."""
    text = DESIGN.read_text(encoding="utf-8")
    assert "пережил миграцию с прежним digest" not in text, (
        "the false claim about digest migration is back in the design"
    )
    assert "отвергаются как tree changed" in text, (
        "the design must name the deliberate re-verify for non-ASCII trees"
    )


# ─── REVIEW iter 6 (architect): the fallback writer keeps T2 ─────────────


def test_m4_fallback_snapshot_writer_does_not_split_on_a_unicode_line_separator(
    tmp_path: Path, monkeypatch
) -> None:
    """T2 held the canonical LEGACY reader to ``split("\\n")``; the lazy
    fallback WRITER (``api.pipeline._untracked_snapshot``, reached when the
    model cannot read the tree) builds the same printed snapshot and must
    follow the same rule. ``splitlines()`` also cuts on U+2028, so a raw name
    under ``core.quotePath=false`` would be written as two paths."""
    from awf.api import pipeline
    from awf.infra import git_model

    repo = _repo(tmp_path)
    _git(repo, "config", "core.quotepath", "false")
    name = "a\u2028b.txt"
    (repo / name).write_bytes(b"data\n")

    def _model_refuses(self, paths=()):
        raise git_model.GitModelError("model cannot read the tree")

    monkeypatch.setattr(git_model.GitRepository, "untracked_paths", _model_refuses)

    assert pipeline._untracked_snapshot(repo, set()) == name
