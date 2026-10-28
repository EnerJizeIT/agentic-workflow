"""Entry point for ``awf doctor`` — print the report, return its rc.

The exit code is the contract (R5): 0 — no WARN, 1 — WARNs, 2 — the project
is not assessed (no ``.agentic/``). Printing lives here, not in
``awf/doctor.py``: the MCP tool consumes the same ``DoctorReport`` data and
decides by ``rc`` (D8).
"""
from __future__ import annotations

from typing import Any

from .doctor import run_doctor


def run(args: Any) -> int:
    """Print every report line; the doctor's rc is the command's rc."""
    report = run_doctor(getattr(args, "project_dir", "."))
    for line in report.lines:
        print(line)
    return report.rc
