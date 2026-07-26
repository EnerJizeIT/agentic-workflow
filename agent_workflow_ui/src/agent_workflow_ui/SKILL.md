# Agent Workflow UI

MCP plugin for [opencode](https://opencode.ai/) that gives agent tools for visual
interaction with users: HTML forms for structured input, dashboards for monitoring.

Use forms when chat is inefficient for the task at hand. Forms are a supplement
to chat, not a replacement.

## When to use a form

Call `open_form` when:

- The user needs to choose from **4+ options with descriptions** (→ `decision-tree` template).
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

## Usage pattern

1. Agent decides a form is needed.
2. Call `open_form(template="...", data={...})` — receives `form_id`.
3. Tell the user in CLI: "I opened a form in your browser. Fill it out and click Submit."
4. Call `wait_for_submit(form_id)` — this blocks until the user submits (up to 5 min).
5. When `wait_for_submit` returns `submitted: true`, analyze `data` and proceed.
6. If data is semantically invalid, open a new form with pre-filled data and error context.
7. If the user changed their mind, call `cancel_form(form_id)`.

**PREFERRED: Use `wait_for_submit`** after `open_form`. It handles polling internally
and returns as soon as the user submits. Don't manually loop with `read_submit`.

## Available templates

Call `list_templates` to see what's available. Default templates:

| Template | Description | Required data keys |
|---|---|---|
| `role-assignment` | Multi-select roles for agents | `available_roles` |
| `skill-picker` | Multi-select skills + custom entries | `available_skills` |
| `model-picker` | Dropdown model per role | `roles`, `available_models` |
| `pipeline-picker` | Radio: simple / full / custom file | `pipeline_options` |
| `conflict-resolver` | Replace / save-as / cancel | `filename`, `existing_path` |

Project-level templates in `.agentic/templates/` override defaults by name.

## Mistakes to avoid

- **DO NOT** open a form for Y/N questions — use chat.
- **DO NOT** open more than 3 forms simultaneously — the user will be confused.
- **DO NOT** forget to tell the user in CLI that a form is open in their browser.
- **DO NOT** manually poll `read_submit` in a loop — use `wait_for_submit` instead.
- **DO NOT** use forms as a chat replacement — they're a supplement.
- **DO NOT** assume the user will submit quickly — they may take minutes. `wait_for_submit` handles this.

## End-user UX guidelines

When you open a form:

- Inform the user in CLI: "I opened a form in your browser. ID: FORM-XXX".
- Call `wait_for_submit(form_id)` — it will return when the user submits.
- When submission arrives, confirm in CLI: "Got your choice: ...".
- If the user answers in CLI instead of the form, cancel the form via `cancel_form`.
- If the user says they can't open the browser, offer a CLI fallback (ask them to type the answer).

## Tool reference

### `open_form(template, data, ttl_seconds?)`

Render and open an HTML form in the user's browser.

**Parameters:**
- `template` (str, required) — Template name without extension (e.g., "role-assignment").
- `data` (dict, optional) — Variables to render in the template.
- `ttl_seconds` (int, optional) — Auto-expire form after N seconds.

**Returns:** `{form_id, browser_opened, submit_url, expires_at?, error?}`

### `read_submit(form_id)`

Check if user submitted the form. Non-blocking — returns immediately.

**Returns:** `{submitted, form_id, status, data?, submitted_at?, error?}`

**Status values:** `pending`, `submitted`, `cancelled`, `expired`, `unknown`, `error`

### `wait_for_submit(form_id, timeout_seconds?, poll_interval_seconds?)` — PREFERRED

Wait for the user to submit. Blocks (async) until submit, cancel, or timeout.

**Arguments:**
- `form_id` (str, required) — Form ID returned by open_form.
- `timeout_seconds` (int, default 300) — Max wait time.
- `poll_interval_seconds` (int, default 5) — Check frequency.

**Returns:** same as `read_submit` on success, or `{submitted: false, status: "timeout"}`.

**Use this instead of manually looping read_submit.** One call, one result.

### `cancel_form(form_id)`

Cancel a pending form. Future HTTP submits for this form_id are ignored.

**Returns:** `{cancelled, form_id, reason?}`

### `list_pending_forms()`

List forms opened but not yet submitted, cancelled, or expired.

**Returns:** `{pending: [{form_id, template, opened_at, age_seconds}], count}`

### `list_templates()`

List available form templates with metadata.

**Returns:** `{templates: [{name, source, description, required_data_keys, optional_data_keys}]}`

## TTL (time-to-live)

Forms can auto-expire. If `ttl_seconds` is set on `open_form`, the form becomes
`expired` after that duration. Expired forms are excluded from `list_pending_forms()`
and `read_submit` returns `status: "expired"`.

Use TTL for time-sensitive decisions. Don't use TTL for forms the user may need
minutes to fill out (e.g., complex role assignment).

## Example workflow

```
Agent: "I need to configure roles for your project. Let me open a form."

→ open_form(template="role-assignment", data={"available_roles": ["worker", "reviewer", "architect"]})
  Returns: {form_id: "FORM-001", browser_opened: true, submit_url: "..."}

Agent (to user): "I opened a role assignment form in your browser (FORM-001).
                   Fill it out and click Submit when done."

→ wait_for_submit("FORM-001")
  [blocks until user submits, up to 5 min]
  Returns: {submitted: true, data: {"selected_roles": ["worker", "reviewer"]}}

Agent (to user): "Got it — worker and reviewer roles selected. Proceeding..."
```

## Troubleshooting

- **Browser didn't open:** The `open_form` result may have `browser_opened: false`
  with an error message. Tell the user the form HTML is at a local file path, or
  offer to collect the answer via chat instead.
- **User can't access browser (SSH/remote):** Offer a CLI fallback — ask the user
  to type their choice in the chat. Call `cancel_form` to clean up.
- **Form expired:** If `read_submit` returns `status: "expired"`, open a new form
  with a longer TTL or ask via chat.
