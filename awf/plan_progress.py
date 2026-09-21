"""BD-33/34: plan progress tracking — auto-mark steps + progress report.

Extracted from orchestrator.py (A6 refactor).
"""
from __future__ import annotations

import re
from pathlib import Path

from . import config as cfg_mod
from ._log import log as _log


def extract_step_id_from_todo(todo_path: Path) -> int | None:
    """BD-33: parse 'Step N' / 'Шаг N' from TODO-NNNN.md frontmatter/body.

    Looks for patterns like:
      **Phase:** Неделя 1 — Step 1
      Step 1:
      Шаг 2 плана
      ## Step 2: fix the thing
      **Шаг 3**
      - [ ] Шаг 1: задача
      step_id: 1

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

    # P3 / AUD02-10: 'Step N' or 'Шаг N' in the header area — require
    # markdown context (line start, ## header, bold, checkbox, or "Phase:"
    # prefix) to avoid matching random prose. Real TODOs in this repo are
    # written with the Russian "Шаг N" — before AUD02-10 it silently
    # returned None and the plan was never auto-marked.
    head = "\n".join(text.splitlines()[:20])
    matches = re.findall(
        r"(?:^[ \t]{0,3}#{1,6}[ \t]+|^[ \t]*|\*\*|-[ \t]*\[[xX ]?[ \t]*\][ \t]*|Phase:.*?)"
        r"[ \t]*(?:Step|Шаг)[ \t]+(\d+)",
        head,
        re.MULTILINE,
    )
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
        # AUD02-10: the plan is not updated — say WHY and what the TODO
        # needs, instead of a quiet skip nobody reads.
        _log(
            logs_dir,
            f"BD-33 WARNING: no step marker in {todo_id}.md — plan not "
            "updated. The TODO needs a 'step_id: N' frontmatter line or a "
            "'Step N' / 'Шаг N' line in its first 20 lines.",
        )
        return False

    # Match: '- [ ] Step N:' OR '- [ ] **Step N**:' OR '- [ ] Шаг N:'
    # (any whitespace). Use [ \t] instead of \s to avoid matching across
    # newlines. AUD02-10: bilingual, mirroring the TODO-side extraction.
    pattern = re.compile(
        r"^([ \t]*-[ \t]*\[[ \t]])(?:[ \t]|\*\*)*(?:Step|Шаг)[ \t]+"
        + str(step_id) + r"\b",
        re.MULTILINE,
    )

    # AUD14-08: read–modify–write with a re-read guard. A concurrent edit
    # landing between our read and write used to be silently lost (our
    # whole-file write won over the fresh content). Re-read just before
    # writing; if the file changed, reapply the tick to the FRESH content.
    for _attempt in range(3):
        try:
            content = plan_path.read_text(encoding="utf-8")
        except OSError as e:
            _log(logs_dir, f"BD-33: failed to read {plan_path}: {e}")
            return False

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

        try:
            fresh = plan_path.read_text(encoding="utf-8")
        except OSError as e:
            _log(logs_dir, f"BD-33: failed to re-read {plan_path}: {e}")
            return False
        if fresh != content:
            continue  # concurrent edit landed — reapply to fresh content

        # H5 fix: atomic write (was direct write_text — crash mid-write
        # corrupts plan.md).
        try:
            from ._atomic import atomic_write_text
            atomic_write_text(plan_path, new_content)
            _log(logs_dir, f"BD-33: marked Step {step_id} done in plan.md (TODO {todo_id})")
            return True
        except OSError as e:
            _log(logs_dir, f"BD-33: failed to write {plan_path}: {e}")
            return False

    _log(logs_dir, f"BD-33: plan.md kept changing — gave up marking Step {step_id}")
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
