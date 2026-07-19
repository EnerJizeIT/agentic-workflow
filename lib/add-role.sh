#!/usr/bin/env bash
# add-role.sh — Generate a template for a new role
set -euo pipefail

ROLE_NAME="${1:-}"
if [[ -z "$ROLE_NAME" ]]; then
    echo "Usage: awf add-role <role-name> [--description <text>] [--model <model>]"
    echo "  --model is recommended. If omitted, you will be prompted for it."
    exit 1
fi

DESCRIPTION=""
MODEL=""

shift
while [[ $# -gt 0 ]]; do
    case "$1" in
        --description) DESCRIPTION="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        *) shift ;;
    esac
done

# No default: an empty model is loud and obvious in config.yaml, instead of
# silently pointing at a provider the user does not have. `awf init` reuses an
# existing opencode agent's model; for `awf add-role` we ask the user.
if [[ -z "$MODEL" ]]; then
    read -rp "Model id for role '$ROLE_NAME' (e.g. claude-sonnet-4-20250514, gpt-4.1): " MODEL
    [[ -z "$MODEL" ]] && MODEL="<set-me-in-.agentic/config.yaml>"
fi

AGENTIC_DIR=".agentic"
ROLES_DIR="$AGENTIC_DIR/roles"

if [[ ! -d "$AGENTIC_DIR" ]]; then
    echo "No .agentic/ found. Run 'awf init' first."
    exit 1
fi

mkdir -p "$ROLES_DIR"

cat > "$ROLES_DIR/$ROLE_NAME.md" <<EOF
# ROLE: $ROLE_NAME

**Role:** ${DESCRIPTION:-new role}
**Runs as:** \`opencode run --agent $ROLE_NAME\`
**Model:** $MODEL

## 1. Who you are

<describe the responsibility of this role>

## 2. Input

This role receives:
- <what comes from the previous stage>

## 3. Actions

<describe what to do with the input>

## 4. Output

When finished, create one of:
- \`.agentic/outbox/APPROVED-{NNNN}.md\` + \`.ready\` — success
- \`.agentic/outbox/REJECTED-{NNNN}.md\` + \`.ready\` — needs fixes
- \`.agentic/outbox/BLOCKED-{NNNN}.md\` + \`.ready\` — needs supervisor

## 5. Prohibitions

<list what this role must NOT do>
EOF

echo "Created: $ROLES_DIR/$ROLE_NAME.md"
echo ""
echo "Next steps:"
echo "  1. Edit the role instructions in $ROLES_DIR/$ROLE_NAME.md"
echo "  2. Add model config to .agentic/config.yaml under models:$ROLE_NAME"
echo "  3. Add a stage to your pipeline YAML that uses role: $ROLE_NAME"
