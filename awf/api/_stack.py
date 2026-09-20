"""Stack auto-detection from project files.

Inspects ``package.json``, ``pyproject.toml``, ``Cargo.toml``, ``go.mod``
to infer the test/lint/typecheck/build commands. Used by
:func:`awf.api.init_project` when the caller doesn't pass them explicitly.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def detect_stack(project_dir: Path) -> dict[str, str]:
    """Auto-detect test/lint/typecheck/build commands from project files.

    Returns a dict with keys: ``test_cmd``, ``lint_cmd``, ``typecheck_cmd``,
    ``build_cmd``, ``stack``. Empty strings when unknown. ``stack`` is one of:
    ``"python"``, ``"typescript"``, ``"javascript"``, ``"rust"``, ``"go"``,
    ``"unknown"``.

    Detection order: package.json → pyproject.toml/setup.py → Cargo.toml →
    go.mod → file-extension heuristic.
    """
    result: dict[str, str] = {
        "test_cmd": "",
        "lint_cmd": "",
        "typecheck_cmd": "",
        "build_cmd": "",
        "stack": "unknown",
    }

    def _as_dict(value: object) -> dict:
        # AUD06-10: sections can be any JSON value (monorepo wrappers,
        # generators) — only a dict is usable, anything else means "absent".
        return value if isinstance(value, dict) else {}

    # 1. Node/TypeScript — package.json scripts.* are authoritative
    pkg = project_dir / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {}
            scripts = _as_dict(data.get("scripts"))
            result["test_cmd"] = str(scripts.get("test", "")) or ""
            result["lint_cmd"] = str(scripts.get("lint", "")) or ""
            result["build_cmd"] = str(scripts.get("build", "")) or ""
            result["typecheck_cmd"] = str(scripts.get("typecheck", "")) or ""
            dev_deps = _as_dict(data.get("devDependencies"))
            deps = _as_dict(data.get("dependencies"))
            all_deps = {**dev_deps, **deps}
            tsconfig_present = (project_dir / "tsconfig.json").is_file()
            if "typescript" in all_deps or tsconfig_present:
                result["stack"] = "typescript"
            else:
                result["stack"] = "javascript"
            if not result["typecheck_cmd"] and tsconfig_present:
                result["typecheck_cmd"] = "tsc --noEmit"
            return result
        except (json.JSONDecodeError, OSError):
            pass

    # 2. Python — pyproject.toml / setup.py
    pyproject = project_dir / "pyproject.toml"
    setup_py = project_dir / "setup.py"
    if pyproject.is_file() or setup_py.is_file():
        result["stack"] = "python"
        result["test_cmd"] = "pytest"
        result["lint_cmd"] = "ruff check ."
        try:
            text = pyproject.read_text(encoding="utf-8") if pyproject.is_file() else ""
        except OSError:
            text = ""
        if "[tool.mypy]" in text or (project_dir / "mypy.ini").is_file():
            result["typecheck_cmd"] = "mypy ."
        if "[build-system]" in text:
            result["build_cmd"] = "pip install -e ."
        return result

    # 3. Rust — Cargo.toml
    if (project_dir / "Cargo.toml").is_file():
        result["stack"] = "rust"
        result["test_cmd"] = "cargo test"
        result["lint_cmd"] = "cargo clippy"
        result["typecheck_cmd"] = "cargo check"
        result["build_cmd"] = "cargo build"
        return result

    # 4. Go — go.mod
    if (project_dir / "go.mod").is_file():
        result["stack"] = "go"
        result["test_cmd"] = "go test ./..."
        result["lint_cmd"] = "golangci-lint run"
        result["typecheck_cmd"] = "go vet ./..."
        result["build_cmd"] = "go build ./..."
        return result

    # 5. Heuristic — file extensions
    try:
        files = list(project_dir.iterdir())
    except OSError:
        files = []
    extensions = {f.suffix.lower() for f in files if f.is_file()}
    if ".ts" in extensions or ".tsx" in extensions:
        result["stack"] = "typescript"
        result["test_cmd"] = "bun test"
        result["typecheck_cmd"] = "tsc --noEmit"
    elif ".py" in extensions:
        result["stack"] = "python"
        result["test_cmd"] = "pytest"
        result["lint_cmd"] = "ruff check ."

    return result


def derive_project_name(project_dir: Path) -> str:
    """Derive human-readable project name from directory name.

    ``jira-epic-presenter`` → ``Jira Epic Presenter``.
    Underscores and hyphens treated as word separators; result is Title Case.
    camelCase names are split for readability (``myApp`` → ``My App``).
    """
    name = project_dir.name
    # Split camelCase: myApp → my App
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    # Replace separators with spaces
    name = name.replace("_", " ").replace("-", " ")
    # Collapse multiple spaces
    name = re.sub(r"\s+", " ", name).strip()
    # Title Case each word
    words = name.split(" ")
    titled = " ".join(w[:1].upper() + w[1:] for w in words if w)
    return titled or "Project"


__all__ = ["detect_stack", "derive_project_name"]
