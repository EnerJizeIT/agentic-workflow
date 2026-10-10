# Release notes

Short, human note for what the current release ships. Full per-version
history lives in `CHANGELOG.md`; this file is the quick "what changed and
why it matters" for the `awf` core and the `agent-workflow-ui` MCP plugin.

## [1.7.0] — 2026-10-10

A week of running awf on real projects, turned into fixes: the supervisor
can trust the run loop through restarts, empty diffs and owner pauses;
the dashboard shows which model plays each role and stops opening empty
tabs; quality work now comes in two tiers — full for behavior, quick for
chores.

### Highlights

- **Honest recovery.** `continue` on verify consumes a valid pending
  approve; the "restart that is really a resume" trap is documented with
  its exact recovery (a NEW session, plus the inbox check after a
  "Connection closed").
- **Honest verdicts.** prove_red never fakes red (timeouts and unrunnable
  commands are `broken-runner`); regression shields get `pin-ok`;
  dispatch warns about dead paths in verify commands before a unit ever
  starts.
- **Honest records.** Unit commits carry titles and file lists; handoffs
  are ~73% slimmer with the DONE referenced, not pasted; `.md` evidence
  survives stage transitions; the MCP server writes a fault log you can
  actually read.
- **Honest budget.** Owner idle time counts as downtime, not as work.

### Upgrade notes

- Nothing breaking: the plugin requires `awf>=1.7.0`; the install flow is
  unchanged (`pip install -U awf agent-workflow-ui`).
- Optional knobs: `automation.owner_idle_minutes` (default 15) for the
  downtime accounting; `quick.yaml` is created by new project setups.

## [1.6.0] — 2026-09-29

The second half of the ORCH program: the supervisor stops and revises live
units, sees the goal behind the run, and can grow the team mid-flight.

### Added

- **Stop a running unit and revise the queue in one call.**
  `awf_run_revise(stop_running=true)` stops the live stage through the
  standard kill path, keeps the resume point, applies the revision, and
  `awf_continue` resumes from where the unit stopped.
- **The run remembers and answers.** `run.stall_minutes` warns about a
  stalled stage; the run report reconciles the goal against what actually
  ran and names the next step; each stage's result can carry its identity
  (`stage_id`/`attempt`) into the handoff automatically.
- **Create a project role mid-run.** A candidate lands in
  `.agentic/roles/draft/`, adoption refuses slug collisions and refreshes
  zone addenda without touching existing roles; a **service run** does the
  creation while the main run stays byte-intact.
- **Composition from a form.** `pipeline-compose` builds the stage list and
  submits through the same validator the API uses; the active run is
  untouched.
- **Tool profiles per assignment.** A stage can narrow its permissions
  (`tools: {allow, deny}`); the base prohibitions (control tools, readonly
  edits) cannot be lifted by a profile.
- **Quality ratchets.** Coverage (total + key modules) and 9 base mutants
  run in CI; the full mutation list has 22 entries.

### Fixed

- A queue revision now moves the run to a new cycle: a stale approval cannot
  survive it.
- A reused process id no longer masquerades as a running pipeline.
- The QA role: a BLOCKED verdict publishes the BLOCKED signal only.

## [1.5.0] — 2026-09-27

The ORCH program: the supervisor's memory survives sessions, decisions are
bound to the state they were made against, and the worker chain is
replaceable unit by unit.

### Added

- **Run memory.** `awf_run_start` takes `goal` and `criteria`; the run stores
  them plus an append-only decision log (approve/reject reasons) in
  `run.yaml`. After a restart or a fresh session the supervisor restores
  what was decided, why, and which step is allowed next.
- **One source for `brief`/`context`.** Both surfaces render the same record;
  a corrupt `run.yaml` degrades with a warning instead of a traceback.
- **Evidence plan.** The unit contract (`verify`/`gates`/`prove_red`) is
  snapshotted into the run memory at launch and shown before the verdict.
- **`awf_run_revise`.** Preview a queue revision without side effects, apply
  it with an idempotency key; only not-started items change; a running stage
  must be stopped first (tool #48).
- **Stage instances.** The same role can appear several times with its own
  `id`, `task` and declared `output`; the output is checked for existence and
  freshness before the next stage.
- **Pipeline snapshot.** Each unit runs and continues from its own snapshot;
  editing the published pipeline does not change a unit already running.

### Changed

- **In-run approvals require the tree fingerprint** (`verified_sha`); outside
  a run the parameter stays optional.
- **Workers don't see controlling tools.** Execute stages get their working
  set without approve/kill/rollback/dispatch/run management. This reduces
  accidents, it is not a security boundary (bash remains).
- **`readonly_roles` is enforced for `edit`/`write`** over host config; bash
  stays allowed so a reviewer can run tests.
- **`run_next` counts a foreground launch successful only on exit 0** and
  rolls back its own effects on any refusal.

### Fixed

- Stale-generation approvals are refused instead of committing an unverified
  tree; invalid pipeline snapshots and corrupt budget fields degrade instead
  of crashing.

## [1.4.0] — 2026-09-27

The breaking change first, then what is new.

### Breaking

- **Legacy `action:` / `kind:` keys are rejected in pipeline YAML.** A stage's
  `kind` (plan / execute / verify) is now computed from the stage's position in
  the pipeline, so a hand-written `action:` or `kind:` — like any other unknown
  key — fails at load with a clear error instead of being silently ignored and
  surfacing later at runtime. If a pipeline file still carries those keys, drop
  them before upgrading; the loader will tell you exactly which stage and key.

### Added

- **Metrics on demand.** `awf metrics` (MCP `awf_metrics`) assembles the
  work-program report — tokens, compressions, code lines per unit, and a cost
  conversion to a reference model — whenever you ask, and writes it to the
  desktop. Nothing runs on its own; you invoke it.
- **The supervisor strategy ships.** `awf add-role` ships ready templates for
  the implementer and the reviewer; `awf init` seeds the project doctrine; the
  supervisor template carries the quality bar — a fix comes with `prove_red`,
  an open P1 blocks a release, and the reviewer report is a verify input.
- **`readonly_roles`.** A listed role receives no new edit/write permission
  overrides from awf. Existing user permissions and allowed bash remain in
  effect, so this is not an enforced read-only boundary. Empty by default.

### Changed

- **Pinned engine runner.** Set `automation.runner_dir` in
  `.agentic/config.yaml` to point at a checkout that contains `awf/__init__.py`.
  The background pipeline child then runs from that pinned checkout instead of
  importing the working tree it is orchestrating (the `python -m awf` case,
  where the current directory would otherwise shadow the installed engine).
  Without the key, behavior is unchanged.
- **Isolated commit index.** The commit gate now commits through a throwaway
  `GIT_INDEX_FILE`. Your index and any uncommitted WIP are left untouched by an
  awf commit — staged files can no longer leak into the unit's commit.
- **Documentation restructured.** The project docs now live in one `docs/`
  tree with a map (`docs/README.md`) — vision, design, file bus, contracts,
  unit contract and release notes, each described in one place, duplicates
  removed; the 2026-09-25 audit stays as history.

### Security

- **One-time checkpoint token.** The plan-checkpoint form carries a one-time
  token. A decision POST is accepted only with that token (default-deny without
  it), so a client that learns the local port but not the token cannot submit a
  decision on your behalf.

### Fixed

- **Concurrent launches are one pipeline, foreground included.** Two concurrent
  foreground launches — or a foreground call while a background pipeline runs —
  yield exactly one running pipeline; the second gets the same noop refusal as
  the background pair.
- **Corrupt state files degrade instead of crashing.** Non-UTF-8 bytes in the
  include link, the DONE facts file or the baseline fingerprint now read as
  "absent" / "skipped", not a traceback on the supervisor path.
