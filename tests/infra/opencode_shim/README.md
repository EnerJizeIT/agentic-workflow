# tests/infra/opencode_shim — scenario-driven `opencode` shim

Deterministic stand-in for the real `opencode` CLI. Lets e2e tests drive
the engine's subprocess container (spawn → signal watch → salvage →
hard-timeout kill) without a real opencode/qwen process. Used by
`tests/e2e/test_w6_salvage_scenario.py`.

## How it is put in front of the engine

Put this directory at the FRONT of `PATH` for the `awf` subprocess —
the engine resolves `opencode` from PATH:

```python
env["PATH"] = f"{SHIM_DIR}:{env['PATH']}"
```

## Invocation contract

The engine calls (see `awf/agent_stage.py::run_agent_stage` and
`awf/supervisor.py::run_supervisor_via_subprocess`):

```
opencode run --auto [--print-logs] --agent <name> --title <title>
             [--model <model>] --file <f1> [--file <f2> ...] -- <prompt>
```

The shim distinguishes the two call classes by `--title`:

| call         | title                    | TODO id source     |
|--------------|--------------------------|--------------------|
| worker       | `awf-<role>-<TODO-NNNN>` | the title          |
| supervisor   | `awf-supervisor-<kind>`  | the prompt text    |

`--file` arguments are ignored (they are role/TODO/handoff .md files).
`cwd` is the project dir, so `.agentic/` paths are relative.

## Modes (env-driven, switchable between engine invocations)

`AWF_SHIM_MODE` (worker): `silent-exit` (default) · `crash` · `hang` ·
`do-step` · `write-blocked` — see the header comment of `opencode` for
exact behavior.

`AWF_SHIM_SUPERVISOR_MODE` (supervisor): `approve` · `reject` ·
`plan-todo` · `noop`. Default is derived from the title kind
(verify → approve, plan/replan → plan-todo, else noop).

Trace (set `AWF_SHIM_STATE_DIR` to a fresh dir per engine run):

- `invocation.log` — one line per invocation:
  `<pid> <worker|supervisor> <mode> <title> <todo>`;
- `hang-pids` — `<main pid>` + `<child pid>` (hang mode), for the
  "the process group is gone after the hard timeout" assertion.

Tunables: `AWF_SHIM_HANG_SECONDS` (default 300),
`AWF_SHIM_PLAN_TODO_ID` (default TODO-0001).

## Differences from `tests/stubs/opencode`

- scenario-driven: behavior is switched via env BETWEEN engine runs
  (a salvage retry sees the new mode); the old stub is one-shot;
- worker/supervisor distinction by `--title` (the old stub cannot serve
  the verify stage);
- invocation trace for assertions;
- hang mode spawns a child `sleep` so the group-kill is observable.
