"""R-06 (TODO-0112, аудит 2026-09-25, слой 11): единый реестр + общий парсер ID.

``create_server`` вручную регистрировал ~47 tools, и метаданные для сервера
и доков разъезжались (A-08). Здесь закреплено:

- ``tools/registry.py::TOOLS`` — единственный источник регистрации: его
  набор имён == ``create_server().list_tools()``, 48 tools, у каждого
  непустое описание (сверка супервизора 26.09; +awf_run_revise — ORCH M3.4);
- сигнатуры и docstrings обёрток не изменились (внешний MCP-контракт) —
  выборка 10 инструментов;
- прямых блокирующих вызовов публичного awf API (``awf.api.*``) из
  ``async def`` по всему пакету не осталось — все через общий адаптер
  (``asyncio.to_thread``/``_exec``). Исключение — чистая функция
  ``awf.api.default_suggested_timeout`` (без I/O): единственное место,
  найденное сверкой супервизора 26.09, — в allowlist с комментарием;
- ``TODO-10000`` принимается везде одинаково: общий парсер
  (``awf/todo_ids.py``) используется и метриками, и публичной валидацией
  ID (run/dispatch).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

# Импорт в сборке (не в теле теста): во время теста autouse-фикстура
# tests/conftest.py подменяет subprocess.Popen функцией, а mcp при импорте
# жадно вычисляет аннотацию subprocess.Popen[bytes] — внутри теста импорт
# падает. Модульный импорт (как в соседних тестах) проходит в сборке, пока
# Popen настоящий.
pytest.importorskip("mcp.server.fastmcp")

from agent_workflow_ui.server import create_server  # noqa: E402
from agent_workflow_ui.tools import registry  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_PKG_DIR = _REPO / "agent_workflow_ui" / "src" / "agent_workflow_ui"

# R-06 allowlist: прямые вызовы awf.api из async def, которые НЕ блокируют
# event loop. Единственный пункт — результат сверки супервизора 26.09:
# это чистая функция (целочисленная арифметика, без I/O), а не блокирующая
# операция; убрана из allowlist'а только при переносе в to_thread.
_ALLOWED_DIRECT_API_CALLS = {
    "awf.api.default_suggested_timeout": (
        "pure function (int arithmetic, no I/O) — safe on the event loop; "
        "the only spot left per the 2026-09-26 supervisor cross-check"
    ),
}


# ─── 1. Реестр == list_tools ─────────────────────────────────────────────


def test_registry_equals_list_tools():
    """Набор из реестра == набор ``create_server().list_tools()``."""
    import asyncio

    TOOLS = registry.TOOLS
    names = [spec.name for spec in TOOLS]
    # 48 tools (сверка супервизора 26.09: 47; +awf_run_revise — ORCH M3.4)
    # и без дублей имён.
    assert len(names) == 48, f"registry has {len(names)} tools, expected 48"
    assert len(set(names)) == len(names), "duplicate tool names in the registry"

    live = asyncio.run(create_server().list_tools())
    live_names = [t.name for t in live]
    assert set(names) == set(live_names), (
        f"registry ≠ create_server().list_tools(): "
        f"registry-only={sorted(set(names) - set(live_names))}, "
        f"server-only={sorted(set(live_names) - set(names))}"
    )
    assert len(live_names) == 48

    # У каждого инструмента непустое описание (и у сервера, и в метаданных).
    empty_live = [t.name for t in live if not (t.description or "").strip()]
    assert empty_live == [], f"tools with empty description: {empty_live}"
    empty_meta = [s.name for s in TOOLS if not s.description.strip()]
    assert empty_meta == [], f"registry entries with empty description: {empty_meta}"

    # Счётчик для docs-проверки A-08 сходится с составом.
    counts = registry.tool_counts()
    assert counts["total"] == 48 and counts["total"] == counts["awf"] + counts["ui"]


# ─── 2. Сигнатуры и docstrings не изменились ─────────────────────────────


@pytest.mark.parametrize(
    ("module", "tool", "params", "doc_first_line"),
    [
        (
            "awf",
            "awf_init",
            ["project_dir", "force", "project_name", "test_cmd",
             "lint_cmd", "typecheck_cmd", "build_cmd"],
            "Initialize .agentic/ in a project and assume the supervisor role.",
        ),
        (
            "awf",
            "awf_status",
            ["project_dir"],
            "Get current workflow state — active TODOs, progress, blocked, conflicts.",
        ),
        (
            "awf",
            "awf_start",
            ["project_dir", "background", "pipeline", "from_stage",
             "auto", "timeout", "todo_id", "no_checkpoints"],
            "Start the pipeline from the beginning.",
        ),
        (
            "awf",
            "awf_approve",
            ["todo_id", "project_dir", "evidence", "verified_sha"],
            "Approve auto-commit for a TODO in --auto mode.",
        ),
        (
            "awf",
            "awf_rollback",
            ["todo_id", "project_dir", "mode"],
            "Roll the project back to BASELINE-{todo_id}.sha.",
        ),
        (
            "awf",
            "awf_dispatch_todo",
            ["content", "project_dir", "role", "todo_id",
             "pipeline", "carry_over_from", "include_untracked"],
            "Create a unit atomically: TODO file + baseline + .ready signal in one call.",
        ),
        (
            "awf",
            "awf_wait_for_event",
            ["project_dir", "timeout", "actionable_only"],
            "Check for pipeline events (reactive, NOT for proactive polling).",
        ),
        (
            "awf",
            "awf_metrics",
            ["project_dir", "reference_model", "since", "out",
             "refresh_subscriptions", "mirror", "all_projects"],
            "Collect token/cost metrics of the work program and write the report (U8).",
        ),
        (
            "forms",
            "open_form",
            ["template", "data", "project_dir", "ttl_seconds"],
            "Open an HTML form in the user's browser for structured input.",
        ),
        (
            "templates",
            "list_templates",
            [],
            "List all available form templates (default + project-level).",
        ),
    ],
)
def test_signatures_and_docstrings_preserved(module, tool, params, doc_first_line):
    """Внешний MCP-контракт: сигнатура и docstring обёртки на месте."""
    from agent_workflow_ui.tools import awf, forms, templates

    fn = getattr({"awf": awf, "forms": forms, "templates": templates}[module], tool)
    got_params = list(inspect.signature(fn).parameters)
    assert got_params == params, (
        f"{tool}: wrapper signature changed — was {params}, got {got_params}"
    )
    doc = (fn.__doc__ or "").strip().splitlines()
    assert doc and doc[0].strip() == doc_first_line, (
        f"{tool}: docstring first line changed — "
        f"was {doc_first_line!r}, got {(doc or ['<empty>'])[0]!r}"
    )


# ─── 3. AST: нет sync-вызовов awf.api в async ────────────────────────────


def _import_alias_map(tree: ast.AST) -> dict[str, str]:
    """Имя в коде → путь импортированного модуля awf (только awf/awf.*)."""
    imap: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "awf" or alias.name.startswith("awf."):
                    imap[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if (
                node.level == 0
                and node.module is not None
                and (node.module == "awf" or node.module.startswith("awf."))
            ):
                for alias in node.names:
                    imap[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return imap


def _resolve_callee(call: ast.Call, imap: dict[str, str]) -> str | None:
    """Доточное имя вызываемого (``awf.api.foo``) или None (не awf)."""
    func = call.func
    if isinstance(func, ast.Name):
        return imap.get(func.id)
    if isinstance(func, ast.Attribute):
        root = func
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name) and root.id in imap:
            attrs = []
            cur = func
            while isinstance(cur, ast.Attribute):
                attrs.append(cur.attr)
                cur = cur.value
            return ".".join([imap[root.id], *reversed(attrs)])
    return None


def _detect_in_tree(tree: ast.AST, label: str) -> list[dict]:
    """Прямые вызовы ``awf.api.*`` из тела async def (не из вложенных def)."""
    imap = _import_alias_map(tree)
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }

    def nearest_function(node: ast.AST) -> ast.AST | None:
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur
            cur = parents.get(cur)
        return None

    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        owner = nearest_function(node)
        if not isinstance(owner, ast.AsyncFunctionDef):
            continue
        resolved = _resolve_callee(node, imap)
        if resolved is None or not (
            resolved == "awf.api" or resolved.startswith("awf.api.")
        ):
            continue
        findings.append(
            {"file": label, "function": owner.name, "line": node.lineno, "call": resolved}
        )
    return findings


def _direct_api_calls_in_async() -> list[dict]:
    """По всему пакету ``agent_workflow_ui``: async def → прямой awf.api-вызов."""
    findings = []
    for path in sorted(_PKG_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        findings.extend(
            _detect_in_tree(tree, f"{path.relative_to(_PKG_DIR).as_posix()}::{path.stem}")
        )
    return findings


def test_no_direct_sync_api_calls_in_async():
    """Все awf.api-вызовы из async — через общий адаптер (to_thread)."""
    findings = _direct_api_calls_in_async()
    unexpected = [f for f in findings if f["call"] not in _ALLOWED_DIRECT_API_CALLS]
    assert unexpected == [], (
        "direct blocking awf.api call(s) from async def (must go through "
        f"asyncio.to_thread/_exec): {unexpected}"
    )
    # Allowlist не мёртвый: место из сверки 26.09 всё ещё в коде.
    allowed = {f["call"] for f in findings if f["call"] in _ALLOWED_DIRECT_API_CALLS}
    assert allowed == set(_ALLOWED_DIRECT_API_CALLS), (
        f"allowlist drifted from the code: found {sorted(allowed)} — if the call "
        "moved to to_thread, drop it from _ALLOWED_DIRECT_API_CALLS"
    )
    # Чувствительность детектора: синтетический прямой вызов обязан ловиться.
    snippet = (
        "from awf import api\n"
        "async def broken(project_dir):\n"
        "    return api.get_status(project_dir)\n"
    )
    caught = _detect_in_tree(ast.parse(snippet), "<snippet>")
    assert [f["call"] for f in caught] == ["awf.api.get_status"], (
        "AST detector must flag a direct awf.api call inside async def"
    )


# ─── 4. TODO-10000 принимается везде одинаково ───────────────────────────


def test_todo_10000_accepted_everywhere(tmp_git_repo):
    """Общий парсер: метрики и публичная валидация (run/dispatch) — одно."""
    from awf import api
    from awf.api._errors import AwfApiError
    from awf.metrics import _todo_id_from_subject
    from awf.todo_ids import extract_todo_id, is_valid_todo_id

    # Общий парсер: 5 цифр не усекаются, суффикс — не ID вовсе (A-10).
    assert extract_todo_id("awf(verify): TODO-10000") == "TODO-10000"
    assert extract_todo_id("awf(verify): TODO-10000x") is None
    assert is_valid_todo_id("TODO-10000")
    # Метрики используют общий парсер (A-10).
    assert _todo_id_from_subject("awf(verify): TODO-10000") == "TODO-10000"

    # Публичный вход: dispatch и run принимают TODO-10000 одинаково.
    api.init_project(tmp_git_repo, project_name="R06")
    dispatched = api.dispatch_todo(tmp_git_repo, "# R06 five-digit id", todo_id="TODO-10000")
    assert dispatched.todo_id == "TODO-10000"
    assert (tmp_git_repo / ".agentic" / "inbox" / "TODO-10000.md").is_file()

    run = api.run_start(tmp_git_repo, queue=["TODO-10000"])
    assert [item["todo_id"] for item in run.queue] == ["TODO-10000"]

    # И 3 цифры отвергнуты везде одинаково.
    assert extract_todo_id("TODO-123") is None
    assert not is_valid_todo_id("TODO-123")
    with pytest.raises(AwfApiError, match="invalid todo_id"):
        api.dispatch_todo(tmp_git_repo, "# bad", todo_id="TODO-123")
    with pytest.raises(AwfApiError, match="invalid TODO id"):
        api.run_start(tmp_git_repo, queue=["TODO-123"])
