"""Port of lib/baseline.sh — ``awf baseline`` command."""
from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import git_utils, paths
from ._atomic import atomic_write_text


def run(args: Any) -> int:
    """Execute ``awf baseline`` and return exit code."""
    todo_id = args.todo_id
    project_dir = Path(getattr(args, "project_dir", "."))

    agentic = project_dir / ".agentic"
    if not agentic.is_dir():
        print(f"No .agentic/ found at {project_dir}. Run 'awf init' first.")
        return 1

    context_dir = paths.context_dir(project_dir)
    context_dir.mkdir(parents=True, exist_ok=True)

    print(f"Creating baseline for {todo_id}...")

    # 1. Git SHA
    is_git = git_utils.is_git_repo(project_dir)
    if is_git:
        sha = git_utils.git_stdout(project_dir, "rev-parse", "HEAD").strip()
        atomic_write_text(context_dir / f"BASELINE-{todo_id}.sha", sha + "\n")
        status = git_utils.git_stdout(project_dir, "status", "--short", check=False)
        atomic_write_text(context_dir / f"BASELINE-{todo_id}.status", status)
    else:
        atomic_write_text(context_dir / f"BASELINE-{todo_id}.sha", "(not a git repo)\n")
        atomic_write_text(context_dir / f"BASELINE-{todo_id}.status", "")

    # 2. Test baseline
    config_file = paths.config_file(project_dir)
    tests_log = context_dir / f"BASELINE-{todo_id}.tests.log"

    if config_file.exists():
        config_data = cfg_mod.load(project_dir)
        test_cmd = cfg_mod.get(config_data, "verification.test_cmd", "") or ""
        if test_cmd:
            parts = shlex.split(test_cmd)
            if parts:
                result = subprocess.run(
                    parts,
                    cwd=str(project_dir),
                    capture_output=True, text=True,
                )
                tests_log.write_text(result.stdout + result.stderr, encoding="utf-8")
                if result.returncode == 0:
                    print("Test baseline saved.")
                else:
                    print("Test baseline saved (command exited non-zero — recorded as-is).")
            else:
                tests_log.write_text("No test_cmd configured, skipping test baseline.\n", encoding="utf-8")
        else:
            tests_log.write_text("No test_cmd configured, skipping test baseline.\n", encoding="utf-8")
    else:
        tests_log.write_text("No config.yaml found, skipping test baseline.\n", encoding="utf-8")

    # 3. Python environment
    python_cmd = "python3"
    if not shutil.which("python3") and shutil.which("python"):
        python_cmd = "python"

    env_parts = []
    for cmd in [[python_cmd, "--version"], [python_cmd, "-m", "pip", "list"]]:
        result = subprocess.run(cmd, capture_output=True, text=True)
        env_parts.append(result.stdout + result.stderr)

    (context_dir / f"BASELINE-{todo_id}.env.log").write_text(
        "".join(env_parts), encoding="utf-8"
    )

    print()
    print("Baseline files created:")
    # L1 fix: was (context_dir / "BASELINE-{id}.*").glob("*") — glob on
    # a non-existent file path returns empty iterator. Use context_dir.glob().
    for f in sorted(context_dir.glob(f"BASELINE-{todo_id}.*")):
        if f.is_file():
            print(f"  {f.name}")

    return 0
