"""U3: unit contract — optional TODO front-matter validated on dispatch +
DONE.json folded into the handoff.

Red-first behavior: before the fix, ``dispatch_todo`` accepted any content
(broken contract block or not) and ``collect_handoff`` ignored DONE.json
entirely.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from awf import api, paths
from awf.agent_stage import collect_handoff
from awf.unit_contract import parse_done_json, parse_todo_contract

# ─── parse_todo_contract: pure validation ──────────────────────────────

class TestParseTodoContract:
    def test_no_block_returns_none(self):
        contract, unknown = parse_todo_contract("# TODO-0001 — task\n\nbody\n")
        assert contract is None
        assert unknown == []

    def test_block_after_role_hint_comment(self):
        content = (
            "<!-- role_hint: agent-implementer -->\n"
            "---\n"
            'verify: ["pytest tests/ -q"]\n'
            "---\n"
            "# Task\n"
        )
        contract, unknown = parse_todo_contract(content)
        assert contract == {"verify": ["pytest tests/ -q"]}
        assert unknown == []

    def test_valid_block_all_keys(self):
        content = (
            "---\n"
            'verify: ["python3 -m pytest tests/unit/test_x.py -q"]\n'
            'gates: ["contracts", "ratchet"]\n'
            'prove_red: ["tests/unit/test_x.py::test_y"]\n'
            'files: ["awf/x.py", "tests/unit/test_x.py"]\n'
            "---\n"
            "# Task\n"
        )
        contract, unknown = parse_todo_contract(content)
        assert contract["verify"] == ["python3 -m pytest tests/unit/test_x.py -q"]
        assert contract["gates"] == ["contracts", "ratchet"]
        assert contract["prove_red"] == ["tests/unit/test_x.py::test_y"]
        assert contract["files"] == ["awf/x.py", "tests/unit/test_x.py"]
        assert unknown == []

    def test_block_only_with_prove_red(self):
        content = '---\nprove_red: ["tests/unit/test_x.py::test_y"]\n---\nbody\n'
        contract, _ = parse_todo_contract(content)
        assert contract == {"prove_red": ["tests/unit/test_x.py::test_y"]}

    def test_empty_block_is_ok(self):
        contract, unknown = parse_todo_contract("---\n---\nbody\n")
        assert contract == {}
        assert unknown == []

    def test_broken_yaml_raises(self):
        content = '---\nverify: ["unclosed\n---\nbody\n'
        try:
            parse_todo_contract(content)
            raise AssertionError("expected ValueError for broken YAML")
        except ValueError as e:
            assert "YAML" in str(e)

    def test_unterminated_block_raises(self):
        content = '---\nverify: ["pytest -q"]\n# no closing dash line\n'
        try:
            parse_todo_contract(content)
            raise AssertionError("expected ValueError for unterminated block")
        except ValueError as e:
            assert "closing" in str(e)

    def test_block_not_a_mapping_raises(self):
        content = "---\n- just\n- a\n- list\n---\nbody\n"
        try:
            parse_todo_contract(content)
            raise AssertionError("expected ValueError for non-mapping block")
        except ValueError as e:
            assert "mapping" in str(e)

    def test_verify_not_a_list_raises(self):
        content = '---\nverify: "pytest -q"\n---\nbody\n'
        try:
            parse_todo_contract(content)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "'verify' must be a list" in str(e)

    def test_verify_empty_list_raises(self):
        content = '---\nverify: []\n---\nbody\n'
        try:
            parse_todo_contract(content)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "empty" in str(e)

    def test_verify_empty_string_raises(self):
        content = '---\nverify: ["   "]\n---\nbody\n'
        try:
            parse_todo_contract(content)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "non-empty string" in str(e)

    def test_verify_non_string_item_raises(self):
        content = '---\nverify: [42]\n---\nbody\n'
        try:
            parse_todo_contract(content)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "non-empty string" in str(e)

    def test_gates_empty_list_raises(self):
        content = '---\ngates: []\n---\nbody\n'
        try:
            parse_todo_contract(content)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "empty" in str(e)

    def test_unknown_gate_raises_with_allowed_list(self):
        content = '---\ngates: ["nope"]\n---\nbody\n'
        try:
            parse_todo_contract(content)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "unknown gate 'nope'" in str(e)
            assert "contracts" in str(e) and "mutations" in str(e)

    def test_prove_red_not_a_list_raises(self):
        content = '---\nprove_red: "tests/test_x.py::test_y"\n---\nbody\n'
        try:
            parse_todo_contract(content)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "'prove_red' must be a list" in str(e)

    def test_prove_red_empty_list_raises(self):
        content = '---\nprove_red: []\n---\nbody\n'
        try:
            parse_todo_contract(content)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "empty" in str(e)

    def test_files_valid_list_is_a_contract_key(self):
        # FU-21 D2: verify-pack already cross-checks the diff against
        # `files`, so the key must be first-class — no unknown-key warning,
        # no missing validation.
        content = '---\nfiles: ["awf/x.py", "tests/unit/test_x.py"]\n---\nbody\n'
        contract, unknown = parse_todo_contract(content)
        assert contract["files"] == ["awf/x.py", "tests/unit/test_x.py"]
        assert unknown == []

    def test_files_not_a_list_raises(self):
        content = '---\nfiles: "awf/x.py"\n---\nbody\n'
        try:
            parse_todo_contract(content)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "'files' must be a list" in str(e)

    def test_files_empty_list_raises(self):
        content = '---\nfiles: []\n---\nbody\n'
        try:
            parse_todo_contract(content)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "empty" in str(e)

    def test_files_non_string_item_raises(self):
        content = '---\nfiles: ["awf/x.py", 42]\n---\nbody\n'
        try:
            parse_todo_contract(content)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "non-empty string" in str(e)

    def test_unknown_key_is_warning_not_error(self):
        content = '---\nverify: ["pytest -q"]\nverfiy: ["typo"]\n---\nbody\n'
        contract, unknown = parse_todo_contract(content)
        assert contract["verify"] == ["pytest -q"]
        assert unknown == ["verfiy"]

    def test_horizontal_rule_mid_file_is_not_a_block(self):
        content = "# Task\n\ntext\n\n---\n\ntext after rule\n"
        contract, unknown = parse_todo_contract(content)
        assert contract is None
        assert unknown == []


# ─── dispatch integration ───────────────────────────────────────────────

VALID_BLOCK_TODO = (
    "---\n"
    'verify: ["python3 -m pytest tests/unit/test_x.py -q"]\n'
    'gates: ["contracts", "ratchet"]\n'
    'prove_red: ["tests/unit/test_x.py::test_y"]\n'
    "---\n"
    "# Task\n\nbody\n"
)


class TestDispatchContractValidation:
    def test_todo_without_block_dispatches_as_before(self, tmp_git_repo):
        api.init_project(tmp_git_repo, project_name="CNoBlock")
        result = api.dispatch_todo(tmp_git_repo, "# Task\n\nbody\n")
        assert result.todo_id == "TODO-0001"
        md = paths.inbox(tmp_git_repo) / "TODO-0001.md"
        assert md.is_file()
        assert "# Task" in md.read_text(encoding="utf-8")

    def test_valid_block_dispatches_and_kept_in_file(self, tmp_git_repo):
        api.init_project(tmp_git_repo, project_name="CValid")
        result = api.dispatch_todo(tmp_git_repo, VALID_BLOCK_TODO)
        assert result.todo_id == "TODO-0001"
        md = paths.inbox(tmp_git_repo) / "TODO-0001.md"
        text = md.read_text(encoding="utf-8")
        assert text.startswith("---")
        assert "verify:" in text

    def test_broken_block_raises_and_writes_nothing(self, tmp_git_repo):
        api.init_project(tmp_git_repo, project_name="CBroken")
        try:
            api.dispatch_todo(tmp_git_repo, "---\nverify: \"not a list\"\n---\nbody\n")
            raise AssertionError("expected AwfApiError for broken contract block")
        except api.AwfApiError as e:
            assert "contract" in str(e)
        assert not (paths.inbox(tmp_git_repo) / "TODO-0001.md").exists()

    def test_unknown_gate_raises_with_allowed_names(self, tmp_git_repo):
        api.init_project(tmp_git_repo, project_name="CUnknownGate")
        try:
            api.dispatch_todo(tmp_git_repo, '---\ngates: ["nope"]\n---\nbody\n')
            raise AssertionError("expected AwfApiError")
        except api.AwfApiError as e:
            assert "unknown gate" in str(e)
            assert "contracts" in str(e)

    def test_empty_verify_raises(self, tmp_git_repo):
        api.init_project(tmp_git_repo, project_name="CEmptyVerify")
        try:
            api.dispatch_todo(tmp_git_repo, '---\nverify: []\n---\nbody\n')
            raise AssertionError("expected AwfApiError")
        except api.AwfApiError as e:
            assert "verify" in str(e)

    def test_unknown_key_warns_not_raises(self, tmp_git_repo):
        api.init_project(tmp_git_repo, project_name="CUnknownKey")
        result = api.dispatch_todo(
            tmp_git_repo,
            '---\nverify: ["pytest -q"]\nverfiy: ["typo"]\n---\nbody\n',
        )
        assert result.todo_id == "TODO-0001"
        assert any("verfiy" in w for w in result.pre_check_warnings)

    def test_role_hint_before_block_still_validates(self, tmp_git_repo):
        """awf prepends `<!-- role_hint: ... -->` — the block must be found after it."""
        api.init_project(tmp_git_repo, project_name="CHint")
        try:
            api.dispatch_todo(
                tmp_git_repo,
                '---\ngates: ["nope"]\n---\nbody\n',
                role="agent-implementer",
            )
            raise AssertionError("expected AwfApiError despite role hint prefix")
        except api.AwfApiError as e:
            assert "unknown gate" in str(e)


# ─── DONE.json in the handoff ───────────────────────────────────────────

def _project(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".agentic" / "outbox").mkdir(parents=True)
    (proj / ".agentic" / "context").mkdir(parents=True)
    (proj / ".agentic" / "logs").mkdir(parents=True)
    return proj


def _stub_git(monkeypatch, changed_files: list[str], stat: str = ""):
    def fake_run(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[:2] == ["git", "diff"]:
            if "--name-only" in cmd:
                return SimpleNamespace(stdout="\n".join(changed_files) + "\n")
            return SimpleNamespace(stdout=stat)
        return None

    # the shared subprocess module object — same trick as test_handoff_v2
    monkeypatch.setattr("awf.signal_watch.subprocess.run", fake_run)


VALID_DONE_JSON = {
    "files_changed": ["awf/unit_contract.py", "tests/unit/test_unit_contract.py"],
    "tests_run": [
        {"cmd": "python3 -m pytest tests/unit/test_unit_contract.py -q",
         "result": "24 passed"},
    ],
    "gates": ["contracts", "ratchet"],
    "notes": "U3 wired: dispatch validates, handoff folds DONE.json",
}


class TestDoneJsonInHandoff:
    def test_valid_json_section_in_handoff(self, tmp_path, monkeypatch):
        proj = _project(tmp_path)
        _stub_git(monkeypatch, [])
        (proj / ".agentic" / "outbox" / "DONE-TODO-0002.json").write_text(
            json.dumps(VALID_DONE_JSON), encoding="utf-8"
        )

        out = collect_handoff("worker", "TODO-0002", proj, proj / ".agentic" / "logs")
        body = out.read_text(encoding="utf-8")

        assert "## Machine facts (DONE.json)" in body
        assert "`awf/unit_contract.py`" in body
        assert "24 passed" in body
        assert "python3 -m pytest tests/unit/test_unit_contract.py -q" in body
        assert "- gates: contracts, ratchet" in body
        assert "handoff folds DONE.json" in body
        assert "DONE-json=present" in body

    def test_short_id_form_also_found(self, tmp_path, monkeypatch):
        proj = _project(tmp_path)
        _stub_git(monkeypatch, [])
        (proj / ".agentic" / "outbox" / "DONE-0002.json").write_text(
            json.dumps({"notes": "short id form"}), encoding="utf-8"
        )

        out = collect_handoff("worker", "TODO-0002", proj, proj / ".agentic" / "logs")
        body = out.read_text(encoding="utf-8")
        assert "## Machine facts (DONE.json)" in body
        assert "short id form" in body

    def test_broken_json_no_section_and_warning_logged(self, tmp_path, monkeypatch):
        proj = _project(tmp_path)
        _stub_git(monkeypatch, [])
        (proj / ".agentic" / "outbox" / "DONE-TODO-0002.json").write_text(
            "{not valid json", encoding="utf-8"
        )

        out = collect_handoff("worker", "TODO-0002", proj, proj / ".agentic" / "logs")
        body = out.read_text(encoding="utf-8")
        assert "## Machine facts (DONE.json)" not in body
        assert "DONE-json=absent" in body

        log = (proj / ".agentic" / "logs" / "orchestrator.log").read_text(encoding="utf-8")
        assert "DONE-TODO-0002.json" in log

    def test_schema_violation_no_section(self, tmp_path, monkeypatch):
        proj = _project(tmp_path)
        _stub_git(monkeypatch, [])
        (proj / ".agentic" / "outbox" / "DONE-TODO-0002.json").write_text(
            json.dumps({"files_changed": "not-a-list"}), encoding="utf-8"
        )

        out = collect_handoff("worker", "TODO-0002", proj, proj / ".agentic" / "logs")
        assert "## Machine facts (DONE.json)" not in out.read_text(encoding="utf-8")

    def test_json_absent_handoff_as_before(self, tmp_path, monkeypatch):
        proj = _project(tmp_path)
        _stub_git(monkeypatch, [])
        out = collect_handoff("worker", "TODO-0002", proj, proj / ".agentic" / "logs")
        body = out.read_text(encoding="utf-8")
        assert "Machine facts" not in body
        assert "DONE-json=absent" in body


# ─── parse_done_json: pure validation ───────────────────────────────────

class TestParseDoneJson:
    def test_valid_full(self):
        assert parse_done_json(json.dumps(VALID_DONE_JSON)) == VALID_DONE_JSON

    def test_empty_object_is_valid(self):
        assert parse_done_json("{}") == {}

    def test_broken_json_is_none(self):
        assert parse_done_json("{nope") is None

    def test_top_level_not_object_is_none(self):
        assert parse_done_json("[1, 2]") is None

    def test_files_changed_not_list_is_none(self):
        assert parse_done_json('{"files_changed": "x"}') is None

    def test_tests_run_item_missing_result_is_none(self):
        assert parse_done_json('{"tests_run": [{"cmd": "x"}]}') is None

    def test_notes_not_string_is_none(self):
        assert parse_done_json('{"notes": 5}') is None

    def test_gates_any_strings_ok(self):
        assert parse_done_json('{"gates": ["contracts"]}') == {"gates": ["contracts"]}
