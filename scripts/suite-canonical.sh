#!/usr/bin/env bash
# suite-canonical.sh — один канонический прогон полного сьюта на состояние дерева.
#
# Зачем: роли гоняли полный pytest много раз за один attempt (R2 — около 66
# прогонов, доминирующая цена витка). Здесь прогон привязан к tree-sha:
# повторный вызов на том же дереве с той же командой возвращает готовый
# артефакт и НЕ запускает тесты заново. Перезапуск — только с `--force`,
# после изменения дерева или смены `AWF_SUITE_CMD`.
#
# Артефакт: `.agentic/context/SUITE-<tree-sha>-<cmd8>.json` — sha, команда,
# старт/конец, exit, счётчики и хвост вывода. `cmd8` — первые 8 hex от
# sha256 фактической команды: узкий прогон не перезаписывает канонический.
# Это runtime (не коммитится).
#
# Команда по умолчанию — полный сьют проекта. Для тестов и нестандартных
# проектов переопределяется `AWF_SUITE_CMD` (shell-команда, запускается из
# корня проекта).
#
#   scripts/suite-canonical.sh [--force] [--project DIR]
#
# Код возврата: код сьюта (0 — зелёный). 2 — не удалось посчитать tree-sha
# или неверные аргументы.
set -u

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2
root=$(CDPATH= cd -- "$script_dir/.." && pwd) || exit 2

project=$(pwd)
force=0

usage() {
  echo "usage: suite-canonical.sh [--force] [--project DIR]" >&2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --force) force=1 ;;
    --project)
      shift
      [ "$#" -gt 0 ] || { echo "suite-canonical: --project needs a path" >&2; usage; exit 2; }
      project=$1
      ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "suite-canonical: unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
  shift
done

cd "$project" 2>/dev/null || { echo "suite-canonical: no such project dir: $project" >&2; exit 2; }

if ! command -v git >/dev/null 2>&1; then
  echo "suite-canonical: git is required (tree-sha is content-addressed)" >&2
  exit 2
fi

# CLI: в дереве исходников — $root/bin/awf; в чужом проекте — `awf` на PATH
# или установленный пакет (`python3 -m awf`).
awf=""
if [ -x "$root/bin/awf" ]; then
  awf="$root/bin/awf"
elif command -v awf >/dev/null 2>&1; then
  awf=$(command -v awf)
elif python3 -c 'import awf' >/dev/null 2>&1; then
  awf=""
else
  echo "suite-canonical: awf CLI not found (tried \$root/bin/awf, 'awf' on PATH, 'python3 -m awf')" >&2
  exit 2
fi

run_awf() {
  if [ -n "$awf" ]; then
    "$awf" "$@"
  else
    python3 -m awf "$@"
  fi
}

sha=$(run_awf tree-sha) || {
  echo "suite-canonical: cannot compute tree-sha (git repo with a commit required)" >&2
  exit 2
}

cmd=${AWF_SUITE_CMD:-python3 -m pytest tests/ -q -n auto --timeout=300 -p no:cacheprovider}
cmd8=$(python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:8])' "$cmd") || exit 2

context=".agentic/context"
artifact="$context/SUITE-$sha-$cmd8.json"
mkdir -p "$context" || exit 2

# Артефакт — runtime. Если context не покрыт .gitignore, добавляем правило
# только в локальный exclude (.git/info/exclude), иначе сам артефакт меняет
# tree-sha и следующий вызов никогда не попадёт в кэш.
if ! git check-ignore -q -- "$context/SUITE-probe.json" 2>/dev/null; then
  exclude=$(git rev-parse --git-path info/exclude 2>/dev/null || true)
  if [ -z "$exclude" ]; then
    echo "suite-canonical: WARNING — cannot locate .git/info/exclude; the artifact may change tree-sha and defeat the cache" >&2
  else
    mkdir -p "$(dirname "$exclude")" 2>/dev/null || true
    if ! grep -qxF '.agentic/context/' "$exclude" 2>/dev/null; then
      if ! printf '\n# awf suite-canonical runtime artifact\n.agentic/context/\n' >> "$exclude" 2>/dev/null; then
        echo "suite-canonical: WARNING — cannot write to $exclude; the artifact may change tree-sha and defeat the cache" >&2
      fi
    fi
  fi
fi

if [ "$force" -ne 1 ] && [ -f "$artifact" ]; then
  # The cache is keyed by the tree AND by the command: the same tree with a
  # different AWF_SUITE_CMD must not serve the other command's result as
  # canonical (the artifact stores "command" for exactly this check).
  cached_exit=$(python3 - "$artifact" "$cmd" <<'PY'
import json
import sys

try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    if data.get("command") == sys.argv[2] and isinstance(data.get("exit"), int):
        print(data["exit"])
except Exception:
    pass
PY
)
  if [ -n "$cached_exit" ]; then
    echo "suite-canonical: cached (tree-sha $sha, same command) — $artifact; use --force to rerun"
    cat "$artifact"
    exit "$cached_exit"
  fi
  echo "suite-canonical: command changed — rerunning (tree-sha $sha)"
fi

log=$(mktemp "${TMPDIR:-/tmp}/awf-suite.XXXXXX") || exit 2
trap 'rm -f "$log"' EXIT

started_iso=$(date -u +%Y-%m-%dT%H:%M:%SZ)
start_s=$(date +%s)

bash -o pipefail -c "$cmd" >"$log" 2>&1
rc=$?

end_s=$(date +%s)
finished_iso=$(date -u +%Y-%m-%dT%H:%M:%SZ)

SUITE_SHA="$sha" \
SUITE_CMD="$cmd" \
SUITE_STARTED="$started_iso" \
SUITE_FINISHED="$finished_iso" \
SUITE_EXIT="$rc" \
SUITE_DURATION="$((end_s - start_s))" \
SUITE_LOG="$log" \
SUITE_ARTIFACT="$artifact" \
python3 - <<'PY' || echo "suite-canonical: WARNING — could not write $artifact" >&2
import json
import os
import re
from pathlib import Path

text = Path(os.environ["SUITE_LOG"]).read_text(encoding="utf-8", errors="replace")


def count(pattern):
    m = re.search(pattern, text)
    return int(m.group(1)) if m else 0


counts = {
    "passed": count(r"(\d+) passed"),
    "failed": count(r"(\d+) failed"),
    "errors": count(r"(\d+) errors?"),
    "skipped": count(r"(\d+) skipped"),
}
tail = "\n".join(
    line for line in text.splitlines()[-20:] if line.strip()
)[-4000:]

payload = {
    "sha": os.environ["SUITE_SHA"],
    "command": os.environ["SUITE_CMD"],
    "started": os.environ["SUITE_STARTED"],
    "finished": os.environ["SUITE_FINISHED"],
    "duration_seconds": int(os.environ["SUITE_DURATION"]),
    "exit": int(os.environ["SUITE_EXIT"]),
    "counts": counts,
    "tail": tail,
}

artifact = Path(os.environ["SUITE_ARTIFACT"])
tmp = artifact.with_suffix(".json.tmp")
tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
tmp.replace(artifact)
PY

echo "suite-canonical: exit $rc (tree-sha $sha) — $artifact"
tail -n 20 "$log"
exit "$rc"
