# Phase: form

## Your task
Based on the goal, recommend roles and open the project-setup form.

## Recommend roles
Based on goal + character of work:
- **Analysis / audit** → system-analyst + qa-review + project-auditor
- **Development** → developer/architector + qa-review
- **Review / refactor** → qa-review + project-auditor
- **Mixed** → dominant character + qa-review

Tell the user: "Based on your goal, I recommend: <roles>. Select them in the form."

## Open the form
Call `awf_open_project_setup_form(project_dir=<path>)`.

The form auto-populates with:
- Global skills (from ~/.config/opencode/skills/)
- Global custom roles (from ~/.config/awf/roles/)
- Available models (from opencode.json)
- Project-local roles

User picks roles, models, pipeline template. After submit → advances to **normalize**.

## Do NOT
- Do NOT propose pipeline stages in chat — the form handles this.
- Do NOT call `awf_dispatch_todo` — pipeline must exist first.
- Do NOT pre-configure pipeline manually — the form is the single source.
