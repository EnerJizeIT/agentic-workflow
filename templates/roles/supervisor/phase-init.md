# Phase: init

## Your task
Initialize awf in the project.

1. Call `awf_init(project_dir)`.
   - If `.agentic/` doesn't exist → creates skeleton.
   - If `.agentic/` exists → cleans runtime, preserves config (R1).
2. After init → call `awf_current_step(project_dir)` to advance to goal phase.

## What awf_init returns
- Project name, stack detection, vision excerpt, supervisor.md content.
- Pipeline status (configured or not).

## Do NOT
- Do NOT configure pipeline here — that's the form phase.
- Do NOT write TODO or Brief — that's the brief phase.
