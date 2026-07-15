#!/usr/bin/env bash
# yaml.sh — YAML helpers for awf.
#
# Prefers `yq` (Go binary) when available. Falls back to `python3` with PyYAML
# (which is already a soft dependency via baseline.sh). Aborts with a clear
# error if neither is present, instead of silently mis-parsing with regex.
#
# Public functions:
#   yaml_get <dotted.path> <file> [default]      — single scalar value
#   yaml_stages_dump <file>                       — pipeline stages, TSV rows
#   yaml_backend                                  — echo which backend is in use
set -uo pipefail

# Detect backend once and cache.
_yaml_backend() {
    if [[ -n "${_YAML_BACKEND:-}" ]]; then
        printf '%s' "$_YAML_BACKEND"
        return
    fi
    if command -v yq &>/dev/null; then
        _YAML_BACKEND="yq"
    elif command -v python3 &>/dev/null && python3 -c 'import yaml' &>/dev/null 2>&1; then
        _YAML_BACKEND="python"
    else
        _YAML_BACKEND="none"
    fi
    printf '%s' "$_YAML_BACKEND"
}

yaml_backend() { _yaml_backend; }

_yaml_require_backend() {
    if [[ "$(_yaml_backend)" == "none" ]]; then
        cat >&2 <<'EOF'
ERROR: cannot parse YAML — neither `yq` nor `python3+PyYAML` is available.
Install one:
  sudo apt-get install yq            # or: pip install pyyaml
EOF
        return 1
    fi
    return 0
}

# yaml_get <path> <file> [default]
# Path is dotted, e.g. "verification.test_cmd", "project.name", "default_pipeline".
# Returns the scalar value (no trailing newline), or default if missing/null.
yaml_get() {
    local path="$1" file="$2" default="${3:-}"
    _yaml_require_backend || return 1

    if [[ ! -f "$file" ]]; then
        printf '%s' "$default"
        return 0
    fi

    local val=""
    case "$(_yaml_backend)" in
        yq)
            val=$(yq -r "${path}" "$file" 2>/dev/null) || val=""
            ;;
        python)
            val=$(YAML_PATH="$path" python3 -c '
import os, sys, yaml
path = os.environ["YAML_PATH"]
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = yaml.safe_load(f)
    cur = data
    if path:
        for part in path.split("."):
            if cur is None:
                cur = None
                break
            if isinstance(cur, list):
                try:
                    cur = cur[int(part)]
                except (ValueError, IndexError):
                    cur = None
                    break
            elif isinstance(cur, dict):
                cur = cur.get(part)
            else:
                cur = None
                break
    if isinstance(cur, (list, dict)):
        sys.stdout.write("")
    else:
        sys.stdout.write("" if cur is None else str(cur))
except Exception:
    sys.stdout.write("")
' "$file" 2>/dev/null) || val=""
            ;;
    esac

    if [[ -z "$val" || "$val" == "null" ]]; then
        val="$default"
    fi
    printf '%s' "$val"
}

# yaml_stages_dump <file>
# Emits one TSV row per stage, in order. Columns:
#   name \t role \t action \t description \t on_blocked \t on_approved
#        \t on_rejected \t on_passed \t on_failed \t max_retries
# Empty/missing scalar fields default to awf's documented defaults so the
# orchestrator can populate arrays uniformly.
yaml_stages_dump() {
    local file="$1"
    _yaml_require_backend || return 1

    if [[ ! -f "$file" ]]; then
        return 0
    fi

    case "$(_yaml_backend)" in
        yq)
            yq -r \
              '.stages[] | [
                (.name // ""),
                (.role // ""),
                (.action // ""),
                (.description // ""),
                (.on_blocked // "escalate"),
                (.on_approved // "next"),
                (.on_rejected // "rollback_to:implement"),
                (.on_passed // "next"),
                (.on_failed // "rollback_to:implement"),
                (.max_retries // 1)
              ] | @tsv' "$file" 2>/dev/null
            ;;
        python)
            PIPE_FILE="$file" python3 -c '
import os, sys, yaml
with open(os.environ["PIPE_FILE"], encoding="utf-8") as f:
    data = yaml.safe_load(f) or {}
stages = data.get("stages") or []
defaults = {
    "on_blocked": "escalate",
    "on_approved": "next",
    "on_rejected": "rollback_to:implement",
    "on_passed": "next",
    "on_failed": "rollback_to:implement",
    "max_retries": 1,
}
cols = ["name","role","action","description",
        "on_blocked","on_approved","on_rejected","on_passed","on_failed","max_retries"]
for s in stages:
    if not isinstance(s, dict):
        continue
    row = []
    for c in cols:
        v = s.get(c, defaults.get(c, ""))
        if c == "max_retries":
            try:
                v = int(v)
            except Exception:
                v = defaults["max_retries"]
        v = str(v)
        v = v.replace("\t", " ").replace("\n", " ").replace("\r", " ")
        row.append(v)
    sys.stdout.write("\t".join(row) + "\n")
' 2>/dev/null
            ;;
    esac
}
