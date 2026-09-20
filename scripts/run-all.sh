#!/usr/bin/env bash
# run-all.sh — единый прогон ворот качества проекта.
#
# Запускает всё, у чего есть конфигурация:
#   docs/contracts/                + check-contracts.sh    — контракты и их Gate:
#   .ratchet-counters              + ratchet.sh            — счётчики долга
#   scripts/instruction-gates.conf + instruction-gates.sh  — бюджет инструкций
#   scripts/project-commands.txt   — команды проекта: тесты, линт, сборка
#
# Не останавливается на первом падении: видно все проблемы за один прогон.
# Команды проекта идут с pipefail: в конвейере «pytest | tail» код возврата
# берётся от последней команды, и падение тестов без pipefail скрылось бы.
#
# proven-red.sh и mutation-smoke.sh сюда не входят: первый запускается в
# рабочем цикле (до реализации), второй медленный — ему место в CI отдельным
# шагом.
#
# Код возврата: 0 — всё зелёное, 1 — что-то упало, 2 — нечего запускать.

# Скрипт на bash, но его могут позвать через sh — как остальные ворота.
# Перезапускаемся в bash, чтобы не падать с невнятным «Syntax error».
if [ -z "${BASH_VERSION:-}" ]; then
  command -v bash > /dev/null 2>&1 || { echo "run-all: нужен bash" >&2; exit 2; }
  exec bash "$0" "$@"
fi

set -u

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2

# корень проекта: git-овский, иначе — на уровень выше самого скрипта
if root=$(git rev-parse --show-toplevel 2>/dev/null); then
  cd "$root" || exit 2
else
  cd "$script_dir/.." || exit 2
fi

failed=()
ran=0

run() {
  name="$1"; shift
  echo "── $name"
  ran=$((ran + 1))
  if "$@"; then :; else failed+=("$name"); fi
}

[ -d docs/contracts ] && run "contracts" sh "$script_dir/check-contracts.sh"
[ -f .ratchet-counters ] && run "ratchet" sh "$script_dir/ratchet.sh"

if [ -f scripts/instruction-gates.conf ]; then
  run "instructions" sh "$script_dir/instruction-gates.sh"
fi

if [ -f scripts/project-commands.txt ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    trimmed="${line#"${line%%[![:space:]]*}"}"
    case "$trimmed" in ''|'#'*) continue ;; esac
    run "$trimmed" bash -o pipefail -c "$trimmed"
  done < scripts/project-commands.txt
fi

echo
if [ "$ran" -eq 0 ]; then
  echo "gates: нечего запускать — нет docs/contracts/, .ratchet-counters и настроенных проверок" >&2
  echo "gates: пустой прогон не считается зелёным" >&2
  exit 2
fi
if [ "${#failed[@]}" -eq 0 ]; then
  echo "gates: всё зелёное ($ran проверок)"
  exit 0
fi
echo "gates: упало ${#failed[@]} из $ran:"
printf '  - %s\n' "${failed[@]}"
exit 1
