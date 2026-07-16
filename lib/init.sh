#!/usr/bin/env bash
# init.sh — Initialize .agentic/ in current project
set -euo pipefail

FRAMEWORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATES_DIR="$FRAMEWORK_DIR/templates"

TEMPLATE="simple"
FORCE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --template) TEMPLATE="$2"; shift 2 ;;
        --force) FORCE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [[ -d ".agentic" ]] && [[ $FORCE -ne 1 ]]; then
    echo "ERROR: .agentic/ already exists. Use --force to overwrite."
    exit 1
fi

if ! git rev-parse --git-dir &>/dev/null; then
    echo "ERROR: Not a git repository. Run 'git init' first."
    exit 1
fi

echo "=== Agentic Workflow Init ==="
echo ""

# Interactive questions
read -rp "Project name: " PROJECT_NAME
echo "(Verify commands below are load-bearing: when set, awf auto-confirms completed"
echo " work whose verify passes — no manual salvage. Leave blank only if none apply.)"
read -rp "Test command (e.g. pytest tests/, bun test): " TEST_CMD
read -rp "Lint command (e.g. ruff check .): " LINT_CMD
read -rp "Typecheck command (e.g. mypy src/, tsc --noEmit): " TYPECHECK_CMD
read -rp "Build command (optional, e.g. docker compose config): " BUILD_CMD

if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "[DRY RUN] Would create:"
    echo "  .agentic/config.yaml"
    echo "  .agentic/roles/supervisor.md"
    echo "  .agentic/roles/worker.md"
    [[ "$TEMPLATE" == "full" ]] && {
        echo "  .agentic/roles/reviewer.md"
        echo "  .agentic/roles/tester.md"
    }
    echo "  .agentic/pipelines/default.yaml"
    echo "  .agentic/phases/"
    echo "  .agentic/inbox/, outbox/, context/, logs/, reports/"
    exit 0
fi

# Create directories
mkdir -p .agentic/{roles,pipelines,phases,inbox,outbox,context,logs,reports}

# Generate config.yaml
cat > .agentic/config.yaml <<EOF
project:
  name: "${PROJECT_NAME}"
  root: "."

models:
  supervisor:
    description: "Current session model"
  worker:
    agent_name: "worker"
    model: "vllm/llm"
    temperature: 0.1
  reviewer:
    agent_name: "reviewer"
    model: "vllm/llm"
    temperature: 0.1
  tester:
    agent_name: "tester"
    model: "vllm/llm"
    temperature: 0.1

verification:
  test_cmd: "${TEST_CMD}"
  lint_cmd: "${LINT_CMD}"
  typecheck_cmd: "${TYPECHECK_CMD}"
  build_cmd: "${BUILD_CMD}"
  coverage_cmd: ""

phases:
  current: ".agentic/phases/plan.md"

default_pipeline: "default"

retry:
  max_attempts: 3
  backoff_seconds: 0
EOF

# Copy role templates
cp "$TEMPLATES_DIR/roles/supervisor.md" .agentic/roles/
cp "$TEMPLATES_DIR/roles/worker.md" .agentic/roles/

if [[ "$TEMPLATE" == "full" ]]; then
    cp "$TEMPLATES_DIR/roles/reviewer.md" .agentic/roles/
    cp "$TEMPLATES_DIR/roles/tester.md" .agentic/roles/
fi

# Copy pipeline template
if [[ "$TEMPLATE" == "full" ]]; then
    cp "$TEMPLATES_DIR/pipelines/full.yaml" .agentic/pipelines/default.yaml
else
    cp "$TEMPLATES_DIR/pipelines/simple.yaml" .agentic/pipelines/default.yaml
fi

# Copy TODO template
cp "$TEMPLATES_DIR/todo-template.md" .agentic/

# Update .gitignore
GITIGNORE_BLOCK=$'.agentic/inbox/\n.agentic/outbox/\n.agentic/context/\n.agentic/logs/\n.agentic/reports/'

if [[ -f .gitignore ]]; then
    if ! grep -q ".agentic/inbox/" .gitignore; then
        echo "" >> .gitignore
        echo "# Agentic workflow runtime files" >> .gitignore
        echo "$GITIGNORE_BLOCK" >> .gitignore
    fi
else
    echo "# Agentic workflow runtime files" > .gitignore
    echo "$GITIGNORE_BLOCK" >> .gitignore
fi

echo ""
echo "Created .agentic/ with ${TEMPLATE} template"

# Offer to create the opencode agents awf needs (worker; +reviewer/tester for full).
# Without these, `awf start` cannot spawn the worker — the #1 gotcha for new users.
OFFER_AGENTS="worker"
[[ "$TEMPLATE" == "full" ]] && OFFER_AGENTS="worker reviewer tester"

create_opencode_agents() {
    local cfg="$HOME/.config/opencode/opencode.json"
    if [[ ! -f "$cfg" ]]; then
        echo ""
        echo "NOTE: no opencode config found at $cfg"
        echo "      Create the opencode agents ($OFFER_AGENTS) manually (see README → Requirements)."
        return
    fi
    command -v python3 &>/dev/null || { echo "NOTE: python3 needed to add agents automatically."; return; }

    read -rp "Create opencode agents ($OFFER_AGENTS) in $cfg? [Y/n] " ans
    [[ "${ans:-Y}" =~ ^[Yy]$ ]] || { echo "Skipping agent creation (create them manually if needed)."; return; }

    local roles="$1"
    cp "$cfg" "${cfg}.bak-$(date +%Y%m%d%H%M%S)"
    python3 - "$cfg" "$roles" <<'PYEOF'
import json, sys
cfg, roles = sys.argv[1], sys.argv[2].split()
with open(cfg) as f:
    d = json.load(f)
agents = d.setdefault("agent", {})
if not isinstance(agents, dict):
    print("  'agent' is not an object — skipping."); sys.exit(0)
# Reuse an existing agent's model so the new agents work out of the box.
model = None
for a in agents.values():
    if isinstance(a, dict) and a.get("model"):
        model = a["model"]; break
if not model:
    model = d.get("model") or input("  No existing model found. Enter model id (e.g. vllm/llm): ").strip()
if not model:
    print("  No model — skipping agent creation."); sys.exit(0)
added = []
for r in roles:
    if r not in agents:
        agents[r] = {"description": f"awf {r} agent", "model": model}
        added.append(r)
if added:
    with open(cfg, "w") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    print(f"  Added agents: {', '.join(added)} (model: {model})")
else:
    print(f"  All agents ({', '.join(roles)}) already present.")
PYEOF
}
create_opencode_agents "$OFFER_AGENTS"

echo ""
echo "Next steps:"
echo "  1. Edit .agentic/config.yaml if needed"
echo "  2. Create .agentic/phases/plan.md with your implementation plan"
echo "  3. Run: awf start --auto --background   (or: awf start for interactive)"
