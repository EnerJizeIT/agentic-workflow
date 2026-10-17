"""``python -m awf.infra.state_store preview|migrate <project_dir>``."""
from __future__ import annotations

import contextlib

from ._migration import main

if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(main())
