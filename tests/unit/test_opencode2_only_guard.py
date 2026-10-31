"""R10: awf must not grow opencode 1.x code back.

awf 3 supports opencode 2.x only. This guard reads every ``awf/**/*.py`` and
fails on the 1.x shapes: reading the v1 tables, the v1 config form, the
launch flags 1.x used, and the removed launch-mode key.

The patterns are word-bounded so the 2.x names stay legal:
``session_v2`` and ``session_message`` both start with a word character
after ``session``, so ``\\bsession\\b`` never matches them.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AWF = ROOT / "awf"

#: (id, human name, regex). Comments and docstrings are scanned too: a stale
#: comment teaching a 1.x command costs the next reader the same debugging.
PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("v1-sql-from", "reading a 1.x table", re.compile(r"(?i)\bfrom\s+(session|part|message)\b")),
    ("v1-sql-into", "writing a 1.x table", re.compile(r"(?i)\binto\s+(session|part|message)\b")),
    ("v1-sql-update", "updating a 1.x table", re.compile(r"(?i)\bupdate\s+(session|part|message)\s+set\b")),
    ("v1-config-form", "the 1.x baseURL form", re.compile(r"""["']options["']""")),
    ("attach-flag", "--attach", re.compile(r"--attach\b")),
    ("port-flag", "--port", re.compile(r"--port\b")),
    ("standalone-key", "the removed launch-mode key", re.compile(r"opencode\.standalone")),
)


def _modules() -> list[Path]:
    return sorted(p for p in AWF.rglob("*.py") if "__pycache__" not in p.parts)


def _hits(path: Path) -> list[tuple[str, str, int, str]]:
    found = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pid, label, pattern in PATTERNS:
            if pattern.search(line):
                found.append((pid, label, line_no, line.strip()))
    return found


def test_no_v1_patterns_in_the_package():
    offenders: list[str] = []
    for path in _modules():
        for pid, label, line_no, line in _hits(path):
            offenders.append(
                f"{path.relative_to(ROOT)}:{line_no} [{pid}: {label}] {line}"
            )
    assert not offenders, "opencode 1.x code is back:\n" + "\n".join(offenders)


def test_each_pattern_is_red_on_a_copy(tmp_path):
    """Teeth: every pattern fires on its own sample, so the guard is not vacuous."""
    samples = {
        "v1-sql-from": 'cur.execute("SELECT id FROM session")',
        "v1-sql-into": 'cur.execute("INSERT INTO part VALUES (?)")',
        "v1-sql-update": 'cur.execute("UPDATE message SET data = ?")',
        "v1-config-form": 'base = provider["options"]',
        "attach-flag": "cmd += ['--attach', host]",
        "port-flag": "cmd += ['--port', '4096']",
        "standalone-key": 'mode = cfg.get("opencode.standalone")',
    }
    for pid, _label, pattern in PATTERNS:
        assert pattern.search(samples[pid]), pid


def test_legal_v2_names_are_not_flagged():
    legal = [
        'cur.execute("SELECT id FROM session_v2")',
        'cur.execute("SELECT data FROM session_message")',
        "from . import session_store",
        'row["session_message"]',
    ]
    for pid, _label, pattern in PATTERNS:
        for line in legal:
            assert not pattern.search(line), (pid, line)
