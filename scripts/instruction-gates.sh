#!/bin/sh
# instruction-gates.sh — проверки самих файлов инструкций для агента.
#
# Инструкции тоже дрейфуют: пухнут, обрастают именами проекта, ссылаются
# на коммиты, которых уже нет. Настройка — scripts/instruction-gates.conf
# (файл подключается как shell-скрипт; шаблон в папке скилла).
#
#   1. Бюджет слов (INSTR_WORD_LIMIT). Файлы, которые агент читает в каждой
#      сессии (INSTR_FILES, по умолчанию AGENTS.md + контракты), вместе не
#      длиннее лимита. Каждое правило стоит контекста в каждой сессии: лимит
#      заставляет удалять, а не только добавлять. 0 — проверка выключена.
#   2. Переносимое ядро (PROJECT_NAMES_FILE + PORTABLE_FILE). В переносимом
#      ядре нет имён проекта: его можно скопировать в другой репозиторий
#      без правок. Выключено, пока PROJECT_NAMES_FILE пуст.
#   3. Живые хэши (HASH_FILES). Каждая ссылка «commit <хэш>» существует
#      в истории. Ссылка на несуществующий коммит — уже не доказательство.
#   4. Бюджет доктрины (U9: DOCTRINE_DIR + DOCTRINE_WORD_LIMIT).
#      .agentic/doctrine/*.md попадают в промпт КАЖДОЙ роли — стоимость
#      платится каждой сессией. Пустой или отсутствующий каталог —
#      проверка не считается (обратная совместимость).
#
# Код возврата: 0 — ок, 1 — есть нарушения, 2 — ошибка конфигурации
# (в том числе «ни одна проверка не включена»).

set -u

CONF="${1:-scripts/instruction-gates.conf}"
[ -f "$CONF" ] || { echo "instruction-gates: нет $CONF" >&2; exit 2; }

INSTR_FILES="AGENTS.md docs/contracts/*.md"
INSTR_WORD_LIMIT=0
PORTABLE_FILE="AGENTS.md"
PROJECT_NAMES_FILE=""
HASH_FILES="docs/playbooks/*.md docs/contracts/*.md"
DOCTRINE_DIR=""
DOCTRINE_WORD_LIMIT=0
# shellcheck disable=SC1090
. "$CONF"

fail=0
checks=0

# 1. Бюджет слов
if [ "$INSTR_WORD_LIMIT" -gt 0 ]; then
  checks=$((checks + 1))
  total=0; nfiles=0; detail=''
  for f in $INSTR_FILES; do
    [ -f "$f" ] || continue
    nfiles=$((nfiles + 1))
    # awk, а не wc -w: в локали C wc не считает слова из кириллицы
    n=$(awk '{ n += NF } END { print n + 0 }' "$f")
    total=$((total + n))
    detail="$detail$f: $n слов
"
  done
  if [ "$nfiles" -eq 0 ]; then
    echo "instruction-gates: бюджет включён, но файлов из INSTR_FILES не найдено — проверять нечего" >&2
    exit 2
  fi
  if [ "$total" -gt "$INSTR_WORD_LIMIT" ]; then
    fail=1
    echo "✗ бюджет: инструкции занимают $total слов при лимите $INSTR_WORD_LIMIT."
    printf '%s' "$detail" | sed 's/^/    /'
    echo "  Удали устаревшее, потом добавляй."
  else
    echo "бюджет: $total из $INSTR_WORD_LIMIT слов, файлов: $nfiles"
  fi
fi

# 2. Переносимое ядро
if [ -n "$PROJECT_NAMES_FILE" ]; then
  checks=$((checks + 1))
  [ -f "$PROJECT_NAMES_FILE" ] || { echo "instruction-gates: нет $PROJECT_NAMES_FILE" >&2; exit 2; }
  [ -f "$PORTABLE_FILE" ] || { echo "instruction-gates: нет $PORTABLE_FILE" >&2; exit 2; }
  while IFS= read -r name || [ -n "$name" ]; do
    name=$(printf '%s' "$name" | tr -d '\r')
    case "$name" in ''|'#'*) continue ;; esac
    if hits=$(grep -niwF -e "$name" "$PORTABLE_FILE"); then
      fail=1
      echo "✗ ядро: в $PORTABLE_FILE встречается имя проекта «$name»:"
      printf '%s\n' "$hits" | head -5 | sed 's/^/    /'
    fi
  done < "$PROJECT_NAMES_FILE"
fi

# 3. Живые хэши
if [ -n "$HASH_FILES" ]; then
  checks=$((checks + 1))
  found_files=0; nhashes=0
  for f in $HASH_FILES; do
    [ -f "$f" ] || continue
    found_files=$((found_files + 1))
    hashes=$(grep -oiE 'commit[: ]+[0-9a-f]{7,40}' "$f" 2>/dev/null | grep -oE '[0-9a-f]{7,40}$' | sort -u || true)
    for h in $hashes; do
      nhashes=$((nhashes + 1))
      if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "instruction-gates: хэш $h в $f — не git-репозиторий, проверить нельзя" >&2
        exit 2
      fi
      if ! git cat-file -e "${h}^{commit}" 2>/dev/null; then
        fail=1
        echo "✗ хэш: $f ссылается на коммит $h, которого нет в истории"
      fi
    done
  done
  echo "хэши: ссылок $nhashes, файлов $found_files"
fi

# 4. Бюджет доктрины (U9)
if [ -n "$DOCTRINE_DIR" ] && [ "$DOCTRINE_WORD_LIMIT" -gt 0 ]; then
  total=0; nfiles=0; detail=''
  for f in "$DOCTRINE_DIR"/*.md; do
    [ -f "$f" ] || continue
    nfiles=$((nfiles + 1))
    # awk, а не wc -w: в локали C wc не считает слова из кириллицы
    n=$(awk '{ n += NF } END { print n + 0 }' "$f")
    total=$((total + n))
    detail="$detail$f: $n слов
"
  done
  # Пустой/отсутствующий каталог — доктрины нет, промпты не меняются:
  # проверку не считаем (обратная совместимость).
  if [ "$nfiles" -gt 0 ]; then
    checks=$((checks + 1))
    if [ "$total" -gt "$DOCTRINE_WORD_LIMIT" ]; then
      fail=1
      echo "✗ доктрина: $total слов при лимите $DOCTRINE_WORD_LIMIT. Урок платит каждая сессия."
      printf '%s' "$detail" | sed 's/^/    /'
      echo "  Один урок — одно место, кратко и с «почему»; лишнее удали."
    else
      echo "доктрина: $total из $DOCTRINE_WORD_LIMIT слов, файлов: $nfiles"
    fi
  fi
fi

if [ "$checks" -eq 0 ]; then
  echo "instruction-gates: ни одна проверка не включена — настрой $CONF" >&2
  exit 2
fi

if [ "$fail" -eq 0 ]; then echo "instruction-gates: ok"; fi
exit "$fail"
