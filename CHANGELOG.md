# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.2.0] - 2026-10-29

### Added.

- ops reliability (R1–R6): per-stage timeout from config, full-suite warning before spawn, hard timeout -> BLOCKED with log tail, xfail/skip gate, `awf doctor` (versions, code identity, config-key registry, dead keys, plugin drift, no secrets), start handshake (rc/version/code identity).
- context efficiency IMP5: fix rounds resume the same implementer/QA opencode session (`engine.reuse_session`, `on`/`always`/`fix-only`/`off`), live capture diagnostics.
- checklist stages: `implementer-close` renamed to `checklist-close`, new `checklist-check` (QA re-check of the closer diff); sessions keyed by role.
- QA-death resilience: stale stage in brief/status/dashboard, pipeline-snapshot hint with a working exit, session-death fresh fallback with reason, kill isolation canary.
- specs READY: `docs/spec/context-efficiency/`, `docs/spec/opencode2/` (opencode 2.x TЗ, v1+v2).

### Changed

- spec tooling: `awf spec check` in project commands and CI gates; frozen contract hash covers `xfail_allow`/`skip_allow`, `flake`, `prove_red_pin`; `python -m awf.cli` runs (no silent no-op).
- stricter reports: tool counters 56/51, docs/claims synced, phase-prompt surface named.

### Fixed

- stale tool counters and brief word budget caught by CI after 0296.
- false green in 0307 (`spec check` through no-op CLI) and broken setup preview in 0304.
- brief: stale wins over setup chain and live-project header follows owner facts.

## [2.1.0] — 2026-10-28

Spec-first becomes the default path for features. A READY specification package
is a product artefact: the engine produces it, gates dev units on it, and hands
the norm to the implementer; the closer stage proves the frozen DoD.

### Added

- **Spec pipeline.** `awf spec init <slug>`, `awf spec check`, six-document
  READY packages, a producer gate that lets a spec revision re-open a BLOCKED
  package, readiness rewind to the author, and the three spec roles shipped as
  templates for a fresh project.
- **Closer stage.** A DoD gate after the last writing stage: `skip_if`, a
  verdict, a budget of verdict runs, block/contract immutability, stats
  (`awf checklist stats`) and delivery in default/full/code-health/quick.
- **Carry diagnostics and gates.** Worker-death diagnosis with auth/network/
  transient/silent classes; a suite gate that recognises `pytest` without a
  target and runner prefixes (`uv`/`poetry`/`pdm`/`hatch`); `config_write` that
  preserves comments and order; managed barriers on the remaining surfaces.
- **Process guards.** Role-specific test policy, tracked carry-over, reject→
  retry in the queue, atomic dispatch with `--no-baseline-tests`, the unit
  review-pass budget, and BUDGET as an actionable event.

### Changed

- One command parser is shared by the gate, verify-pack and the canonical
  script; raw `pytest` without a target counts as full when `testpaths` is set.
- `pipeline-write --variant` prints a WARNING and returns 0 on unresolved
  roles (CLI/form parity, spec AC23/T11; role files are not materialized —
  add one with `awf add-role`); Windows is documented as unsupported for
  state/start paths.

### Upgrade

`pip install -U awf agent-workflow-ui` (the plugin requires `awf>=2.1.0`).

## [2.0.0] — 2026-10-25

Two demanding quality waves turned awf from a launcher into a durable
orchestrator with an evidence-based definition of done. Commits, state and
worker lifecycle now have single owners; verification is stricter and honest
about what it actually ran.

### Added

- **Managed filesystem barriers.** State, archives, locks, todo edits, init and
  the engine write through one no-follow managed layer; every writer checks the
  surface and the leaf. Symlinked surfaces are refused before any effect.
- **Durable state and cancellation.** CAS snapshots, a cancellation journal with
  reservations, a single interlock section, one generation allocator, and
  preview-only destructive plans.
- **Raw git identity.** Unit commits, candidate plans and resumed publication use
  NUL/raw paths with a legacy fallback; non-UTF-8 names no longer break approve.
- **Verification you can trust.** A canonical suite artifact keyed by tree sha
  (reused across attempts), a flake probe for test-like commands, a
  `qa_findings` section that runs the QA red tests under a safety tripwire,
  prove-red verdicts (`coverage`, `new-api`), and one git codec with
  `surrogateescape`.
- **Engine liveness.** The engine runs detached from the MCP host, `awf
  supervise` restarts it with a budget, heartbeat and engine-exit records make
  silent deaths visible, and execute workers are isolated from heavyweight MCP
  servers.
- **Definition-of-done checklist (core).** Declarative base/class/wave
  checklists, structured read-only probes, a gate that re-runs evidence instead
  of trusting reports, `awf checklist show/check/apply/hash`, and a verify-pack
  section. The block is frozen at dispatch; edits after the start are refused.
- **Work contracts.** `source_ref` copies a source section into the unit,
  a standard BLOCKED schema is documented, `blocked-draft` produces a review
  draft, markdown parsing is fence-aware, and the run queue supports anchors,
  replacement and removal.
- **Roles and pipelines.** Roles are materialized with discipline and suite
  canon blocks, `add-role --refresh` updates managed blocks while keeping local
  lines, packaged `full` and `code-health` variants exist, the supervisor
  template carries the canon, and `docs/practices.md` records the practices.
- **Hygiene.** The ratchet counts real debt labels, `--update` rewrites numbers
  in place, an iteration counter and checkpoint template exist, and stage tests
  live in packages.

### Changed

- Unit commits carry only the unit's own edit; the candidate check is mandatory
  and verifies the same tree the supervisor verifies.
- `verify` commands are argv-based; a command that needs shell syntax belongs in
  a script.
- The dashboard/read-model advice is derived from one status snapshot, and
  `awf status`, `awf brief` and `awf run-status` tell the same story.

### Breaking

- `verify:` commands containing `&&`, `||`, `;`, `|`, `>`, `<`, `$(`, backticks,
  a bare `&` or internal newlines are refused at dispatch. Wrap the logic in a
  script.
- A contract block that exists but cannot be parsed is now an error, not "no
  contract". The parser reports `invalid` and the gate fails.
- The engine runs detached from the MCP host; killing the CLI no longer kills a
  worker. Use `awf supervise` for unattended runs.
- Queue revision refuses to empty the last element; finish the run instead.

### Upgrade notes

- `pip install -U awf agent-workflow-ui`; the plugin requires `awf>=2.0.0`.
- Existing projects keep working: a unit without a checklist block reports
  `checklist: none`, and only contracts with shell operators must be rewritten.
- The source tree no longer carries a root `templates/` directory; packaged
  templates live in `awf/templates` and `awf init` installs them.

## [1.9.0] — 2026-10-13

A hard, honest day on the commit gate: unit commits now carry only the
unit's own edit, a property-test steward guards the mechanism, a live
pipeline can no longer be hijacked by an unrelated dispatch, and the
dashboard grew run tabs.

### Added

- **Unit commits carry the unit's edit only.** Tracked files that were dirty
  at dispatch enter the commit solely through the unit's own delta: a
  `.worktree` snapshot is taken once, and the snapshot→worktree diff is
  merged into HEAD coordinates. An unplaceable hunk degrades to the legacy
  whole-file commit with a loud warning — never a silent hybrid. The
  dispatch-time dirty warning now tells the truth about what stays outside.
- **The delta guard (property test).** A seeded steward (~75 tests: 64
  generated scenarios plus named regressions for the whole defect family —
  duplicate-context anchors, exec bits, emptied files, append geometries,
  stale snapshots, CRLF) runs the invariant "commit == HEAD + unit delta or
  loud degradation" and provably catches four injected mutations.
- **`replan_for` marker.** A replan answer is only the unit that carries
  the marker: an unmarked dispatch no longer hijacks a live pipeline
  (the BD-30 incident), and a replan unit restarts from the first stage.
- **Role files materialized from skills carry the awf overrides** — the
  engine commits after verify, no `git add`/`commit`/`push`, no nested
  opencode — as an idempotent block, so standalone instructions cannot
  bypass the cycle.
- **`awf_brief` recognizes a project continuation**: an existing project
  with history gets working next-steps instead of the new-project chain.
- **Setup form: stage composition + preview.** Choose one working role per
  unit (default) or a linear chain; the form previews the resulting
  `pipeline.yaml` before saving.
- **Dashboard: one window, run tabs.** The dashboard opens once (dedupe
  marker, `already_open: true` on repeats) and a tab bar switches between
  the live run and completed runs (report viewer with traversal-safe
  endpoints).

### Fixed

- **`awf_run_revise` / `awf_run_status` bookkeeping**: a completed unit is
  never reported as "current"; no unit sits in two states at once.
- **`awf_brief` "What's new"** cuts the changelog at paragraph (or
  sentence) boundaries, never mid-phrase.
- **`awf_init`** reads the vision excerpt from the actual anchor file
  (README when VISION is absent) with an honest fallback line.
- **Dashboard live panels survive a report view** — the tab bar is
  restored when switching back to the active run.
- **Coverage ratchet**: the `run_plan_read` branches lost to the wave are
  covered again (the only CI red after the wave).

### Upgrade notes

- Nothing breaking: the plugin requires `awf>=1.9.0`; the install flow is
  unchanged (`pip install -U awf agent-workflow-ui`).
- Dispatching while the tree is dirty is possible as before, but the
  warning lists exactly which files stay outside the unit commit — claim
  them with `include_untracked` when they are intended for the unit.

## [1.8.0] — 2026-10-12

Two fix waves born from a full day of running awf on a real JS project:
unit commits keep every file the unit touched, recovery stops needing
python workarounds, and prove-red and roles become first-class citizens.

### Added

- **`prove_red_kind: coverage`** (+ required `prove_red_note`) — a
  coverage/validation unit whose tests are green on the baseline by
  construction declares itself: the gate records the rationale instead of
  a manual waiver. A tests-only diff with a `not-red` verdict gets a hint.
- **prove-red command mode copies the tree's test files** — changed and
  untracked test files (patterns derived from the unit diff) land in the
  baseline worktree, as the docs always promised; an empty set is stated
  explicitly in the verdict; a piped command runs under `bash -o pipefail`
  with a warning that a pipe masks the exit code.
- **`awf_run_revise` append/insert** — not-started units join an active
  run queue (at the end, or before a queued element); CAS, idempotency
  and the stop path are unchanged.
- **Role preset `office`** — `awf_add_role(preset="office", models=…)`
  seeds the standard set (system-analyst, spec-writer, architector,
  project-auditor, qa-review, qa-table, auditor-table) from the installed
  skills in one call; existing roles are never touched, a missing skill is
  a reported skip.
- **Roles write their model to the config** — `awf_add_role(model=…)`
  records `models.<role>.model` in `.agentic/config.yaml` (the engine's
  source of truth) and names the storage in its answer; `awf_status` lists
  the office (role → model → source).
- **Dirty-tree dispatch warning** — dispatching while the working tree is
  dirty names every file that will stay outside the unit commit.

### Fixed

- **Unit commits no longer lose files.** Foreign staged files are
  subtracted only per the dispatch-time snapshot; files staged during the
  unit count as its work; a skipped commit with a non-empty diff is loud
  (never a silent archive); RUN-REPORT lists everything left out; a
  catch-up commit mentioning the unit id is no longer mistaken for the
  unit's own commit.
- **Recovery without python workarounds.** `run_finish` clears a stale
  stage-state of the current unit itself (already archived, pipeline dead)
  and records it in RUN-REPORT; a worker that exited without a signal but
  left work gets one continue-push retry ("do not redo it") before salvage;
  a zombie stage-state shows a hint in `awf_status`.
- **The MCP server survives parallel awf calls.** Mutating tools run
  under a mutex: the two parallel calls whose out-of-order replies used to
  kill the connection now both answer, with a deterministic repro stand in
  the tests.
- **Event attribution.** A standalone close without a commit no longer
  reports another unit's id; the done event names the unit actually
  archived.
- **Dashboard shows the active model immediately**, from the config
  through the engine's own resolver; a non-string model value can no
  longer 500 `/api/state`.
- **`awf_start` warns about the plan checkpoint** and how to skip it;
  `awf_status` separates the active run's units from standalone ones.

### Upgrade notes

- Nothing breaking: the plugin requires `awf>=1.8.0`; the install flow is
  unchanged (`pip install -U awf agent-workflow-ui`).
- New optional knob: `retry.silent_work_attempts` (default 1) — the
  continue-push budget for a silent worker that left work in the tree.

## [1.7.0] — 2026-10-10

A week of field reports turned into honesty fixes: the run loop keeps its
promises through restarts, empty diffs and owner pauses; the dashboard
stops lying; quality work comes in two tiers.

### Added

- **`awf commit-workflow`** (CLI + MCP, the 53rd tool): commits the
  non-ignored `.agentic` workflow definitions (config, roles, pipelines,
  phases, doctrine) through the isolated-add path;
  `awf_status` lists `uncommitted_workflow_files`.
- **Quick unit tier.** Project setup writes `quick.yaml` (implementer →
  verify, no QA stage); `USAGE` and the supervisor templates explain
  when a unit is quick (docs, tests, counters, one-liners) and what stays
  mandatory on it (gates, prove_red, binding, evidence, full CI).
- **prove_red beyond pytest.** Runner detection from the project
  manifest; non-pytest projects get the honest `runner-unsupported`
  verdict, plus the new `prove_red_cmd` contract key; a timed-out or
  unrunnable command is `broken-runner`, never a false red.
- **`prove_red_pin` → `pin-ok`**: regression shields of existing behavior
  are no longer misreported as failures.
- **`verify_path_warnings`** on dispatch: missing files in `verify:`
  commands are named in the response (a warning, never a refusal).
- **Roles carry their model.** Role files get a model line from `model=`
  or the project config; `awf_check_model_config` cross-checks it.
- **Dashboard.** Model chips on the Agent Chat cards; per-attempt
  timestamps and an explicit return mark after continue/replan; the
  browser tab opens only when the dashboard server actually started (the
  port itself is reused).
- **MCP fault log.** `$XDG_STATE_HOME/awf/mcp-server.log` (override
  `AWF_MCP_LOG`): start/stop/version lines, tracebacks via excepthook and
  threading.excepthook, size rotation.
- **Honest run budget.** Owner-idle gaps longer than
  `automation.owner_idle_minutes` (default 15) are credited to downtime.
- **Pre-start unit editing.** `awf_todo_update` gains `append` and
  `section_updates`; `include_untracked` and removal work for
  dispatched-but-not-started units.
- **`awf_run_finish(force=...)`** with an unfinished-unit guard.

### Changed

- **Unit commits tell the story**: subject `awf(<stage>): <unit id> —
  <title>`, body with the title, unit id and the files from the commit
  plan.
- `awf_run_next` answers a running run with the run-loop instruction —
  no more GO IDLE inside a run.
- **Handoffs are slim**: the "Run facts"/"Stage facts" overlap is merged
  into one section; the worker DONE report is a digest plus a reference
  (−73% on real handoffs).
- **The U6c watchdog is CPU-aware**: a quiet log with a live,
  CPU-burning child (a full suite run) no longer gets the worker killed;
  a frozen tree still does.
- Baseline JS commands go through the package manager detected from the
  lockfile (npm/yarn/pnpm/bun); `require_awf_project` (config.yaml)
  guards write entry points; `resolved_project_dir` is reported.
- `clean_stage_signals` keeps `.md` evidence (NEG-4); `restore_todo` no
  longer moves the archived handoff tail back over live files.
- The subprocess-timeout scanner resolves import aliases.

### Fixed

- **continue on verify consumes an already-present VALID approve**
  (binding of the current attempt) and otherwise prints an exact
  "re-approve" line, surfaced as `stale_decision_hint` in the wait; the
  dead-approve protection (U6b) is intact.
- `done` after approve in no-commit cycles; `run_status` reconciles
  archived units into `completed`.
- `analyze_roles` no longer wipes role text after the BD-31 block.
- Baseline: a missing test binary is a failed record with a PATH hint,
  not a crash; zero residue when the baseline fails unexpectedly.
- Dispatch: a foreign unit number in the heading is renumbered (or
  refused with `allow_mismatch`); a stale `.agentic` without
  `config.yaml` is refused with a clear message.
- `run_finish` refuses to close a run over an unfinished unit (unless
  forced); the report says "pipeline is not running; unit NOT
  archived".
- A-13: a release no longer clobbers a captured reservation (owner
  stamp).
- CI flakes: salvage-signal and seam waits pinned to one controlled
  clock timeline; sibling pre-write tests converted; wheel-smoke
  isolates `XDG_STATE_HOME`; coverage ratchet restored for the plugin
  tool and `run_state`.

## [1.6.0] — 2026-09-29

ORCH milestones M4–M7: the supervisor can stop and revise a running unit,
sees stalled stages and reconciles the run against its goal, gets a formal
stage result, creates project roles mid-run, and configures the worker
composition from a form; quality now has CI ratchets.

### Added

- **`awf_run_revise(stop_running=true)`.** Stops the live unit through the
  standard kill path (TERM → grace → KILL, worker tree), keeps the resume
  point, applies the revision, and `awf_continue` resumes from the stopped
  stage; a failed stop leaves the previous plan in force.
- **Progress control.** `run.stall_minutes` (off by default) warns about a
  stalled stage in `brief`/`status`; the RUN-REPORT reconciles the goal and
  criteria against what ran, lists risks, and names the next step.
- **StageResult.** `DONE.json` accepts an optional `stage` block
  (`stage_id`/`role`/`model`/`attempt`); the handoff carries "Stage facts"
  filled by the engine even without a worker report.
- **Role drafts.** `awf add-role --draft` writes a candidate to
  `.agentic/roles/draft/<slug>.md`; list/adopt/discard; adopt refuses a slug
  that collides with a live role.
- **Service runs (4 tools).** A role can be created during an active run:
  the service run keeps its own state file and slot, the main run stays
  byte-intact, the candidate is the declared output, and finishing reports
  how to resume the main queue.
- **Incremental adopt.** Adopting a candidate refreshes zone-overlap
  addenda through the existing mechanism — no setup-phase restart, existing
  role files are never overwritten.
- **Composition form.** `pipeline-compose` builds the stage list
  (role/id/task/outputs) and submits through the shared validator
  (`write_pipeline`); an active run is not touched.
- **Per-assignment tool profiles.** A stage accepts `tools: {allow, deny}`;
  deny beats allow and the base prohibitions (control tools of execute
  stages, `edit`/`write` of readonly roles) cannot be lifted.
- **Quality ratchets in CI.** `scripts/coverage.sh` (total plus per-module
  floors for the new modules) and the mutation smoke (9 base mutants) run in
  the test job; the full mutation report carries 22.

### Changed

- **A revision moves the run to a new cycle.** The applied revision bumps
  the run generation in the same CAS write, so an approval published for the
  pre-revision generation is refused instead of committing work the revision
  was meant to invalidate.
- **Liveness verifies the project.** A reused PID that holds another
  project's pipeline is no longer treated as a running pipeline here.
- MCP tool surface: 48 → **52**.

### Fixed

- A stale approval could survive a queue revision and unlock the commit.
- A reused PID (parallel test workers) made `run_revise` refuse a legitimate
  change as "a stage is running".
- The QA role contract: a BLOCKED verdict must publish the BLOCKED signal
  and must not write a DONE report.

## [1.5.0] — 2026-09-27

The ORCH program (development strategy 2026-09-27): the supervisor keeps a
durable memory of the run, approve is bound to the state it was made against,
workers stop seeing controlling tools, and the worker chain becomes
replaceable per unit.

### Added

- **Run memory.** The active run stores `goal`, `criteria` and an append-only
  decision log (approve/reject with reasons) in `run.yaml`; `awf_run_start`
  accepts `goal`/`criteria`; run surfaces show them; broken fields degrade
  with a warning.
- **One source for supervisor surfaces.** `awf_brief` and
  `awf_load_supervisor_context` render the same record instead of parallel
  retellings.
- **Evidence plan per unit.** At launch the unit contract
  (`verify`/`gates`/`prove_red`) is snapshotted into the run memory and shown
  before the verdict.
- **`awf_run_revise` (tool 48).** One typed call: a side-effect-free preview,
  an idempotency key, a generation-checked apply; changes the pipeline of
  not-started queue items; refuses while a stage is running.
- **Stage instances.** Stages accept `id` (unique address — the same role may
  appear twice), `task` (short assignment) and declared `input`/`output`; the
  declared output is checked for existence and freshness before the next
  stage; the retry budget is per instance.
- **Pipeline snapshot per unit.** Launch and continue work from
  `.agentic/context/PIPELINE-{todo}.yaml`; editing the published pipeline no
  longer changes a unit already running.
- **CI:** the mutation smoke (base list) runs in the test job.

### Changed

- **In-run approvals require `verified_sha`** (outside a run it stays
  optional).
- **Role tool profiles.** Execute-stage workers no longer see the controlling
  MCP tools (approve, kill, rollback, dispatch, run management) — 48 tools
  shipped, a worker sees only its working set. This reduces accidental
  actions; it is not a security boundary.
- **`readonly_roles` denies `edit`/`write` over host config** (bash stays
  allowed so a reviewer can run tests).
- **`run_next` success condition.** A foreground launch counts as successful
  only when it completes with exit 0; any refusal rolls back its own effects
  (the `.ready`, the queue reservation).

### Fixed

- A stale-generation approval is refused by the commit gate instead of
  committing a tree the supervisor never pinned.
- A structurally invalid pipeline snapshot degrades like a byte-corrupt one.
- A corrupt `budget_minutes` degrades in reports and stops the run (the
  budget gate cannot be verified) instead of crashing the loop.

### Security

- The approve binding, the isolated commit index and the plan-checkpoint
  token close the "publish a decision for a state you did not verify" class.

## [1.4.0] — 2026-09-27

The stabilization program (audit 2026-09-25, 31 units) addressed the 21
original findings and one user-visible breaking change. A later
[verification](docs/audit-2026-09-27-gpt6-sol-xhigh/README.md) identified
remaining failure paths and limits of the supervisor approval boundary.
The follow-up verification wave (9 units) closed the remaining acceptance
checks, shipped the supervisor strategy, and cleaned the documentation.
The documentation wave (3 units) restructured the project docs: one `docs/`
tree with a map, each living document rewritten to the shipped facts,
duplicates removed.

### Breaking
- **Legacy `action:` / `kind:` keys rejected in pipeline YAML** — a stage's kind (plan / execute / verify) is now computed from its position in the pipeline; a hand-written `action:` or `kind:` fails at load with a clear error naming the stage and key, instead of being silently ignored and surfacing later at runtime

### Added
- **Metrics on demand** — `awf metrics` (MCP `awf_metrics`) assembles the work-program report (tokens, compressions, code lines per unit, cost conversion to a reference model) whenever you ask and writes it to the desktop; nothing runs on its own
- **Pinned engine runner** — `automation.runner_dir` in `.agentic/config.yaml` points the background pipeline child at a checkout that contains `awf/__init__.py`, so a `python -m awf` launch no longer shadows the installed engine with the working tree it orchestrates; without the key, behavior is unchanged
- **Isolated commit index** — the commit gate commits through a throwaway `GIT_INDEX_FILE`; your index and uncommitted WIP are left untouched, and staged files can no longer leak into the unit's commit
- **One-time checkpoint token** — the plan-checkpoint form carries a one-time token; a decision POST is accepted only with it (default-deny) and is spent after the first valid submit; the decision body is capped at 64 KiB and concurrent handlers at 16
- **NEG-4 shim** — a deterministic scenario stand-in for the `opencode` CLI (`tests/infra/opencode_shim`) drives the engine's subprocess container (spawn → signal watch → salvage → hard-timeout kill) in `tests/e2e/test_w6_salvage_scenario.py`; the salvage scenario runs in CI alongside the wheel builds
- **Single tool registry** — the MCP registration list and metadata live in one module (`tools/registry.py`) that `create_server()` and the documentation counts read; a shared unit-ID parser (`awf/todo_ids.py`, four-plus digits with an explicit right boundary) serves metrics and public ID validation
- **Supervisor strategy ships** — role templates for the implementer and the reviewer, project doctrine seeds on `awf init`, and a quality section in the shipped supervisor template (a fix comes with `prove_red`; an open P1 blocks a release; the reviewer report is a verify input)
- **`readonly_roles`** — a listed role receives no new edit/write overrides from awf; pre-existing user permissions and allowed bash remain in effect, so this is not enforced read-only. Empty by default
- **Wheel smoke covers the plugin surface** — the smoke installs both wheels into a clean venv and proves the tool registry, the project-setup template render, and the plugin entry point under an isolated XDG (no network, no `$HOME`); the package job runs it on Python 3.10 and 3.12
- **Mutation smoke** — `scripts/mutation-smoke.sh` runs a fixed list of 13 targeted mutants over the launch and state contour (the base gate list plus a report mode)

### Changed
- **Documentation restructured** — the project docs now live in one `docs/` tree with a map (`docs/README.md`): vision, design, file bus, contracts, unit contract and release notes, each described in one place, duplicates removed; the 2026-09-25 audit is retained in Git history and superseded by the verification dated 2026-09-27

### Fixed
- **Audit 2026-09-25: 21 findings (5 P1)** — data safety, launch ownership (one owner for the launch and the queue transition: file lease, run generation, a single task-file storage layer), the git commit contour (commit exactly what was verified; a gate refusal stops the stage), metrics, load boundaries (core + plugin), and release hygiene — each fix with a regression test and a reviewer verdict
- **R-03-F1** — the commit path no longer leaves the git index "frozen" after a unit commit (incident of 2026-09-25)
- **A-13** — the launch double-reservation hole closed: a second concurrent launch gets the text noop, not a second spawn
- **`prove_red`** — bootstrap + verdict classifier: a broken runner (0 collected, collection error, missing new-code symbol) is reported as `broken-runner` instead of a misleading red/green
- **FU-06** — the one-shot `no_checkpoints` flag no longer leaks from the parent's environment into the pipeline child; the plan-checkpoint gate wiring is restored (AUD16-02)
- **A-02: foreground and mixed pairs** — the launch lease and the liveness check now cover the foreground path: two concurrent foreground launches (or a foreground call while a background pipeline runs) yield exactly one running pipeline, the second gets the noop refusal. The background child carries a double marker (env plus own argv), so it is not refused by its own launch
- **NEG-3: corrupt state degrades** — non-UTF-8 bytes in the include link, the DONE facts file or the baseline fingerprint read as "absent" / "skipped" instead of a traceback

## [1.3.0] — 2026-09-23

Four autonomous work programs (RUN6–RUN9): supervisor-focused polish. `awf brief`
gains defaults and scenarios; recovery paths pin their unit and terminate the
stage worker; the `awf` console command and the wait cap become first-class.

### Added
- **`done` event** — `awf_wait_for_event` answers `done` with the exact next command when a cycle completes (state cleared + process gone + a finished commit/archive); no more `git log` to know the pipeline finished
- **Onboarding** — `awf brief` carries Defaults (checkpoint on/off, what "pipeline finished" means, `run_next` gating, several active tasks in a run are normal, USAGE link) and five Scenarios with exact steps; the plugin system-prompt block has a single source in the repo (`agents_md.py`, self-healing on plugin start, "start with `awf_brief`"); the global skill `awf-supervisor` ships the full doctrine; tool descriptions and responses lead to the next step
- **`awf todo-update <id>`** (MCP `awf_todo_update`) — edit an existing task keeping its number (backup in `context/`, refuses once started)
- **MCP `awf_tree_sha`** — the verify-ritual fingerprint inside the typed contour (same value as the CLI twin)
- **`awf kill` in the CLI** — recovery without MCP; the same guarded API as the tool
- **Console command `awf`** — the wheel installs the entry point (CI smoke included); `python -m awf` still works
- **Wait cap from config** — `wait.cap_seconds` / `AWF_WAIT_CAP` (default 55s); `suggested_timeout` adapts to measured stage durations up to the cap

### Changed
- **Dashboard renders the actual pipeline of the running task** — the engine records the resolved pipeline name; state → run queue → task front-matter → default; a pipeline chip shows the name (fixes roles from another pipeline appearing)
- **`awf_status` in a run** — a waiting run queue is reported neutrally ("items wait for a turn") instead of the destructive stale warning; the stale "raise the mcp timeout" advice appears only at the default cap
- **`awf_continue --ack` pins its own unit** — priority: explicit `todo_id` > ack > state > newest active; `todo_id` parity added
- **`awf_retry_stage` pins the salvaged unit** — no more "newest active" pick after a salvage

### Fixed
- **`awf_kill` terminates the stage worker** — `kill_pid_tree` (pid>1 guard intact; the group is signaled only for the worker's own session leader) with survivor and orphan warnings; a worker that outlives a kill no longer commits into the next unit
- **False `done` after a pipeline death** — a marker-less or salvage-counter leftover never counts as an exited cycle; an old completed cycle is not reported as the finished unit
- **Foreground `run_next` + `no_checkpoints`** — the run flag reaches the launch guard; no false BD-36 refusal
- **Cyrillic role slugs** — core and plugin share one transliteration (`QA Лид` → `qa-lid`)
- **Atomic log rotation** — unique archive names (`<name>.<µs-stamp>-<pid>`) plus prune; concurrent rotators no longer lose lines
- **Salvage-signal test flake** — the real freshness-gate race removed (the signal is written after the wait starts)

## [1.2.0] — 2026-09-22

Three autonomous work programs (RUN3–RUN5) on top of 1.1.1: named pipelines,
state hygiene, the supervisor onboarding card, the feedback contour, the
leak-gate, and TODO retirement. Eight new MCP tools — a minor bump.

### Added
- **Named pipelines** (RUN3 #1) — `awf pipeline-write <name> --role R1 [--role R2] [--force]` (MCP `awf_write_pipeline`) writes only `.agentic/pipelines/<name>.yaml` (config.yaml and supervisor.md untouched); `awf pipelines` (MCP `awf_pipelines`) lists them and marks the active one; `awf start --pipeline <name>` runs the named pipeline
- **Per-item pipelines in a run queue** (RUN3 #2) — the queue accepts `{"todo_id": "TODO-0023", "pipeline": "audit-llm"}` objects alongside plain id strings; `awf_dispatch_todo(pipeline=...)` writes the name into the TODO's front-matter and `awf run-next` reads it for items without a queue-level pipeline
- **Role from a skill** (RUN3 #3) — `awf add-role <name> --from-skill <slug>` (MCP `awf_add_role(from_skill=...)`) creates the role with the SKILL.md body (front-matter stripped) instead of the empty template; project skills win over global ones
- **State hygiene** (RUN3 #4/#5) — `awf unblock <id>` (MCP `awf_unblock`) moves stale BLOCKED/ACK closure signals to a `context/` trace directory so a re-issued TODO is visible again; `awf todo-remove <id>` (MCP `awf_todo_remove`) removes a TODO that never started, trace in `done/<id>/removed-<ts>.md`
- **`awf brief`** (MCP `awf_brief`, `--json` for the machine variant; RUN4 #1) — the supervisor's onboarding/recovery card, assembled from the LIVE project state: header (version, project, phase, date), what's next, state (run, active TODOs, blocked/salvage, last signal), a tool map by situation (every tool of the registry, checked both ways by tests), rituals, recovery recipes (`awf/data/recovery.md`) plus project doctrine, what's new (latest CHANGELOG section), and the feedback line. Deterministic for the same state; a new project gets the setup-chain hint instead of the working cycle
- **`awf feedback`** (MCP `awf_feedback`; RUN4 #2) — friction with awf becomes a structured bug/feature report on the owner's desktop: `awf feedback --type bug|feature --title "…" --body "…"`; awf assembles the header facts, the report skeleton, and the newest log tail itself; the report never reads the environment
- **Leak-gate** (RUN5 #1) — `awf reject` (and the engine REVIEW path) snapshot the unit's untracked new files to `REJECT-<todo>.files`; `awf_dispatch_todo(carry_over_from=<id>)` re-claims those paths for the retry's baseline so the retry commit includes them; `awf verify-pack` warns when a rejected unit's files are still leaking
- **`awf todo-retire`** (MCP `awf_todo_retire`; RUN5 #2) — `awf todo-retire <id> --reason "…"` moves a rejected/abandoned TODO that stays "active" (DONE files without the `.ready` signal) to `done/<id>/` with a RETIRED note — no fake closure signal, `awf restore` still brings it back

### Changed
- An unknown explicit `--pipeline` / `awf_start(pipeline=...)` name is now a clear error with the list of available pipelines (previously it silently fell back to `default.yaml`)
- `awf_start` / `awf_continue` accept `no_checkpoints=true` — the BD-36 plan form is skipped for that one launch only (process-scoped: not written to config or state, the next launch asks again); the run-level `awf_run_start(no_checkpoints=...)` is unchanged
- `awf_current_step` recognizes a live project: an active pipeline, roles, or stale state means phase `run` (with a setup note when no goal is stored), and `awf set-goal` refuses on a live project
- `awf_status` / `awf_run_brief` show the active pipeline and the number of available ones
- Tool set: 45 MCP tools (5 UI + 40 workflow); README/USAGE counters and reference tables updated to match `server.py`

### Fixed
- `awf run-next` re-baselining swallowed the carry-over exclusion, so the leak came back in run mode — the exclusion now survives the re-baseline (RUN5 #1 QA)

## [1.1.1] — 2026-09-22

Post-audit additions (U8–U11) plus fixes from the first real autonomous run
(RUN2, topic-trainer bug report). The published 1.1.0 artifact predates them.

### Added
- **`awf metrics`** (MCP `awf_metrics`) — on-demand work-program metrics from `opencode.db`: worker and supervisor tokens, compactions, code lines per unit, and the "if workers ran on model X" cost conversion; markdown report to the desktop with an optional archive mirror (`metrics.mirror_dir`)
- **Subscriptions section** in the metrics report — GPT / Claude / GLM plans with limits, the exact fractional share ("need 2.9 → buy 3") and a pessimistic cache-inclusive line; GLM follows the vendor credits model; `--refresh-subscriptions` refreshes from a live source with cached snapshots and a built-in table as fallback
- **Project doctrine** — `.agentic/doctrine/*.md` is injected into every role's prompt (deterministic assembly, covered by the instruction budget); a new lesson needs no role-template edits
- **`awf tree-sha`** + **`awf approve --verified-sha <hash>`** — "what was verified = what is committed": approve refuses when the tree moved after verification
- **`awf mutations`** — mutation smoke over a quiet tree (once per wave / before release); **`awf todo-draft`** — a task-file skeleton from an audit registry
- **Model-price and subscription caches** — live source → awf cache → built-in table, with the source and date recorded in the report
- **Run checkpoints** — `awf_run_start(no_checkpoints=True)` skips the BD-36 plan form for the whole run; a submitted checkpoint decision survives a died pipeline (`.agentic/context/CHECKPOINT-<todo>.json`) and is applied on the next start without re-asking
- **Productive run budget** — the run budget counts productive minutes (wall clock minus recorded downtime: checkpoint waits, salvage handling, net-backoff pauses); status/brief and RUN-REPORT show both numbers
- **Wait cap** — every `awf_wait_for_event` response states the single-wait transport cap (55s) and how to raise it (`mcp` timeout in `opencode.json`)

### Changed
- `awf_approve` (MCP) accepts `verified_sha`; `awf_metrics` (MCP) accepts `refresh_subscriptions` and `mirror`
- Supervisor doctrine: replan / spec rewrite / task split need no owner approval — escalation only on the stop-list (role templates + USAGE)

## [1.1.0] — 2026-09-21

Release after the 2026-09 audit and the 2026-09-20
session-loss incident. The 1.0.0 PyPI build predates several data-safety and
security fixes — this release includes them.

### Added
- **Autonomous run (забег)** — `awf_run_start` / `awf_run_status` / `awf_run_next` / `awf_run_finish` / `awf_run_note`: a queue of TODOs with mechanical gates; the owner is pinged only on stop conditions
- **`awf_prove_red`** — machine proof that declared tests are red on the baseline sha
- **`awf_verify_pack`** — one deterministic verify report (GATES file)
- **`awf_restore`** — restore an archived TODO back to the inbox
- **Quality gates** — `bash scripts/run-all.sh`: contracts, ratchets, instruction budget, tests, lint
- **CI `package` job** — builds the real wheels, checks package-data contents, smoke-installs in a clean venv (package-data drift now fails CI)

### Changed
- `awf/data/role_zones.yaml` (BD-31 zone table) is now shipped in the wheel — PyPI installs no longer silently degrade to the 5-key fallback
- Tool set: 37 MCP tools (5 UI + 32 workflow); docs reference updated to match `server.py`
- Version constants (`awf.__version__`, plugin `__version__`) pinned to pyproject by a drift test; dead `AWF_VERSION` in `bin/awf` removed
- USAGE pipeline examples fixed to the real stage schema (`on_approved`, no computed `kind`); "Where things live" tree matches `awf init` output

### Fixed
- **A-run safety** — queue pinning, non-destructive reconcile, restore net (the run/reconcile data-destruction incident)
- **Dashboard** — XSS (Jinja2 autoescape), port reuse, chat hygiene
- **Network auto-retry** — worker death on API unreachability now retries with backoff instead of hard-stopping
- **`awf_init` R1 warning** — re-init on a live project deletes runtime dirs; docs no longer recommend it as a first-aid measure
- **`killpg` safety** — `pid > 1` guard + tripwire + regression test after the 2026-09-20 incident (a fake pid=1 killed the owner's session three times)
- USAGE troubleshooting brought to fact (orphan cleanup, phase stuck, supervisor timeout `AWF_SUPERVISOR_TIMEOUT` / `awf start --timeout`)
- `protocols/communication.md` v1.2 — handoff naming and signal table match the engine

### Security
- Origin/Referer check on the form server (A2) on top of the 127.0.0.1 binding

## [1.0.0] — 2026-08-11

### Added
- **SMO (State-Machine Orchestration)** — phase system: `init → goal → form → normalize → brief → run → verify → done`
- **29 MCP tools** (5 UI + 24 workflow), all with `next_action` guidance
- **Dashboard v2** — HTTP server with live `/api/state` polling, two-panel layout, chat-style handoffs, TODO timeline, browser notifications
- **`awf_reject`** tool — symmetric to `awf_approve` for verify stage
- **`awf_retry_stage`** tool — kill + retry from salvage stage in one call
- **`awf_current_step`**, **`awf_set_goal`**, **`awf_confirm_normalized`** — SMO phase tools
- **Pre-dispatch check** — `awf_dispatch_todo` greps codebase for already-implemented patterns
- **Form auto-advance** — form submission advances phase form→normalize automatically
- **Batch guidance** — phase-brief.md instructs supervisor to batch tasks by pipeline depth
- **TODO rollback** — failed dispatch cleans up orphan TODO files
- **Handoff sort** — pipeline order (not alphabetical)
- **Per-stage live timer** — JS ticker for current running agent
- **Markdown rendering** — handoffs and TODO content rendered via `markdown` library

### Changed
- `awf_init` returns compact phase prompt (not full 600-line supervisor.md)
- `awf_load_supervisor_context` returns `phase` + `phase_prompt`
- `next_action` is phase-aware in all SMO flow tools
- `awf_approve` next_action: "Wait for user" (was: "dispatch next TODO" — caused auto-dispatch without user consent)
- `awf_start` message: "GO IDLE" (was: "call awf_wait_for_event" — caused polling)
- Dashboard auto-refresh pauses on verify
- `requires-python` = `>=3.10` (PEP 604 syntax)

### Fixed
- Circular dependency orchestrator↔pipeline_engine eliminated
- Jinja2 XSS via `from_string` (autoescape=True)
- `OPENCODE_CONFIG_CONTENT` leaking API keys (only permission field serialized)
- `git commit` without reset on failure (staged files remain)
- Rollback path traversal (regex validation on todo_id)
- Dashboard `location.reload()` race condition (replaced with HTTP server)
- Handoff role extraction (split on `-TODO-`, not `-`)

### Security
- `inputs/*.yaml` chmod 0o600
- Worker log chmod 0o600
- Path validation on `roles_processor._copy_to_project`
- `yaml.safe_load` try/except in config.py and pipeline.py

## [0.4.0] — 2026-07-25

### Added
- Python migration (bash → Python core)
- Pipeline engine with stage handlers
- Commit gate with baseline isolation
- Signal-based worker communication
- BD-36 plan checkpoint
- Salvage path for crashed workers

[1.1.0]: https://github.com/EnerJizeIT/agentic-workflow/releases/tag/v1.1.0
[1.0.0]: https://github.com/EnerJizeIT/agentic-workflow/releases/tag/v1.0.0
[0.4.0]: https://github.com/EnerJizeIT/agentic-workflow/releases/tag/v0.4.0
