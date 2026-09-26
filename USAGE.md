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

**New session or lost context** → `awf brief` (MCP `awf_brief`, CLI `awf brief`,
`--json` for the machine variant). One card assembled from the LIVE state, so it
does not go stale: header (awf version, project, phase, date), what's next, state
(run, active TODOs, blocked/salvage, last signal), the tool map by situation
(launch / check / run / hygiene / forms / metrics / context), rituals, recovery
recipes, what's new. Deterministic — the same state gives the same card (except
the date line). An empty or new project gets the setup-chain hint instead of the
working cycle.

## Verify: approve or reject

| You say | What happens |
|---|---|
| *"Verify"* | Supervisor reads handoffs, checks git diff, reviews code quality |
| *"Approve"* (implicit) | Commits changes, archives TODO, pipeline exits |
| *"Reject — T8 DOMParser not done"* | Kills pipeline, asks you to dispatch fix TODO |
| *"Show me the diff"* | Supervisor runs `git diff`, shows changes |

**After approve:** pipeline exits. Say *"continue"* to dispatch next TODO, or *"stop"* to pause.

## Supervisor tools (U11): tree-sha, mutations, todo-draft

Three CLI tools for the verify ritual — supervisor instruments, not
pipeline gates:

**`awf tree-sha`** + **`awf approve --verified-sha <hash>`** — «что
проверили = что коммитим». At the verify stage, BEFORE the checks, run
`awf tree-sha` (MCP: `awf_tree_sha`) and keep the working-tree hash (HEAD
+ all tracked changes + untracked files; same tree → same hash, at any
time). After the checks, pass it to approve — `awf approve <todo-id>
--verified-sha <hash>` (MCP: `awf_approve(verified_sha=...)`). If the tree
moved meanwhile (new commit,
edited file, new file), approve refuses with «the tree changed after
verification». The hash is stored to
`.agentic/context/VERIFIED-<todo-id>.sha`, and the run report lists both
the evidence and the verified tree. Without `--verified-sha` the behavior
is exactly as before.

**`awf mutations [--list] [--file PATH] [--timeout N]`** — mutation smoke
over `scripts/mutations.txt` (same format as `mutation-smoke.sh`, which
stays the CI step). Runs on a QUIET tree only (dirty `git status` →
refusal), reports killed/survived/timeout per mutation with test tails,
and always restores the mutated files (content + mtime, even on crash).
It is a «once per wave / before release» tool, not an auto-gate. Exit
codes: 0 = all killed, 1 = a mutation survived or the run was refused,
2 = configuration error (bad line, stale mutation, empty list).

**`awf todo-draft <AUDIT-ID|FU-NN> [--index PATH] [--out PATH]
[--force]`** — task-file skeleton from an `AUDIT-INDEX.md` registry:
facts (finding ids, sev, essence, unit, file scope extracted from titles)
plus explicit «супервизор дополняет замысел» placeholders for the intent
(criteria, verify commands, scope). It invents no criteria and touches no
network — a local index file only (default search: `<project>/`,
`.agentic/context/`). An existing `--out` file is
never overwritten without `--force`.

**`awf feedback --type bug|feature --title "…" [--body "…"]
[--severity low|medium|high] [--stdout]`** (MCP: `awf_feedback`) — the
feedback contour: friction with awf becomes a report on the owner's
desktop (config `feedback.dir`, default `~/Desktop`; the dir is created),
file `awf-<bug|feature>-<YYYYMMDD>-<slug>.md` (same day + same slug →
suffix `-2`, never an overwrite). The report assembles itself: facts
(awf version, project, phase, run — position + no_checkpoints, current
task, awf-repo git sha best-effort, date), the skeleton «Что пытался /
Ожидал / Что получил / Почему мешает / Предложение» (`--body` fills
«Что пытался»), and the tail of the newest project log (≤20 lines).
`--stdout` prints the report without writing a file. The supervisor does
not stay silent: silence does not fix the tool.

## Metrics

`awf metrics` (MCP: `awf_metrics`) collects the work program on demand: worker
tokens (in/out/cache-read) and compactions per unit from opencode.db, supervisor
tokens attributed to unit windows, +/− code lines per unit commit, and the cost
conversion "if workers had run on model X" (models.dev prices).

Scope (RUN10 #3-fix): by default only the current project's sessions are
collected — a session belongs to the project when its content (`part.data` in
opencode.db) contains the project path; `session.directory` is not a
discriminator because workers (and the supervisor) run from $HOME. The report
header says `Область: проект: …`. Sessions of other projects are excluded
from the default scope with an honest count in a report warning, never
silently; a project whose path is found in no session yields empty session
data plus a warning (explicit "no data", not foreign numbers).
`--all-projects` (MCP: `all_projects=True`) collects the whole shared
opencode.db, data mixed across projects (the pre-RUN10 behavior).

The markdown report lands in `metrics.output_dir` (default: ~/Desktop) as
`awf-metrics-<YYYYMMDD-HHMM>.md`. Set `metrics.mirror_dir` in
`.agentic/config.yaml` to keep an archive copy of every report (a failed copy
is a warning, not an error); `--no-mirror` skips the copy for one run. Update
the subscriptions table (💳 section of the report) with
`awf metrics --refresh-subscriptions` — it fetches `metrics.subscriptions_url`
and falls back to the built-in table on failure.

## Dashboard

Opens automatically in browser when pipeline starts:

| Tab | What you see |
|---|---|
| **💬 Agent Chat** | Handoffs as conversation: `🔍 Analyst → 📐 Architector → 🔧 Implementer` |
| **📝 Задача** | Full TODO content in rendered markdown |
| **📊 События** | Pipeline events (stage starts, completions, signals) |

- **TODO timeline** at top: `[✅ TODO-0001] ─ [✅ TODO-0002] ─ [🔄 TODO-0003]`
- **Pipeline chip** next to the TODO: the pipeline the stage list actually draws (a run pinned on a non-default pipeline shows its own stages, not the default's)
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
| `awf_kill` while the worker runs | Kill stops the stage TOGETHER WITH the worker (its process group). The answer names the pipeline + worker pids; a worker that survived gets a loud warning with its pid. The next `awf_start`/`awf_continue` warns about a live orphan from a previous kill — its edits would land in the new unit. CLI twin (recovery without MCP): `python3 -m awf kill --project-dir <path>` — same API, same behavior |
| Orphan TODO (failed dispatch) | Not auto-cleaned. Remove with `awf_reset(orphans=True)` |
| Commit failed (pre-commit hook) | TODO NOT archived, changes left for manual review |

## Where things live

```
.agentic/
├── config.yaml              # Project config (roles, models, pipeline)
├── pipelines/default.yaml   # Pipeline stages definition
├── roles/                   # Role files (supervisor.md, worker roles)
├── doctrine/                # Project lessons (U9) — auto-injected into every role's prompt
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

**Multiple pipelines side by side** (RUN3 #1). Keep several pipelines in one
project and run the needed one by name:

```console
$ awf pipeline-write audit-llm --role agent-implementer
Wrote pipeline 'audit-llm' (3 stages) → .agentic/pipelines/audit-llm.yaml
$ awf pipelines
  audit-llm
* default  (active)
$ awf start --pipeline audit-llm
```

- `awf pipeline-write <name> --role R1 [--role R2] [--force]` (MCP
  `awf_write_pipeline`) writes ONLY `.agentic/pipelines/<name>.yaml` —
  stages are generated like in the setup form (plan → roles → verify);
  config.yaml and supervisor.md are not touched.
- `awf pipelines` (MCP `awf_pipelines`) lists the files and marks the
  active one (`default_pipeline` from config.yaml).
- An unknown `--pipeline` name (or `awf_start(pipeline=...)`) is a clear
  error with the list of what exists — it never silently runs `default`.
- `awf status` shows the active pipeline and how many are available.

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

**Role from an opencode skill** (RUN3 #3) — one command instead of a
hand-written file. The role receives the SKILL.md body (the skill's own
YAML front-matter is stripped) under a provenance comment with the
source path and date. Project skills (`.opencode/skills/`) are checked
before global ones (`~/.config/opencode/skills/`); the role name
defaults to the skill name; an existing role file is refused without
`--force` (MCP: `awf_add_role(name, from_skill=..., force=...)`):

```console
$ awf add-role security-audit --from-skill agent-security-auditor
Created: ./.agentic/roles/security-audit.md
  (content copied from skill 'agent-security-auditor')
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
| **Phase stuck** | Check current state with `awf_current_step`, then re-run the phase-advancing tool (`awf_set_goal`, `awf_confirm_normalized`, …). Never use `awf_init(force=True)` here — it wipes runtime data (TODOs, signals, logs) and is refused while a pipeline is live. Re-running `awf_init` without `force` is safe: it deletes nothing and preserves the project byte-for-byte. A full reset is a last resort, see below |
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

47 tools: 42 `awf_*` workflow + 5 UI (forms).

### Lifecycle
| Tool | What it does |
|---|---|
| `awf_init` | Create `.agentic/`, detect stack, return phase prompt |
| `awf_status` | Active TODOs, pipeline state, stage info, suggestion |
| `awf_brief` | RUN4 #1: onboarding/recovery card — live state, tool map, rituals, recovery recipes |
| `awf_report` | Task statuses + git diff + latest test log |
| `awf_reset` | Clear runtime data (tasks_only / full / orphans) |

### Pipeline
| Tool | What it does |
|---|---|
| `awf_start` | Launch pipeline (background), dashboard opens |
| `awf_continue` | Resume interrupted pipeline — the ack pins its own unit; for an explicit pin — `todo_id` |
| `awf_kill` | Kill the pipeline **and the stage worker** (its process group) — the answer names both pids; a surviving worker is warned by pid |
| `awf_retry_stage` | Kill + retry from salvage stage — restarts EXACTLY the salvage unit (TODO pinned, not "newest active") |
| `awf_write_pipeline` | Write a named pipeline file from stages (config/supervisor untouched) |
| `awf_pipelines` | List pipelines in `.agentic/pipelines/` + the active one |

**Single-launch checkpoint bypass (RUN3 #6).** `awf_start` / `awf_continue`
accept `no_checkpoints=true` — the BD-36 plan form is skipped for that one
launch only. It is process-scoped: not written to config or state, the next
launch asks again (the run-level `awf_run_start(no_checkpoints=...)` is
unchanged).

**Engine pin (TODO-0077).** When the project IS awf (or otherwise the engine
must not import the working tree — `python -m awf` puts cwd first on
`sys.path`), set `automation.runner_dir` in `.agentic/config.yaml` to a
directory containing `awf/__init__.py`. The background child of
`awf_start` / `awf_continue` then runs from that pinned checkout;
`--project-dir` and the rest of the child argv are unchanged. The value is
validated before spawn (invalid path → `AwfApiError`, no process, no PID
file). Without the key, behavior is unchanged (`cwd` = project directory).

### TODO
| Tool | What it does |
|---|---|
| `awf_dispatch_todo` | Create TODO + baseline + signal (+ pre-check grep) |
| `awf_baseline` | Snapshot git HEAD + tests + env |
| `awf_rollback` | Reset to baseline (hard / soft / dry-run) |
| `awf_restore` | Restore an archived TODO back to the inbox |
| `awf_unblock` | Clear stale BLOCKED/ACK closures so a re-issued TODO is visible again |
| `awf_todo_remove` | Remove a never-started TODO (trace in `done/<id>/removed-<ts>.md`) |
| `awf_todo_retire` | Retire a rejected/abandoned TODO that stays "active" (RETIRED note in `done/<id>/`) |
| `awf_todo_update` | Reword a not-started TODO, keeping the number (backup in `context/`, `.ready` + baseline untouched) |

**`awf_unblock`** (CLI `awf unblock`). A re-issued TODO stays hidden while an old
`BLOCKED-<id>.ready` / `ACK-<id>.ready` is still around — `awf_status` shows an empty
list and `awf start` answers "No active TODO". Unblock moves those closure signals
(canonical and legacy forms) to a `context/unblock-<id>-<ts>/` directory — the trace
stays, the TODO is active again. DONE closures are never touched: an archived TODO
comes back only via `awf restore`. `awf_dispatch_todo` clears stale BLOCKED/ACK
closures on its own when re-issuing the same number, and refuses to re-issue a number
whose DONE closure is still in the outbox.

**`awf_todo_remove`** (CLI `awf todo-remove`). Removes a TODO that never started — no
dispatch `.ready`, no signals, no progress. The file moves to
`done/<id>/removed-<timestamp>.md`, the trace stays. A `.ready` or any signal/progress
refuses the removal and points to `awf unblock` / `awf reset --orphans`.

**`awf_todo_retire`** (CLI `awf todo-retire TODO-NNNN --reason "…"`). The reject path
writes `DONE-<id>.{md,json}` to the outbox but NOT the `DONE-<id>.ready` signal — so a
rejected TODO stays "active" in `awf status` forever (no closure, no archive). Retire
moves the TODO's files (`TODO-<id>.md`, `.ready`, `PROGRESS-*`, `DONE-*.md/.json`
without `.ready`, `REVIEW-*`) to `done/<id>/` and writes
`done/<id>/RETIRED-<timestamp>.md` with the reason, who and time — no fake DONE signal.
Refusals: no TODO file in inbox or `done/`; already archived (`done/<id>/TODO.md`);
a live pipeline on this id (`awf kill` or wait first); empty `--reason` (required).
`awf restore` still brings a retired TODO back.

**`awf_todo_update`** (CLI `awf todo-update TODO-NNNN --content-file <path> |
--content "…"`). Reword a TODO that has NOT started: the content of
`inbox/TODO-<id>.md` is replaced in place while the number, the dispatch
`.ready` and the baseline stay untouched (the baseline pins a git sha, not
the text). The previous content is backed up byte-identical to
`context/TODO-<id>.md.bak-<timestamp>`; `--reason` goes to the orchestrator
log. Refusals: no TODO file in the inbox; empty content; a started TODO
(any outbox signal, an inbox `ACK-`/`APPROVE-`, or a non-empty `PROGRESS` —
the unit is in flight: fix it via REVIEW/replan, or retire it and
re-dispatch); a live pipeline on this id.

**Untracked files and the unit commit (RUN10 #4).** The commit gate commits
only changes since the unit baseline — a file that was already untracked
BEFORE the dispatch is not the unit's work, so it is excluded from the unit
commit by design. That protection used to be silent; now it is visible:

- `awf_dispatch_todo` answers with a warning listing the pre-existing
  untracked files that will NOT join the unit commit (capped at 10, the rest
  as "…N more"; no line when the tree is clean).
- `awf_verify_pack` has an informational `untracked_excluded` section with
  the same list — pass, not fail, the exclusion is normal behavior.
- To include a pre-existing file consciously, dispatch with
  `awf_dispatch_todo(..., include_untracked=["docs/notes.md", ...])` (MCP).
  Every path must exist, be untracked, not gitignored, and stay inside the
  project; any invalid path refuses the dispatch before any side effect
  (no TODO, no baseline). The re-claimed paths are traced in
  `.agentic/context/BASELINE-<id>.include` and join the unit commit.

### Verify
| Tool | What it does |
|---|---|
| `awf_approve` | Authorize commit + archive TODO |
| `awf_reject` | Write REVIEW signal + kill pipeline |
| `awf_wait_for_event` | Check for pipeline events (reactive, not for polling); after approve it wakes with `done` — the TODO is committed + archived and the message names the next step (`awf_run_next` in a run) |
| `awf_prove_red` | Prove declared tests are red on the baseline sha |
| `awf_verify_pack` | One deterministic verify report (GATES file) |
| `awf_tree_sha` | Working-tree fingerprint (HEAD + tracked + untracked) for `awf_approve(verified_sha=...)` |

### Metrics
| Tool | What it does |
|---|---|
| `awf_metrics` | U8: token/cost metrics of the work program; report to desktop + "if workers ran on model X" cost |

### Run (autonomous queue)
| Tool | What it does |
|---|---|
| `awf_run_start` | Start a run: queue of TODOs with mechanical gates |
| `awf_run_status` | Current run state: position, budget, rejects |
| `awf_run_next` | Launch the next queue item, or stop on a gate |
| `awf_run_finish` | Close the run (write RUN-REPORT) |
| `awf_run_note` | Set the run's live description for the dashboard |

**Per-item pipeline (RUN3 #2).** The queue accepts
`{"todo_id": "TODO-0023", "pipeline": "audit-llm"}` objects alongside plain
id strings (mixed is fine); an empty/absent `pipeline` = the config default.
`awf_dispatch_todo(..., pipeline=...)` writes the name into the TODO's
front-matter, and `awf_run_next` reads it for items without a queue-level
pipeline. An unknown name refuses the launch with the list of available
pipelines (RUN3 #1).

**Long waits.** A single `awf_wait_for_event` call is cut at the single-wait
cap — 55s by default, raised via `wait.cap_seconds` in `.agentic/config.yaml`
(or env `AWF_WAIT_CAP`, which wins). The 55s default is the tool's OWN cap,
not the transport: a raised mcp timeout in opencode.json does not lift it.
The tool says so in every `next_action` and names the exact lever: while the
cap is the default it advises `wait.cap_seconds: <T>` / `AWF_WAIT_CAP=<T>`
(T = your `agent-workflow-ui` mcp timeout from opencode.json minus ~30s, when
readable). The "raise the mcp timeout in opencode.json" clause appears only
when that timeout is unknown or below the cap — never once the cap is raised
in config or env, and `suggested_timeout` then follows your cap (no stage
history: ~90% of it, 55 → 55, 600 → 540).

**Per-mode hints (RUN10 #1).** The response hints of `awf_wait_for_event` /
`awf_approve` depend on the mode. With an active run: a timeout offers the
next loop call (`awf_wait_for_event(timeout=<suggested>, actionable_only=True)`)
and the step after approve is `awf_run_next` — "wait for the user" appears
only when NO run is active. The phase reads `run` while a run is active
(not the previous cycle's `done`), and a `done` event inside a run fires
only when the run's current element is really finished (archived/committed)
— a pipeline death mid-iteration keeps the wait (timeout/idle).

**Supervisor authority.** Replanning, rewriting the spec, and splitting a
task are a standard supervisor option — no owner approval required.
Escalation to the owner happens only on the stop-list: a twice-rejected
iteration, BLOCKED without a resolution, budget, an audit point. The reason
for every replan/split goes into the run report/note.

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
| `awf_add_role` | Create a role at `.agentic/roles/{name}.md`: template, or from an opencode skill (`from_skill`) |
| `awf_analyze_roles` | Detect role zone overlaps, write disambiguation |
| `awf_check_model_config` | Validate models in config.yaml vs opencode.json |
| `awf_feedback` | Write a bug/feature report about awf friction to the owner's desktop (RUN4 #2) |

### UI (forms)
| Tool | What it does |
|---|---|
| `open_form` | Open HTML form in browser |
| `read_submit` | Check if form was submitted, return data |
| `cancel_form` | Cancel a pending form |
| `list_pending_forms` | List opened-but-not-submitted forms |
| `list_templates` | List available form templates |
