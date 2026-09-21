"""AUD07-05: the CLI must never print a raw traceback on corrupted data.

MCP degrades to ``{"status": "error", "error": "..."}`` via ``_exec``;
the CLI used to leak the full stack trace to a human. The dispatcher now
wraps every subcommand in a unified handler that mirrors ``_exec``.
"""
from __future__ import annotations

import subprocess


class TestCliNoTraceback:
    """Each audited repro: rc == 1 (or clean 0), no 'Traceback' anywhere."""

    def _run(self, awf_bin: str, awf_env: dict, proj, args: list, input_data: bytes = b""):
        return subprocess.run(
            [awf_bin, *args, "--project-dir", str(proj)],
            cwd=proj,
            env=awf_env,
            input=input_data.decode("utf-8", errors="replace") if input_data else "",
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_rollback_bad_baseline_sha(self, awf_bin, awf_env, initialized_project):
        """Repro 1: garbage BASELINE-*.sha → CalledProcessError from
        `git reset --hard <junk>` — must become a clean ERROR line."""
        ctx = initialized_project / ".agentic" / "context"
        ctx.mkdir(parents=True, exist_ok=True)
        (ctx / "BASELINE-TODO-0001.sha").write_text(
            "0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8"
        )
        r = self._run(awf_bin, awf_env, initialized_project, ["rollback", "TODO-0001"])
        assert r.returncode == 1
        assert "Traceback" not in r.stderr
        assert "ERROR" in r.stderr

    def test_add_role_eof_on_stdin(self, awf_bin, awf_env, initialized_project):
        """Repro 2: `awf add-role tester < /dev/null` without --model →
        EOFError from input() — must become a clean ERROR line."""
        r = self._run(
            awf_bin, awf_env, initialized_project, ["add-role", "tester"],
            input_data=b"",
        )
        assert r.returncode == 1
        assert "Traceback" not in r.stderr
        assert "ERROR" in r.stderr

    def test_analyze_roles_non_utf8_pipeline(self, awf_bin, awf_env, initialized_project):
        """Repro 3: non-UTF-8 pipelines/default.yaml — must degrade (loader
        prints ERROR, analysis continues without pipeline order), no trace."""
        pipe = initialized_project / ".agentic" / "pipelines" / "default.yaml"
        pipe.write_bytes(b"\xff\xfe\x00stages: [plan]")
        r = self._run(awf_bin, awf_env, initialized_project, ["analyze-roles"])
        assert "Traceback" not in r.stderr
        assert "Traceback" not in r.stdout

    def test_status_in_non_agentic_dir(self, awf_bin, awf_env, tmp_path):
        """AwfApiError path: already clean, but must stay 'ERROR' + rc 1
        (no traceback) — pins the handler's first branch."""
        empty = tmp_path / "empty"
        empty.mkdir()
        r = self._run(awf_bin, awf_env, empty, ["status"])
        assert r.returncode == 1
        assert "Traceback" not in r.stderr
