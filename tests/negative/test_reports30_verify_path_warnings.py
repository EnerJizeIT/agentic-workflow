"""REPORTS30 (TODO-0186): verify-path validation at dispatch.

Incident (09.10 supervisor backlog, 4 cases in a week): a contract's
verify: command referenced a file that does not exist, so the contract
gate went red mid-unit and the unit needed a manual re-plan. The dispatch
must catch this the moment the unit is issued — as a WARNING, never a
refusal (the heuristic has no right to block a dispatch).

Invariant:
- a verify: command that references a missing path → the result carries
  ``verify_path_warnings`` with that token, and the dispatch still
  succeeds (the TODO is created);
- every referenced path exists → ``verify_path_warnings`` is empty;
- a command with no path-like token (``bash echo hi``) → empty;
- no false positives: quoted flag values (``-k "stack or init"``), URLs,
  shell variables and value-taking-flag arguments are never flagged.

Red on the baseline (the result has no ``verify_path_warnings`` field and
the helper functions do not exist); green after the fix. The helper
functions are imported inside the tests that use them so the module still
collects on the baseline. The dispatch (integration) tests run LAST on
purpose: prove-red classifies the baseline run from the tail of the
output, and the red must be the missing-field ``AttributeError`` (a real
red), not the helpers' ``ImportError`` (a missing symbol).
"""
from __future__ import annotations

from awf import api, paths


def _todo(verify_cmds: list[str]) -> str:
    """Build a TODO whose contract block declares verify: verify_cmds."""
    body = "\n".join(f"  - {c}" for c in verify_cmds)
    return f"---\nverify:\n{body}\n---\n# Task\n\nbody\n"


def _dispatch(project, verify_cmds: list[str]):
    api.init_project(project, project_name="VPW")
    return api.dispatch_todo(project, _todo(verify_cmds))


class TestVerifyPathTokenHeuristic:
    """Unit: the token-extraction heuristic (no dispatch, fast)."""

    def test_extracts_path_with_separator(self):
        from awf.api.dispatch import _verify_path_tokens

        assert _verify_path_tokens(["pytest tests/a.py"]) == ["tests/a.py"]

    def test_extracts_bare_extension_token(self):
        # no '/' but a known extension → still a path candidate
        from awf.api.dispatch import _verify_path_tokens

        assert _verify_path_tokens(["pytest script.sh"]) == ["script.sh"]

    def test_ignores_flags_and_flag_values(self):
        from awf.api.dispatch import _verify_path_tokens

        toks = _verify_path_tokens(
            ['python3 -m pytest tests/a.py -q --timeout=600 -k "x or y" -m slow']
        )
        assert toks == ["tests/a.py"]

    def test_ignores_urls(self):
        from awf.api.dispatch import _verify_path_tokens

        assert _verify_path_tokens(["curl https://example.com/a.py"]) == []

    def test_ignores_shell_variables(self):
        from awf.api.dispatch import _verify_path_tokens

        assert _verify_path_tokens(["bash $SCRIPT/check.sh"]) == []

    def test_ignores_flag_argument_even_if_pathlike(self):
        # -c FILE is a value-taking flag: the path-like FILE is its argument,
        # not a positional path → only the real test path is extracted.
        from awf.api.dispatch import _verify_path_tokens

        assert _verify_path_tokens(
            ["pytest -c tests/conf.yaml tests/a.py"]
        ) == ["tests/a.py"]

    def test_dedupes_and_keeps_order(self):
        from awf.api.dispatch import _verify_path_tokens

        assert _verify_path_tokens(
            ["pytest a/b.py", "pytest a/b.py c/d.sh"]
        ) == ["a/b.py", "c/d.sh"]

    def test_broken_quotes_do_not_raise(self):
        from awf.api.dispatch import _verify_path_tokens

        toks = _verify_path_tokens(['pytest tests/a.py -k "unclosed'])
        assert "tests/a.py" in toks

    def test_multiple_missing_paths_all_reported(self):
        from awf.api.dispatch import _verify_path_tokens

        toks = _verify_path_tokens(
            ["pytest one/x.py", "pytest two/y.sh three/z.md"]
        )
        assert toks == ["one/x.py", "two/y.sh", "three/z.md"]


class TestMissingVerifyPaths:
    """Unit: existence check relative to the project root."""

    def test_reports_only_missing(self, tmp_path):
        from awf.api.dispatch import _missing_verify_paths

        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "a.py").write_text("x")
        assert _missing_verify_paths(tmp_path, ["tests/a.py", "tests/b.py"]) == [
            "tests/b.py"
        ]

    def test_directory_reference_counts_as_existing(self, tmp_path):
        from awf.api.dispatch import _missing_verify_paths

        (tmp_path / "tests").mkdir()
        assert _missing_verify_paths(tmp_path, ["tests"]) == []

    def test_absolute_path_uses_its_own_root(self, tmp_path):
        # an absolute token is not joined onto the project root
        from awf.api.dispatch import _missing_verify_paths

        assert _missing_verify_paths(tmp_path, ["/no/such/abs.py"]) == [
            "/no/such/abs.py"
        ]


class TestVerifyPathWarningsDispatch:
    """End-to-end: dispatch_todo surfaces missing verify paths as a warning.

    Runs LAST so its missing-field ``AttributeError`` is in the tail of the
    baseline output that prove-red classifies from.
    """

    def test_no_contract_block_still_dispatches(self, tmp_git_repo):
        """A TODO without a contract block has no verify: → the field is []
        and nothing changes for the existing no-block path."""
        api.init_project(tmp_git_repo, project_name="VPWNoBlock")
        result = api.dispatch_todo(tmp_git_repo, "# Task\n\nbody\n")
        assert result.verify_path_warnings == []

    def test_command_without_paths_empty(self, tmp_git_repo):
        """(c) a command with no path-like token → []."""
        result = _dispatch(tmp_git_repo, ["bash echo hi"])
        assert result.verify_path_warnings == []

    def test_missing_path_warns_and_dispatch_succeeds(self, tmp_git_repo):
        """(a) a verify: command referencing a missing file → the warning
        names the token and the TODO is still created."""
        result = _dispatch(
            tmp_git_repo,
            ["python3 -m pytest tests/no_such_file.py -q --timeout=600"],
        )
        assert result.todo_id, "the dispatch must succeed despite the warning"
        assert (paths.inbox(tmp_git_repo) / f"{result.todo_id}.md").is_file()
        assert result.verify_path_warnings == ["tests/no_such_file.py"]
        assert any("no_such_file" in w for w in result.pre_check_warnings)

    def test_all_paths_exist_empty_warnings(self, tmp_git_repo):
        """(b) every referenced path exists → verify_path_warnings is []."""
        tests = tmp_git_repo / "tests"
        tests.mkdir()
        (tests / "real_test.py").write_text("def test_x(): pass\n")
        result = _dispatch(
            tmp_git_repo,
            ["python3 -m pytest tests/real_test.py -q --timeout=600"],
        )
        assert result.verify_path_warnings == []

    def test_no_false_positives_on_flags_and_quotes(self, tmp_git_repo):
        """(d) quoted flag values and flags are never flagged; when the real
        (existing) path is the only path, the warnings are empty."""
        tests = tmp_git_repo / "tests"
        tests.mkdir()
        (tests / "real_test.py").write_text("def test_x(): pass\n")
        result = _dispatch(
            tmp_git_repo,
            ['python3 -m pytest tests/real_test.py -q -k "stack or init" -m slow'],
        )
        assert result.verify_path_warnings == []

    def test_flags_do_not_shadow_a_missing_path(self, tmp_git_repo):
        """(d, negative side) flags/quotes are ignored, but the real missing
        test path is still reported — exactly once."""
        result = _dispatch(
            tmp_git_repo,
            ['python3 -m pytest tests/gone.py -q -k "stack or init" -m slow'],
        )
        assert result.verify_path_warnings == ["tests/gone.py"]
