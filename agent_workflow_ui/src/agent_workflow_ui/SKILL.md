# Agent Workflow UI

MCP plugin for [opencode](https://opencode.ai/) that gives agent tools for visual
interaction with users: HTML forms for structured input, dashboards for monitoring.

Use forms when chat is inefficient for the task at hand. Forms are a supplement
to chat, not a replacement.

## When to use a form

Call `open_form` when:

- The user needs to choose from **4+ options with descriptions** (→ `decision-tree` template).
- **Setting up a new project** with full configuration (→ `project-setup` composite template).
- Selecting **roles, skills, or models** for a project (→ `role-assignment`, `skill-picker`, `model-picker`).
- Choosing a **pipeline template** (→ `pipeline-picker`).
- Resolving a **conflict** like "replace / save-as / cancel" (→ `conflict-resolver`).
- A **file upload** is needed (product-vision doc, custom role definition).
- **Prioritizing** 10+ items (→ `priority-matrix`, future template).
- Monitoring a **long-running pipeline** (→ dashboard, future scope).

## When NOT to use a form (use chat instead)

- A simple **Y/N** answer.
- Choosing between **2-3 short options**.
- **Clarifying questions** during work.
- **Tone calibration** or approach discussion.
- Anything that can be answered in one line of text.

## Usage pattern — NON-BLOCKING

**Always use `open_form` (non-blocking). Never use `open_form_and_wait`.**

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

> "I've opened a form in your browser (Form ID: FORM-001).
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
- `template` (str, required) — Template name without extension (e.g., "role-assignment").
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

### `open_form_and_wait(template, data, timeout?)` — DO NOT USE

Blocking version of open_form. **Do not use this tool** — it freezes the agent
while waiting for the user. Use `open_form` instead and let the user notify you
when they're done.

## Available templates

Call `list_templates` to see what's available. Default templates:

| Template | Description | Required data keys |
|---|---|---|
| `role-assignment` | Multi-select roles for agents | `available_roles` |
| `skill-picker` | Multi-select skills + custom entries | `available_skills` |
| `model-picker` | Dropdown model per role | `roles`, `available_models` |
| `pipeline-picker` | Radio: simple / full / custom file | — |
| `conflict-resolver` | Replace / save-as / cancel | `conflict_name`, `conflict_context` |
| `project-setup` | Composite form: roles + skills + models + pipeline + verification. Models pulled from opencode config automatically. | `available_roles` |

Project-level templates in `.agentic/templates/` override defaults by name.

## Mistakes to avoid

- **DO NOT** use `open_form_and_wait` — it blocks the agent. Use `open_form` instead.
- **DO NOT** forget to tell the user to come back to CLI after submitting.
- **DO NOT** open a form for Y/N questions — use chat.
- **DO NOT** open more than 3 forms simultaneously — the user will be confused.
- **DO NOT** use forms as a chat replacement — they're a supplement.

## Example workflow

```
Agent: "I need to configure roles for your project. Let me open a form."

→ open_form(template="role-assignment", data={"available_roles": ["worker", "reviewer", "tester"]})
  Returns: {form_id: "FORM-001", browser_opened: true, submit_url: "..."}

Agent (to user): "I've opened a role assignment form in your browser (FORM-001).
                   Fill it out and click Submit, then tell me here when you're done."

[Agent is free — can answer other questions, do other work, or wait]

User (in CLI): "Done!"

→ read_submit("FORM-001")
  Returns: {submitted: true, data: {"selected_roles": ["worker", "reviewer"]}}

Agent (to user): "Got it — you selected worker and reviewer. Proceeding with setup..."
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
