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

# Pick the worker model: reuse an existing opencode agent's model if possible,
# otherwise ask. This replaces the old hardcoded `vllm/llm` default that silently
# produced non-working configs on machines with a different provider.
OC_CFG="$HOME/.config/opencode/opencode.json"
REUSED_MODEL=""
if [[ -f "$OC_CFG" ]] && command -v python3 &>/dev/null; then
    REUSED_MODEL=$(python3 -c '
import json, os
try:
    with open(os.path.expanduser("~/.config/opencode/opencode.json")) as f:
        d = json.load(f)
    agents = d.get("agent") or {}
    if isinstance(agents, dict):
        for a in agents.values():
            if isinstance(a, dict) and a.get("model"):
                print(a["model"]); break
except Exception:
    pass
' 2>/dev/null)
fi

echo ""
if [[ -n "$REUSED_MODEL" ]]; then
    read -rp "Model for worker/reviewer/tester agents [default: $REUSED_MODEL]: " WORKER_MODEL
    WORKER_MODEL="${WORKER_MODEL:-$REUSED_MODEL}"
else
    read -rp "Model id for worker/reviewer/tester agents (e.g. claude-sonnet-4-20250514, gpt-4.1): " WORKER_MODEL
    if [[ -z "$WORKER_MODEL" ]]; then
        echo "  (left blank — edit .agentic/config.yaml before \`awf start\`)"
    fi
fi

if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "[DRY RUN] Would create:"
    echo "  .agentic/config.yaml (worker model: ${WORKER_MODEL:-<blank>})"
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
    model: "${WORKER_MODEL}"
    temperature: 0.1
  reviewer:
    agent_name: "reviewer"
    model: "${WORKER_MODEL}"
    temperature: 0.1
  tester:
    agent_name: "tester"
    model: "${WORKER_MODEL}"
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
# Two-phase: PROPOSE (dry-run, print what would change) → confirm → APPLY (backup + write).
# Model is taken from $WORKER_MODEL (already chosen above), so we don't ask twice.
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

    # Phase 1: dry-run — what would change?
    local proposal
    proposal=$(python3 - "$cfg" "$OFFER_AGENTS" "${WORKER_MODEL:-}" <<'PYEOF'
import json, sys
cfg, roles, model = sys.argv[1], sys.argv[2].split(), sys.argv[3]
try:
    with open(cfg) as f: d = json.load(f)
except Exception as e:
    print(f"ERR:cannot read {cfg}: {e}"); sys.exit(0)
agents = d.get("agent")
if agents is None:
    print(f"ERR:no 'agent' key in {cfg}"); sys.exit(0)
if not isinstance(agents, dict):
    print("ERR:'agent' is not an object"); sys.exit(0)

to_add, to_update, unchanged = [], [], []
for r in roles:
    cur = agents.get(r)
    if cur is None:
        to_add.append(r)
    elif isinstance(cur, dict) and model and cur.get("model") != model:
        to_update.append(f"{r}: model {cur.get('model')!r} -> {model!r}")
    else:
        unchanged.append(r)

if not to_add and not to_update:
    print(f"NOTHING: all of {', '.join(roles)} already present.")
    sys.exit(0)

lines = []
if to_add:
    lines.append(f"  + add agents: {', '.join(to_add)}")
if to_update:
    lines.append("  ~ update:")
    for u in to_update: lines.append(f"      {u}")
if model:
    lines.append(f"  model: {model}")
else:
    lines.append("  model: <blank> — agent entries will have empty model, edit them manually")
print("PROPOSE:")
print("\n".join(lines))
PYEOF
)

    # If python errored or there's nothing to do, skip the apply phase.
    case "$proposal" in
        ERR:*|NOTE:*)
            echo ""
            echo "NOTE: cannot propose agent changes — ${proposal}"
            echo "      Add $OFFER_AGENTS manually to $cfg."
            return
            ;;
        NOTHING:*)
            echo ""
            echo "$proposal"
            return
            ;;
        PROPOSE:*)
            : # fall through to confirmation
            ;;
        *)
            echo ""
            echo "NOTE: unexpected proposal output: $proposal"
            return
            ;;
    esac

    echo ""
    echo "Proposed change to $cfg:"
    echo "$proposal" | sed 's/^PROPOSE://'
    echo "  (a timestamped backup ${cfg}.bak-<ts> will be created before writing)"
    read -rp "Apply this change to opencode config? [Y/n] " ans
    [[ "${ans:-Y}" =~ ^[Yy]$ ]] || { echo "Skipping agent creation (create them manually if needed)."; return; }

    # Phase 2: APPLY — backup, then write.
    cp "$cfg" "${cfg}.bak-$(date +%Y%m%d%H%M%S)"
    python3 - "$cfg" "$OFFER_AGENTS" "${WORKER_MODEL:-}" <<'PYEOF'
import json, sys
cfg, roles, model = sys.argv[1], sys.argv[2].split(), sys.argv[3]
with open(cfg) as f: d = json.load(f)
agents = d.setdefault("agent", {})
if not isinstance(agents, dict):
    print("  'agent' is not an object — skipping."); sys.exit(0)
added, updated = [], []
for r in roles:
    cur = agents.get(r)
    if cur is None:
        agents[r] = {"description": f"awf {r} agent", "model": model}
        added.append(r)
    elif isinstance(cur, dict) and model and cur.get("model") != model:
        cur["model"] = model
        updated.append(r)
if added or updated:
    with open(cfg, "w") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    parts = []
    if added:   parts.append(f"added: {', '.join(added)}")
    if updated: parts.append(f"updated model: {', '.join(updated)}")
    print(f"  {'; '.join(parts)} (model: {model or '<blank>'})")
else:
    print("  Nothing to write — all agents already up to date.")
PYEOF
}
create_opencode_agents "$OFFER_AGENTS"

echo ""
echo "Next steps:"
echo "  1. Edit .agentic/config.yaml if needed"
echo "  2. Create .agentic/phases/plan.md with your implementation plan"
echo "  3. Run: awf start --auto --background   (or: awf start for interactive)"
