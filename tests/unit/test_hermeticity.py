"""U7a: hermeticity guard — the suite must not depend on the user's git config.

Regression for the 2026-09-20 incident: a corporate ~/.gitconfig with
commit.gpgsign=true hung full pytest runs on pinentry (gpg prompt).
conftest pins GIT_CONFIG_GLOBAL=/dev/null, GIT_CONFIG_NOSYSTEM=1 and a
fixed test identity. These tests fail if that block is removed from
tests/conftest.py.
"""
import subprocess


def _git(args, cwd=None):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )


class TestHermeticGit:
    def test_global_config_does_not_leak_gpgsign(self):
        """`git config --global --list` must not expose the user's
        commit.gpgsign=true to the test suite."""
        r = _git(["config", "--global", "--list"])
        # rc 0 = empty config (hermetic /dev/null), rc 1 = config file
        # missing. Both are acceptable; anything else is a git error.
        assert r.returncode in (0, 1), r.stderr
        gpgsign = [
            line for line in r.stdout.splitlines()
            if "=" in line
            and line.split("=", 1)[0].strip() == "commit.gpgsign"
            and line.split("=", 1)[1].strip().lower() == "true"
        ]
        assert not gpgsign, (
            f"user's commit.gpgsign=true leaked into the test env: {gpgsign}"
        )

    def test_fresh_repo_commit_is_not_signed(self, tmp_path):
        """A commit in a fresh repo (no local config) must NOT be gpg-signed.

        %G? == N (unsigned). With a leaked gpgsign=true the commit would
        fail (no key) or be signed — both break hermetic test repos.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        r = _git(["init", "-q"], cwd=repo)
        assert r.returncode == 0, r.stderr
        (repo / "f.txt").write_text("x")
        assert _git(["add", "-A"], cwd=repo).returncode == 0
        r = _git(["commit", "-qm", "hermeticity"], cwd=repo)
        assert r.returncode == 0, (
            "commit failed without local config — env identity (U7a) missing:\n"
            + r.stderr.strip()
        )
        r = _git(["log", "--format=%G?", "-1"], cwd=repo)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "N", (
            f"commit is gpg-signed (%G?={r.stdout.strip()!r}) — "
            "corporate gpgsign leaked into the test environment"
        )

    def test_commit_identity_comes_from_env(self, tmp_path):
        """Commit identity must be the fixed test identity from env, not
        from any user config."""
        repo = tmp_path / "repo"
        repo.mkdir()
        assert _git(["init", "-q"], cwd=repo).returncode == 0
        (repo / "f.txt").write_text("x")
        assert _git(["add", "-A"], cwd=repo).returncode == 0
        r = _git(["commit", "-qm", "identity"], cwd=repo)
        assert r.returncode == 0, r.stderr
        r = _git(["log", "--format=%an <%ae> %cn <%ce>", "-1"], cwd=repo)
        assert r.stdout.strip() == (
            "awf-test <test@example.invalid> awf-test <test@example.invalid>"
        )
