# Контракт юнита (U3)

Необязательный машиночитаемый контракт по TODO: план-stage объявляет, что и
как проверять, исполнитель отчитывается машиночитаемыми фактами вместо прозы.
Всё необязательно — TODO без блока и DONE без json работают ровно как раньше.

## Блок в TODO (front-matter)

В самом верху TODO-файла, между двумя строками `---`. Строки-комментарии
`<!-- ... -->` (например role hint, который добавляет dispatch) до блока
пропускаются.

```yaml
---
verify: ["python3 -m pytest tests/unit/test_x.py -q"]
gates: ["contracts", "ratchet"]
prove_red: ["tests/unit/test_x.py::test_y"]
---
# TODO-0001 — задача

тело…
```

Ключи (все необязательны, но объявленный ключ обязан быть заполнен):

- `pipeline` — имя пайплайна для запуска в забеге (строка, не список).
  Когда `awf_run_next` запускает TODO без пайплайна на уровне очереди
  забега, он берёт имя отсюда (RUN3 #2). Имя — как у пайплайнов: буквы,
  цифры, `_`, `.`, `-`; пустое имя не принимается (пусто = пайплайн из
  config, ключ просто не пишут). `awf_dispatch_todo(..., pipeline=...)`
  пишет ключ в блок автоматически (блок создаёт, если его нет).
- `verify` — список непустых команд проверки.
- `gates` — список коротких имён проверок: канонический набор
  `KNOWN_GATES` из `awf/unit_contract.py`
  (`contracts`, `ratchet`, `instructions`, `tests`, `lint`, `mutations`).
  Это НЕ «имена проверок из run-all.sh»: `scripts/run-all.sh` гоняет
  `contracts`/`ratchet`/`instructions` под этими короткими именами, а
  pytest- и ruff-команды — строками из `scripts/project-commands.txt`
  (это и есть `tests`/`lint`); `mutations` в run-all не входит:
  базовый набор (4 мутации, `scripts/mutations.txt`) гоняется в CI test-job
  отдельным шагом `scripts/mutation-smoke.sh`, а `--report`-прогон
  (`scripts/mutations-neg5.txt`) остаётся ручным шагом. В verify-pack секция
  `gates` исполняет только первые три; `tests`/`lint` покрывают команды
  из `verify:`, а `mutations` — декларация для QA и для CI-базового набора.
- `prove_red` — список тест-идов, которые обязаны быть красными до фикса.
  `awf prove-red --todo <id>` проверяет это машинно (baseline в worktree:
  красный от ассерта + зелёный на текущем дереве; вердикты `red-ok` /
  `not-red` / `broken-runner` / `green-after`) — в отличие от `verify`,
  который просто прогоняет команды проверки на текущем дереве.

Валидация в `awf/api/dispatch.py::dispatch_todo` (один путь для MCP и CLI):

- блока нет — не ошибка;
- битый YAML, ключ не-списком, пустой список, пустая строка в списке,
  неизвестное имя gate — `AwfApiError` с текстом, что именно не так и как
  правильно;
- неизвестный ключ в блоке (опечатка) — не ошибка, но предупреждение в
  `pre_check_warnings` ответа dispatch.

## DONE.json

Исполнитель может (не обязан) писать `.agentic/outbox/DONE-<id>.json`:

```json
{
  "files_changed": ["awf/unit_contract.py", "tests/unit/test_unit_contract.py"],
  "tests_run": [
    {"cmd": "python3 -m pytest tests/unit/test_unit_contract.py -q", "result": "38 passed"}
  ],
  "gates": ["contracts", "ratchet"],
  "notes": "коротко, что важно для следующей роли",
  "stage": {"stage_id": "impl", "attempt": 1}
}
```

Схема (все ключи необязательны): `files_changed: [str]`,
`tests_run: [{cmd: str, result: str}]`, `gates: [str]`, `notes: str`,
`stage: {stage_id: str, attempt: int, role: str, model: str}` — блок
идентичности стадии, под-ключи внутри него тоже все необязательны
(ORCH M4.3). Старый файл без блока работает ровно как раньше.

`stage` — идентичность стадии. Worker получает её в промпте (строка
«Stage identity»: stage_id/attempt/role), так что блок — копия
данных, а не угадывание. Битый json или нарушение схемы (в том числе в
блоке `stage`) — предупреждение в `orchestrator.log`, файл пропускается,
пада нет.

Движок при сборке handoff (`awf/agent_stage.py::collect_handoff`) включает
валидный файл в секцию «Machine facts (DONE.json)» handoff-файла следующей
роли и в Run facts (`DONE-json=present`). Независимо от DONE.json handoff
всегда несёт секцию «Stage facts»: stage_id/attempt/role и model, если он
известен из конфига, — движок заполняет её сам, из стадии и счётчика
ретраев. Если в DONE.json есть блок `stage`, секция получает строку
сверки (matched/mismatched — пометка для следующей роли, не ошибка).

Отчёт на стадии verify: `.agentic/context/GATES-<todo>.md` (собирает
`awf verify-pack --todo <id>`, U5) — быстрые гейты, safety-канарейки,
минимальность диффа против baseline, команды из `verify:`, prove-red и
`ruff check .`; полный сьют туда не входит (он у QA). Тумблер
`automation.verify_pack` в `.agentic/config.yaml` (дефолт true); файл
архивируется в `done/<id>/DONE.json` вместе с остальными артефактами TODO.

## Переезды и удаления файлов

Переезд — обычным `mv`, удаление — `rm`; не `git mv` / `git add` /
`git rm`.

Почему: git-команды стейджат, а коммит-гейт R-03 считает
staged-против-baseline WIP владельца и исключает такие файлы из
коммита юнита. Переезд через git тихо выпадает из коммита (инцидент
8.1, волна 8.4: переезд трёх доков через `git mv` не попал в коммит
юнита; супервизор снял стейджинг, фиксация — отдельным юнитом).
