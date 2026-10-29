"""Result dataclasses for awf.api public functions.

Each ``*Result`` carries the structured outcome of one API call. All have
``as_dict()`` for JSON serialization (MCP responses, plugin payloads).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class InitResult:
    """Result of :func:`awf.api.init_project`."""

    project_name: str
    project_dir: str
    stack: str
    vision_path: str | None
    vision_excerpt: str
    supervisor_md: str
    plan_md: str
    pipeline_configured: bool
    next_action: str
    warnings: list[str] = field(default_factory=list)
    created_files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StatusResult:
    """Result of :func:`awf.api.get_status`."""

    project_name: str
    active_todos: list[dict[str, Any]]
    done_count: int
    blocked_count: int
    blocked_ids: list[str]
    conflict_warning: str | None
    suggestion: str | None
    # MCP-4: long-running pipeline state
    pipeline_running: bool = False
    pipeline_pid: int | None = None
    log_tail: str | None = None
    # Dogfood-2: pipeline stage visibility (extracted from log + pipeline.yaml)
    current_stage_name: str | None = None
    next_stage_role: str | None = None
    last_signal: str | None = None
    # Dogfood-6: structural determinism — supervisor sees expected action,
    # doesn't have to read rules from supervisor.md
    current_stage_kind: str | None = None  # "plan" | "execute" | "verify" | "idle"
    expected_action: str | None = None  # what supervisor should do NOW
    checkpoint_pending: bool = False  # BD-36 form open in user's browser
    checkpoint_port: int | None = None  # for debugging / direct access
    # Dogfood-8: form URL supervisor can tell user about
    checkpoint_form_url: str | None = None  # file://path or http://127.0.0.1:PORT
    # DF5-4: salvage state (worker didn't signal)
    salvage_needed: bool = False
    salvage_stage: str | None = None
    # TODO-0196: zombie-state — the pipeline is dead, the stage state still
    # sits on disk, and no decision signal lies for the unit: the run limps
    # in every status until someone resumes it. The hint text (with the
    # exact awf_continue call) when detected; None otherwise.
    zombie_state: str | None = None
    # TODO-0241: state says a stage runs, but the engine PID is dead or its
    # heartbeat is stale. The line is "stage=..., todo=..., <reason>";
    # None when the engine is alive (or no stage is on disk). Read-only —
    # state is never mutated by a status call.
    engine_dead: str | None = None
    engine_dead_todo: str | None = None
    # TODO-0306: the explicit "not green" line — ``STALE stage … — dead: …``.
    # A dead engine always yields one (same fact, display form); a live but
    # quiet stage yields one when ``updated_at`` is older than
    # ``automation.stale_stage_minutes``. The resume advice still comes from
    # ``engine_dead`` alone (one owner of the advice order).
    stale_stage: str | None = None
    # TODO-0262 REVIEW (CTX1): the unit stopped for its review-pass budget
    # (a BUDGET note on disk). The owner's decision outranks ``awf_continue``
    # — an exhausted unit cannot be resumed into the same limit again.
    budget_exhausted_todo: str | None = None
    # RUN3 #1: named pipelines — active (config default_pipeline, "default"
    # when undeclared) + number of pipeline files in .agentic/pipelines/
    active_pipeline: str | None = None
    pipeline_count: int = 0
    # SPEC A-run: autonomous run state (active run only; None otherwise)
    run_state: dict[str, Any] | None = None
    # TODO-0242 REVIEW (F8): whether a run is ACTIVE. The status surface
    # needs the fact to decide resume advice — a closed run with a stale
    # stage state must hear "start a new one", not "awf_continue".
    run_active: bool = False
    # REPORTS26 F6 (TODO-0166): workflow definitions not yet in git
    # (.agentic/config.yaml, roles/, pipelines/, phases/, doctrine/ —
    # changed tracked + untracked non-gitignored). [] = nothing to commit.
    uncommitted_workflow_files: list[str] = field(default_factory=list)
    # TODO-0197 (REPORTS32 п.4): active inbox units that are NOT part of
    # the active run's queue — ready to start standalone or closing a
    # previous cycle. Same entry shape as active_todos. [] when there is
    # no active run (everything is "active" — unchanged behavior) or no
    # such units.
    standalone_todos: list[dict[str, Any]] = field(default_factory=list)
    # REPORTS32 (TODO-0202): the "office" — one line per team role with its
    # model and where it comes from ({role, model, source}); source is
    # "config" / "md" / "none". See role_model_office.
    office: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BaselineResult:
    """Result of :func:`awf.api.create_baseline`."""

    todo_id: str
    sha: str
    # REPORTS26 B2: where the baseline actually landed (absolute path) —
    # the MCP answer passes it through so the supervisor can verify the
    # unit went into the project it asked for, not the MCP process cwd.
    resolved_project_dir: str = ""
    is_git_repo: bool = False
    files_created: list[str] = field(default_factory=list)
    test_status: str = ""
    test_log_excerpt: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RollbackResult:
    """Result of :func:`awf.api.rollback`."""

    todo_id: str
    baseline_sha: str
    mode: str
    ack_file: str | None
    diff_stat: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApproveResult:
    """Result of :func:`awf.api.approve_commit` (U11: +verified_sha_file)."""

    todo_id: str
    signal_file: str
    evidence_file: str = ""
    verified_sha_file: str = ""  # U11 (B5): context/VERIFIED-{todo}.sha, on match
    # AUD05-05 (rest): non-empty when a reject won the race ('approved' ⇒ rejects == 0)
    message: str = ""
    # RUN5 #1 (Part B): rejected-attempt files that would be SILENTLY
    # excluded from this commit (still untracked AND in the baseline's
    # untracked list). The approve itself is NOT blocked — the list is the
    # loud warning (re-issue with carry_over_from, or commit consciously).
    orphaned_files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RejectResult:
    """Result of :func:`awf.api.reject_commit` (SPEC A-run.5 accounting)."""

    todo_id: str
    review_file: str
    rejects: int = 0
    run_stopped: bool = False
    report_file: str = ""
    message: str = ""
    # RUN5 #1 (Part A.1) + RB-2: the rejected attempt's files (its untracked
    # NEW files and its tracked delta), recorded to
    # context/REJECT-{todo}.files (leak-gate). Empty when the attempt left no
    # new or changed path, or the snapshot could not be taken.
    reject_files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReportResult:
    """Result of :func:`awf.api.get_report`."""

    project_name: str
    generated_at: str
    items: list[dict[str, str]]
    done_count: int
    blocked_count: int
    git_diff: str
    latest_test_log_tail: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResetResult:
    """Result of :func:`awf.api.reset_runtime` and :func:`awf.api.remove_orphans`."""

    cleaned_dirs: list[str]
    orphan_ids: list[str]
    mode: str
    # R1.1: a destructive reset may fail mid-way after the preflight
    # succeeded. The applied subset is journaled in ``cleaned_dirs`` +
    # ``notes``; ``partial_failure`` marks it. No rollback of what was
    # already deleted is promised — a repeat finishes the leftover plan.
    partial_failure: bool = False
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AddRoleResult:
    """Result of :func:`awf.api.add_role`."""

    role_name: str
    role_file: str
    model: str
    # REPORTS32 (TODO-0202): where an explicit ``model=`` was written in
    # config.yaml ("models.<role>.model"); "" when nothing was written
    # (no model, or the draft path, which never touches the config).
    model_config: str = ""
    # TODO-0230 (M6): refresh mode report. "" (create), "updated",
    # "unchanged", "skipped" (not skill-sourced / skill gone / no managed
    # markers without --force) or "conflict" (role file missing or
    # unreadable). ``refresh_reason`` explains a skip/conflict.
    refresh_status: str = ""
    refresh_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SeedRolesResult:
    """Result of :func:`awf.api.seed_roles` (REPORTS32, TODO-0203).

    The preset seed ("typical office") answers with three tables:
    - ``created`` — ``{"role", "role_file", "skill", "model"}`` per role
      file created from its catalog skill (``model``: the resolved model,
      "" when unassigned);
    - ``skipped`` — ``{"role", "reason"}`` per existing role ("already
      exists" — never touched) or missing skill ("skill not found: …" —
      not an error, a note);
    - ``models`` — ``{"role", "model", "locus", "action"}`` per
      ``models=`` entry: what was set where
      (``models.<role>.model``), action ``written`` / ``unchanged``
      (idempotent repeat).
    """

    preset: str
    created: list[dict[str, str]]
    skipped: list[dict[str, str]]
    models: list[dict[str, str]]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AdoptRoleDraftResult:
    """Result of :func:`awf.api.adopt_role_draft` (ORCH M5.1 + M5.3).

    ``normalization`` (ORCH M5.3) reports adopt's normalization step: the
    adopted role joins the team, so zone overlaps are re-analyzed through
    ``analyze_roles_core`` and the BD-31 disambiguation addenda refreshed.
    Keys: ``overlaps`` (list of ``{role_a, role_b, zone}``), ``addenda``
    (roles whose BD-31 block was (re)written), ``failed`` (roles whose
    addendum write raised OSError). Normalization is best-effort: on a
    failure it is ``{"error": <message>}`` and the adopt itself stands.
    """

    role_name: str
    role_file: str
    normalization: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RoleDraftsResult:
    """Result of :func:`awf.api.list_role_drafts` (ORCH M5.1).

    ``drafts`` is a list of ``{"name", "source", "created", "file"}``
    dicts — source is ``skill:<name>`` / ``builtin`` / ``template`` from
    the draft marker, or ``file`` for a hand-placed candidate without one.
    """

    drafts: list[dict[str, str]]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DiscardRoleDraftResult:
    """Result of :func:`awf.api.discard_role_draft` (ORCH M5.1).

    The candidate leaves ``.agentic/roles/draft/`` and is kept as a trace
    under ``.agentic/context/`` (``trace_file``); ``draft_file`` names the
    removed candidate.
    """

    role_name: str
    draft_file: str
    trace_file: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StartResult:
    """Result of :func:`awf.api.start_pipeline` and :func:`awf.api.continue_pipeline`."""

    run_mode: str
    run_id: int | None
    log_file: str | None
    exit_code: int | None
    message: str
    # TODO-0197 (REPORTS32 п.3): the launch will open the BD-36 plan
    # checkpoint form (checkpoint enabled, no no_checkpoints bypass) —
    # the warning the supervisor needs to warn the user BEFORE the form
    # pops up. "" when the checkpoint is off for this launch.
    checkpoint_warning: str = ""
    # OPS IMP6 (R6/K7): the launch handshake — the version of the code that
    # will execute stages (pinned runner, else process), the serving plugin
    # module's version, and the drift warning ("" when they agree). The
    # warning is advisory: it never changes the outcome or rc (INV7).
    runner_version: str = ""
    plugin_version: str = ""
    warning: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AnalyzeRolesResult:
    """Result of :func:`awf.api.analyze_roles`."""

    overlaps: list[dict[str, Any]]
    patches_applied: list[dict[str, str]]
    dry_run: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApplyProjectSetupResult:
    """Result of :func:`awf.api.apply_project_setup`."""

    pipeline_file: str | None
    # TODO-0173 (quick tier): the S-class variant written next to the
    # active pipeline (pipelines/quick.yaml); None when skipped (QA-only
    # team / active pipeline already named 'quick') or when an existing
    # quick.yaml is not a pipeline the engine can load (kept as is, a
    # warning names why).
    quick_pipeline_file: str | None = None
    config_updated: bool = False
    supervisor_md_updated: bool = False
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DispatchTodoResult:
    """Result of :func:`awf.api.dispatch_todo`."""

    todo_id: str
    baseline_sha: str
    role_hint: str | None
    files_written: list[str]
    # REPORTS26 B2: where the unit actually landed (absolute path) — the
    # MCP answer passes it through so the supervisor can verify the TODO
    # went into the project it asked for, not the MCP process cwd.
    resolved_project_dir: str = ""
    pre_check_warnings: list[str] = field(default_factory=list)
    # RUN5 #1 (Part A.2): leak-gate carry-over for a retry.
    carry_over_from: str | None = None  # the rejected origin TODO id
    carry_over_files: list[str] = field(default_factory=list)  # paths pulled in
    # RUN10 #4 (TODO-0074): pre-existing untracked visibility — what the
    # commit gate will EXCLUDE from this unit's commit (fresh baseline
    # snapshot minus carry-over/include re-claims). Empty = nothing to say.
    pre_existing_untracked: list[str] = field(default_factory=list)
    untracked_warning: str = ""  # capped one-line warning; "" when the list is empty
    # REPORTS26 F5: True when the auto-issued number replaced a foreign
    # TODO-NNNN in the title heading (a copied template kept its
    # old title). Always False for an explicit todo_id (mismatch is
    # refused, or written as-is via allow_mismatch).
    renumbered: bool = False
    # TODO-0242 (B.13): the soft unit-size lock — one line when the text
    # looks like a mega-unit (>6 numbered requirements or >12 files in the
    # boundaries block). Not a refusal: the supervisor decides.
    size_warning: str = ""
    # TODO-0186: verify-path validation at dispatch — the contract's
    # verify: path tokens that do not exist relative to the project.
    # Warning only (the dispatch never refuses); [] when the verify
    # commands carry no paths or all of them exist.
    verify_path_warnings: list[str] = field(default_factory=list)
    # TODO-0195: the dirty tree at dispatch — tracked-dirty / staged /
    # untracked non-ignored paths that pre-date the unit and will NOT
    # enter its commit (gitignored and a clean tree → []). R2.2: the ADOPTED
    # paths (claim_from / carry_over_from / include_untracked) are owned, not
    # dirt, and are excluded here. The warning is the loud part; the dispatch
    # itself never refuses on dirt.
    dirty_files: list[str] = field(default_factory=list)
    dirty_warning: str = ""  # capped one-line warning; "" on a clean tree
    # R2.2 (roadmap R2): the explicit dirty-source-tree policy that governed
    # this dispatch — its value, the project's config_version and the
    # migration warning for a legacy project ("" when versioned).
    dirty_policy: str = ""
    dirty_policy_warning: str = ""
    # R2.2: the isolate artefacts — the excluded-operations table summary and
    # the dependency warning (both "" unless dirty_policy=isolate on a dirty
    # tree).
    isolate_warning: str = ""
    isolate_dependency_warning: str = ""
    # R2.2: the code transfer applied to this unit — the source unit whose
    # changes were claimed and the claimed display paths ([] when no claim).
    claim_from: str | None = None
    claimed_files: list[str] = field(default_factory=list)
    # TODO-0261 (RB-4): the dispatch baseline is a smoke snapshot with its own
    # timeout. The supervisor must SEE what it got: the status ("passed" /
    # "timeout" / "skipped" / ...) and the timeout that was in force.
    baseline_test_status: str = ""
    baseline_timeout: float | None = None
    #: True when the unit was finished from its write-ahead journal after a
    #: killed dispatch (``resume_dispatch``), not created from scratch.
    resumed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BlockedDraftResult:
    """Result of :func:`awf.api.blocked_draft` (TODO-0229, M5)."""

    todo_id: str
    #: The ready ``## REVIEW-фикс`` text: the BLOCKED note's findings and fix
    #: directions plus the supervisor's empty decision slots. The supervisor
    #: pastes it into the TODO — this tool never writes the TODO itself.
    review_text: str
    #: Where the BLOCKED note was read from (absolute path).
    note_path: str
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SupervisorContextResult:
    """Result of :func:`awf.api.load_supervisor_context`.

    Aggregate payload for one-shot supervisor bootstrap: everything needed
    to act as supervisor in a single MCP response.
    """

    project_name: str
    project_dir: str
    vision_path: str | None
    vision_excerpt: str
    plan_md: str
    supervisor_md: str
    phase: str  # SMO: current phase (init/goal/form/normalize/brief/run/verify/done)
    phase_prompt: str  # SMO: compact phase-specific instructions
    active_todos: list[dict[str, Any]]
    done_count: int
    blocked_count: int
    pipeline_running: bool
    pipeline_pid: int | None
    log_tail: str | None
    next_stage_role: str | None
    next_role_prohibitions: str | None
    current_stage_name: str | None
    last_signal: str | None
    git_diff_stat: str
    warnings: list[str] = field(default_factory=list)
    # Dogfood-9: structural trigger for increment planning
    increment_planning_needed: bool = False
    final_stage_commit_policy: str | None = None  # on_approved value from last stage
    # ORCH M1.2: the run record — the SAME reading the brief card renders
    # (awf/run_plan_read.read_run_record), the full view: all decisions
    # (which, why, when) + sources. Additive — outside a run every field
    # is empty/False ("0/0" position); run_warning is set when run.yaml
    # exists but is corrupted (degradation, not a crash).
    run_active: bool = False
    run_goal: str = ""
    run_criteria: list[str] = field(default_factory=list)
    run_position: str = "0/0"
    run_budget_minutes: int = 0
    run_budget_left_minutes: int = 0
    run_note: str = ""
    run_last_decision: str = ""  # one line: "reject TODO-0001 — reason"
    run_decisions: list[dict[str, str]] = field(default_factory=list)
    run_sources: list[str] = field(default_factory=list)
    run_warning: str = ""
    # ORCH M2.3: the current/last element's evidence plan — the
    # launch-time snapshot of the TODO's contract (verify/gates/
    # prove_red) + file hash (the same entry the brief card renders
    # compactly). None when nothing was launched.
    run_evidence_plan: dict | None = None
    # TODO-0242 (A.3): the engine-death fact reaches the supervisor
    # bootstrap too — a state that outlived the engine must not read as a
    # calm phase hint. "" when the engine is alive (or no stage is on disk).
    engine_dead: str = ""
    engine_dead_todo: str = ""
    # the nearest permitted action (run-aware after a reject: awf_run_next)
    next_action: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApplyIncrementPlanResult:
    """Result of :func:`awf.api.apply_increment_plan`."""

    plan_path: str
    selected_variant_id: str | None
    variants_offered: int | None
    updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WaitEventResult:
    """Result of :func:`awf.api.wait_for_event` (Phase 3 — supervisor wake-up).

    event_type is one of:
    - ``verify`` — pipeline reached verify stage
    - ``blocked`` — worker wrote BLOCKED
    - ``budget`` — the unit exhausted ``limits.max_review_passes`` (owner decides)
    - ``checkpoint`` — BD-36 checkpoint form open
    - ``done`` — pipeline exited
    - ``timeout`` — no event within timeout
    - ``idle`` — pipeline not running
    """

    event_type: str
    message: str
    state_snapshot: dict[str, Any] = field(default_factory=dict)
    # SPEC A-run: recommended wait size for the next call (median stage/3,
    # clamped to the project's wait cap — wait_event.wait_cap: env
    # AWF_WAIT_CAP / config wait.cap_seconds, default TRANSPORT_CAP); 0 when
    # not computed for this event type.
    suggested_timeout: int = 0
    # REPORTS29 (TODO-0169): on a ``verify`` event — the text about a
    # pre-existing APPROVE the verify wait will IGNORE (older than the wait
    # and its binding does not match the current attempt): why + the
    # re-approve instruction. "" when there is nothing to warn about
    # (no signal, a fresh signal, or a valid pre-existing one the engine
    # will consume).
    stale_decision_hint: str = ""
    # TODO-0242 (A.3): the concrete next command for actionable events —
    # on ``engine_dead`` exactly ``awf_continue(todo_id=...)``. "" for the
    # passive events (timeout/idle/done/stage_changed), which keep their
    # instruction in ``message``.
    next_action: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── Autonomous run (забег) — SPEC A-run v1 ─────────────────────────────


@dataclass
class RunStartResult:
    """Result of :func:`awf.api.run_start`."""

    active: bool
    # RUN3 #2: normalized items {"todo_id", "pipeline"} (empty pipeline =
    # the config default).
    queue: list[dict[str, str]]
    position: str
    budget_minutes: int
    stop_flags: dict[str, list[str]]
    message: str
    next_action: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunStatusResult:
    """Result of :func:`awf.api.run_status`."""

    active: bool
    position: str
    # RUN3 #2: normalized items {"todo_id", "pipeline"} (empty pipeline =
    # the config default).
    queue: list[dict[str, str]]
    index: int
    current: str
    completed: list[str]
    rejects: dict[str, int]
    stop_flags: dict[str, list[str]]
    budget_minutes: int
    budget_left_minutes: int
    elapsed_minutes: int
    stop_reason: str
    report_file: str
    message: str
    no_checkpoints: bool = False
    # B2: the budget counts productive minutes (elapsed − downtime)
    downtime_minutes: int = 0
    productive_minutes: int = 0
    # ORCH M1.1: the run plan (goal, criteria) + the causal memory
    # (decisions: [{ts, kind, todo_id, reason}]). Additive — old state
    # files read as ""/[]/[].
    goal: str = ""
    criteria: list[str] = field(default_factory=list)
    decisions: list[dict[str, str]] = field(default_factory=list)
    # ORCH M4.2: the stalled-stage warning ("" when off / no stall) —
    # the same line the brief card shows (one shared detector).
    stall: str = ""
    # R2.5 (REVIEW, iter 2): the current unit's publication facts —
    # ``commit`` (short OID), ``receipt`` (disposition), ``archived``
    # (path), ``index_sync``, ``pending_intent`` / ``superseded``. Empty
    # dict when there is no current unit or no publication happened yet.
    # Requirement 4: commit / receipt / archive are separate facts, not
    # one inferred from another.
    publication: dict[str, Any] = field(default_factory=dict)
    # TODO-0241: the engine-death line ("stage=..., todo=..., <reason>").
    # "" when the engine is alive or no stage state is on disk.
    engine_dead: str = ""
    engine_dead_todo: str = ""
    # TODO-0241 (REVIEW E1): the BLOCKED closures of the same read — the
    # run surface must keep the shared advice order (blocked above
    # engine_dead) instead of building its own branch.
    blocked_ids: list[str] = field(default_factory=list)
    # The shared liveness answer of the same read: True = a live pipeline
    # owns the project (the stage state is merely not rewritten yet). The
    # run surface publishes the fact instead of letting its consumer
    # rebuild it to tell a live run from a dead one.
    pipeline_running: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunNextResult:
    """Result of :func:`awf.api.run_next`."""

    # AUD05-09: dead "finished" removed from the contract (never produced).
    action: str  # started | stopped | refused
    todo_id: str
    message: str
    run_mode: str = ""
    run_id: int | None = None
    log_file: str = ""
    report_file: str = ""
    next_action: str = ""
    # OPS IMP6 (R6/K7): the same launch handshake ``StartResult`` carries —
    # runner/plugin versions and the advisory drift warning (INV7).
    runner_version: str = ""
    plugin_version: str = ""
    warning: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RestoreResult:
    """Result of :func:`awf.api.restore_todo` (NEG-2026-09-19 R2a safety net)."""

    todo_id: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UnblockResult:
    """Result of :func:`awf.api.unblock_todo` (RUN3 #4 stale-closure clearing)."""

    todo_id: str
    moved: list[str]
    trace: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RemoveTodoResult:
    """Result of :func:`awf.api.remove_todo` (RUN3 #5 never-started removal)."""

    todo_id: str
    trace_path: str
    removed_files: list[str]  # project-relative paths deleted by the removal
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RetireTodoResult:
    """Result of :func:`awf.api.retire_todo` (RUN5 #2 rejected/abandoned archive)."""

    todo_id: str
    moved: list[str]  # project-relative paths of the archived files
    retired_note: str  # project-relative RETIRED-<timestamp>.md
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UpdateTodoResult:
    """Result of :func:`awf.api.update_todo` (RUN6 #4 reword, keep the number)."""

    todo_id: str
    backup: str  # project-relative context/TODO-<id>.md.bak-<ts>
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewAppendResult:
    """Result of :func:`awf.api.review_append` (TODO-0240).

    ``section`` is the heading written (``## REVIEW-фикс (супервизор,
    виток N)``); ``round`` is N. The append works on a STARTED unit (the
    plain reword refuses one), but never while the pipeline is live on it.
    """

    todo_id: str
    section: str
    round: int
    backup: str  # project-relative context/TODO-<id>.md.bak-<ts>
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WritePipelineResult:
    """Result of :func:`awf.api.write_pipeline` (RUN3 #1 named pipelines)."""

    name: str
    file: str
    stages: int  # stage count written
    overwritten: bool  # True when force=True replaced an existing file

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ListPipelinesResult:
    """Result of :func:`awf.api.list_pipelines` (RUN3 #1 named pipelines)."""

    pipelines: list[str]  # names of .agentic/pipelines/*.yaml (sorted)
    active: str  # name the runtime uses (config default_pipeline / "default")
    active_exists: bool  # whether the active pipeline file is present

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunFinishResult:
    """Result of :func:`awf.api.run_finish`."""

    active: bool
    reason: str
    report_file: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunReviseResult:
    """Result of :func:`awf.api.run_revise` (ORCH M3.4 / M4.1 /
    REPORTS32).

    ``action`` is one of:
    - ``preview`` — nothing written; ``changes``/``conflicts`` are the plan;
    - ``applied`` — the not-started elements' pipelines changed, elements
      were replaced (``changes`` entries carry ``class: "replaced"`` with
      ``from`` / ``to``; RB-3: a replace whose ``from`` is the REJECTED
      current element is a retry — the run position moves back to that
      slot, the reject diary stays), and/or new elements joined the queue
      (``class: "appended"`` / ``"inserted"``), the revision is recorded
      (``revision`` = the stored entry);
    - ``noop`` — a repeat with the same ``key`` (``revision`` = the stored
      entry) or a request with no effective change;
    - ``refused`` — no active run / live engine / conflicts (current /
      approved / rejected / started / completed / TODO file missing /
      duplicate target / replace source not queued) / missing key /
      invalid ``position`` / CAS mismatch / a failed stop (M4.1);
      ``current_queue`` + ``generation`` carry the current state (the
      previous plan stays in force).

    ``resume_from`` (ORCH M4.1, ``stop_running=True``): the stage the
    stopped unit resumes from — the ``awf continue`` answer and the
    revision record name it explicitly; ``""`` when nothing was stopped
    (no live stage) or for the preview/noop/refused actions.
    """

    action: str
    preview: bool
    key: str = ""
    generation: int = 0
    current_queue: list[dict[str, Any]] = field(default_factory=list)
    changes: list[dict[str, str]] = field(default_factory=list)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    unchanged: list[dict[str, str]] = field(default_factory=list)
    revision: dict[str, Any] | None = None
    resume_from: str = ""
    message: str = ""
    next_action: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ServiceRunResult:
    """Result of ORCH M5.2 service-run operations
    (:func:`awf.api.run_service_start` / :func:`awf.api.run_service_finish`).

    ``action`` is one of:
    - ``started`` — the service unit launched (the main run untouched);
    - ``stopped`` — the service run closed (``report_file`` = the
      SERVICE-RUN-REPORT); ``next_action`` names the main continuation
      (awf_run_next / awf_continue);
    - ``refused`` — a live pipeline (one per project) / launch failure /
      the unit still in flight; no side effects (or rolled back);
    - ``noop`` — no (or already closed) service run to finish.

    ``main_active``/``main_position``/``main_current`` — the MAIN run's
    snapshot (the service run's answer always shows that the main run
    survived, with its position).
    """

    action: str
    message: str
    todo_id: str = ""
    slug: str = ""
    pipeline: str = ""
    candidate_path: str = ""
    run_mode: str = ""
    run_id: int | None = None
    log_file: str = ""
    report_file: str = ""
    main_active: bool = False
    main_position: str = ""
    main_current: str = ""
    next_action: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ServiceRunStatusResult:
    """Result of :func:`awf.api.run_service_status` — the service run's
    snapshot (unit, slug, pipeline, candidate path, position) PLUS the
    main run's snapshot (position, current) in one read.

    ``warning`` (ORCH M6.4) is non-empty when ``state/service-run.yaml``
    EXISTS but is unreadable (broken YAML or invalid shape): the read
    degrades to "no service run" and says so instead of being indistinguishable
    from "never started" (the run.yaml precedent: RunRecord.warning). The
    file is kept for inspection."""

    service_active: bool
    message: str
    warning: str = ""
    todo_id: str = ""
    slug: str = ""
    pipeline: str = ""
    candidate_path: str = ""
    position: str = "0/0"
    index: int = 0
    current: str = ""
    completed: list[str] = field(default_factory=list)
    stop_reason: str = ""
    report_file: str = ""
    main_active: bool = False
    main_position: str = ""
    main_current: str = ""
    next_action: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ServiceRunApproveResult:
    """Result of :func:`awf.api.run_service_approve` — the approve signal
    pair for the service unit's verify stage (the verdict is recorded in
    the SERVICE slot, never the main run's diary)."""

    todo_id: str
    signal_file: str
    evidence_file: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "InitResult",
    "StatusResult",
    "BaselineResult",
    "RollbackResult",
    "ApproveResult",
    "RejectResult",
    "ReportResult",
    "ResetResult",
    "AddRoleResult",
    "AdoptRoleDraftResult",
    "StartResult",
    "AnalyzeRolesResult",
    "ApplyProjectSetupResult",
    "DispatchTodoResult",
    "SupervisorContextResult",
    "ApplyIncrementPlanResult",
    "WaitEventResult",
    "RunStartResult",
    "RunStatusResult",
    "RunNextResult",
    "RunFinishResult",
    "RunReviseResult",
    "ServiceRunResult",
    "ServiceRunStatusResult",
    "ServiceRunApproveResult",
    "RestoreResult",
    "UnblockResult",
    "RemoveTodoResult",
    "RetireTodoResult",
    "ReviewAppendResult",
    "UpdateTodoResult",
]
