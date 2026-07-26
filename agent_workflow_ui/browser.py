"""Cross-platform browser open via stdlib subprocess."""
from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path


def detect_open_command(configured: str = "auto") -> str:
    """Detect the platform-appropriate command to open a file in the browser.

    Args:
        configured: "auto" for platform detection, or an explicit command name.

    Returns:
        Command string: "xdg-open" (Linux), "open" (macOS), "explorer" (Windows).
    """
    if configured != "auto":
        return configured

    system = platform.system()
    if system == "Linux":
        return "xdg-open"
    if system == "Darwin":
        return "open"
    if system == "Windows":
        return "explorer"
    # Fallback
    return "xdg-open"


def open_path(target: Path | str, command: str = "auto") -> tuple[bool, str]:
    """Open a file path or URL in the default browser.

    Fire-and-forget: does not block the calling process beyond a short timeout.
    stdout/stderr are captured so they don't interfere with MCP stdio transport.

    Args:
        target: File path or URL to open.
        command: "auto" (detect by platform), or explicit command name.

    Returns:
        Tuple (success, message). On success: (True, "opened via <cmd>").
        On failure: (False, error description).
    """
    cmd = detect_open_command(command)

    if shutil.which(cmd) is None:
        return False, f"Browser open command '{cmd}' not found on PATH"

    try:
        result = subprocess.run(
            [cmd, str(target)],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            return True, f"opened via {cmd}"

        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        return (
            False,
            f"'{cmd}' exited with code {result.returncode}: {stderr or 'unknown error'}",
        )
    except subprocess.TimeoutExpired:
        return False, f"'{cmd}' timed out after 5s"
    except FileNotFoundError:
        return False, f"Browser open command '{cmd}' not found"
