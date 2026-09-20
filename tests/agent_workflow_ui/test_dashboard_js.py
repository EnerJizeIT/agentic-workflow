"""AUD12-06: dashboard JS tests — node --test + DOM stub (OWNER-DECISIONS).

The dashboard's ~300 lines of inline JS lived without tests. This wrapper
extracts the <script> block from the RENDERED Jinja template (the template
stays the single source of truth — no JS copy to drift), drops it next to
the static test file tests/js/dashboard_js_test.js and runs `node --test`.

Skips with an honest message when node is unavailable in CI.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None,
    reason="node is not available in this environment — JS harness skipped",
)

# tests/agent_workflow_ui/test_dashboard_js.py → parents[1] = tests/
_JS_TEST_FILE = Path(__file__).resolve().parents[1] / "js" / "dashboard_js_test.js"

_EXPORT_FOOTER = (
    "module.exports = { updateAll, updateTopbar, updateSidebar, updateChat,"
    " updateTodo, updateEvents, updateElapsed, tickElapsed, switchTab,"
    " checkNotification, escapeHtml, fmtElapsed };"
)


def _extract_dashboard_js() -> str:
    """The inline <script> of the rendered dashboard (INITIAL_STATE = {})."""
    from awf.api.dashboard import _get_template

    html = _get_template().render(
        project_name="T",
        todo_id="",
        status="idle",
        status_text="",
        elapsed="",
        elapsed_epoch=0,
        elapsed_frozen=False,
        initial_state_json="{}",
    )
    m = re.search(r"<script>(.*)</script>", html, re.DOTALL)
    assert m, "rendered dashboard has no <script> block"
    return m.group(1)


def test_dashboard_js_suite(tmp_path):
    assert _JS_TEST_FILE.is_file(), f"missing static JS test: {_JS_TEST_FILE}"
    js = _extract_dashboard_js() + "\n" + _EXPORT_FOOTER + "\n"
    (tmp_path / "dashboard.js").write_text(js, encoding="utf-8")
    shutil.copy(_JS_TEST_FILE, tmp_path / "dashboard.test.js")

    proc = subprocess.run(
        [NODE, "--test", "--test-reporter=spec", "dashboard.test.js"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        "node --test failed for the dashboard JS:\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
