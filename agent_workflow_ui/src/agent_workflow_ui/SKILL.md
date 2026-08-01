# Agent Workflow UI

MCP plugin for [opencode](https://opencode.ai/) that gives agents tools for visual
interaction with users: HTML forms for structured input, dashboards for monitoring.

Use forms when chat is inefficient for the task at hand. Forms are a supplement
to chat, not a replacement.

## When to use a form

Call `open_form` when:

- **Setting up a new project** with full configuration (→ `project-setup` composite template — primary for MVP).
- **A file upload** is needed (ТЗ, product-vision, custom role `.md`).
- Need to choose from **4+ options with descriptions** (no specific template yet — agent may create one, see below).
- Monitoring a **long-running pipeline** (→ dashboard, future scope).

## When NOT to use a form (use chat instead)

- A simple **Y/N** answer.
- Choosing between **2-3 short options**.
- **Clarifying questions** during work.
- **Tone calibration** or approach discussion.
- Anything that can be answered in one line of text.

## Usage pattern — NON-BLOCKING

**Always use `open_form` (non-blocking). Never use `open_form_and_wait` (it is not registered as MCP tool anyway).**

1. Agent decides a form is needed.
2. Call `open_form(template="...", data={...})` — opens form, returns `form_id` immediately.
3. Tell the user in CLI: **"I opened a form in your browser. Fill it out, click Submit, then tell me here when you're done."**
4. Agent is now **free** — can continue other work, answer questions, or wait for the user.
5. When the user types something in CLI (e.g., "done", "submitted", "I filled it"):
   - Call `read_submit(form_id)` to retrieve the submitted data.
   - Analyze `data` and proceed.

**Why non-blocking?**

The agent stays responsive. The user controls the pace — they fill the form
when ready, then explicitly tell the agent to proceed. This avoids the agent
"hanging" silently while the user is still thinking.

**Critical:** You MUST tell the user to come back to CLI and notify you after
submitting. The form's "Submitted!" page also reminds them, but you should
say it in your message too.

### What to tell the user

After calling `open_form`, ALWAYS say something like:

> "I've opened a form in your browser (Form ID: FORM-...).
> Please fill it out and click Submit.
> When done, come back here and tell me — I'll pick up your answers."

### When the user says "done" / "submitted" / "filled it"

Call `read_submit(form_id)` immediately to get the data. Don't make the user
repeat themselves. If `read_submit` returns `submitted: false`, tell the user
the form hasn't been submitted yet and ask them to check.

## Tool reference

### `open_form(template, data, ttl_seconds?)` — PRIMARY TOOL

Render and open an HTML form in the user's browser. Non-blocking — returns immediately.

**Arguments:**
- `template` (str, required) — Template name without extension (e.g., "project-setup").
- `data` (dict, optional) — Variables to render in the template.
- `ttl_seconds` (int, optional) — Auto-expire form after N seconds.

**Returns:** `{form_id, browser_opened, submit_url, expires_at?, error?}`

### `read_submit(form_id)` — USE WHEN USER SAYS "DONE"

Check if user submitted the form. Non-blocking — returns immediately.

**Returns:** `{submitted, form_id, status, data?, submitted_at?, error?}`

**Status values:** `pending`, `submitted`, `cancelled`, `expired`, `unknown`, `error`

### `cancel_form(form_id)`

Cancel a pending form. Future HTTP submits for this form_id are ignored.

**Returns:** `{cancelled, form_id, reason?}`

### `list_pending_forms()`

List forms opened but not yet submitted, cancelled, or expired.

**Returns:** `{pending: [{form_id, template, opened_at, age_seconds}], count}`

### `list_templates()`

List available form templates with metadata.

**Returns:** `{templates: [{name, source, description, required_data_keys, optional_data_keys}]}`

## Available templates

Call `list_templates` to see what's available at runtime.

### Primary (MVP)

| Template | Description | Required data keys |
|---|---|---|
| `project-setup` | Composite form: context + ТЗ files + supervisor + team (with models). Models pulled from opencode automatically. | `available_roles` |

Plugin injects automatically into every template:
- `form_id`, `submit_url`, `template_name`
- `available_models`, `recent_models` (from opencode CLI + SQLite history)
- `custom_supervisor_roles`, `custom_agents` (from `~/.config/awf/roles/`)
- `existing_supervisor_slugs`, `existing_agent_slugs` (for client-side conflict detection)

### Future templates

Templates for scenarios 2-6 (decision forks, blockage recovery, dashboards, priority planning, onboarding wizard) will be added as those scenarios are implemented. Currently only `project-setup` ships as a default template.

**Until future scenarios are implemented:** Use `project-setup` for project configuration. Use chat for everything else.

## Creating new templates (agent-driven)

**Only the agent (you, LLM) can create new templates.** The user does not edit `.agentic/templates/` manually — if a custom form is needed, you create it.

### How to create a new template

1. **Front (HTML form):** write a Jinja2 template at `.agentic/templates/<name>.html.j2`. Structure:
   ```jinja2
   ---
   description: What this form does
   required_data_keys:
     - some_required_var
   optional_data_keys:
     - some_optional_var
   ---
   <!DOCTYPE html>
   <html>
   <head><title>...</title></head>
   <body>
     <form action="{{ submit_url }}" method="POST">
       <input type="hidden" name="form_id" value="{{ form_id }}">
       <!-- form fields here -->
       <button type="submit">Submit</button>
     </form>
   </body>
   </html>
   ```

2. **Back (interpretation):** describe in supervisor.md (or in your response to the user) how to interpret the submit data. Plugin's HTTP endpoint saves the raw form data to `inputs/<form_id>.yaml` — you read it via `read_submit` and parse the fields yourself.

3. **Use:** call `open_form(template="<name>", data={...})`. Plugin's `ChoiceLoader` finds project templates first, then defaults — so your new template is available immediately.

### Template variables available

Every template gets these injected by plugin:
- `{{ form_id }}` — current form ID
- `{{ submit_url }}` — POST endpoint
- `{{ template_name }}` — current template name
- `{{ available_models }}`, `{{ recent_models }}` — opencode models
- `{{ custom_supervisor_roles }}`, `{{ custom_agents }}` — saved roles
- Plus any keys you pass in `data` argument

### Important rules

- Always use `<form action="{{ submit_url }}" method="POST">` — that's how submits reach the plugin.
- Always include `<input type="hidden" name="form_id" value="{{ form_id }}">`.
- Plugin uses Jinja2 with autoescape — user input is safe.
- YAML frontmatter is stripped from output automatically.

## Mistakes to avoid

- **DO NOT** use blocking wait tools — they are not registered. Use `open_form` + user notification pattern.
- **DO NOT** forget to tell the user to come back to CLI after submitting.
- **DO NOT** open a form for Y/N questions — use chat.
- **DO NOT** open more than 3 forms simultaneously — the user will be confused.
- **DO NOT** use forms as a chat replacement — they're a supplement.
- **DO NOT** invent template names — only `project-setup` ships as default. Future scenarios will add more.

## Example workflow

```
Agent: "I need to configure your project. Let me open a form."

→ open_form(template="project-setup", data={"available_roles": [
    {"id": "backend-dev", "title": "Backend Developer"},
    {"id": "frontend-dev", "title": "Frontend Developer"},
    {"id": "tester", "title": "Tester"}
  ]})
  Returns: {form_id: "FORM-20260726143022-a1b2", browser_opened: true, submit_url: "..."}

Agent (to user): "I've opened project setup form in your browser.
                   Fill context, pick supervisor, build your team, click Submit.
                   When done, tell me here."

[Agent is free — can answer other questions, do other work, or wait]

User (in CLI): "Done!"

→ read_submit("FORM-20260726143022-a1b2")
  Returns: {submitted: true, data: {
    context_message: "...",
    team_config: '[{"type":"default","agent":"backend-dev","model":"anthropic/claude-3.5"}]',
    spec_files_json: '[...]',
    ...
  }}

After submit, plugin writes:
- Custom roles to global + project (BD-3-B, BD-5).
- Pipeline stages to .agentic/pipelines/default.yaml (BD-9).
- Per-role models to .agentic/config.yaml (BD-32).

Next `awf start` runs the pipeline. Supervisor (interactive session or
subprocess in --auto mode) creates TODO from phases/plan.md, agents
execute, supervisor verifies.

Optional: run `awf analyze-roles` (BD-31) before `awf start` if roles
have overlapping zones — adds disambiguation patches to role files.

Agent (to user): "Got it. Generating .agentic/config.yaml and roles/..."
[Agent writes project files using its own tools — plugin does NOT generate them]
```

## Troubleshooting

- **Browser didn't open:** The `open_form` result may have `browser_opened: false`
  with an error message. Tell the user the form HTML is at a local file path, or
  offer to collect the answer via chat instead.
- **User can't access browser (SSH/remote):** Offer a CLI fallback — ask the user
  to type their choice in the chat. Call `cancel_form` to clean up.
- **User says "done" but read_submit returns `submitted: false`:** The user may
  have closed the form without submitting. Ask them to check and submit again.
- **Form expired:** If `read_submit` returns `status: "expired"`, open a new form
  with a longer TTL or ask via chat.
- **Conflict on save:** If the user is saving a custom role with a name that already exists, the form will show a JS confirm dialog. If they confirm — overwrite; if cancel — they change the name or uncheck save.
