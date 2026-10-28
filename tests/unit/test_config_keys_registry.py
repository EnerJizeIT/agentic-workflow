"""R5 (ops reliability): the config-key registry ratchet.

``awf/config_keys.py`` is the single list of ``.agentic/config.yaml`` keys the
code reads; ``awf doctor`` uses it to name keys that no code reads. Runtime
introspection cannot see a literal read (D5), so this test re-scans the sources
and reddens when a reader appears that the registry does not know.

Scan rules (conservative on purpose):

* a dotted literal in any ``.get(...)`` call is a config path;
* a dotted literal passed to a config helper (the call name carries
  ``cfg``/``config``) is a config path — ``_cfg_int(config, "retry.…")``;
* a module-level ``NAME_KEY = "dotted"`` constant is a config path — the code
  hands such constants to the getter (``stage_timeout.PROJECT_KEY``,
  ``closer_stage.CLOSER_ATTEMPTS_KEY``, ``api/dispatch.BASELINE_TIMEOUT_CONFIG_KEY``);
* a non-dotted key counts only when the receiver is a config dict by name
  (``config``, ``config_data``, ``cfg_data``) or a config module alias
  (``cfg_mod``, ``config_mod``, ``_cfg``, ``awf_config``, ``_config``):
  ``dict.get("status")`` all over the codebase must not be mistaken for a
  config key. ``_config`` is a MODULE alias (``from . import config as
  _config``), so its dict is the first argument — ``_config.get(cfg, "key")``
  and ``_config.get(_config.load(p), "key")`` both name ``key``.

Literals with spaces, a trailing dot or an ``=`` are prose/fragments, not keys.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from awf import config_keys
from awf.verify import verify_command_keys

_SRC = Path(__file__).resolve().parents[2] / "awf"

#: First segment of a dotted literal must be a config section for the
#: call-argument rule; otherwise ``"run.yaml"`` / ``"supervisor.md"`` would
#: look like config paths.
_SECTIONS = frozenset({
    "automation", "checklist", "context", "discipline", "engine", "feedback",
    "limits", "metrics", "mcp", "models", "phases", "project", "retry", "run",
    "spec", "supervisor", "verification", "wait", "worker",
})

#: Receivers that are the parsed project config (dict).
_DICT_RECEIVERS = frozenset({"config", "config_data", "cfg_data"})

#: Receivers that are the ``awf.config`` module (the dict is the first arg).
#: ``_config`` belongs here: the code does ``from . import config as _config``
#: and calls ``_config.get(cfg, "dotted.key")`` — treating it as a dict read
#: the first argument (the dict variable) and missed the key, so the ratchet
#: could not see ``automation.owner_idle_minutes`` / ``run.stall_minutes`` /
#: ``feedback.dir`` (AUD-0296 CTX1).
_MODULE_RECEIVERS = frozenset({
    "cfg_mod", "config_mod", "_cfg", "awf_config", "_config",
})

_HELPERS = ("cfg", "config")


def _key_text(node: ast.AST) -> str | None:
    """The literal as a config-path candidate, or ``None``."""
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return None
    value = node.value
    if " " in value or "=" in value or value.endswith("."):
        return None
    return value or None


def _receiver_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _literal_reads(tree: ast.AST) -> set[str]:
    """Every config-path literal the module reads."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "get":
            receiver = _receiver_name(func.value)
            if receiver in _MODULE_RECEIVERS:
                args = node.args[1:2]
            else:
                args = node.args[:1]
            for arg in args:
                key = _key_text(arg)
                if key and ("." in key or receiver in _DICT_RECEIVERS):
                    found.add(key)
        name = func.attr if isinstance(func, ast.Attribute) else _receiver_name(func)
        if any(marker in name for marker in _HELPERS):
            for arg in node.args:
                key = _key_text(arg)
                if key and "." in key and key.split(".")[0] in _SECTIONS:
                    found.add(key)
    return found


def _key_constants(tree: ast.Module) -> set[str]:
    """Module-level ``SOME_KEY = "dotted.path"`` constants."""
    found: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None:
            continue
        key = _key_text(value)
        if key is None or "." not in key:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id.endswith("_KEY"):
                found.add(key)
    return found


def _source_reads() -> set[str]:
    """All config keys the ``awf`` package reads, from the sources."""
    reads: set[str] = set()
    files = sorted(_SRC.rglob("*.py"))
    assert files, f"no sources under {_SRC}"
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        reads |= _literal_reads(tree)
        reads |= _key_constants(tree)
    return reads


def test_every_literal_read_is_registered():
    """A new literal reader reddens until its key is added to ``READ_KEYS``."""
    missing = sorted(key for key in _source_reads() if not config_keys.is_read_key(key))
    assert not missing, (
        "these config keys are read by awf/**/*.py but missing from "
        f"awf/config_keys.py READ_KEYS: {missing}"
    )


def test_verification_command_keys_are_registered():
    """``verify_command_keys`` is read dynamically — mirror it into the registry."""
    missing = [
        f"verification.{key}"
        for key in verify_command_keys()
        if not config_keys.is_read_key(f"verification.{key}")
    ]
    assert not missing, f"verification command keys missing: {missing}"


def test_wildcards_are_well_formed():
    """A wildcard entry is ``prefix.*`` and names a real family, not ``.*``."""
    for key in config_keys.READ_KEYS:
        if key.endswith(".*"):
            assert key.count(".*") == 1, key
            assert len(key) > 2, key


def test_dead_keys_names_unread_leaves():
    """``retry.max_attempts`` / ``retry.backoff_seconds`` have no reader (AC16)."""
    dead = config_keys.dead_keys({
        "retry": {"max_attempts": 3, "backoff_seconds": 0},
        "automation": {"runner_dir": "/tmp/runner"},
        "models": {"worker": {"model": "p/one"}},
    })
    assert dead == ["retry.backoff_seconds", "retry.max_attempts"]


def test_dead_keys_is_quiet_when_every_key_is_read():
    """A config whose keys are all read has no dead keys (AC16, second half)."""
    config = {
        "project": {"name": "Test"},
        "config_version": 2,
        "discipline": {"dirty_policy": "isolate"},
        "verification": {"test_cmd": "pytest -q", "lint_cmd": "ruff check ."},
        "automation": {"stage_timeout_seconds": 3600},
        "models": {"worker": {"model": "p/one", "agent_name": "worker"}},
        "empty_section": {},
    }
    assert config_keys.dead_keys(config) == []


def test_empty_mapping_is_not_a_dead_key():
    """An empty section names no key — nothing to report."""
    assert config_keys.dead_keys({"automation": {}}) == []


@pytest.mark.parametrize(
    ("leaf", "expected"),
    [
        ("models", True),
        ("models.worker.model", True),
        ("automation.runner_dir", True),
        ("verification.lint_cmd", True),
        ("verification.coverage_cmd", False),
        ("retry.max_attempts", False),
        ("project.root", False),
    ],
)
def test_is_read_key(leaf, expected):
    assert config_keys.is_read_key(leaf) is expected


def test_tiers_map_leaves_are_read():
    """``metrics.tiers`` is a map of id lists — its leaves are data, not code.

    A project that configures tiers (``metrics.tiers.top`` …) must not be
    told those keys have no reader: the class names are the user's data.
    """
    config = {
        "metrics": {"tiers": {"top": ["a/b"], "optimal": ["c/d"], "cheap": []}}
    }
    assert config_keys.dead_keys(config) == []
    assert config_keys.is_read_key("metrics.tiers") is True
    assert config_keys.is_read_key("metrics.tiers.top") is True


# ─── AUD-0296 CTX1: the ``_config`` module alias ─────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "automation.owner_idle_minutes",
        "run.stall_minutes",
        "feedback.dir",
    ],
)
def test_config_module_read_is_seen(key):
    """CTX1: ``_config`` is a module, so the dict is the FIRST argument.

    ``_config.get(_config.load(p), "key")`` (and the sibling
    ``_config.get(cfg, "key")``) must name ``key``; treating ``_config`` as a
    dict read the first argument and missed these three live settings.
    """
    code = f'v = _config.get(_config.load(p), "{key}")'
    assert _literal_reads(ast.parse(code)) == {key}
    assert "_config" not in _DICT_RECEIVERS
    assert "_config" in _MODULE_RECEIVERS


def test_config_dict_receiver_still_reads_its_first_argument():
    """The dict form is unchanged: ``config.get("key")`` is that key."""
    assert _literal_reads(ast.parse('v = config.get("automation.runner_dir")')) == {
        "automation.runner_dir"
    }


# ─── AUD-0296 CTX2: the reverse registry check ───────────────────────────


#: Registry entries the literal scanner cannot see. ``discipline.dirty_policy``
#: is read from the section mapping (``discipline.get("dirty_policy")``), not
#: by a dotted literal; the four ``verification.*`` commands are read
#: dynamically through ``verify_command_keys()`` and are added by the test.
_NESTED_READ_EXCEPTIONS = frozenset({"discipline.dirty_policy"})


def test_every_registered_key_has_a_reader():
    """CTX2: a registry entry whose reader was deleted must not stay "alive".

    ``test_every_literal_read_is_registered`` only walks code → registry; a
    removed reader would leave its key registered forever, and ``awf doctor``
    would keep calling a genuinely dead key alive — the OPS7 scenario the
    tool exists to catch. This walk is registry → code.
    """
    reads = _source_reads() | {
        f"verification.{key}" for key in verify_command_keys()
    }

    def covered(key: str) -> bool:
        if key.endswith(".*"):
            prefix = key[:-2]
            return any(r == prefix or r.startswith(prefix + ".") for r in reads)
        return key in reads

    orphans = sorted(
        key
        for key in config_keys.READ_KEYS
        if key not in _NESTED_READ_EXCEPTIONS and not covered(key)
    )
    assert not orphans, (
        "these keys are in awf/config_keys.py READ_KEYS but no code reads "
        f"them — drop them or name the dynamic reader: {orphans}"
    )


# ─── AUD-0296 CTX3: the init template carries no dead key ────────────────


def test_init_template_has_no_dead_keys():
    """CTX3: a fresh ``awf init`` project must not start with a dead key.

    The template used to write ``project.root: "."`` with no reader, so
    every fresh project failed ``awf doctor`` on its very first run.
    """
    from awf.api._templates import _CONFIG_TEMPLATE

    text = _CONFIG_TEMPLATE.format(
        project_name="Test",
        config_version=2,
        test_cmd="pytest -q",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    config = yaml.safe_load(text)
    assert config_keys.dead_keys(config) == []
