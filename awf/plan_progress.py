"""BD-33/34: plan progress tracking — auto-mark steps + progress report.

Extracted from orchestrator.py (A6 refactor).
"""
from __future__ import annotations

import re
from pathlib import Path

from . import config as cfg_mod
from ._log import log as _log


def extract_step_id_from_todo(todo_path: Path) -> int | None:
    """BD-33: parse 'Step N' from TODO-NNNN.md frontmatter/body.

    Looks for patterns like:
      **Phase:** Неделя 1 — Step 1
      Step 1:
      step_id: 1
      **Step 1**

    Returns the integer step number, or None if not found.
    """
    if not todo_path.is_file():
        return None
    try:
        text = todo_path.read_text(encoding="utf-8")
    except OSError:
        return None

    # Try YAML frontmatter first (most reliable): step_id: 1
    m = re.search(r"^step_id:\s*(\d+)\s*$", text, re.MULTILINE)
    if m:
        return int(m.group(1))

    # P3: 'Step N' in header area — require markdown context (start of line,
    # bold, checkbox, or "Phase:" prefix) to avoid matching random prose.
    head = "\n".join(text.splitlines()[:20])
    matches = re.findall(r"(?:^|\*\*|-\s*\[\s*[xX ]?\s*\]|Phase:.*?)(?:\s*)Step\s+(\d+)", head, re.MULTILINE)
    if matches:
        return int(matches[0])

    return None


def mark_plan_step_done(
    project_dir: Path,
    todo_id: str,
    logs_dir: Path,
) -> bool:
    """BD-33: after verify, mark the corresponding Step in phases/plan.md as done.

    Parses phases/plan.md for a line matching the Step number extracted from
    TODO-NNNN.md, replaces '- [ ]' with '- [x]' and appends the TODO id.

    Returns True if a line was updated, False otherwise.
    """
    config = cfg_mod.load(project_dir)
    phases_rel = cfg_mod.get(config, "phases.current", ".agentic/phases/plan.md")
    plan_path = (
        project_dir / phases_rel if not Path(phases_rel).is_absolute() else Path(phases_rel)
    )

    if not plan_path.is_file():
        _log(logs_dir, f"BD-33: no plan file at {plan_path} — skip step update")
        return False

    from . import paths

    todo_path = paths.inbox(project_dir) / f"{todo_id}.md"
    step_id = extract_step_id_from_todo(todo_path)
    if step_id is None:
        _log(logs_dir, f"BD-33: no Step N found in {todo_id}.md — skip step update")
        return False

    try:
        content = plan_path.read_text(encoding="utf-8")
    except OSError as e:
        _log(logs_dir, f"BD-33: failed to read {plan_path}: {e}")
        return False

    # Match: '- [ ] Step N:' OR '- [ ] **Step N**:' (any whitespace)
    # Use [ \t] instead of \s to avoid matching across newlines.
    pattern = re.compile(
        r"^([ \t]*-[ \t]*\[[ \t]])(?:[ \t]|\*\*)*Step[ \t]+" + str(step_id) + r"\b",
        re.MULTILINE,
    )
    match = pattern.search(content)
    if not match:
        _log(logs_dir, f"BD-33: no '- [ ] Step {step_id}' in plan.md — skip")
        return False

    # Replace '[ ]' with '[x]' and append TODO id at end of line
    line_start = match.start()
    line_end = content.find("\n", line_start)
    if line_end == -1:
        line_end = len(content)
    original_line = content[line_start:line_end]

    updated_line = original_line.replace("[ ]", "[x]", 1)
    # Append TODO marker if not already present
    if todo_id not in updated_line:
        updated_line = updated_line.rstrip() + f"  — {todo_id}"

    new_content = content[:line_start] + updated_line + content[line_end:]
    # H5 fix: atomic write (was direct write_text — crash mid-write corrupts plan.md).
    try:
        from ._atomic import atomic_write_text
        atomic_write_text(plan_path, new_content)
        _log(logs_dir, f"BD-33: marked Step {step_id} done in plan.md (TODO {todo_id})")
        return True
    except OSError as e:
        _log(logs_dir, f"BD-33: failed to write {plan_path}: {e}")
        return False


def print_progress_report(project_dir: Path, logs_dir: Path) -> None:
    """BD-34: print a progress summary after pipeline completes.

    Counts [ ] vs [x] in phases/plan.md, lists next 3 unfinished steps.
    """
    config = cfg_mod.load(project_dir)
    phases_rel = cfg_mod.get(config, "phases.current", ".agentic/phases/plan.md")
    plan_path = (
        project_dir / phases_rel if not Path(phases_rel).is_absolute() else Path(phases_rel)
    )

    print()
    print("=" * 51)
    print("  PROGRESS REPORT")
    print("=" * 51)

    if not plan_path.is_file():
        print(f"  (no plan file at {plan_path})")
        return

    try:
        content = plan_path.read_text(encoding="utf-8")
    except OSError:
        print(f"  (failed to read {plan_path})")
        return

    done = re.findall(r"^\s*-\s*\[x\]", content, re.MULTILINE)
    todo = re.findall(r"^\s*-\s*\[\s\]", content, re.MULTILINE)

    total = len(done) + len(todo)
    print(f"  Steps done:     {len(done)} / {total}")
    print(f"  Steps remaining: {len(todo)}")
    print()

    # List next 3 unfinished steps
    unfinished = []
    for line in content.splitlines():
        if re.match(r"^\s*-\s*\[\s\]", line):
            clean = re.sub(r"^\s*-\s*\[\s\]\s*", "", line)
            clean = clean.strip()
            if len(clean) > 100:
                clean = clean[:97] + "..."
            unfinished.append(clean)

    if unfinished:
        print("  Next steps:")
        for i, step in enumerate(unfinished[:3], 1):
            print(f"    {i}. {step}")
        if len(unfinished) > 3:
            print(f"    ... and {len(unfinished) - 3} more")
    else:
        print("  All steps complete! 🎉")

    print()
    _log(logs_dir, f"BD-34: progress report: {len(done)}/{total} steps done")
