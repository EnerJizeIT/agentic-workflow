# Release notes

Short, human note for what the current release ships. Full per-version
history lives in `CHANGELOG.md`; this file is the quick "what changed and
why it matters" for the `awf` core and the `agent-workflow-ui` MCP plugin.

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
