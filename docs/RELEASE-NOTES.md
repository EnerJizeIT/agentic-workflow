# Release notes

Short, human note for what is shipping next. Full per-version history lives in
`CHANGELOG.md`; this file is the quick "what changed and why it matters" for the
`awf` core and the `agent-workflow-ui` MCP plugin.

## [1.4.0] — 2026-09-26

Five things change since 1.3.0. One is a breaking change — read that first.

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

### Security

- **One-time checkpoint token.** The plan-checkpoint form carries a one-time
  token. A decision POST is accepted only with that token (default-deny without
  it), so a client that learns the local port but not the token cannot submit a
  decision on your behalf.
