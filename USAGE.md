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

## Supervisor tools: tree-sha, mutations, todo-draft, feedback

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
is exactly as before — **except in an active run (забег), where it is
REQUIRED** (V-03, 2026-09-27): approve without the fingerprint is refused
and no APPROVE signal is published. In an active run the approve is also
BOUND to the cycle (M2.1, 2026-09-27): the `APPROVE-<todo-id>.ready`
signal carries the run generation, the verified fingerprint, and the
digest of the file set the commit gate will apply. If the run is
restarted or revised after the approve, the stale approval no longer
unlocks the commit — the gate refuses with a clear message, nothing is
committed, and a fresh `awf_approve` on the current run is required.
Outside a run the signal stays an empty marker as before.

**`awf mutations [--list] [--file PATH] [--timeout N]`** — mutation smoke
over `scripts/mutations.txt` (the same base list the CI test job runs as
a gate step, `bash scripts/mutation-smoke.sh`). Runs on a QUIET tree only
(dirty `git status` → refusal), reports killed/survived/timeout per
mutation with test tails, and always restores the mutated files (content
+ mtime, even on crash). The 4-mutant base list is an auto-gate in CI on
every push/PR; this command (and the `--report` run over
`scripts/mutations-neg5.txt`) is the «once per wave / before release»
ritual on top of it. Exit
codes: 0 = all killed, 1 = a mutation survived or the run was refused,
2 = configuration error (bad line, stale mutation, empty list).

**`awf todo-draft <AUDIT-ID|FU-NN> [--index PATH] [--out PATH]
[--force]`** — task-file skeleton from an `AUDIT-INDEX.md` registry:
facts (finding ids, sev, essence, unit, file scope extracted from titles)
plus explicit «супервизор дополняет замысел» placeholders for the intent
(criteria, verify commands, scope). It invents no criteria and touches no
network — a local index file only (default search: `<project>/`,
`.agentic/context/`). `--out` must land under the project's `.agentic/`:
an outside path (the project root included) or a symlinked component is
refused before a file appears, and a relative path resolves from the
current directory. An existing `--out` file is never overwritten without
`--force`.

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

**`awf config-get <dotted.key> [--project-dir DIR]`** — prints one
`config.yaml` value for scripts: strings raw, other values as YAML. Exit
codes: 0 — value printed, 1 — key absent, 3 — YAML `null`, 4 — a
whitespace-only string, 5 — `config.yaml` is malformed. The canonical
suite helper asks not `config-get` but `awf suite-command`, which returns 0
for the configured command, 1 for the legacy pytest fallback, 2 for a
`null`/empty key ("not configured") and 5 for a broken `config.yaml` (it
names the broken project instead of falling back to pytest). The helper
honours `AWF_SUITE_CMD` (a shell command that replaces the configured
suite command) and `AWF_PYTHON` (the interpreter for the default pytest
command).

## Workflow commit

**`awf commit-workflow`** (MCP: `awf_commit_workflow`) — commits the
non-gitignored workflow definitions of `.agentic/`: `config.yaml`,
`roles/`, `pipelines/`, `phases/`, `doctrine/` (changed tracked + new
untracked). Roles/pipelines added mid-project never reach unit commits
(a unit commit carries only the unit's diff; read-only units commit
nothing at all) — this is the commit path for them. Runtime
(inbox/outbox/state/logs/context/handoff) is never touched; the commit
runs through a throwaway git index, so the user's index and any foreign
staged changes are unaffected. Subject: `awf(workflow): <n> files:
<groups>`; body: the file list. Empty set — a clear refusal, nothing
committed. `awf_status` shows the same list as
`uncommitted_workflow_files` (empty = nothing left to commit). Unit
commits are unchanged: workflow files still ride along in them as before.

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
| Worker didn't signal, but work is in the tree | Continue-push retry with the note "the work is already in the tree — do not redo it, finish it and signal" — budget `retry.silent_work_attempts` (config.yaml, default 2), counted per (TODO, stage), survives kill+continue. The first retry is plain; the second also reduces the pass to targeted checks and gates, then the salvage path. Each retry names the last worker log and DONE draft so the worker resumes instead of re-reading the task |
| Worker didn't signal, no work | Two auto continue-push retries, then the salvage path — awf writes SALVAGE prompt, supervisor decides |
| Pipeline process died | `awf_continue` — resumes from last checkpoint |
| Stage state lingers after the engine died, no signal | Zombie-state: `awf_status`/`awf_brief` show the hint with `awf_continue(todo_id=…)`. When the unit is already archived, `awf_run_finish` clears the stale state itself and records the reset in the RUN-REPORT |
| Pipeline killed by an opencode/MCP restart (parked at verify) | The restart terminates the pipeline's process group — the `setsid`-detached process does not survive. Recovery: relaunch the stage + re-approve (below) |
| awf tools gone from the session after a "restart" | A "restart" that is really a resume keeps the dead MCP connections (the run id in the log is unchanged). Recovery: a NEW session, not "continue" (below) |
| "Connection closed" on a dispatch/approve | The call may have SUCCEEDED — the unit was created and only the answer was lost. Check the inbox before retrying (below) |
| `awf_kill` while the worker runs | Kill stops the stage TOGETHER WITH the worker (its process group). The answer names the pipeline + worker pids; a worker that survived gets a loud warning with its pid. The next `awf_start`/`awf_continue` warns about a live orphan from a previous kill — its edits would land in the new unit. CLI twin (recovery without MCP): `python3 -m awf kill --project-dir <path>` — same API, same behavior |
| Orphan TODO (failed dispatch) | Not auto-cleaned. Remove with `awf_reset(orphans=True)` |
| Commit failed (pre-commit hook) | TODO NOT archived, changes left for manual review |

### Pipeline died on an opencode/MCP restart

Restarting opencode kills the MCP connection and the pipeline's process
group — the `setsid`-detached pipeline process does not survive it. That
is a fact, not a bug: cgroup termination takes the whole group, `setsid`
only detaches from the terminal session. Recovery:

1. Restart opencode. Read `.agentic/state/current.yaml` — it names the
   dead stage and the TODO.
2. Relaunch the stage — MCP `awf_continue(todo_id="TODO-NNNN",
   from_stage="<stage from state>")`, or CLI
   `python3 -m awf continue --project-dir <path> --from-stage <stage>
   --background`.
3. Approve AFTER the stage starts waiting. An ACK/APPROVE written before
   the new wait started is dropped as stale (AUD04-04: its mtime predates
   the wait). That is expected, not a failure — write the approval again;
   the second one is the one consumed.

### Tools didn't come back after the "restart"

The previous recipe assumes the tools come back with the restart. They
do not, when the "restart" was a resume of the SAME session — the run id
in the opencode log is unchanged, and session resume does not recreate
MCP connections that dropped before the restart. The awf tools
(`agent-workflow-ui_*`) stay unavailable to that session even though the
server itself starts fine. When the tools are gone after a restart:

- Open a NEW session, not "continue". On start, opencode launches the
  MCP server and registers the tools.
- The run and unit state live on disk (`.agentic/`) — the new session
  picks them up immediately; just continue the run (`awf_run_next` /
  `awf_continue`).

MCP auto-restart after a dropped connection is opencode client behavior,
not awf — awf cannot make the client reconnect. From the awf side, the
server's fault log (below) shows where the server died.

### "Connection closed" on a dispatch/approve

A dropped MCP connection can mask a SUCCESSFUL call: the unit was
created (TODO + baseline + `.ready` on disk) and only the answer was
lost. After "Connection closed" on a mutating call (dispatch, approve):

- Check the inbox BEFORE retrying: `.agentic/inbox/TODO-*.md`, the
  `.ready` signals, and `.agentic/outbox/`.
- The idempotent check is `awf_status` (MCP or
  `python3 -m awf status --project-dir <path>`): it names what already
  exists. Retry only when the unit is genuinely absent.

### Traces of the MCP server

When opencode kills the MCP session, the plugin leaves a small fault log
— lifecycle and crash traces, not just pipeline state: a `START` line
(plugin version, pid, argv), a `STOP` line on clean exit, and a
`CRASH`/`THREAD-CRASH` traceback if the server dies on an unhandled
exception. The file lives at `$XDG_STATE_HOME/awf/mcp-server.log`
(fallback `~/.local/state/awf/mcp-server.log`); point it elsewhere with
the `AWF_MCP_LOG` env var. It rotates once to `.1` past 1 MB.

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

## Engine liveness and worker isolation

Two optional `config.yaml` blocks (defaults shown — an old config without
them keeps the documented behaviour):

```yaml
engine:
  heartbeat_seconds: 30          # clamped to <= 30 (a larger value warns)
  heartbeat_stale_seconds: 120   # default max(3 x heartbeat_seconds, 120)
  kill_with_parent: false        # true = the engine dies with its launcher
worker:
  mcp_servers: [agent-workflow-ui]  # MCP servers an EXECUTE worker keeps
```

`kill_with_parent: false` is the durable-run default (TODO-0242): a restart
of opencode or the MCP server must not take a running pipeline with it. The
heartbeat drives `engine_dead`; the ceiling keeps the verdict within the
status surfaces' patience. `worker.mcp_servers` trims the execute worker's
tool context to the control server; every other configured MCP server is
switched off for that subprocess (a name the config does not define is a
warning, never a failure). An explicit empty list (`mcp_servers: []`) is a
value, not an absence: it turns every MCP server off for the worker.

A third block caps the unit's review rounds:

```yaml
limits:
  max_review_passes: 3      # 0 disables; valid range 0..20
```

The counter is the number of `## REVIEW-фикс` sections in the unit's TODO —
the rounds the supervisor appended. A silent retry, a salvage or another
attempt does not change it. When the count reaches the cap, the unit does not
continue: the verify entry stops the pipeline, `awf continue` refuses the
implementer stage and `awf run_next` stops the run with `budget exhausted`.
`outbox/BUDGET-<id>.md` carries the price (passes, the last BLOCKED findings,
per-stage time, the round headings); no role switch or automatic retry
follows — the owner decides. A value outside 0..20 is a config refusal, not a
silent "no limit".

A stage's time cap has a third input besides the project key:

```yaml
automation:
  stage_timeout_seconds: 3600   # cap for ONE agent stage, seconds
```

Priority: an explicit `awf start --timeout N` (MCP `timeout`) wins, then the
stage's own `timeout: N` in `pipeline.yaml`, then this key, then 3600. Only a
positive integer is accepted: `0`, `-5` and `"1h"` refuse the launch and name
the input instead of silently falling back to 3600. The stage log line
`stage timeout: <N>s (source: <caller|stage|project|default>)` names the
effective cap and where it came from.

`awf_status` and `awf_brief` show the active unit's `passes: N/max`; at the
cap the State section adds `budget: exhausted`. The RUN-REPORT's
`## Unit economics` section gives every unit in the run one line: `passes`,
`wall`, `stages: plan a/impl b/arch c/qa d/audit e/verify f` (from
`orchestrator.log`) and `gates: qa_fail K раз, contract_fail L` (from the
unit's `context/GATES-<id>.md`). Missing data reads `n/a`, never a measured
zero.

`awf supervise` is the watchdog for the leftover case: while the run has an
active unit it restarts a dead engine (`awf continue`) up to
`--max-restarts` (default 5) with `--pause` seconds between checks (default
30), logging `supervise: restart #k` into `logs/awf-supervise.log`. When the
budget is exhausted it stops and reports instead of looping forever.

## Custom pipelines

You design the pipeline in the setup form. Any roles, any depth. Every
generated pipeline (except the quick tier, which drops the closer) is
`plan (supervisor) → worker stages → closer gate →
verify (supervisor)` — the form adds the wrapper stages, you pick
the middle. Stage keys:
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

**Unit levels: quick vs full.** The setup form writes a second pipeline
next to the active one — `.agentic/pipelines/quick.yaml`, the same team
minus the QA stage (`agent-qa-review`) and minus the closer gate with its
follow-up check (`checklist-close` / `checklist-check`). Pick the level per unit:

- **quick** — S-class units: docs, shield tests, counters, one-line fixes
  that change no behavior. No closer: the quick tier stays the short set,
  so a red completion checklist is not closed for you — it still shows up
  as a failed check at verify; fix it by hand or run the unit on the full
  pipeline.
- **full** (the active pipeline) — engine, security, behavior, data
  schemas. Carries the closer gate and its follow-up check. The shipped `full`
  and `code-health` starters carry both too.
- **single** (a `stage_mode` of the setup form) — one working role per
  unit, NOT one agent: the closer gate and `verify` still run.
- When in doubt, full.

An existing `quick.yaml` is never overwritten: the setup form keeps the file
and returns it as is, so delete it when you want it rebuilt for a new team.

How to choose: `pipeline: quick` in the TODO front-matter, or a per-item
`{"todo_id": "TODO-NNNN", "pipeline": "quick"}` in the run queue (the
queue item wins over the front-matter). A quick unit's `verify` block is
a slice of the suite that covers the changed behavior — fewer probes,
but targeted.

The level changes the pipeline, not the contract: the gates
(contracts/ratchet/instructions), `prove_red` (where declared), binding
and evidence, and the full CI still apply to quick units.

**Non-code composition (analysis, research, documentation).** The same
schema serves tasks whose product is documents, not code: each stage
declares the document it produces as `output`, and the engine checks it
(present, written by this stage) before the stage advances. The verify
commit carries those documents — a unit commit is not code-only.

```yaml
stages:
  - name: plan
    role: supervisor
  - name: analyst
    role: agent-analyst
    task: Gather and structure the requirements
    output: docs/requirements.md
    tools:
      deny: [edit, write]
  - name: writer
    role: agent-writer
    task: Write the final analysis
    input: docs/requirements.md
    output: docs/analysis.md
  - name: verify
    role: supervisor
    on_approved: commit_and_next
```

**Multiple pipelines side by side.** Keep several pipelines in one
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
  stages are generated like in the setup form
  (plan → roles → closer gate → verify); config.yaml and supervisor.md
  are not touched.
- `awf pipeline-write <name> --variant full|code-health` writes one of the
  built-in starter pipelines — byte-identical to what the project-setup
  form offers. Role files are the project's responsibility here: the
  command warns about a missing role instead of materializing it (that is
  the setup flow's job).
- `awf pipelines` (MCP `awf_pipelines`) lists the files and marks the
  active one (`default_pipeline` from config.yaml).
- An unknown `--pipeline` name (or `awf_start(pipeline=...)`) is a clear
  error with the list of what exists — it never silently runs `default`.
- `awf status` shows the active pipeline and how many are available.

**Spec pipeline for a named package.** `awf spec init <slug> [--project DIR]
[--force]` writes `.agentic/pipelines/spec-<slug>.yaml` from the packaged
`spec` template: plan → six author stages → verify. The six outputs are
`docs/spec/<slug>/<slug>.*`; an existing pipeline is refused without
`--force`. A requirements review whose document says `Статус: FAIL` rewinds
the stage to `spec-requirements` without a supervisor replan; a readiness
report whose document says `Статус: BLOCKED` rewinds to its author
(requirements or architecture). Check package readiness with
`awf spec check docs/spec/<slug>` (rc 0 = READY).

The spec unit is the package PRODUCER: a TODO with `spec_package` and the
`spec` / `spec-<slug>` pipeline is not held by the consumer readiness gate
(its run is what makes the package READY); only consumer units are gated.
The pipeline name comes from the unit's frozen snapshot, the launch
argument or the TODO's own `pipeline:` key — a crashed spec run resumes
without repeating `--pipeline`.
The six stage roles (`agent-project-auditor`, `agent-system-analyst`,
`agent-requirements-reviewer`, `agent-architector`, `agent-qa-review`)
ship as role templates, so setup and `awf add-role` install them in a clean
project.

**Compose a pipeline in the browser.** The `pipeline-compose` form (MCP
`open_form(template="pipeline-compose", project_dir=..., data={"pipeline": "<name>"})`)
is the pipeline editor: stage rows (role, id, task, input, output,
policies, budgets) in the run order, prefilled from the selected (or
active) pipeline file, with role/policy suggestions from the project and
the pipeline schema. "Preview" shows the target file and the exact stage
list without writing. Submit applies through the same `write_pipeline`
API as `awf_write_pipeline` (the "overwrite" checkbox is its `force`) and
always warns: an active run is not changed — that is `awf_run_revise`.

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

**Role from an opencode skill** — one command instead of a
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

**Role draft area** — create a role candidate without touching the live
team: `--draft` writes it to `.agentic/roles/draft/<name>.md` (live roles,
config and the pipeline stay as they were; the candidate carries a source
marker: skill / builtin / template). List candidates with `--list-drafts`
(name + source, a hand-placed file lists as `file`), adopt one with
`--adopt <name>` — a name conflict with a live role is an explicit
refusal, awf never overwrites a live role — and remove one with
`--discard <name>` (the candidate is kept as a trace in
`.agentic/context/`). Adopt is also the normalization step (M5.3):
zone analysis re-runs and the BD-31 disambiguation addenda are
refreshed — only the BD-31 blocks of role files change, config and
the pipeline are untouched, the setup phase is not re-run. MCP:
`awf_add_role(name, draft=True)`,
`awf_add_role(list_drafts=True)`, `awf_add_role(adopt=...)`,
`awf_add_role(discard=...)` — one action per call.

**Refresh a role from its skill** — `awf add-role <name> --refresh` (MCP
`awf_add_role(name, refresh=True)`) re-materializes an existing
skill-sourced role: the skill body and model line are rebuilt, while
AWF-OVERRIDE, BD-31, the discipline/suite-canon blocks and the owner's
own lines are kept. Pre-marker copies of the managed rules are replaced,
never duplicated. A role without managed markers is refused (nothing is
written) unless `--force` is passed; a role whose provenance skill is
gone reports `skipped` with the reason. A repeated call is a no-op.

**Role model line** — every role file created by `awf add-role`
names its model in a uniform line near the top
(`**Модель:** \`vllm/llm\``), resolved as: explicit `--model`/`model=`
→ `models:` of `.agentic/config.yaml` by role name → "not assigned
(the config's default model applies)" (built-in templates, the
placeholder and `from_skill` sources all get the line). The config
stays the source of truth — which model a stage actually runs is
decided by the engine, the line is a marker.
`awf_check_model_config` cross-checks the role files against the
config and reports a drift as a warning (role file says X, config
says Y); a missing line is not an error.

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
- **Linux-first:** `PR_SET_PDEATHSIG` for worker cleanup is Linux-only. macOS should work. Windows is unsupported: `start` and the destructive paths (`reset`/`rollback`/`init --force`) refuse on non-POSIX.
- **One TODO at a time:** a pipeline processes one TODO per run; a run (забег)
  chains them — `awf_run_next` launches the next queued TODO.
- **No streaming:** dashboard polls every 3 seconds (not WebSocket/SSE).

## All tools (reference)

56 tools: 51 `awf_*` workflow + 5 UI (forms).

### Lifecycle
| Tool | What it does |
|---|---|
| `awf_init` | Create `.agentic/`, detect stack, return phase prompt |
| `awf_status` | Active TODOs, pipeline state, stage info, suggestion |
| `awf_brief` | Onboarding/recovery card — live state (run: goal, last decision, sources), tool map, rituals, recovery recipes |
| `awf_report` | Task statuses + git diff + latest test log |
| `awf_commit_workflow` | Commit `.agentic/` workflow definitions (config, roles, pipelines, phases, doctrine) the units missed; `awf_status` shows the dry-run list |
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

**Single-launch checkpoint bypass.** `awf_start` / `awf_continue`
accept `no_checkpoints=true` — the BD-36 plan form is skipped for that one
launch only. It is process-scoped: not written to config or state, the next
launch asks again (the run-level `awf_run_start(no_checkpoints=...)` is
unchanged).

**Engine pin.** When the project IS awf (or otherwise the engine
must not import the working tree — `python -m awf` puts cwd first on
`sys.path`), set `automation.runner_dir` in `.agentic/config.yaml` to a
directory containing `awf/__init__.py`. The background child of
`awf_start` / `awf_continue` then runs from that pinned checkout;
`--project-dir` and the rest of the child argv are unchanged. The value is
validated before spawn (invalid path → `AwfApiError`, no process, no PID
file). Without the key, behavior is unchanged (`cwd` = project directory).

**Reviewer permission hint.** `automation.readonly_roles` in
`.agentic/config.yaml` is a list of role names (default: empty). For a
listed role awf forces `edit`/`write` to `deny` in the generated
`OPENCODE_CONFIG_CONTENT`, overriding any user permissions; `bash` stays
allowed (deliberate — QA runs tests). Full isolation of a role is out of
scope for awf and belongs to the ORCH plan. Example:
`automation.readonly_roles: [agent-qa-review]`.

**Worker tool profile.** For execute stages (all pipeline stages
between `plan` and `verify`) awf adds an agent-scoped permission block
to the generated `OPENCODE_CONFIG_CONTENT` that denies the control MCP
tools — approve, reject, start, continue, kill, retry-stage, reset,
rollback, restore, unblock, init, baseline, dispatch-todo,
todo-remove/retire/update, run-start/next/finish/note, set-goal,
confirm-normalized (22 in total, `awf._env.CONTROL_TOOLS`). The worker
model does not see these tools in its tool list; `bash`, file and
search tools and observability tools (status, brief, report) stay
available. `plan`/`verify` stages keep the full set. This reduces
accidental errors — it is not a security boundary; strict isolation of
a worker is a separate effort.

**Stage tools profile (ORCH M7.2).** A stage may declare `tools` with
`allow`/`deny` lists of opencode permission keys — `bash`, `edit`,
`write`, `webfetch` and MCP tools as `<server>_<tool>`:

```yaml
stages:
  - name: plan
    role: supervisor
  - name: analyst
    role: agent-analyst
    tools:
      deny: [edit, write]
  - name: verify
    role: supervisor
```

The profile merges onto the base permission rules at spawn (host
`opencode.json` → awf overrides → `readonly_roles` → the M2.2 control
denial): deny beats allow, and the base control prohibitions are never
cancelled — a profile cannot re-enable the control tools on an execute
stage or `edit`/`write` for a readonly role. An unknown key is a
pipeline load error (single registry, same rule set as the stage
schema: load and write). Without `tools` the behavior is unchanged.
The profile narrows the stage's discretionary tools — it is not a
security boundary (`bash` stays allowed unless the profile denies it).
`plan`/`verify` stages are unrestricted by default; a declared profile
applies to any stage kind.

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
| `awf_review_append` | Append a supervisor `## REVIEW-фикс (супервизор, виток N)` section to a started TODO (numbered, backup in `context/`) |
| `awf_blocked_draft` | Build a ready `## REVIEW-фикс` draft from a standard BLOCKED note (findings + fix directions + empty decision slots; the TODO is not touched) |

**`awf_unblock`** (CLI `awf unblock`). A re-issued TODO stays hidden while an old
`BLOCKED-<id>.ready` / `ACK-<id>.ready` is still around — `awf_status` shows an empty
list and `awf start` answers "No active TODO". Unblock moves those closure signals
(canonical and legacy forms) to a `context/unblock-<id>-<ts>/` directory — the trace
stays, the TODO is active again. DONE closures are never touched: an archived TODO
comes back only via `awf restore`. `awf_dispatch_todo` clears stale BLOCKED/ACK
closures on its own when re-issuing the same number, and refuses to re-issue a number
whose DONE closure is still in the outbox.

**`awf_todo_remove`** (CLI `awf todo-remove`). Removes a TODO that never started — no
signals, no progress. The dispatch `.ready` is an artifact and does not block (it moves
beside the file). The file moves to
`done/<id>/removed-<timestamp>.md`, the trace stays. A signal or progress refuses
the removal: stale closures point to `awf unblock`, a started unit to
`awf todo-retire`. The removal is
also refused while the active run queue (main or service slot) still names the TODO —
replace it in the queue first (`awf_run_revise` with `{"from": …, "to": …}`) or finish
the run; otherwise the queue would keep a dangling reference.

**`awf_review_append`** (CLI `awf review-append TODO-NNNN --text "…" |
--text-file <path>`). Appends a numbered `## REVIEW-фикс (супервизор, виток N)`
section to a STARTED unit (PROGRESS, an outbox signal, or an inbox ACK/APPROVE),
backing the previous content up to `context/TODO-<id>.md.bak-<ts>`; the number, the
dispatch `.ready` and the baseline stay untouched. Refusals: empty text; no TODO file
in the inbox; a unit that never started (a queued TODO takes `awf todo-update`); an
archived unit (`awf restore` first); a live pipeline on this id (`awf kill` first).

**`awf_todo_retire`** (CLI `awf todo-retire TODO-NNNN --reason "…"`). The reject path
writes `DONE-<id>.{md,json}` to the outbox but NOT the `DONE-<id>.ready` signal — so a
rejected TODO stays "active" in `awf status` forever (no closure, no archive). Retire
moves the TODO's files (`TODO-<id>.md`, `.ready`, `PROGRESS-*`, `DONE-*.md/.json`
without `.ready`, `REVIEW-*`) to `done/<id>/` and writes
`done/<id>/RETIRED-<timestamp>.md` with the reason, who and time — no fake DONE signal.
Refusals: no TODO file in inbox or `done/`; already archived (`done/<id>/TODO.md`);
a live pipeline on this id (`awf kill` or wait first); an active run slot still
planning to launch the TODO in its not-started queue region (replace it there
first); empty `--reason` (required). A rejected CURRENT element is not refused —
retry-replace it first (`{"from", "to"}`): a retire first marks it completed and
closes the retry slot.
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

**Untracked files and the unit commit.** The commit gate commits
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

**Replans are marked (BD-30, TODO-0213).** When a unit blocks and the
pipeline escalates to the supervisor, the live pipeline waits for the
supervisor's answer. Only a TODO whose front-matter carries the marker
`replan_for: <blocked unit id>` is picked up as that answer — an ordinary
mid-run dispatch (no marker) does NOT switch the running pipeline; the
escalation keeps waiting. `awf_dispatch_todo(..., replan_for="TODO-0204")`
writes the marker into the TODO's contract block (a hand-written
`replan_for:` line works the same way, the parameter wins over a
copy-pasted one). The referenced unit must exist and be live: a missing
TODO, an archived one, or one closed as done refuses the dispatch before
any side effect. The replanned unit restarts the pipeline from the first
stage (the full pipeline, not the blocked stage); the pickup line in the
orchestrator log names the marker and the replaced unit. The marker is
enforced on BOTH wait surfaces (the interactive wait and the auto
supervisor subprocess — watcher and fallback detection). When no marked
answer arrives (wait timeout, or the auto supervisor exits without one),
the pipeline stops cleanly on the blocked unit — the BLOCKED closure
stays; dispatch a marked TODO and `awf continue`.

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
| `awf_metrics` | Token/cost metrics of the work program; report to desktop + "if workers ran on model X" cost |

### Run (autonomous queue)
| Tool | What it does |
|---|---|
| `awf_run_start` | Start a run: queue of TODOs with mechanical gates |
| `awf_run_status` | Current run state: position, budget, rejects |
| `awf_run_next` | Launch the next queue item, or stop on a gate |
| `awf_run_finish` | Close the run (write RUN-REPORT) |
| `awf_run_note` | Set the run's live description for the dashboard |
| `awf_run_revise` | Revise the run's NOT-STARTED queue elements, or replace a REJECTED current element with a retry TODO (preview + apply, idempotent by key; `stop_running` stops a live unit via the engine's kill path — not for the retry path, `awf_kill` first) |
| `awf_run_service_start` | Start a SERVICE run — create a role candidate during the main run (separate state file; the main run is untouched) |
| `awf_run_service_status` | One read, two snapshots — the service run and the main run |
| `awf_run_service_finish` | Close the service run (SERVICE-RUN-REPORT) and name the main continuation |
| `awf_run_service_approve` | Approve the service unit at verify (evidence required; verdict in the service slot) |

**Per-item pipeline.** The queue accepts
`{"todo_id": "TODO-0023", "pipeline": "audit-llm"}` objects alongside plain
id strings (mixed is fine); an empty/absent `pipeline` = the config default.
`awf_dispatch_todo(..., pipeline=...)` writes the name into the TODO's
front-matter, and `awf_run_next` reads it for items without a queue-level
pipeline. An unknown name refuses the launch with the list of available
pipelines (RUN3 #1).

**Queue revision (ORCH M3.4).** `awf_run_revise(project_dir, queue,
reason, key, preview, stop_running)` revises the pipeline of the run's
not-started queue elements and, with RB-3, replaces a REJECTED current
element with a retry TODO — a completed or approved current is never
touched. `preview=true` shows the current queue, the elements that
would change and the conflicts (current/started/approved/rejected/
completed/not in the queue) without writing anything. The apply is atomic
(a conflict refuses the whole
request) and is idempotent by `key`: a repeat with the same key AND the
same payload is a no-op (a different payload under the same key is refused).
By default it is refused while a stage is running (stop the unit first).
`stop_running=true` authorizes the stop: the unit is stopped by the
engine's standard kill path (`kill_pipeline`: the pipeline's TERM → grace →
KILL plus the worker-tree kill and the `last-kill` record), liveness is
re-checked, and the revision applies only after the stop is confirmed. The
resume point (the stopped stage, from `stage_name`/`todo_id` captured
BEFORE the kill, which clears the pipeline state) is restored afterwards,
so a plain `awf continue` resumes the unit from that stage — the response
names it explicitly (`resume_from`) and the revision record in
`state/run.yaml` (`revisions`) carries it too. A stop that does not take
(the process is still alive after the kill) refuses WITHOUT applying the
revision: the run stays consistent and the retry is possible. With no live
stage, `stop_running=true` is a no-op stop, not an error. The applied
revision is recorded in `state/run.yaml` (`revisions`, the decisions' path)
in the same CAS write as the queue change (A-13 generation condition); the
revised pipelines take effect at each element's launch (`awf_run_next`).

**Retry a rejected element (RB-3).** When the current element is REJECTED,
`awf_run_revise` takes `{"from": "TODO-OLD", "to": "TODO-NEW"}` as a retry:
the fresh TODO takes the rejected element's slot (same position, same queue
length), the run stays active, and the reject counter/verdict stay in
`awf_run_status`. `awf_run_next` then launches the retry there and the rest
of the queue follows. The retry target must be a fresh id (no earlier
reject — approve would refuse it forever). The path applies only to a dead
stage: `stop_running` does NOT stop a live unit for a retry — call
`awf_kill` first. A live/approved/completed current is a conflict. After
the retry is applied, retire the replaced TODO (`awf_todo_retire`): a
retire first would mark it completed and close the retry slot.

**Queue shapes and the idempotency key (TODO-0242).** A revision item is
one of three shapes: `{"todo_id": "TODO-NNNN", "pipeline": "<name>"}`
(re-pipeline), `{"from": "TODO-A", "to": "TODO-B"}` (replace an element in
place — a NOT-STARTED one, or the REJECTED current one as a retry), or
`{"remove": "TODO-NNNN"}` (drop a NOT-STARTED element). New
elements land at the end by default; `position` names the slot, and
`after="TODO-A"` / `before="TODO-A"` anchor them to an existing element
(mutually exclusive with `position` and with each other). The key is the
identity of ONE request: the same key with the same payload is a no-op,
the same key with a DIFFERENT payload is refused as recovery-required —
a different revision needs a new key.

**Service run (ORCH M5.2): role creation during the main run.**
`awf_run_service_start(project_dir, slug, description)` starts a SERVICE
run — a FULL run (queue, position, verdicts, report) in a SEPARATE state
file, `state/service-run.yaml`. Its queue holds ONE unit: «создай роль
`<slug>` по описанию …». The main `state/run.yaml` is never replaced or
finished: after the service run completes or fails the main run is exactly
as it was (the isolation is a byte-level invariant, tested). The unit runs
the SAME engine: awf generates a pipeline (`pipelines/role-draft-<slug>.yaml`,
a snapshot of the active composition) whose first non-supervisor stage
declares the draft candidate (`.agentic/roles/draft/<slug>.md`) as its
output (M3.2 freshness check) and whose verify never commits (the candidate
stays in the draft area — adopt is a separate explicit step, M5.1). Launch
is refused while any stage is live (one pipeline per project — stop the unit
first) and rolls back every side effect of the call on a failed launch.
`awf_run_service_approve(todo_id, evidence)` approves the service unit at
verify (evidence required, as for any run unit; the verdict lands in the
service slot, never the main run's diary). `awf_run_service_finish(reason)`
closes the service run, writes `SERVICE-RUN-REPORT-{ts}.md` to the outbox,
and names the main continuation (`awf_run_next` / `awf_continue`) with the
main run's snapshot. `awf_run_service_status` reads both runs in one call.
The generic `awf_approve` / `awf_reject` on a service unit is a REFUSAL
with text (no signal, no REVIEW file, no verdict, no counter in the main
run): the service unit's verdict goes through `awf_run_service_approve`
only, and rejecting the candidate means closing the service run
(`awf_run_service_finish`) and re-issuing the start.

**Run plan (goal, criteria, decisions).** `awf_run_start` accepts
`goal` (one line) and `criteria` (a list of lines) — stored in
`state/run.yaml` (a derived RunPlan, no second store) and shown by
`awf_run_status` and in the RUN-REPORT. Inside a run every
`awf_approve` / `awf_reject` appends a causal entry
(`{ts, kind, todo_id, reason}`; the approve keeps a ≤200-char evidence
excerpt — the full text stays in `context/RUN-EVIDENCE-{todo}.md`) to the
append-only `decisions` list; repeating the same (kind, todo_id, reason)
adds no duplicate. Old run.yaml files without these fields read as before
(no migration); corrupt values degrade to empty with a warning.

**Evidence plan (per launched item, ORCH M2.3).** At the launch of a
queue item, `awf_run_next` snapshots the launched TODO's contract
(verify/gates/prove_red) + the file hash of `TODO-*.md` into
`state/run.yaml` (an append-only `evidence_plans` list, in the same
atomic write as the position commit — no second store). The plan
survives a later edit or retire of the TODO: `awf_brief` and
`awf_load_supervisor_context` show the current/last item's plan (the
verify commands and the prove_red ids, brief) before the supervisor
decides. A TODO without a contract → an empty plan + a note; corrupt
values degrade to empty with a warning.

**Two views of one record (ORCH M1.2).** `awf_brief` and
`awf_load_supervisor_context` are two views of the SAME run record, read
by one shared reader (`awf/run_plan_read.py`), not parallel retellings:
the card shows the goal, position, budget, the last decision in one line
and links to the sources (run.yaml, the last decision's REVIEW /
RUN-EVIDENCE file, the RUN-REPORT); the full context adds all the
decisions (which, why, when). After a reject in a run both surfaces show
the decision and the ONE next step — closing it: replace the rejected
current element with a retry via `awf_run_revise({"from": "<id>", "to":
"<retry-id>"})`, then retire the old TODO with `awf_todo_retire`; or
`awf_run_finish` (the run gate refuses `awf_run_next` over an open
reject). Retrying before retiring matters: retire moves the element to
completed and closes the retry slot. A corrupted run.yaml degrades both
to "no run" with a warning — no traceback.

**Progress control + goal check (ORCH M4.2).** `run.stall_minutes` in
`.agentic/config.yaml` turns on the stalled-stage warning: while the
active pipeline stage has produced no state event for at least that many
minutes (timestamps: `current.yaml`'s `updated_at` + the stage's
`Stage N:` log stamps — no new store), `awf_brief` and
`awf_run_status` show a line naming the stage, the unit's TODO and the
quiet duration. Absent/0/negative/broken value → the warning is off
(opt-in diagnostic, not a gate). At run finish, the RUN-REPORT carries a
"Goal vs done" section: the goal + criteria from the run plan versus the
completed/remaining queue items, the risks (salvage events, rejects,
stop reason) and the proposed next step (`awf_run_next` while items
remain, `awf_run_start` when the queue is exhausted). Empty goal/criteria
→ a marker line, not an error.

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

**Per-mode hints.** The response hints of `awf_wait_for_event` /
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
| `awf_load_supervisor_context` | One-shot: vision + plan + phase + state + next role + full run record (goal, criteria, all decisions, sources) |
| `awf_open_pipeline_dashboard` | Open dashboard (HTTP or file:// fallback) |

### Roles & Config
| Tool | What it does |
|---|---|
| `awf_add_role` | Create a role at `.agentic/roles/{name}.md`: template, or from an opencode skill (`from_skill`); `refresh=True` re-materializes an existing skill-sourced role; draft area: `draft` / `adopt` / `discard` / `list_drafts` |
| `awf_analyze_roles` | Detect role zone overlaps, write disambiguation |
| `awf_check_model_config` | Validate models in config.yaml vs opencode.json |
| `awf_doctor` | Diagnose versions (awf, pinned runner commit, plugin, install metadata), models and config keys no code reads; rc 0/1/2, never blocks a launch |
| `awf_feedback` | Write a bug/feature report about awf friction to the owner's desktop |

### UI (forms)
| Tool | What it does |
|---|---|
| `open_form` | Open HTML form in browser |
| `read_submit` | Check if form was submitted, return data |
| `cancel_form` | Cancel a pending form |
| `list_pending_forms` | List opened-but-not-submitted forms |
| `list_templates` | List available form templates |
