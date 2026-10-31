# awf на opencode 2.x: как устроено

awf 3 работает только на opencode ≥ 2. Этот документ описывает контракт
между awf и opencode так, как он есть в коде; истории перехода здесь нет.

## Запуск воркера и супервизора

Оба спавна собирают argv одним помощником (`awf/supervisor.py::argv_agent_flag`
для флага агента, `awf/opencode_version.py::apply_standalone` для изоляции):

```
opencode run <--standalone> --auto --print-logs [--agent <name>] --title … --file … [--model p/m] -- <prompt>
```

- `--standalone` передаётся всегда: без него `opencode run` — клиент к общему
  фоновому серверу, и kill оставляет сессию на нём. Исключение — managed
  (`opencode.managed`), где политикой владеет оператор.
- `--agent` передаётся только когда роль отображена на агента
  (`models.<role>.agent_name`). Иначе запуск идёт на дефолтном агенте —
  opencode 2.x отвергает выдуманное имя (`Agent not found`).
- Версия проверяется до первого побочного эффекта любого входа:
  major < 2 — отказ «awf 3 требует opencode 2.x»; версия не определена —
  отдельный отказ (fail-closed); нет бинаря — «opencode не найден».

## База

Единственная читаемая схема — 2.x: `session_v2` + `session_message`
(данные в JSON `session_message.data`, таблицы `part` нет). Признак схемы —
именно `session_v2`. Схема 1.x, пустая база или база без таблиц дают `n/a`
с причиной; история 1.x не читается и в отчёты не попадает.

Захват сессии пишет id только из `session_v2`; на v1-базе молча не пишет.

## Конфиг

Читается и пишется только форма 2.x:

| Что | Ключ |
|---|---|
| провайдер и baseURL | `providers.<p>.settings.baseURL` |
| агенты | `agents.<name>` |
| MCP-серверы | `mcp.servers.<name>` |
| разрешения | `permissions` (упорядоченный список правил) |

`OPENCODE_CONFIG_CONTENT` для воркера собирается в этой же форме; правила
стадии и `locked_deny` идут последними, поэтому сильнее хостовых.
Файл с ключами 1.x (`provider`, `agent`, `permission`, `mcp.<имя>`) awf не
переписывает: `awf doctor` показывает WARN «сконвертируй в V2», файл
остаётся байт-в-байт.

## Сеть и маркеры

Классификация сетевых смертей знает только строки 2.x (`ECONNREFUSED`,
`ECONNRESET`, `ETIMEDOUT`, `Unable to connect`) плюс версионно-нейтральный
`_NETWORK_RE` (connect/reset/timeout). Словесные формулировки 1.x удалены:
`Cannot connect to API` и `Connection reset` ловит регулярка, `fetch failed`
не классифицируется намеренно.

## Проверки

- `awf doctor` — версия и `--standalone`, формат конфига, роли без
  `agent_name`, мёртвые ключи конфига awf, socks-прокси в окружении.
- `tests/unit/test_opencode2_only_guard.py` — страж: код 1.x (чтение
  `session`/`part`/`message`, форма `options.baseURL`, `--attach`, `--port`,
  `opencode.standalone`) не должен вернуться в `awf/`.
