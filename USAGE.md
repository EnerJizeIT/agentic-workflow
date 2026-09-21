# USAGE — Agentic Workflow (awf)

[Русский](USAGE.ru.md) · [README](README.md)

## What you get

Structure your AI coding sessions: plan → build → verify → commit. A supervisor agent orchestrates worker agents through a pipeline, and you approve each result.

- **Never ship unreviewed code** — every change goes through a pipeline you design: any roles, any depth. From a single worker to a full analyst → architect → implementer → QA → audit chain.
- **Stay in control** — approve or reject each TODO. Reject sends the work back with fix instructions.
- **Watch in real-time** — live dashboard with agent handoffs, TODO content, worker status.
- Works **through chat**: just say what you want — the supervisor calls the right tool.

## Is this for you?

Yes, if you delegate coding to AI agents (opencode) and want:
- Quality gates (planning, review, audit) instead of raw code dumping
- Control over what gets committed
- A structured pipeline with clear roles
- Visibility into what agents are doing

## 30-second start

1. **Initialize** — *"Initialize awf in my project"* → `.agentic/` created, stack detected.
2. **Set goal** — *"Develop the MVP"* → supervisor asks what you want, opens setup form.
3. **Configure** — fill the form in browser (pick roles, pipeline, models) → *"done"*.
4. **First TODO** — supervisor studies project, dispatches first task, starts pipeline.
5. **Monitor** — dashboard opens automatically. Watch agents work in real-time.
6. **Verify** — when pipeline reaches verify, say *"verify"* → supervisor reviews, approves or rejects.

> **Tip:** You don't need to learn tool names. Just talk naturally — the supervisor figures out which tool to call.

## Everyday

| You say | What happens |
|---|---|
| *"Initialize awf"* | Creates `.agentic/`, detects stack, asks for goal |
| *"Develop the MVP"* | Opens setup form → configures pipeline → plans first TODO |
| *"Continue with the backlog"* | Supervisor reads BACKLOG, pre-checks code, dispatches next batch |
| *"Verify"* | Supervisor reads handoffs + git diff → approves or rejects |
| *"Reject — the cache is missing"* | Pipeline killed, new TODO with fix instructions dispatched |
| *"Stop after this TODO"* | Supervisor approves current work, waits for your signal |
| *"What's the status?"* | `awf_status` — active TODOs, pipeline state, stage info |

## SMO phases

Awf guides the supervisor through a deterministic flow:

| Phase | You do | Supervisor does |
|---|---|---|
| **init** | — | Creates `.agentic/`, returns compact prompt |
| **goal** | Answer "what do you want?" | Stores goal, advances to form |
| **form** | Fill setup form in browser | Opens form, recommends roles |
| **normalize** | — | Analyzes role overlaps, confirms normalization |
| **brief** | — | Studies project + BACKLOG, pre-checks code, dispatches TODO |
| **run** | Monitor dashboard | **IDLE** — waits for you to write |
| **verify** | Say *"verify"* | Reads handoffs, checks diff, approves/rejects |
| **done** | Say "continue" or "stop" | Waits for your instruction |

Every tool returns `next_action` — a hint for the next step. Even weak models follow it.

## Verify: approve or reject

| You say | What happens |
|---|---|
| *"Verify"* | Supervisor reads handoffs, checks git diff, reviews code quality |
| *"Approve"* (implicit) | Commits changes, archives TODO, pipeline exits |
| *"Reject — T8 DOMParser not done"* | Kills pipeline, asks you to dispatch fix TODO |
| *"Show me the diff"* | Supervisor runs `git diff`, shows changes |

**After approve:** pipeline exits. Say *"continue"* to dispatch next TODO, or *"stop"* to pause.

## Dashboard

Opens automatically in browser when pipeline starts:

| Tab | What you see |
|---|---|
| **💬 Agent Chat** | Handoffs as conversation: `🔍 Analyst → 📐 Architector → 🔧 Implementer` |
| **📝 Задача** | Full TODO content in rendered markdown |
| **📊 События** | Pipeline events (stage starts, completions, signals) |

- **TODO timeline** at top: `[✅ TODO-0001] ─ [✅ TODO-0002] ─ [🔄 TODO-0003]`
- **Worker status** in sidebar: PID, CPU, last log line
- **Browser notification** when verify is ready
- **Elapsed timer** from first agent start, freezes on verify

## Pre-dispatch check

Before launching a pipeline, awf greps your code for keywords from the TODO content. If a pattern is already in the codebase:

```
⚠️ Pre-check: 'days_in_period' already found in 3 file(s).
Task may be already implemented — verify before running pipeline.
```

This prevents wasted pipeline runs on already-completed work.

## Batch guidance

The supervisor batches BACKLOG tasks based on pipeline depth:

- **1-2 stages:** small TODOs OK (single fix)
- **3-5 stages:** batch multiple tasks (aim for 10+ lines of real change)
- **6+ stages:** large increments (full feature)

**Rule:** don't run a 5-agent pipeline for a one-line fix. Combine tasks.

## Crash recovery

| Situation | What happens |
|---|---|
| Worker didn't signal (no DONE/BLOCKED) | Salvage path — awf writes SALVAGE prompt, supervisor decides |
| Pipeline process died | `awf_continue` — resumes from last checkpoint |
| Orphan TODO (failed dispatch) | Not auto-cleaned. Remove with `awf_reset(orphans=True)` |
| Commit failed (pre-commit hook) | TODO NOT archived, changes left for manual review |

## Where things live

```
.agentic/
├── config.yaml              # Project config (roles, models, pipeline)
├── pipelines/default.yaml   # Pipeline stages definition
├── roles/                   # Role files (supervisor.md, worker roles)
├── phases/plan.md           # Project plan — steps with [x] checkmarks
├── inbox/                   # TODO files + dispatch signals
├── outbox/                  # Worker signals (DONE, BLOCKED, REVIEW)
├── handoff/                 # Per-stage handoff files (created at dispatch)
├── context/                 # Baselines (SHA, tests, env snapshots)
├── logs/                    # orchestrator.log, worker output
├── inputs/                  # Form submissions (created by the plugin)
├── done/{todo_id}/          # Archived TODOs (after approve)
├── state/current.yaml       # Pipeline state (stage, todo, PID, phase)
├── state/run.yaml           # Autonomous run (забег) state, if one is active
└── state/dashboard_port     # Dashboard HTTP server port
```

## Custom pipelines

You design the pipeline in the setup form. Any roles, any depth. Every
pipeline is `plan (supervisor) → worker stages → verify (supervisor)` —
the form generates the wrapper stages, you pick the middle. Stage keys:
`name`, `role`, `description`, `on_blocked` / `on_approved` /
`on_rejected`, `max_retries`, `max_rollbacks`. `kind` (plan / execute /
verify) is computed from the stage's position — do not write it.

**Single worker (fast, for small fixes):**
```yaml
# .agentic/pipelines/default.yaml
stages:
  - name: plan
    role: supervisor
    description: Supervisor studies the plan and creates a TODO
  - name: agent-implementer
    role: agent-implementer
    description: implementer executes the TODO
    max_retries: 3
  - name: verify
    role: supervisor
    description: Supervisor verifies the result
    on_approved: commit_and_next
    on_rejected: replan
```

**Full quality chain (for features):**
```yaml
stages:
  - name: plan
    role: supervisor
    description: Supervisor studies the plan and creates a TODO
  - name: agent-system-analyst
    role: agent-system-analyst
    description: analyst writes the TODO contract
  - name: agent-architector
    role: agent-architector
    description: architector designs the solution
  - name: agent-implementer
    role: agent-implementer
    description: implementer writes the code
  - name: agent-qa-review
    role: agent-qa-review
    description: qa-review finds bugs and adds tests
  - name: verify
    role: supervisor
    description: Supervisor verifies the result
    on_approved: commit_and_next
    on_rejected: replan
```

**Custom role file** (`.agentic/roles/my-custom-role.md`):
```markdown
# My Custom Role

## Responsibility
Review database migrations for safety.

## Actions
- Read migration files
- Check for destructive operations (DROP, TRUNCATE)
- Verify rollback script exists

## Prohibitions
- Do not modify application code
- Do not create new migrations
```

## Troubleshooting

| Problem | Solution |
|---|---|
| **Pipeline stuck** | `awf_kill` → `awf_continue` to resume |
| **Orphan TODO in inbox** | `awf_reset(orphans=True)` removes TODOs without progress (a live pipeline is not touched) |
| **Dashboard port taken** | Port is auto-assigned (random). If stuck: delete `.agentic/state/dashboard_port` |
| **Commit failed (pre-commit hook)** | TODO is NOT archived. Fix hook issue, then `awf_approve` again |
| **Worker didn't signal** | Salvage path triggers automatically. Supervisor reads SALVAGE prompt and decides |
| **Supervisor paused on verify >1 h** | Stage times out → salvage. Extend the window: `AWF_SUPERVISOR_TIMEOUT=7200` (env) or `awf start --timeout 7200` |
| **Phase stuck** | Check current state with `awf_current_step`, then re-run the phase-advancing tool (`awf_set_goal`, `awf_confirm_normalized`, …). Never use `awf_init(force=True)` here — it wipes runtime data (TODOs, signals, logs); a full reset is a last resort, see below |
| **Wrong roles after setup** | `awf_analyze_roles` to re-check overlaps, `awf_confirm_normalized` to advance |

To **hard reset** everything: this deletes all runtime data (TODOs, signals, logs, state).
Only do it if you are sure. Delete the `.agentic/` directory, run `awf_init(force=True)`.

## Limitations

- **Token usage:** ~2M input tokens per session (4 TODOs). Supervisor reads vision, BACKLOG, source files.
- **Pipeline depth:** tested up to 5 agent stages. More stages = longer runs, more tokens.
- **Single-machine:** not distributed. Orchestrator, workers, dashboard all run locally.
- **Linux-first:** `PR_SET_PDEATHSIG` for worker cleanup is Linux-only. macOS should work. Windows untested.
- **One TODO at a time:** pipeline processes one TODO per run. Dispatch next + `awf_start` for the next.
- **No streaming:** dashboard polls every 3 seconds (not WebSocket/SSE).

## All tools (reference)

37 tools: 32 `awf_*` workflow + 5 UI (forms).

### Lifecycle
| Tool | What it does |
|---|---|
| `awf_init` | Create `.agentic/`, detect stack, return phase prompt |
| `awf_status` | Active TODOs, pipeline state, stage info, suggestion |
| `awf_report` | Task statuses + git diff + latest test log |
| `awf_reset` | Clear runtime data (tasks_only / full / orphans) |

### Pipeline
| Tool | What it does |
|---|---|
| `awf_start` | Launch pipeline (background), dashboard opens |
| `awf_continue` | Resume interrupted pipeline |
| `awf_kill` | Kill running pipeline cleanly |
| `awf_retry_stage` | Kill + retry from salvage stage |

### TODO
| Tool | What it does |
|---|---|
| `awf_dispatch_todo` | Create TODO + baseline + signal (+ pre-check grep) |
| `awf_baseline` | Snapshot git HEAD + tests + env |
| `awf_rollback` | Reset to baseline (hard / soft / dry-run) |
| `awf_restore` | Restore an archived TODO back to the inbox |

### Verify
| Tool | What it does |
|---|---|
| `awf_approve` | Authorize commit + archive TODO |
| `awf_reject` | Write REVIEW signal + kill pipeline |
| `awf_wait_for_event` | Check for pipeline events (reactive, not for polling) |
| `awf_prove_red` | Prove declared tests are red on the baseline sha |
| `awf_verify_pack` | One deterministic verify report (GATES file) |

### Run (autonomous queue)
| Tool | What it does |
|---|---|
| `awf_run_start` | Start a run: queue of TODOs with mechanical gates |
| `awf_run_status` | Current run state: position, budget, rejects |
| `awf_run_next` | Launch the next queue item, or stop on a gate |
| `awf_run_finish` | Close the run (write RUN-REPORT) |
| `awf_run_note` | Set the run's live description for the dashboard |

### SMO
| Tool | What it does |
|---|---|
| `awf_current_step` | Detect phase + return compact prompt |
| `awf_set_goal` | Store user goal, advance to form phase |
| `awf_confirm_normalized` | Confirm roles normalized, advance to brief |

### Setup & Context
| Tool | What it does |
|---|---|
| `awf_open_project_setup_form` | Open setup form (auto-populates roles/models) |
| `awf_open_increment_planning_form` | Open decomposition variants form |
| `awf_load_supervisor_context` | One-shot: vision + plan + phase + state + next role |
| `awf_open_pipeline_dashboard` | Open dashboard (HTTP or file:// fallback) |

### Roles & Config
| Tool | What it does |
|---|---|
| `awf_add_role` | Create role template at `.agentic/roles/{name}.md` |
| `awf_analyze_roles` | Detect role zone overlaps, write disambiguation |
| `awf_check_model_config` | Validate models in config.yaml vs opencode.json |

### UI (forms)
| Tool | What it does |
|---|---|
| `open_form` | Open HTML form in browser |
| `read_submit` | Check if form was submitted, return data |
| `cancel_form` | Cancel a pending form |
| `list_pending_forms` | List opened-but-not-submitted forms |
| `list_templates` | List available form templates |
