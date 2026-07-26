"""E2E tests for `awf init` — structure, templates, gitignore, model reuse."""
import subprocess
from pathlib import Path

from conftest import run_awf


class TestInit:

    def test_init_creates_structure(self, empty_project: Path, awf_bin: str, awf_env: dict):
        """awf init --template simple creates all expected files and directories."""
        init_input = b"test-project\npytest\nruff\nmypy\n\nclaude-test-model\nn\nn\n"
        result = run_awf(awf_bin, ["init", "--template", "simple"],
                         cwd=empty_project, env=awf_env, input_data=init_input)
        assert result.returncode == 0, f"awf init failed: {result.stderr.decode()}"

        # Required files
        assert (empty_project / ".agentic/config.yaml").exists()
        assert (empty_project / ".agentic/roles/supervisor.md").exists()
        assert (empty_project / ".agentic/roles/worker.md").exists()
        assert (empty_project / ".agentic/pipelines/default.yaml").exists()

        # Required directories
        assert (empty_project / ".agentic/phases").is_dir()
        assert (empty_project / ".agentic/inbox").is_dir()
        assert (empty_project / ".agentic/outbox").is_dir()
        assert (empty_project / ".agentic/context").is_dir()
        assert (empty_project / ".agentic/logs").is_dir()
        assert (empty_project / ".agentic/reports").is_dir()

        # Config content
        cfg = (empty_project / ".agentic/config.yaml").read_text()
        assert 'name: "test-project"' in cfg
        assert 'model: "claude-test-model"' in cfg

    def test_init_full_template(self, empty_project: Path, awf_bin: str, awf_env: dict):
        """awf init --template full creates reviewer and tester roles."""
        init_input = b"test-project\npytest\nruff\nmypy\n\nclaude-test-model\nn\nn\n"
        result = run_awf(awf_bin, ["init", "--template", "full"],
                         cwd=empty_project, env=awf_env, input_data=init_input)
        assert result.returncode == 0, f"awf init failed: {result.stderr.decode()}"

        assert (empty_project / ".agentic/roles/reviewer.md").exists()
        assert (empty_project / ".agentic/roles/tester.md").exists()
        # Also has the base roles
        assert (empty_project / ".agentic/roles/supervisor.md").exists()
        assert (empty_project / ".agentic/roles/worker.md").exists()

    def test_init_gitignore_correct(self, empty_project: Path, awf_bin: str, awf_env: dict):
        """awf init creates .gitignore with correct entries and git respects them."""
        init_input = b"test-project\npytest\nruff\nmypy\n\nclaude-test-model\nn\nn\n"
        run_awf(awf_bin, ["init", "--template", "simple"],
                cwd=empty_project, env=awf_env, input_data=init_input)

        gitignore = (empty_project / ".gitignore").read_text()
        assert ".agentic/inbox/" in gitignore
        assert ".agentic/outbox/" in gitignore
        assert ".agentic/context/" in gitignore
        assert ".agentic/logs/" in gitignore
        assert ".agentic/reports/" in gitignore

        # Create a file in inbox and verify git doesn't see it
        (empty_project / ".agentic/inbox/test-file.md").write_text("test")
        git_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=empty_project, capture_output=True, text=True
        )
        assert ".agentic/inbox/" not in git_status.stdout

    def test_init_reuses_opencode_model(self, empty_project: Path, awf_bin: str, awf_env: dict, tmp_path: Path):
        """When an existing opencode agent is present, init reuses its model."""
        # Override the opencode.json to have an existing agent with a specific model
        oc_dir = tmp_path / "fake_home" / ".config" / "opencode"
        (oc_dir / "opencode.json").write_text(
            '{"agent": {"existing-bot": {"model": "claude-sonnet-4-20250514"}}}\n'
        )

        # Answer model prompt with empty input to accept the default
        init_input = b"test-project\npytest\nruff\nmypy\n\n\nn\nn\n"
        result = run_awf(awf_bin, ["init", "--template", "simple"],
                         cwd=empty_project, env=awf_env, input_data=init_input)
        assert result.returncode == 0, f"awf init failed: {result.stderr.decode()}"

        cfg = (empty_project / ".agentic/config.yaml").read_text()
        assert 'model: "claude-sonnet-4-20250514"' in cfg
