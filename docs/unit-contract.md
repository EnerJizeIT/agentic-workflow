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

- `verify` — список непустых команд проверки.
- `gates` — список имён проверок из `scripts/run-all.sh`:
  `contracts`, `ratchet`, `instructions`, `tests`, `lint`, `mutations`.
- `prove_red` — список тест-идов, которые обязаны быть красными до фикса.

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
  "notes": "коротко, что важно для следующей роли"
}
```

Схема (все ключи необязательны): `files_changed: [str]`,
`tests_run: [{cmd: str, result: str}]`, `gates: [str]`, `notes: str`.

Движок при сборке handoff (`awf/agent_stage.py::collect_handoff`) включает
валидный файл в секцию «Machine facts (DONE.json)» handoff-файла следующей
роли и в Run facts (`DONE-json=present`). Битый json или нарушение схемы —
предупреждение в `orchestrator.log`, handoff собирается без секции, пада
нет.
