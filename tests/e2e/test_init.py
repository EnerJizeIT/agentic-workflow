"""E2E tests for `awf init` — BD-28: simplified skeleton (no template arg)."""
import subprocess
from pathlib import Path

from conftest import run_awf


class TestInit:

    def test_init_creates_skeleton(self, empty_project: Path, awf_bin: str, awf_env: dict):
        """awf init creates bare .agentic/ skeleton (BD-28).

        No pipeline.yaml, no worker/reviewer/tester roles — UI form
        configures those on first run. Only supervisor.md (used by current
        opencode session) + config.yaml + directories + plan.md stub.
        """
        init_input = b"test-project\npytest\nruff\nmypy\n\nclaude-test-model\nn\nn\n"
        result = run_awf(awf_bin, ["init"],
                         cwd=empty_project, env=awf_env, input_data=init_input)
        assert result.returncode == 0, f"awf init failed: {result.stderr.decode()}"

        # Required files (BD-28: only supervisor + config + plan stub)
        assert (empty_project / ".agentic/config.yaml").exists()
        assert (empty_project / ".agentic/roles/supervisor.md").exists()
        assert (empty_project / ".agentic/phases/plan.md").exists()

        # BD-28: these are NO LONGER created by init (UI form does it)
        assert not (empty_project / ".agentic/roles/worker.md").exists()
        assert not (empty_project / ".agentic/pipelines/default.yaml").exists()

        # Required directories
        for d in ("pipelines", "phases", "inbox", "outbox", "context", "logs"):
            assert (empty_project / ".agentic" / d).is_dir(), f"missing dir: {d}"

        # Config content — BD-29: only supervisor in models (agent roles added by form)
        cfg = (empty_project / ".agentic/config.yaml").read_text()
        assert 'name:' in cfg and 'test-project' in cfg in cfg
        # Worker model no longer in init template — added dynamically by form
        assert "supervisor" in cfg

    def test_init_gitignore_correct(self, empty_project: Path, awf_bin: str, awf_env: dict):
        """awf init creates .gitignore with correct entries and git respects them."""
        init_input = b"test-project\npytest\nruff\nmypy\n\nclaude-test-model\nn\nn\n"
        run_awf(awf_bin, ["init"],
                cwd=empty_project, env=awf_env, input_data=init_input)

        gitignore = (empty_project / ".gitignore").read_text()
        assert ".agentic/inbox/" in gitignore
        assert ".agentic/outbox/" in gitignore
        assert ".agentic/context/" in gitignore
        assert ".agentic/logs/" in gitignore
        # AUD16-08: reports/ is no longer a runtime dir — not in gitignore
        assert ".agentic/reports/" not in gitignore
        # agent-workflow-ui runtime dirs (added when plugin is installed)
        assert ".agentic/inputs/" in gitignore
        assert ".agentic/dashboards/" in gitignore

        # Create a file in inbox and verify git doesn't see it
        (empty_project / ".agentic/inbox/test-file.md").write_text("test")
        git_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=empty_project, capture_output=True, text=True
        )
        assert ".agentic/inbox/" not in git_status.stdout

    def test_init_reuses_opencode_model(self, empty_project: Path, awf_bin: str, awf_env: dict, tmp_path: Path):
        """When an existing opencode agent is present, init completes.

        BD-29: model no longer baked into CONFIG_TEMPLATE at init time
        (per-role models come from the project-setup form). Verifying init
        completes successfully.
        """
        oc_dir = tmp_path / "fake_home" / ".config" / "opencode"
        (oc_dir / "opencode.json").write_text(
            '{"agent": {"existing-bot": {"model": "claude-sonnet-4-20250514"}}}\n'
        )

        init_input = b"test-project\npytest\nruff\nmypy\n\n\nn\nn\n"
        result = run_awf(awf_bin, ["init"],
                         cwd=empty_project, env=awf_env, input_data=init_input)
        assert result.returncode == 0, f"awf init failed: {result.stderr.decode()}"

        # BD-29: model not stored in init config.yaml anymore — just verify init ran
        cfg = (empty_project / ".agentic/config.yaml").read_text()
        assert 'name:' in cfg and 'test-project' in cfg in cfg

    def test_init_rejects_template_flag_bd28(self, empty_project: Path, awf_bin: str, awf_env: dict):
        """BD-28: --template flag is no longer accepted (was removed)."""
        result = run_awf(awf_bin, ["init", "--template", "simple"],
                         cwd=empty_project, env=awf_env, input_data=b"")
        # argparse should reject unknown arg
        assert result.returncode != 0
