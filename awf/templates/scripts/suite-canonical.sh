#!/usr/bin/env bash
# suite-canonical.sh — один канонический прогон полного сьюта на состояние дерева.
#
# Зачем: роли гоняли полный pytest много раз за один attempt (R2 — около 66
# прогонов, доминирующая цена витка). Здесь прогон привязан к tree-sha:
# повторный вызов на том же дереве с той же командой возвращает готовый
# артефакт и НЕ запускает тесты заново. Перезапуск — после изменения дерева
# или смены `AWF_SUITE_CMD` (другая команда — другой `cmd8`); готовый артефакт
# на том же дереве `--force` не обходит — правила ниже (A75).
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
#   scripts/suite-canonical.sh --wait [--timeout SECONDS] [--project DIR]
#
# A57 (TODO-0380): `--wait` блокируется до конца прогона (опрос раз в 10 с,
# переопределяется `AWF_SUITE_WAIT_INTERVAL`), печатает итог как `--status` и
# возвращает код прогона. Прогон не запущен — `--wait` стартует его сам.
# Таймаут ожидания (`--timeout`, по умолчанию 1800 с) — отдельный код 124.
#
# A75 (TODO-0399): один полный прогон на дерево и на пайплайн. Владение —
# `flock` на `<project>/.agentic/context/SUITE.lock` (не pid: pid может
# переиспользоваться), внутри — pid, tree-sha, команда и время старта.
# Замок держится весь прогон, включая отсоединённый: дочерний процесс
# наследует его файловый дескриптор. В саму команду сьюта дескриптор не
# передаётся (`9>&-`): оставленный тестом демон не должен держать замок после
# конца прогона. Живость прогона на всех поверхностях решает замок, не pid.
# Занятый живым прогоном замок:
# `--background` → 3; `--wait` ждёт освобождения и продолжает; `--force`
# (в том числе с `--wait`) → 2 с pid владельца. Замок мёртвого процесса
# ядро снимает само.
#
# `--force` больше не обходит готовый артефакт: если на текущий tree-sha
# есть `SUITE-<sha>-*.json` с `exit`, ответ — 2 с путём артефакта. Обход —
# изменить дерево или удалить артефакт.
#
# `runs` в артефакте и в `--status` — сколько раз ЭТА команда стартовала на
# этот tree-sha (счётчик `<context>/SUITE-<sha>-<cmd8>.runs`): узкий прогон
# через `AWF_SUITE_CMD` полным не считается и счётчик канонической команды не
# двигает. Нужно, чтобы вердикт видел цифру, а не верил на слово.
#
# Код возврата: код сьюта (0 — зелёный). 2 — не удалось посчитать tree-sha,
# неверные аргументы, пустой verification.test_cmd или битый config.yaml
# («сьют не настроен»; битый файл называется отдельным текстом), `--force`
# при живом прогоне или при готовом артефакте на это дерево. 3 — идёт
# (только `--background`/`--status`), 4 — не запущен (`--status`),
# 124 — истёк `--timeout` у `--wait`.
set -u

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2
root=$(CDPATH= cd -- "$script_dir/.." && pwd) || exit 2

project=$(pwd)
force=0
# TODO-0316 (incident 31.10): the full suite outlives the worker's command
# budget, so the run must be startable detached and polled. ``run`` is the
# historical foreground mode (tests, CI, humans). A57 adds ``wait``: the
# worker blocks in one call instead of blind ``sleep`` + ``--status`` polls.
mode=run
wait_timeout=1800
# Интервал опроса: дроби (`0.2`) нужны тестам, но «.» или «1.2.3» превратили
# бы ожидание в холостой цикл с ошибкой `sleep` на каждой итерации.
wait_interval=${AWF_SUITE_WAIT_INTERVAL:-10}
case "$wait_interval" in
  ''|.|*[!0-9.]*|*.*.*) wait_interval=10 ;;
esac

usage() {
  echo "usage: suite-canonical.sh [--force] [--background|--status] [--project DIR]" >&2
  echo "       suite-canonical.sh --wait [--timeout SECONDS] [--force] [--project DIR]" >&2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --force) force=1 ;;
    --background) mode=background ;;
    --status) mode=status ;;
    --wait) mode=wait ;;
    --timeout)
      shift
      [ "$#" -gt 0 ] || { echo "suite-canonical: --timeout needs seconds" >&2; usage; exit 2; }
      # Только положительное целое: 0 дал бы мгновенный 124 на пустом месте и
      # читался бы как «сьют не уложился».
      timeout_arg=""
      case "$1" in
        ''|*[!0-9]*) : ;;
        *[1-9]*) timeout_arg=$1 ;;
      esac
      if [ -z "$timeout_arg" ]; then
        echo "suite-canonical: --timeout needs a positive integer, got: $1" >&2
        usage
        exit 2
      fi
      wait_timeout=$timeout_arg
      ;;
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

# A75: замок один на проект, не на артефакт: полный прогон — один на дерево,
# даже если команда прогона названа иначе. `runs` считает старты этой команды
# на этом sha: у узкого прогона свой счётчик, иначе `runs` в каноническом
# артефакте врал бы про «два полных прогона» после одного узкого.
if ! command -v flock >/dev/null 2>&1; then
  echo "suite-canonical: flock is required (one run per tree is enforced by a file lock)" >&2
  exit 2
fi
lock_file="$context/SUITE.lock"
runs_file="$context/SUITE-$sha-$cmd8.runs"

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

# pid-файл — подсказка человеку и тестам (кого убивать), а не источник истины:
# «идёт / не идёт» решает только замок (A75), pid переиспользуется.
pid_file="$artifact.pid"
run_log="$artifact.log"

# Exit из готового артефакта: $1 — файл, $2 — нужная команда (пусто — любая).
# Печатает код и возвращает 0 только для ПОЛНОЙ записи: файл есть, JSON
# читается, `exit` — целое. Один разбор JSON на всех читателей (кэш,
# «прогон на дерево уже есть») — то же правило, что у
# `awf/suite_guard.artifact_for`, чтобы две поверхности не отвечали по-разному.
artifact_exit_of() {
  local out
  [ -f "$1" ] || return 1
  out=$("$pybin" - "$1" "${2:-}" <<'PYCODE'
import json
import sys

path, wanted = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path, encoding="utf-8"))
    if (not wanted or data.get("command") == wanted) and isinstance(
        data.get("exit"), int
    ):
        print(data["exit"])
except Exception:
    pass
PYCODE
) || return 1
  [ -n "$out" ] || return 1
  echo "$out"
}

# Артефакт ТЕКУЩЕЙ команды — кэш ответа. «Прогон на это дерево уже был»
# спрашивает complete_artifact ниже: там команда не важна.
artifact_exit() {
  artifact_exit_of "$artifact" "$cmd"
}

# ── Замок одного прогона (A75) ────────────────────────────────────────────
# Файл замка — источник истины, а не pid внутри него: flock снимается ядром
# вместе с владельцем, переиспользованный pid выглядел бы живым. Содержимое
# (pid, tree-sha, cmd, started) нужно только для сообщения человеку.
lock_held=0

lock_try() {  # 0 — взят, 1 — занят живым прогоном, 2 — файл не открыть
  # Без `2>/dev/null`: перенаправление stderr у `exec` без команды остаётся
  # в оболочке навсегда и глушит сообщения об отказе.
  exec 9>>"$lock_file" || return 2
  if flock -n 9 2>/dev/null; then
    lock_held=1
    return 0
  fi
  exec 9>&-
  return 1
}

lock_hold() {  # ждёт освобождения замка; 0 — взят, 2 — файл не открыть
  exec 9>>"$lock_file" || return 2
  flock 9 2>/dev/null || { exec 9>&-; return 2; }
  lock_held=1
  return 0
}

lock_release() {
  exec 9>&-
  lock_held=0
}

# Отказ «файл замка не открыть» — одно место: текст одинаков во всех режимах.
lock_open_die() {
  echo "suite-canonical: cannot open the run lock: $lock_file" >&2
  exit 2
}

# Взять замок или выйти 2: политика `--force` — живой прогон не ждём и второй
# не стартуем.
lock_force_acquire() {
  local rc=0 owner
  lock_try || rc=$?
  [ "$rc" -eq 0 ] && return 0
  [ "$rc" -eq 2 ] && lock_open_die
  owner=$(lock_pid || true)
  echo "suite-canonical: полный прогон уже идёт, pid ${owner:-?}" >&2
  exit 2
}

# Дождаться замка; неоткрываемый файл — та же ошибка 2.
lock_hold_die() {
  lock_hold || lock_open_die
}

lock_write() {  # $1 — pid владельца
  "$pybin" - "$lock_file" "$1" "$sha" "$cmd" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" <<'PY'
import json
import sys

path, pid, sha, cmd, started = sys.argv[1:6]
with open(path, "w", encoding="utf-8") as fh:
    json.dump(
        {"pid": int(pid), "tree_sha": sha, "cmd": cmd, "started": started},
        fh,
        ensure_ascii=False,
    )
    fh.write("\n")
PY
}

lock_pid() {  # pid владельца из содержимого; нет/битый файл — пусто
  [ -f "$lock_file" ] || return 1
  "$pybin" - "$lock_file" <<'PY'
import json
import sys

try:
    pid = json.load(open(sys.argv[1], encoding="utf-8")).get("pid")
    if isinstance(pid, int) and pid > 0:
        print(pid)
except Exception:
    pass
PY
}

# Артефакт на этот tree-sha (ЛЮБАЯ команда) с непустым exit — прогон на это
# дерево уже был. Команда не проверяется намеренно: такой прогон — тоже
# потраченный старт на дереве, и второй `--force` его не отменяет.
complete_artifact() {
  local candidate
  for candidate in "$context"/SUITE-"$sha"-*.json; do
    if artifact_exit_of "$candidate" >/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

# Готовый прогон на это дерево — отказ 2: путь артефакта и один обход
# (изменить дерево или удалить артефакт). Текст один на все режимы.
refuse_complete_artifact() {
  local done_path
  if done_path=$(complete_artifact); then
    echo "suite-canonical: полный прогон на это дерево уже есть: $done_path; перезапуск не нужен, дерево не менялось" >&2
    exit 2
  fi
}

read_runs() {  # стартов этой команды на этот tree-sha; старый артефакт = 1
  local n
  n=$(cat "$runs_file" 2>/dev/null || true)
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
  if [ "$n" -eq 0 ] && [ -f "$artifact" ]; then
    n=$("$pybin" - "$artifact" <<'PY'
import json
import sys

try:
    runs = json.load(open(sys.argv[1], encoding="utf-8")).get("runs")
    print(runs if isinstance(runs, int) and runs > 0 else 1)
except Exception:
    print(1)
PY
)
  fi
  echo "$n"
}

bump_runs() {  # +1 под замком; печатает новое значение
  local n
  n=$(read_runs)
  n=$((n + 1))
  printf '%s\n' "$n" > "$runs_file" 2>/dev/null || true
  echo "$n"
}

# Один старт отсоединённого прогона: child снимает маркер стадии (A44) и не
# принадлежит стадии, которая его запустила. Печатает pid — вызывающий пишет
# его в pid-файл сам, чтобы «уже идёт» было видно следующему вызову.
# Замок передаётся наследованием (fd 9): parent выходит, а дочерний прогон
# держит файл до конца — второй старт на том же дереве не пройдёт.
start_detached() {
  local child_force=""
  [ "$force" -eq 1 ] && child_force="--force"
  AWF_SUITE_LOCK_ACQUIRED=1 \
    env -u AWF_STAGE_ANCHOR setsid nohup "$0" $child_force --project "$project" \
    >"$run_log" 2>&1 </dev/null &
  echo "$!"
}

# Итог прогона одинаков во всех режимах: текст, артефакт целиком, код прогона.
print_and_exit() {
  echo "$1"
  cat "$artifact"
  exit "$2"
}

# Ожидание `--wait`: секунды с начала, отказ 124 и итог «done». Текст и
# арифметика — в одном месте: у `--wait` два цикла (ждём чужой замок, ждём
# свой артефакт), и разъехавшиеся формулировки читались бы как разные исходы.
wait_elapsed() {
  echo $(( $(date +%s) - wait_start_s ))
}

wait_timeout_die() {
  echo "suite-canonical: wait timeout after $(wait_elapsed)s (tree-sha $sha); the run may still be going" >&2
  exit 124
}

wait_done() {  # $1 — код прогона
  print_and_exit "suite-canonical: done (tree-sha $sha) after $(wait_elapsed)s — $artifact" "$1"
}

# TODO-0316: status mode answers without touching the suite — a worker polls
# it between short commands instead of blocking on a 12-minute run.
if [ "$mode" = status ]; then
  runs=$(read_runs)
  if rc=$(artifact_exit); then
    print_and_exit "suite-canonical: done (tree-sha $sha, runs $runs) — $artifact" "$rc"
  fi
  lock_rc=0
  lock_try || lock_rc=$?
  if [ "$lock_rc" -eq 1 ]; then
    pid=$(lock_pid || true)
    echo "suite-canonical: running (pid ${pid:-?}, tree-sha $sha, runs $runs); poll again"
    exit 3
  fi
  if [ "$lock_rc" -ne 0 ]; then
    lock_open_die
  fi
  lock_release
  echo "suite-canonical: not started (tree-sha $sha, runs $runs) — run --background"
  exit 4
fi

if [ "$mode" = background ]; then
  if [ "$force" -ne 1 ] && rc=$(artifact_exit); then
    print_and_exit "suite-canonical: cached (tree-sha $sha, same command) — $artifact" "$rc"
  fi
  if [ "$force" -eq 1 ]; then
    lock_force_acquire
  else
    lock_rc=0
    lock_try || lock_rc=$?
    if [ "$lock_rc" -eq 1 ]; then
      pid=$(lock_pid || true)
      echo "suite-canonical: already running (pid ${pid:-?}, tree-sha $sha)"
      exit 3
    fi
    if [ "$lock_rc" -ne 0 ]; then
      lock_open_die
    fi
  fi
  # A75: --force на дерево, где прогон уже был, — отказ, а не дубль.
  if [ "$force" -eq 1 ]; then
    refuse_complete_artifact
  fi
  pid=$(start_detached)
  echo "$pid" > "$pid_file"
  lock_write "$pid"
  echo "suite-canonical: started (pid $pid, tree-sha $sha); follow with --wait"
  exit 3
fi

# A57 (TODO-0380): one blocking call instead of repeated blind ``sleep``.
# A cached artifact returns immediately (same cache rule as ``--background``);
# no run in flight — start it detached and wait for the artifact. The exit
# code is the suite's own; 124 is the wait timeout, never a suite verdict.
if [ "$mode" = wait ]; then
  # A75: `--force` на неизменном дереве с готовым артефактом — отказ, а не
  # дубль. Без `--force` готовый артефакт отвечает сразу (та же кэш-правило).
  if [ "$force" -eq 1 ]; then
    refuse_complete_artifact
    # Перезапуск поверх живого прогона — тоже отказ: иначе `--force`
    # останется вторым способом получить два прогона на дереве.
    lock_force_acquire
  elif rc=$(artifact_exit); then
    print_and_exit "suite-canonical: cached (tree-sha $sha, same command) — $artifact" "$rc"
  fi
  wait_start_s=$(date +%s)

  # Замок решает, стартовать прогон или ждать чужой: pid-файл этого не
  # гарантирует — pid переиспользуется.
  if [ "$lock_held" -ne 1 ]; then
    lock_rc=0
    lock_try || lock_rc=$?
    if [ "$lock_rc" -eq 2 ]; then
      lock_open_die
    fi
    if [ "$lock_rc" -eq 1 ]; then
      pid=$(lock_pid || true)
      echo "suite-canonical: already running (pid ${pid:-?}, tree-sha $sha); waiting"
      while :; do
        lock_rc=0
        lock_try || lock_rc=$?
        [ "$lock_rc" -eq 0 ] && break
        if [ "$lock_rc" -ne 1 ]; then
          lock_open_die
        fi
        if [ "$(wait_elapsed)" -ge "$wait_timeout" ]; then
          wait_timeout_die
        fi
        sleep "$wait_interval"
      done
      # Замок свободен: владелец успел дописать артефакт или умер, не дописав.
      if rc=$(artifact_exit); then
        wait_done "$rc"
      fi
    fi
  fi
  # Замок у нас, артефакта на текущую команду нет — стартуем и ждём.
  pid=$(start_detached)
  echo "$pid" > "$pid_file"
  lock_write "$pid"
  lock_release
  echo "suite-canonical: started (pid $pid, tree-sha $sha); waiting"
  while :; do
    if rc=$(artifact_exit); then
      wait_done "$rc"
    fi
    if [ "$(wait_elapsed)" -ge "$wait_timeout" ]; then
      wait_timeout_die
    fi
    # Живость прогона решает замок, а не pid-файл: pid переиспользуется, и
    # чужой процесс с тем же pid держал бы ожидание до таймаута. Дочерний
    # прогон держит замок от fork до выхода (наследованный fd 9).
    lock_rc=0
    lock_try || lock_rc=$?
    if [ "$lock_rc" -eq 2 ]; then
      lock_open_die
    fi
    if [ "$lock_rc" -eq 1 ]; then
      sleep "$wait_interval"
      continue
    fi
    # Замок свободен: прогон закончился. Артефакт мог лечь между двумя
    # проверками — поэтому решение принимает повторное чтение артефакта.
    if rc=$(artifact_exit); then
      wait_done "$rc"
    fi
    echo "suite-canonical: run died without an artifact (tree-sha $sha)" >&2
    exit 2
  done
fi

# A75: замок держится от старта до конца прогона. Дочерний процесс получает
# его наследованием (fd 9) и заново не берёт — новая копия fd отпустила бы
# родительскую блокировку и открыла окно второму прогону.
if [ "$lock_held" -ne 1 ] && [ "${AWF_SUITE_LOCK_ACQUIRED:-}" = "1" ] \
  && flock -n 9 2>/dev/null; then
  lock_held=1
fi
# Наследованный замок уже наш. Переменная не должна уехать во вложенный
# вызов: там она выдала бы чужой замок за собственный, и второй прогон на
# дереве стал бы возможен.
unset AWF_SUITE_LOCK_ACQUIRED

if [ "$force" -ne 1 ]; then
  if [ -f "$artifact" ]; then
    # The cache is keyed by the tree AND by the command: the same tree with a
    # different AWF_SUITE_CMD must not serve the other command's result as
    # canonical (the artifact stores "command" for exactly this check).
    if cached_exit=$(artifact_exit); then
      echo "suite-canonical: cached (tree-sha $sha, same command) — $artifact; delete it or change the tree to rerun"
      cat "$artifact"
      exit "$cached_exit"
    fi
    echo "suite-canonical: command changed or the artifact is incomplete — rerunning (tree-sha $sha)"
  fi
  # A75: ждём чужой прогон, а не стартуем второй. Дождавшись, сначала
  # смотрим его артефакт: второй прогон на том же дереве не нужен.
  if [ "$lock_held" -ne 1 ]; then
    lock_hold_die
  fi
  if rc=$(artifact_exit); then
    print_and_exit "suite-canonical: cached (tree-sha $sha, same command) — $artifact" "$rc"
  fi
else
  # A75: --force не обходит ни живой прогон, ни готовый артефакт.
  if [ "$lock_held" -ne 1 ]; then
    lock_force_acquire
  fi
  refuse_complete_artifact
fi

# Здесь замок уже наш на всех исходах выше: не-`--force` дождался
# (lock_hold_die), `--force` взял или вышел 2. Догоняющей попытки не нужно.
runs=$(bump_runs)
lock_write "$$"

log=$(mktemp "${TMPDIR:-/tmp}/awf-suite.XXXXXX") || exit 2
echo "$$" > "$pid_file"
trap 'rm -f "$log" "$pid_file"' EXIT

started_iso=$(date -u +%Y-%m-%dT%H:%M:%SZ)
start_s=$(date +%s)

# Полезная нагрузка замок не наследует: `9>&-` закрывает fd 9 только для
# сьюта. Иначе демон, оставленный тестом, держал бы замок после конца прогона,
# и следующий вызов читал бы «идёт» у давно мёртвого прогона. Замок держит
# этот процесс, а не его потомки.
bash -o pipefail -c "$cmd" 9>&- >"$log" 2>&1
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
SUITE_RUNS="$runs" \
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
    # A75: сколько раз прогон стартовал на этот tree-sha — вердикт видит
    # цифру, а не верит «один прогон» на слово.
    "runs": int(os.environ["SUITE_RUNS"]),
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

echo "suite-canonical: exit $rc (tree-sha $sha, runs $runs) — $artifact"

# AUD5: the artifact keeps 20 lines for the counts; the reason a run failed
# can sit far above them. On a red run the longer tail goes next to it.
if [ "$rc" -ne 0 ]; then
  err_file="$artifact.err"
  if tail -n 120 "$log" >"$err_file" 2>/dev/null; then
    echo "suite-canonical: failure tail — $err_file" >&2
  else
    echo "suite-canonical: WARNING — could not write $err_file" >&2
  fi
fi

tail -n 20 "$log"
exit "$rc"
