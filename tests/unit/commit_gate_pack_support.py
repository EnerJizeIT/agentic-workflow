"""Shared fixtures for the A37 pack-gate tests (TODO-0349).

Split by meaning so no file crosses the ratchet's 400-line threshold:
``test_commit_gate_pack_verdict`` pins the refusal, and
``test_commit_gate_override`` pins the approve override, the audit fact and
the green/missing pack. The repo, pack and audit builders live here once.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from awf import git_utils
from awf.commit_gate import maybe_commit

TODO = "TODO-0349"
PACK = "GATES-{todo}.md"


def pack_body(
    verdict: str,
    sections: dict[str, str],
    tree_sha: str,
    *,
    first_failure: str = "- `pytest tests/unit/test_x.py`: FAIL (rc=1) — 1 failed",
) -> str:
    """A ``GATES-<todo>.md`` shaped like the writer's report (markdown + JSON)."""
    lines = [
        f"# GATES-{TODO} — verify-pack report",
        "",
        f"## Verdict: **{verdict}** (exit {0 if verdict == 'ok' else 1}) — "
        f"{len(sections)} section(s) measured",
        "",
    ]
    for index, (name, status) in enumerate(sections.items(), 1):
        lines += [f"## {index}. {name}: {status.upper()}", ""]
        if status == "fail":
            lines += [first_failure, ""]
    payload = {
        "todo_id": TODO,
        "verdict": verdict,
        "exit_code": 0 if verdict == "ok" else 1,
        "measured": len(sections),
        "sections": sections,
        "details": {},
        "report": f".agentic/context/GATES-{TODO}.md",
        "tree_sha": tree_sha,
    }
    lines += ["---", "", "```json", json.dumps(payload), "```", ""]
    return "\n".join(lines)


def prepared_repo(root: Path) -> Path:
    """Committed repo with a unit change ready to commit."""
    (root / ".gitignore").write_text(".agentic/\n")
    (root / ".agentic" / "context").mkdir(parents=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "gitignore"], cwd=root, check=True)
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("work\n")
    return root


def baseline(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def write_pack(
    repo: Path, verdict: str, sections: dict[str, str], tree_sha: str
) -> None:
    (repo / ".agentic" / "context" / PACK.format(todo=TODO)).write_text(
        pack_body(verdict, sections, tree_sha), encoding="utf-8"
    )


def write_pack_body(repo: Path, body: str) -> None:
    (repo / ".agentic" / "context" / PACK.format(todo=TODO)).write_text(
        body, encoding="utf-8"
    )


def commit(repo: Path, sha: str):
    logs = repo / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return maybe_commit(
        "implement", TODO, "commit_and_report", repo, logs, auto=False, baseline_sha=sha
    )


def head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def write_binding(repo: Path, binding: dict) -> None:
    """A hand-written APPROVE signal — the shape the gate re-reads."""
    inbox = repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"APPROVE-{TODO}.ready").write_text(
        json.dumps(binding) + "\n", encoding="utf-8"
    )


def audit(repo: Path) -> dict:
    return json.loads(
        (repo / ".agentic" / "context" / f"COMMIT-AUDIT-{TODO}.json").read_text(
            encoding="utf-8"
        )
    )


def fingerprint(repo: Path) -> str:
    return git_utils.tree_fingerprint(repo)
