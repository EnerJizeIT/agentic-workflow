<!-- Фикстура: дословный текст .agentic/done/TODO-0396/TODO.md (закрытый юнит A69); добавлена только эта строка. -->
<!-- role_hint: agent-implementer -->
---
files:
  - "awf/_env.py"
  - "awf/agent_stage.py"
  - "tests/unit/test_worker_env_pwd.py"
verify:
  - "/home/pklochkov/.local/share/awf-venv/bin/python -m pytest tests/unit/test_worker_env_pwd.py -q --timeout=300 -p no:cacheprovider"
  - "/home/pklochkov/.local/share/awf-venv/bin/ruff check ."
gates: ["tests", "lint"]
---
# TODO — A69: сессия агента живёт в проекте, а не в домашнем каталоге

## Факт (эксперимент 05.11)

Все стадии awf работают в каталоге `/home/pklochkov`:

- `session_v2.directory` у каждой стадии — `/home/pklochkov`, проект —
  `5d16c0e…` с worktree `/home/pklochkov`;
- первый вызов инструмента у большинства стадий — `pwd; ls -la` в домашнем
  каталоге, в IMP2b — `find / -maxdepth 3 -name agent_stage.py`;
- 90–100 % shell-команд начинаются с `cd "<проект>" &&`: 0386 — 43 из 43,
  0387 — 48 из 48, 0394 — 51 из 60 у QA и 52 из 55 у аудитора.

**Корень найден экспериментом:** opencode определяет каталог сессии по
переменной `PWD`, а движок передаёт унаследованный `PWD` родителя
(`/home/pklochkov`). Проверка:

    cd "<проект>" && PWD=/home/pklochkov opencode run … 
    → session_v2.directory = /home/pklochkov
    cd "<проект>" && opencode run …            (PWD = проект)
    → session_v2.directory = <проект>

## Что сделать

1. В env воркера (`awf/_env.py::awf_subprocess_env`, параметр
   `project_dir`) выставлять `PWD = str(project_dir)` и
   `OLDPWD = str(project_dir)`. Больше ничего в окружении не менять.
2. Если `project_dir` не передан — `PWD` не трогать (регресс: тесты,
   которые зовут без проекта).
3. `agent_stage` уже передаёт `project_dir` в `awf_subprocess_env` —
   проверить, что передаёт, и не дублировать логику в спавне.

## Приёмка (тесты в `tests/unit/test_worker_env_pwd.py`)

- `awf_subprocess_env(project_dir=tmp)` → `env["PWD"] == str(tmp)` и
  `env["OLDPWD"] == str(tmp)`.
- Без `project_dir` → `PWD` равен родительскому (не переписан).
- Путь с пробелами и кириллицей не ломает env.
- В спавне стадии (`agent_stage`) env, отданный в
  `run_subprocess_until_signal`, содержит `PWD == project_dir`
  (проверка на подменённом раннере, без реального opencode).
- `ruff` чисто.

## Ручная проверка (в DONE, фактом)

После коммита движок один раз запускает короткую стадию; в
`session_v2.directory` у новой сессии — путь проекта, а не `$HOME`.
Если это нельзя проверить в этом юните — записать в DONE как
«проверить на следующем юните» с точной командой.

## Границы

- Никаких других переменных не добавлять (`HOME`, `TMPDIR` не трогать).
- `PWD` — единственный ключ правды о каталоге; второй путь («передать
  каталог аргументом CLI») не заводим.

## Чек-лист завершения

- [ ] base-todo-file: Юнит задиспатчен — его TODO лежит в inbox — probe(file_exists [".agentic/inbox/{todo_id}.md"]) expect present
- [ ] base-baseline-sha: Бейзлайн снят до работы — есть SHA для prove-red и диффа — probe(file_exists [".agentic/context/BASELINE-{todo_id}.sha"]) expect present
- [ ] base-agentic-config: Конфиг проекта на месте — probe(file_exists [".agentic/config.yaml"]) expect present
- [ ] base-context-dir: Каталог артефактов контекста на месте — probe(file_exists [".agentic/context"]) expect present
- [ ] base-inbox-dir: Каталог входящих юнитов на месте — probe(file_exists [".agentic/inbox"]) expect present
- [ ] base-git-repo: Проект — рабочий git-репозиторий (дифф и коммит возможны) — probe(command_rc ["git", "status", "--porcelain"]) expect rc=0
