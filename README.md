# Agentic Workflow Framework

Declarative multi-agent workflow framework with supervisor/worker pipeline orchestration via file-based message bus.

## What it is

A lightweight framework that lets you run LLM agents in a structured pipeline: **Supervisor plans → Worker implements → (optional Reviewer checks) → (optional Tester verifies) → Supervisor approves**. All communication happens through files on disk. No Python code to write — just YAML config and Markdown instructions.

## Quick start

```bash
# 1. Install the CLI
cp bin/awf ~/.local/bin/awf
chmod +x ~/.local/bin/awf

# 2. Initialize your project
cd /path/to/your-project
awf init --template simple   # or --template full for reviewer+tester

# 3. Start the pipeline
awf start
```

See `proposal/` for full specification.
