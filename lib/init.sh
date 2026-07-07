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
read -rp "Test command (e.g. pytest tests/): " TEST_CMD
read -rp "Lint command (e.g. ruff check .): " LINT_CMD
read -rp "Typecheck command (e.g. mypy src/): " TYPECHECK_CMD
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
echo ""
echo "Next steps:"
echo "  1. Edit .agentic/config.yaml if needed"
echo "  2. Create .agentic/phases/plan.md with your implementation plan"
echo "  3. Run: awf start"
