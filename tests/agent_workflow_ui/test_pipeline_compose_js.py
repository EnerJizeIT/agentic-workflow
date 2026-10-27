"""PA2 (closer IMP7): pipeline-compose form JS — node --test + DOM stub.

The form's ``collectStages``/``buildYamlPreview`` used to drop the stage
keys ``skip_if``, ``write_check``, ``write_allow`` and ``tools`` on
re-save. Grepping the rendered HTML cannot prove the values survive; this
wrapper extracts the inline ``<script>`` of the RENDERED Jinja template
(the template stays the single source of truth — no JS copy to drift) and
runs ``tests/js/pipeline_compose_js_test.js`` with node.

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

_JS_TEST_FILE = (
    Path(__file__).resolve().parents[1] / "js" / "pipeline_compose_js_test.js"
)

# The compose script is an IIFE; the test driver needs its functions. One
# export hook before the closing call — no second copy of the logic.
_EXPORT_HOOK = (
    "\n      module.exports = {"
    " collectStages: collectStages,"
    " buildYamlPreview: buildYamlPreview,"
    " validate: validate };\n    })();"
)


def _extract_compose_js() -> str:
    """The inline ``<script>`` of the rendered compose form, exported."""
    from agent_workflow_ui.render.engine import create_default_env, render_template

    env = create_default_env()
    html = render_template(
        env,
        "pipeline-compose",
        {
            "form_id": "FORM-test",
            "submit_url": "http://127.0.0.1:1/submit/FORM-test",
            "pipeline_names": ["default"],
            "active_pipeline": "default",
            "initial_pipeline": "default",
            "initial_stages": [],
            "existing_pipeline": False,
            "project_roles": [],
            "policy_options": {},
            "pipelines_dir": "/proj/.agentic/pipelines",
        },
    )
    m = re.search(r"<script>(.*)</script>", html, re.DOTALL)
    assert m, "rendered compose form has no <script> block"
    js = m.group(1).rstrip()
    assert js.endswith("})();"), (
        "the compose script is no longer an IIFE — update the export hook"
    )
    return js[: -len("})();")] + _EXPORT_HOOK


def test_pipeline_compose_js_suite(tmp_path):
    assert _JS_TEST_FILE.is_file(), f"missing static JS test: {_JS_TEST_FILE}"
    (tmp_path / "pipeline_compose.js").write_text(
        _extract_compose_js(), encoding="utf-8"
    )
    shutil.copy(_JS_TEST_FILE, tmp_path / "pipeline_compose.test.js")

    proc = subprocess.run(
        [NODE, "--test", "--test-reporter=spec", "pipeline_compose.test.js"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        "node --test failed for the pipeline-compose JS:\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
