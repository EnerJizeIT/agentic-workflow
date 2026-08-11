# Agentic Workflow (awf)

[![license: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![tests](https://img.shields.io/badge/tests-1000%2B-brightgreen.svg)](#)

> **Multi-agent pipeline orchestrator for opencode. Plan → build → verify → commit — through typed MCP tools, not bash.**

## The problem it solves

AI coding agents are powerful but chaotic. They jump straight to code without planning, skip review, leave bugs. You watch helplessly as tokens burn.

**awf** adds structure: a supervisor agent plans the work, worker agents execute through a pipeline (analyst → architect → implementer → QA → audit), and you approve each result before it commits. All through natural language — *"start working on the backlog"*, *"verify and approve"*, *"reject — the DOMParser fix is missing"*.

You stay in control. The agent stays on rails.

## Features

- 🎯 **State-Machine Orchestration (SMO)** — awf guides the supervisor through phases: `init → goal → form → normalize → brief → run → verify → done`. Every tool returns a `next_action` hint — even weak models (Qwen vLLM) follow the full flow without getting lost.
- 🔧 **29 MCP tools** — typed pipeline control: init, dispatch, start, approve, reject, rollback, dashboard, model validation. No bash, no manual file editing.
- 📊 **Live Dashboard** — HTTP server with real-time polling. Chat-style agent handoffs, TODO content, TODO timeline, worker status, browser notifications. No page reloads.
- 🧱 **Custom pipelines** — any roles, any depth. 1 stage or 10. Analyst → architect → implementer → QA → audit, or just a single worker. You choose in the setup form.
- ✅ **Approve / Reject** — symmetric verify tools. Approve commits and archives. Reject kills the pipeline and asks for fixes.
- 🔍 **Pre-dispatch check** — before launching a pipeline, awf greps your codebase for keywords from the TODO. Warning if the task might already be done.
- 🔄 **Crash recovery** — salvage path when workers don't signal, orphan TODO cleanup, state reconciliation on startup.
- 📋 **Increment planning** — decomposition variants (vertical, horizontal, risk-first) presented as an HTML form for user choice.

## Installation

```bash
pip install -e .
pip install -e ./agent_workflow_ui
```

Add to `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "agent-workflow-ui": {
      "type": "local",
      "command": ["python3", "-m", "agent_workflow_ui"],
      "enabled": true
    }
  }
}
```

Restart opencode.

## Usage

Talk in natural language — the supervisor agent calls the right tools:

| You say | What happens |
|---|---|
| *"Initialize awf in my project"* | Creates `.agentic/`, detects stack, asks for your goal |
| *"Develop the MVP"* | Opens setup form → configures pipeline → plans first TODO |
| *"Verify"* | Supervisor reads handoffs, checks git diff, approves or rejects |
| *"Reject — the cache is missing"* | Pipeline killed, new TODO dispatched with fix instructions |

### SMO Flow

```
init → goal → form → normalize → brief → run → verify → done
  │       │       │         │         │       │       │
  awf     user    form      roles     TODO    agents  approve/
  sets    sets    creates   analyzed  dispatch+  work  reject
  up      goal    pipeline  +confirmed  start
```

Every phase: compact prompt (~50 lines) + `next_action` in every tool result.

## Architecture

```
opencode (supervisor LLM)
  ↕ MCP stdio (29 typed tools)
agent-workflow-ui plugin
  ↕ Python import
awf orchestrator
  ↕ subprocess
opencode run (worker agents)
  ↕ HTTP daemon thread
Dashboard (live /api/state polling)
```

Two packages:
- **`awf`** — Python core. Pipeline engine, phase state machine, signals, commit gate, dashboard server.
- **`agent_workflow_ui`** — MCP plugin. Thin async wrappers + `next_action` guidance + HTML forms.

## Dashboard

Live HTTP dashboard opens automatically when pipeline starts:

- **Two-panel layout** — pipeline sidebar (stages, progress, worker) + content tabs
- **💬 Agent Chat** — handoffs as conversation messages with chain visualization (`↓ передал → 🔧 Implementer`)
- **📝 Задача** — full TODO content in rendered markdown
- **📊 События** — meaningful events, newest first
- **TODO timeline** — `[✅ TODO-0001] ─ [✅ TODO-0002] ─ [🔄 TODO-0003]`
- **Browser notification** when pipeline reaches verify

## Documentation

- [USAGE.md](USAGE.md) — usage scenarios and tool reference
- [Architecture](vision/architecture.md) — components, data flow, design decisions
- [Product Vision](vision/agent-ui-plugin.md) — competitive advantages, dogfood results
- [Supervisor Flow (SMO)](vision/supervisor-flow.md) — phase system, next_action pattern
- [BACKLOG](BACKLOG.md) — open tasks
- [README.ru.md](README.ru.md) — Russian README

## Dogfood results

6 dogfood sessions on jira-epic-presenter (Qwen vLLM):
- Full SMO flow end-to-end: init → goal → form → normalize → brief → run → verify
- 4 TODOs per session, 1× approve (no loops), zero polling
- Reject flow tested: supervisor found missing work, rejected, re-dispatched with fix
- Pre-dispatch check caught already-implemented tasks

## License

MIT — see [LICENSE](LICENSE).
