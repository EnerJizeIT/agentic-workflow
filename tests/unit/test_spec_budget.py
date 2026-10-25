"""SPEC IMP6 — K6 volume budget: 2200 words per document, 11000 per package."""
from __future__ import annotations

import re
from pathlib import Path

from awf import spec_package as sp


def _pad_to(path: Path, target: int) -> None:
    text = path.read_text(encoding="utf-8")
    current = sp.count_words(text)
    assert current <= target
    if current < target:
        path.write_text(
            text + "\n" + " ".join(["слово"] * (target - current)) + "\n",
            encoding="utf-8",
        )
    assert sp.count_words(path.read_text(encoding="utf-8")) == target


def _refresh_sha(package: Path) -> None:
    sha = sp.package_fingerprint(package)
    readiness = package / "pkg.readiness.md"
    text = readiness.read_text(encoding="utf-8")
    text = re.sub(r"Пакет-sha: \S+", f"Пакет-sha: {sha}", text, count=1)
    readiness.write_text(text, encoding="utf-8")


def test_count_words_matches_awk() -> None:
    assert sp.count_words("a  b\tc\n\nd\n") == 4


def test_budget_defaults() -> None:
    assert sp.budget_limits({}) == (2200, 11000)


def test_budget_rows_cover_six_documents(make_spec_package) -> None:
    package = make_spec_package()
    rows = sp.budget_rows(package)
    assert len(rows) == 6
    assert rows[0].path == "pkg.context.md"
    assert all(row.limit == 2200 for row in rows)
    assert sum(row.words for row in rows) == sum(
        sp.count_words((package / row.path).read_text(encoding="utf-8"))
        for row in rows
    )


def test_document_over_budget_refused(make_spec_package, tmp_path: Path) -> None:
    package = make_spec_package()
    _pad_to(package / "pkg.md", 2201)
    _refresh_sha(package)
    gate = sp.spec_gate(tmp_path, package)
    assert not gate.ok
    text = gate.reason + " " + " ".join(gate.details)
    assert "2201" in text
    assert "2200" in text
    assert "MAJOR" in text


def test_document_at_budget_passes(make_spec_package, tmp_path: Path) -> None:
    package = make_spec_package()
    _pad_to(package / "pkg.md", 2200)
    _refresh_sha(package)
    gate = sp.spec_gate(tmp_path, package)
    assert gate.ok, gate.reason + " / " + " / ".join(gate.details)


def test_package_over_budget_refused(make_spec_package, tmp_path: Path) -> None:
    package = make_spec_package()
    rows = sp.budget_rows(package)
    readiness = next(row for row in rows if row.path.endswith(".readiness.md"))
    others = [row for row in rows if row is not readiness]
    for row in others:
        _pad_to(package / row.path, 1834)
    _refresh_sha(package)
    _pad_to(package / readiness.path, 1831)
    total = sum(row.words for row in sp.budget_rows(package))
    assert total == 11001

    gate = sp.spec_gate(tmp_path, package)
    assert not gate.ok
    text = gate.reason + " " + " ".join(gate.details)
    assert "11001" in text
    assert "11000" in text


def test_config_overrides_budget_limits(make_spec_package, tmp_path: Path) -> None:
    package = make_spec_package()
    agentic = tmp_path / ".agentic"
    agentic.mkdir()
    config = agentic / "config.yaml"
    requirements_words = sp.count_words(
        (package / "pkg.md").read_text(encoding="utf-8")
    )
    package_words = sum(row.words for row in sp.budget_rows(package))

    config.write_text(
        "spec:\n  budget:\n"
        f"    document: {requirements_words - 1}\n"
        "    package: 99000\n",
        encoding="utf-8",
    )
    gate = sp.spec_gate(tmp_path, package)
    assert not gate.ok
    assert str(requirements_words) in gate.reason
    assert str(requirements_words - 1) in gate.reason

    config.write_text(
        "spec:\n  budget:\n"
        "    document: 2200\n"
        f"    package: {package_words - 1}\n",
        encoding="utf-8",
    )
    gate = sp.spec_gate(tmp_path, package)
    assert "пакет" in gate.reason
    assert str(package_words) in gate.reason
    assert str(package_words - 1) in gate.reason
