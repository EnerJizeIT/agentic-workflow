"""RUN4 #2: ``awf feedback`` — фидбек-контур (отчёт владельцу на стол)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Выполнить ``awf feedback`` и вернуть код выхода."""
    result = api.feedback(
        Path(args.project_dir),
        ftype=args.ftype,
        title=args.title,
        body=args.body or "",
        severity=args.severity or "",
        expected=args.expected or "",
        got=args.got or "",
        why=args.why or "",
        proposal=args.proposal or "",
        stdout=args.stdout,
    )
    if args.stdout:
        print(result.report)
    else:
        print(f"Отчёт: {result.file}")
    return 0
