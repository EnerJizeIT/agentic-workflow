"""U8: ``awf metrics`` — метрики программы работ (отчёт на рабочий стол).

Тонкий CLI-обёрток над :func:`awf.metrics.collect_metrics`. Код возврата:
0 = измерено что-то (отчёт написан), 1 = измерять нечего вовсе.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Выполнить ``awf metrics`` и вернуть код выхода."""
    result = api.collect_metrics(
        Path(args.project_dir),
        reference_model=args.reference_model or None,
        since=args.since or None,
        out=args.out or None,
    )
    if getattr(args, "json", False):
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    else:
        if result.report_path:
            print(f"Отчёт: {result.report_path}")
        t = result.totals
        print(
            f"Юнитов: {len(result.units)} (окно {t['hours']:.1f} ч); "
            f"воркеры in {t['win']:,} / out {t['wout']:,}, компрессий {t['comp']}; "
            f"строки +{t['ins']:,} / −{t['dels']:,}"
        )
        sup = result.supervisor_outside
        print(
            f"Супервизор: в юнитах ${t['scost']:.3f}, вне ${sup['cost']:.3f}, "
            f"всего ${t['scost'] + sup['cost']:.3f}"
        )
        print(result.conversion["line"])
        for w in result.warnings:
            print(f"предупреждение: {w}", file=sys.stderr)
    return result.exit_code
