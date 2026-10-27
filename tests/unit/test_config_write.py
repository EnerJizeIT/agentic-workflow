"""R10/T12: setup-вариант правит config.yaml, сохраняя комментарии и порядок."""
from __future__ import annotations

import difflib

import pytest
import yaml

from awf import api
from awf._errors import AwfApiError
from awf.config_write import patch_config_values, patch_mapping

CONFIG = """# top comment
project:
  name: demo  # inline

# models comment
models:
  supervisor:
    description: Current session model
  developer:
    agent_name: worker  # keep me
    model: old/model

# trailing comment
limits:
  max_review_passes: 3
"""


@pytest.fixture
def project(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="Test")
    config = tmp_git_repo / ".agentic" / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    return tmp_git_repo


def test_patch_changes_only_target_scalars():
    out = patch_mapping(
        CONFIG,
        {"developer": {"model": "new/model"}, "qa": {"agent_name": "worker"}},
    )
    before = CONFIG.split("\n")
    after = out.split("\n")
    assert "# top comment" in after
    assert "# models comment" in after
    assert "# trailing comment" in after
    assert "    agent_name: worker  # keep me" in after
    assert "    model: new/model" in after
    # Top-level order and the comments that separate the blocks survive.
    order = [
        "# top comment",
        "project:",
        "# models comment",
        "models:",
        "# trailing comment",
        "limits:",
    ]
    positions = [after.index(line) for line in order]
    assert positions == sorted(positions)
    # Untouched lines keep their relative order.
    for line in before:
        if "old/model" in line or line == "  developer:":
            continue
        assert line in after


def test_patch_result_matches_target_mapping():
    out = patch_mapping(
        CONFIG,
        {"developer": {"model": "new/model"}, "qa": {"agent_name": "worker"}},
    )
    data = yaml.safe_load(out)
    assert data["models"]["developer"] == {
        "agent_name": "worker",
        "model": "new/model",
    }
    assert data["models"]["qa"] == {"agent_name": "worker"}
    assert data["limits"]["max_review_passes"] == 3


def test_patch_idempotent_returns_same_text():
    once = patch_mapping(CONFIG, {"developer": {"model": "new/model"}})
    twice = patch_mapping(once, {"developer": {"model": "new/model"}})
    assert twice == once


def test_patch_removes_model_with_none_and_empty_string():
    out = patch_mapping(CONFIG, {"developer": {"model": None}})
    assert "model" not in yaml.safe_load(out)["models"]["developer"]
    out2 = patch_mapping(CONFIG, {"developer": {"model": ""}})
    assert out2 == out


def test_patch_preserves_other_role_keys():
    out = patch_mapping(CONFIG, {"supervisor": {"model": "x/y"}})
    entry = yaml.safe_load(out)["models"]["supervisor"]
    assert entry == {"description": "Current session model", "model": "x/y"}


def test_patch_keeps_comment_on_replaced_line():
    out = patch_mapping(CONFIG, {"developer": {"agent_name": "custom"}})
    assert "agent_name: custom  # keep me" in out


def test_creates_models_section_when_absent():
    out = patch_mapping("project:\n  name: demo\n", {"qa": {"agent_name": "worker"}})
    data = yaml.safe_load(out)
    assert data["project"] == {"name": "demo"}
    assert data["models"]["qa"]["agent_name"] == "worker"
    # Appending a block must not drop the file's final newline.
    assert out.endswith("\n")


def test_broken_yaml_is_refused():
    with pytest.raises(AwfApiError, match="invalid YAML"):
        patch_mapping("key: [unclosed\n", {"developer": {"model": "x"}})


def test_flow_style_models_is_refused():
    with pytest.raises(AwfApiError, match="flow-style"):
        patch_mapping("models: {a: {}}\n", {"developer": {"model": "x"}})


def test_nonstandard_indent_is_refused():
    text = "models:\n   developer:\n      agent_name: worker\n"
    with pytest.raises(AwfApiError, match="non-standard indentation"):
        patch_mapping(text, {"developer": {"model": "x"}})


def test_removes_the_last_key_of_a_role():
    # A model-only role (the normal shape of models.<role> written by
    # `awf add_role`) must survive clearing its model: the writer's own
    # `role: {}` output has to stay readable by the next patch.
    text = "# head\nmodels:\n  dev:\n    model: old\nlimits:\n  x: 1\n"
    out = patch_mapping(text, {"dev": {"model": None}})
    assert yaml.safe_load(out)["models"] == {"dev": {}}
    assert "# head" in out
    assert "limits:\n  x: 1" in out
    # The next patch of the same file — for this role and for another —
    # is not refused by the form the writer just produced.
    again = patch_mapping(out, {"dev": {"model": "new/m"}})
    assert yaml.safe_load(again)["models"] == {"dev": {"model": "new/m"}}
    other = patch_mapping(out, {"qa": {"agent_name": "worker"}})
    assert yaml.safe_load(other)["models"] == {
        "dev": {},
        "qa": {"agent_name": "worker"},
    }


def test_cleared_role_keeps_a_commented_header_and_inner_comment():
    text = (
        "models:\n"
        "  dev:  # dev block\n"
        "    # why this model\n"
        "    model: old\n"
        "limits:\n"
        "  x: 1\n"
    )
    out = patch_mapping(text, {"dev": {"model": None}})
    assert yaml.safe_load(out)["models"] == {"dev": {}}
    assert "# dev block" in out
    assert "# why this model" in out


def test_flow_empty_role_is_patched_in_place():
    text = "models:\n  dev: {}  # keep\n  qa:\n    model: m\n"
    out = patch_mapping(text, {"dev": {"model": "x/y"}})
    assert yaml.safe_load(out)["models"] == {
        "dev": {"model": "x/y"},
        "qa": {"model": "m"},
    }
    assert "# keep" in out
    # An unrelated patch must not be blocked by the empty role entry either.
    out2 = patch_mapping(text, {"qa": {"model": "z"}})
    assert yaml.safe_load(out2)["models"]["qa"] == {"model": "z"}


def test_empty_flow_models_is_rewritten_as_block():
    out = patch_mapping("project: demo\nmodels: {}\n", {"worker": {"agent_name": "worker"}})
    assert "models: {}" not in out
    assert yaml.safe_load(out)["models"]["worker"]["agent_name"] == "worker"


class TestUpdateConfigRoleMapping:
    def test_preserves_comments_and_order(self, project):
        config = project / ".agentic" / "config.yaml"
        assert api.setup.update_config_role_mapping(
            [{"role": "developer", "model": "new/model"}, {"role": "qa"}],
            project,
        )
        text = config.read_text(encoding="utf-8")
        assert "# top comment" in text
        assert "# models comment" in text
        assert "agent_name: worker  # keep me" in text
        data = yaml.safe_load(text)
        assert data["models"]["developer"]["model"] == "new/model"
        assert data["models"]["qa"]["agent_name"] == "worker"
        assert list(data) == ["project", "models", "limits"]

    def test_repeat_is_idempotent_and_backup_exists(self, project):
        config = project / ".agentic" / "config.yaml"
        team = [{"role": "developer", "model": "new/model"}]
        assert api.setup.update_config_role_mapping(team, project)
        once = config.read_bytes()
        backup = config.parent / "config.yaml.bak"
        assert backup.is_file()
        # T12: the .bak is the ORIGINAL bytes, not the patched ones.
        assert backup.read_bytes() == CONFIG.encode()
        # P3: the timestamped history copy exists beside the latest one.
        assert list(config.parent.glob("config.yaml.*.bak"))
        assert api.setup.update_config_role_mapping(team, project) is False
        assert config.read_bytes() == once

    def test_broken_yaml_not_overwritten(self, project):
        config = project / ".agentic" / "config.yaml"
        original = b"key: [unclosed\n"
        config.write_bytes(original)
        assert api.setup.update_config_role_mapping([{"role": "qa"}], project) is False
        assert config.read_bytes() == original
        # INV1: the refusal leaves no backup behind either.
        assert not (config.parent / "config.yaml.bak").exists()

    def test_flow_style_refused_without_overwrite(self, project):
        config = project / ".agentic" / "config.yaml"
        original = b"models: {a: {}}\n"
        config.write_bytes(original)
        with pytest.raises(AwfApiError, match="flow-style"):
            api.setup.update_config_role_mapping([{"role": "qa"}], project)
        assert config.read_bytes() == original
        assert not (config.parent / "config.yaml.bak").exists()

    def test_clears_model_of_model_only_role_and_result_stays_patchable(
        self, project
    ):
        config = project / ".agentic" / "config.yaml"
        config.write_text(
            "models:\n"
            "  agent-architect-review:\n"
            "    model: old/model\n"
            "limits:\n"
            "  max_review_passes: 3\n",
            encoding="utf-8",
        )
        # The form sends the role again with model="" (BD-32: user cleared).
        assert api.setup.update_config_role_mapping(
            [{"role": "agent-architect-review", "model": ""}], project
        )
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert data["models"]["agent-architect-review"] == {"agent_name": "worker"}
        assert data["limits"]["max_review_passes"] == 3
        assert (config.parent / "config.yaml.bak").is_file()
        # The project keeps working: a later model selection is applied.
        assert api.setup.update_config_role_mapping(
            [{"role": "agent-architect-review", "model": "new/model"}], project
        )
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert data["models"]["agent-architect-review"] == {
            "agent_name": "worker",
            "model": "new/model",
        }


# ─── AUD1: the other writer of config.yaml (UI-2/UI-3) ────────────────────


def test_patch_config_values_keeps_comments_and_order():
    text = (
        "# top comment\n"
        "project:\n"
        "  name: demo  # inline\n"
        "supervisor:\n"
        "  instructions: \"old\n"
        "    wrapped value\"\n"
        "# trailing comment\n"
        "limits:\n"
        "  max_review_passes: 3\n"
    )
    out = patch_config_values(
        text,
        {
            "context": {"message": "new context"},
            "supervisor": {"instructions": "fresh\nsecond line"},
        },
    )
    data = yaml.safe_load(out)
    assert data["context"]["message"] == "new context"
    assert data["supervisor"]["instructions"] == "fresh\nsecond line"
    assert "# top comment" in out
    assert "name: demo  # inline" in out
    assert "# trailing comment" in out
    assert list(data)[:2] == ["project", "supervisor"]
    # Idempotent: the same patch again changes no byte.
    assert patch_config_values(out, {
        "context": {"message": "new context"},
        "supervisor": {"instructions": "fresh\nsecond line"},
    }) == out


def test_patch_config_values_diff_is_only_target_scalars():
    """T12/AC26: the byte diff names only the changed keys — no reflow."""
    text = (
        "# top\n"
        "project:\n"
        "  name: demo\n"
        "context:\n"
        "  other: keep\n"
        "  message: old\n"
        "supervisor:\n"
        "  # note\n"
        "  instructions: old\n"
        "limits:\n"
        "  x: 1\n"
    )
    out = patch_config_values(
        text,
        {
            "context": {"message": "new"},
            "supervisor": {"instructions": "fresh"},
        },
    )
    changed = [
        line
        for line in difflib.unified_diff(
            text.splitlines(), out.splitlines(), lineterm="", n=0
        )
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    assert changed == [
        "-  message: old",
        "+  message: new",
        "-  instructions: old",
        "+  instructions: fresh",
    ]


def test_patch_config_values_refuses_crlf():
    with pytest.raises(AwfApiError, match="CRLF"):
        patch_config_values("context:\r\n  message: x\r\n", {"context": {"message": "y"}})


# ─── QA-0286-1: PyYAML folds plain scalars at 80 columns ──────────────────


LONG_MESSAGE = (
    "Build a command line tool that parses the config, validates it and "
    "prints a friendly report to the user every day"
)


def test_long_single_line_value_is_written_whole():
    # The writer takes one line of a safe_dump; PyYAML wraps a long plain
    # scalar at 80 columns, so the first line was a truncated value and the
    # round-trip check refused every patch of a normal-length sentence.
    out = patch_config_values(
        "context:\n  message: old\nproject: x\n",
        {"context": {"message": LONG_MESSAGE}},
    )
    assert yaml.safe_load(out)["context"]["message"] == LONG_MESSAGE
    assert patch_config_values(
        out, {"context": {"message": LONG_MESSAGE}}
    ) == out


def test_long_non_ascii_value_is_written_whole():
    text = "  Нужно объяснить усталому человеку в пятницу вечером, что " \
        "происходит в проекте, без лишнего жаргона и повторов"
    out = patch_config_values(
        "context:\n  message: old\n", {"context": {"message": text}}
    )
    assert yaml.safe_load(out)["context"]["message"] == text


def test_long_role_scalar_is_written_whole():
    model = "provider/" + "x" * 120
    out = patch_mapping(
        "models:\n  qa:\n    agent_name: worker\n", {"qa": {"model": model}}
    )
    assert yaml.safe_load(out)["models"]["qa"]["model"] == model


# ─── AUD0286-1: a comment on the flow-style header survives ───────────────


def test_empty_flow_models_keeps_header_comment():
    out = patch_mapping(
        "# head\nmodels: {}  # keep me\nproject: demo\n",
        {"worker": {"agent_name": "worker"}},
    )
    assert "# keep me" in out
    assert "models:  # keep me" in out
    assert yaml.safe_load(out)["models"]["worker"]["agent_name"] == "worker"


# ─── AUD0286-3: a freshly created config ends with a newline ──────────────


def test_new_file_from_empty_input_ends_with_newline():
    out = patch_config_values("", {"context": {"message": "hi"}})
    assert out.endswith("\n")
    assert yaml.safe_load(out)["context"]["message"] == "hi"


# ─── PA-0286-2: a quoted key is refused, not read as a second entry ───────


def test_quoted_role_key_is_refused_not_a_duplicate():
    # A quoted key parses to the same name as its unquoted form; keying the
    # scan by raw text made an update for ``dev`` insert a second ``dev:`` and
    # the round-trip check failed with a misleading "duplicate key" error.
    text = 'models:\n  "dev":\n    agent_name: worker\n'
    with pytest.raises(AwfApiError) as excinfo:
        patch_mapping(text, {"dev": {"model": "provider/m"}})
    message = str(excinfo.value)
    assert "quoted key not supported" in message
    assert "duplicate key" not in message


def test_quoted_scalar_key_is_refused():
    with pytest.raises(AwfApiError) as excinfo:
        patch_config_values(
            'context:\n  "message": old\n', {"context": {"message": "hi"}}
        )
    assert "quoted key not supported" in str(excinfo.value)
