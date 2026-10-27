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
# Код возврата: код сьюта (0 — зелёный). 2 — не удалось посчитать tree-sha,
# неверные аргументы, пустой verification.test_cmd или битый config.yaml
# («сьют не настроен»; битый файл называется отдельным текстом).
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

# Интерпретатор проекта — один на весь скрипт: `AWF_PYTHON` (если задан) или
# `python3`. Тот же, что запускает awf, считает sha команды и пишет артефакт;
# иначе «чистый PATH без python3» ломал бы скрипт после успешного выбора awf.
pybin=${AWF_PYTHON:-python3}

# CLI: в дереве исходников — $root/bin/awf; в чужом проекте — `awf` на PATH
# (только если он реально запускается: битый entry point с
# ModuleNotFoundError не должен ронять скрипт) или установленный пакет через
# `$pybin -m awf`.
awf=""
if [ -x "$root/bin/awf" ]; then
  awf="$root/bin/awf"
elif command -v awf >/dev/null 2>&1 \
  && "$(command -v awf)" --help >/dev/null 2>&1; then
  awf=$(command -v awf)
fi
if [ -z "$awf" ]; then
  if "$pybin" -c 'import awf' >/dev/null 2>&1; then
    awf=""
  else
    echo "suite-canonical: awf CLI not found (tried \$root/bin/awf, a working 'awf' on PATH, '$pybin -m awf')" >&2
    exit 2
  fi
fi

run_awf() {
  if [ -n "$awf" ]; then
    "$awf" "$@"
  else
    "$pybin" -m awf "$@"
  fi
}

sha=$(run_awf tree-sha) || {
  echo "suite-canonical: cannot compute tree-sha (git repo with a commit required)" >&2
  exit 2
}

# Команда: `AWF_SUITE_CMD` побеждает (оболочка — единственное место
# переопределения); иначе — `awf suite-command`, единственная сборка
# канонической строки (config / легаси-фолбэк / «не настроен»). Код возврата
# резолвера: 0 — config, 1 — легаси, 2 — пустая/пробельная строка или null,
# 5 — битый config.yaml (отдельный код: «файл не читается» и «ключа нет» —
# разные состояния, K14).
if [ -n "${AWF_SUITE_CMD:-}" ]; then
  cmd=$AWF_SUITE_CMD
else
  cmd=$(run_awf suite-command)
  cfg_rc=$?
  if [ "$cfg_rc" -eq 5 ]; then
    echo "suite not configured: broken .agentic/config.yaml" >&2
    exit 2
  fi
  if [ "$cfg_rc" -ne 0 ] && [ "$cfg_rc" -ne 1 ]; then
    echo "suite not configured: set verification.test_cmd or AWF_SUITE_CMD" >&2
    exit 2
  fi
fi
cmd8=$("$pybin" -c 'import hashlib, sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:8])' "$cmd") || exit 2

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
  cached_exit=$("$pybin" - "$artifact" "$cmd" <<'PY'
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

# Раннер называет общий разбор awf (AUD-27-3): гейт, verify-pack и скрипт
# не держат три разных парсера. Разбор не отозвался (старый awf на PATH) —
# честное "unknown" с предупреждением: counts останутся null, и это видно,
# а не выглядит как «нулей не было». Причина остаётся в артефакте
# (`runner_source: cli-unavailable`) — иначе читатель гадает, почему counts
# пустые (CTX4).
runner_source=cli
if ! runner=$(run_awf suite-command --runner="$cmd" 2>/dev/null); then
  runner="unknown"
  runner_source="cli-unavailable"
  echo "suite-canonical: WARNING — cannot name the runner (awf suite-command --runner failed); counts stay null" >&2
fi

SUITE_SHA="$sha" \
SUITE_RUNNER="$runner" \
SUITE_RUNNER_SOURCE="$runner_source" \
SUITE_CMD="$cmd" \
SUITE_STARTED="$started_iso" \
SUITE_FINISHED="$finished_iso" \
SUITE_EXIT="$rc" \
SUITE_DURATION="$((end_s - start_s))" \
SUITE_LOG="$log" \
SUITE_ARTIFACT="$artifact" \
"$pybin" - <<'PY' || echo "suite-canonical: WARNING — could not write $artifact" >&2
import json
import os
import re
from pathlib import Path

text = Path(os.environ["SUITE_LOG"]).read_text(encoding="utf-8", errors="replace")


runner = os.environ.get("SUITE_RUNNER") or "unknown"
runner_source = os.environ.get("SUITE_RUNNER_SOURCE") or "cli"
_found = [False]


def count(pattern):
    m = re.search(pattern, text, re.IGNORECASE)
    if not m:
        return 0
    _found[0] = True
    return int(m.group(1))


# Known formats: pytest/Jest ("3 passed, 2 failed"), cargo ("test result:
# ok. 3 passed; 0 failed") and any runner that prints the same words. When
# NOTHING numeric is recognised the artifact carries counts: null and the
# runner name — zeros would claim "nothing failed, nothing ran" as a fact.
# go test prints no counts unless -v; that stays null, not a fake zero.
counts = None
if runner in ("pytest", "npm", "go", "cargo"):
    counts = {
        "passed": count(r"(\d+) passed"),
        "failed": count(r"(\d+) failed"),
        "errors": count(r"(\d+) errors?"),
        "skipped": count(r"(\d+) (?:skipped|ignored)"),
    }
    if not _found[0]:
        counts = None
tail = "\n".join(
    line for line in text.splitlines()[-20:] if line.strip()
)[-4000:]

if runner_source != "cli":
    counts_hint = (
        "runner unavailable: awf suite-command --runner failed, so no "
        "count format could be chosen"
    )
elif counts is None:
    counts_hint = (
        "no counts recognized in the output: the runner has no known count "
        "format or its output named no totals"
    )
else:
    counts_hint = ""

payload = {
    "sha": os.environ["SUITE_SHA"],
    "command": os.environ["SUITE_CMD"],
    "started": os.environ["SUITE_STARTED"],
    "finished": os.environ["SUITE_FINISHED"],
    "duration_seconds": int(os.environ["SUITE_DURATION"]),
    "exit": int(os.environ["SUITE_EXIT"]),
    "counts": counts,
    # Honest zeros are not facts — when nothing was recognised the artifact
    # says why: the runner has no known count format, or its output named no
    # totals, or the runner itself could not be named (runner_source).
    "counts_hint": counts_hint,
    "runner": runner,
    "runner_source": runner_source,
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
