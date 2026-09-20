#!/usr/bin/env bash
# mutation-smoke.sh — ломает критичный модуль одной правкой и ждёт, что тесты покраснеют.
#
# Proven red доказывает, что НОВЫЕ тесты умеют падать. Мутационный smoke
# доказывает то же про СТАРЫЕ: если после порчи модуля тесты остались
# зелёными, модуль они не защищают.
#
#   bash mutation-smoke.sh [файл-мутаций]   (по умолчанию scripts/mutations.txt)
#
# Формат строки (одна мутация на строку):
#   ФАЙЛ @@ ЧТО_ЗАМЕНИТЬ @@ НА_ЧТО @@ КОМАНДА_ТЕСТОВ
# Замена буквальная, меняется первое вхождение:
#   src/auth/token.py @@ if expired: @@ if not expired: @@ python3 -m pytest tests/test_token.py -q
#
# Если ЧТО_ЗАМЕНИТЬ в файле не нашлось — мутация протухла вместе с кодом,
# это ошибка конфигурации (exit 2), а не молчаливый пропуск.
#
# Скрипт временно правит рабочее дерево и возвращает файлы обратно по trap.
# Запускай его на чистом дереве — лучше в CI отдельным шагом, там нет
# незакоммиченных правок, с которыми мутации могут конфликтовать.
# Против кэшей раннера (байткод Python сверяет время изменения и размер)
# скрипт сдвигает время файла вперёд и отключает запись байткода.
#
# Код возврата: 0 — все мутации убиты, 1 — какая-то выжила, 2 — ошибка
# конфигурации (в том числе пустой список мутаций).

# Скрипт на bash, но его могут позвать через sh — как остальные ворота.
# Перезапускаемся в bash, чтобы не падать с невнятным «Syntax error».
if [ -z "${BASH_VERSION:-}" ]; then
  command -v bash > /dev/null 2>&1 || { echo "mutation-smoke: нужен bash" >&2; exit 2; }
  exec bash "$0" "$@"
fi

set -u

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2
if root=$(git rev-parse --show-toplevel 2>/dev/null); then
  cd "$root" || exit 2
else
  cd "$script_dir/.." || exit 2
fi

FILE_LIST="${1:-scripts/mutations.txt}"
[ -f "$FILE_LIST" ] || { echo "mutation-smoke: нет файла мутаций $FILE_LIST" >&2; exit 2; }

if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
  echo "mutation-smoke: предупреждение: в дереве есть незакоммиченные правки — мутации" >&2
  echo "mutation-smoke: могут конфликтовать с ними. Лучше чистого дерева или CI." >&2
fi

backup=""; target=""
restore() { if [ -n "$backup" ] && [ -f "$backup" ]; then cp -p "$backup" "$target"; rm -f "$backup"; fi; }
trap restore 0 1 2 3 15

survived=0; total=0; lineno=0
while IFS= read -r line || [ -n "$line" ]; do
  lineno=$((lineno + 1))
  trimmed="${line#"${line%%[![:space:]]*}"}"
  case "$trimmed" in ''|'#'*) continue ;; esac
  if [[ "$line" != *" @@ "*" @@ "*" @@ "* ]]; then
    echo "mutation-smoke: $FILE_LIST:$lineno — нужно четыре поля через ' @@ '" >&2; exit 2
  fi
  target="${line%% @@ *}"; rest="${line#* @@ }"
  find="${rest%% @@ *}";   rest="${rest#* @@ }"
  repl="${rest%% @@ *}";   cmd="${rest#* @@ }"

  [ -f "$target" ] || { echo "mutation-smoke: нет файла $target ($FILE_LIST:$lineno)" >&2; exit 2; }
  IFS= read -r -d '' content < "$target" || true
  if [[ "$content" != *"$find"* ]]; then
    echo "mutation-smoke: в $target нет строки «$find» — мутация устарела ($FILE_LIST:$lineno)" >&2
    exit 2
  fi

  backup="$(mktemp)"; cp -p "$target" "$backup"
  printf '%s' "${content/"$find"/"$repl"}" > "$target"
  # Кэш, сверяющий время изменения и размер файла (байткод Python), не должен
  # принять мутировавший файл за исходный: сдвигаем mtime вперёд.
  # cp -p при восстановлении вернёт исходное время.
  touch -d "@$(( $(date +%s) + 5 ))" "$target" 2> /dev/null || true

  total=$((total + 1))
  if PYTHONDONTWRITEBYTECODE=1 bash -c "$cmd" > /dev/null 2>&1; then
    survived=1
    echo "✗ выжила: $target — «$find» → «$repl»; тесты остались зелёными"
  else
    echo "✓ убита: $target — «$find» → «$repl»"
  fi
  restore; backup=""
done < "$FILE_LIST"

if [ "$total" -eq 0 ]; then
  echo "mutation-smoke: в $FILE_LIST нет мутаций — проверять нечего, это не зелёный результат" >&2
  exit 2
fi

if [ "$survived" -eq 0 ]; then echo "mutation-smoke: ok (мутаций: $total)"; fi
exit "$survived"
