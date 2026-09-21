# Phase: goal

## Your task
Ask the user for the goal of this session. 1-3 questions maximum.

## Questions to ask
- "What do you want to achieve?" (feature, bugfix, review, refactor, analysis)
- "Any constraints?" (no code changes, specific files, deadline)
- "What character of work?" (analysis / development / review)

## After user answers
Call `awf_set_goal(goal="<user's answer>", project_dir=<path>)`.

This stores the goal in state and advances to **form** phase.

## Why
The goal determines:
- What artifacts to read (don't load everything — read what's relevant).
- What roles to recommend (analysis → analyst+qa; dev → developer+qa).
- What goes into the TODO (the goal becomes the TODO's "Goal" section).

## Do NOT
- Do NOT skip this step even if the project is familiar.
- Do NOT load full context yet — focus comes from the goal.
- Do NOT study the codebase broadly — study under the goal.
