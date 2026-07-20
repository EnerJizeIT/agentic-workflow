"""Port of lib/baseline.sh — ``awf baseline`` command."""
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import paths


def run(args: Any) -> int:
    """Execute ``awf baseline`` and return exit code."""
    todo_id = args.todo_id

    agentic = Path(".agentic")
    if not agentic.is_dir():
        print("No .agentic/ found. Run 'awf init' first.")
        return 1

    context_dir = paths.context_dir(".")
    context_dir.mkdir(parents=True, exist_ok=True)

    print(f"Creating baseline for {todo_id}...")

    # 1. Git SHA
    is_git = _is_git_repo(".")
    if is_git:
        sha = _git_stdout(".", "rev-parse", "HEAD").strip()
        (context_dir / f"BASELINE-{todo_id}.sha").write_text(sha + "\n", encoding="utf-8")
        status = _git_stdout(".", "status", "--short", check=False)
        (context_dir / f"BASELINE-{todo_id}.status").write_text(status, encoding="utf-8")
    else:
        (context_dir / f"BASELINE-{todo_id}.sha").write_text("(not a git repo)\n", encoding="utf-8")
        (context_dir / f"BASELINE-{todo_id}.status").write_text("", encoding="utf-8")

    # 2. Test baseline
    config_file = paths.config_file(".")
    tests_log = context_dir / f"BASELINE-{todo_id}.tests.log"

    if config_file.exists():
        config_data = cfg_mod.load(".")
        test_cmd = cfg_mod.get(config_data, "verification.test_cmd", "") or ""
        if test_cmd:
            result = subprocess.run(
                test_cmd, shell=True,
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
        tests_log.write_text("No config.yaml found, skipping test baseline.\n", encoding="utf-8")

    # 3. Python environment
    python_cmd = "python3"
    if not shutil.which("python3") and shutil.which("python"):
        python_cmd = "python"

    env_parts = []
    for cmd in [f"{python_cmd} --version", f"{python_cmd} -m pip list"]:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        env_parts.append(result.stdout + result.stderr)

    (context_dir / f"BASELINE-{todo_id}.env.log").write_text(
        "".join(env_parts), encoding="utf-8"
    )

    print()
    print("Baseline files created:")
    for f in sorted((context_dir / f"BASELINE-{todo_id}.*").glob("*")):
        if f.is_file():
            print(f"  {f.name}")

    return 0


def _is_git_repo(project_dir: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=project_dir, capture_output=True, check=False,
    )
    return result.returncode == 0


def _git_stdout(cwd: str, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout
