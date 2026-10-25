# opencode 2.*: совместимость awf и план миграции

Дата: 2026-10-24. Статус: **запланировано**, не выполнено. Источник задачи — BACKLOG.md,
раздел «opencode 2.* — миграция».

## 1. Зачем

opencode выпустил ветку 2.x (последняя проверенная — 2.1.4). Ветка 1.x продолжается
(1.19.4), апгрейд на 2.x — явный выбор. awf запускает воркеров через CLI `opencode run`
и читает opencode-конфиг/базу, поэтому переход требует проверки и, вероятно, правок.
Сейчас на машине opencode **1.19.3**; awf работает на ней.

## 2. Что зависит от opencode (карта)

| Место | Контракт | Статус в 2.x |
|---|---|---|
| `awf/agent_stage.py:171` | spawn `opencode run --auto --print-logs --agent … --title … --file … [--model p/m] -- <prompt>`; успех = rc 0 + сигнал, иначе авария | флаги сохранились; сессия по умолчанию живёт на фоновом сервере |
| `awf/supervisor.py:1122` | тот же spawn без `--print-logs` | то же |
| `awf/signal_watch.py:269` + `awf/_net.py:46` | preflight `provider.<p>.options.baseURL` из opencode.json → GET `{base}/models`; kill группы процессов | kill группы ломается в default-режиме 2.x (сессия на detached-сервере) |
| `awf/_env.py:249` | `OPENCODE_CONFIG_CONTENT` (v1-формат: permission-мапа, agent-deny, mcp-disable), merge с opencode.json | env читается, v1-мапа нормализуется; в managed-режиме сервер использует свою конфигурацию → env воркера игнорируется |
| `awf/opencode_agents.py:87` | читает/пишет v1-формат `agent.*`, `mcp.*` | v1-формат принимается, файл не переписывается |
| `awf/metrics.py:203,339` | sqlite `~/.local/share/opencode/opencode.db`: `session`, `part`, `message` | v2 пишет `session_v2` + `session_message`, таблицы `part` нет → тихие нули |
| `awf/metrics.py:675` | `~/.cache/opencode/models.json` (цены) | на чистом v2 файла нет (каталог в `kv` БД); код деградирует в built-in-таблицу |
| `awf/api/model_check.py:54` | `opencode models`, `SELECT … FROM session` | вывод тот же, но команда — серверный вызов (может поднять сервис); таблица `session` — см. выше |
| `agent_workflow_ui/.../opencode_config.py` | те же `opencode models`/БД | то же; stdio-MCP плагин совместим |
| `tests/infra/opencode_shim` | эмулятор CLI | не зависит от реального CLI |

awf не использует HTTP API opencode; ломается не API, а архитектура процессов и чтение БД.

## 3. Проверенные факты о 2.x (локально и по официальным источникам)

- Версия: **2.1.4**; установка отдельная (`@opencode/cli`, brew tap, v2-install), апгрейд
  через `/update` или `opencode upgrade --major`. Ветка 1.x поддерживается параллельно.
- Репозиторий: `anomalyco/opencode` (был `sst/opencode`).
- Архитектура: по умолчанию `opencode run` — клиент к общему фоновому сервису
  (detached, реестр `~/.local/state/opencode/service.json`). Флаг **`--standalone`** —
  приватный сервер в группе процессов; «server logs require --standalone».
- `opencode run` 2.1.4: есть `--auto`, `--print-logs`, `--agent`, `--title`, `--file`,
  `--model`; разделитель `--` сохранён; exit codes 0/1/130. Исчезли `--attach`, `--port`,
  `--dir`, `-i`.
- Конфиг: v1 читается с той же локации и нормализуется в памяти (файл не переписывается):
  `permission` → `permissions` (список, последнее совпадение побеждает), `bash`→`shell`,
  `write`→`edit`, `agent`→`agents`, `mcp.<n>`→`mcp.servers.<n>`, `provider`→`providers`,
  `api`→`settings.baseURL`; `external_directory` по умолчанию allow (в v1 — ask).
- БД: тот же файл `~/.local/share/opencode/opencode.db`; таблицы **`session_v2`** и
  **`session_message`** (данные сообщений внутри JSON `data`), `part` отсутствует.
- `OPENCODE_CONFIG_CONTENT` читается (после файлов, до managed).

Проверено лично: `/tmp/oc2/opencode --version` = 2.1.4, `run --help` содержит
`--standalone` и предупреждение о логах; в исходниках v2.1.4 есть `session_v2`/
`session_message` (тест миграции). Полный отчёт исследования сабагента сохранён в этой
задаче; артефакты `/tmp/oc2/opencode`, `/tmp/oc2/oc2-src` — временные.

Источники: opencode.ai/v2/docs (cli, permissions, migrate-v1), releases GitHub
`anomalyco/opencode`, npm registry (`@opencode/cli` 2.1.4, `opencode-ai` 1.19.4).

## 4. Риски

| # | Риск | Влияние |
|---|---|---|
| R1 | Без `--standalone` kill/таймаут убивает только CLI-клиент; сессия продолжает работать на фоновом сервере → орфаны, правки в чужой коммит (рестарт инцидента RUN8 #2) | критическое |
| R2 | `OPENCODE_CONFIG_CONTENT` воркера (permission, скрытие контрольных MCP, изоляция) не применяется в managed-режиме | высокое |
| R3 | Метрики: чтение `session/part/message` на v2 даёт 0 новых сессий без ошибок | среднее |
| R4 | `model_check`: `SELECT … FROM session` — та же смена таблицы (graceful) | низкое |
| R5 | `opencode models` поднимает фоновый сервис; холодный старт vs таймаут 10с | низкое |
| R6 | `~/.cache/opencode/models.json` отсутствует на чистом v2 → built-in цены | низкое |
| R7 | Preflight читает v1-путь `provider.*.options.baseURL`; после перевода конфига в v2-нативный отключится | низкое |
| R8 | `external_directory` по умолчанию allow — проверить, что deny воркера побеждает | низкое-среднее |
| R9 | `--print-logs` без `--standalone` не печатает серверные логи → классификация сетевых смертей деградирует | низкое-среднее |
| R10 | MCP-плагин: v2 Code Mode меняет подачу инструментов; имена и permission-gating сохранены (проверить) | среднее |

Откат прост: конфиг файлом не переписывается, `.agentic/` не затрагивается; вернуть
бинарь v1 (`npm i -g opencode-ai@1.19.4` или прежний установщик).

## 5. План миграции (отдельный ран)

**P0 — до апгрейда (иначе орфаны):**
1. `awf/agent_stage.py` и `awf/supervisor.py`: добавлять `--standalone` при opencode ≥ 2
   (версия через `opencode --version`, кэш; конфиг `opencode.standalone: auto|on|off`,
   дефолт `auto`).
2. Preflight в старте пайплайна: major ≥ 2 и standalone не активен → явный отказ с
   текстом. Лучше отказ, чем орфан-сессия.

**P1 — после перехода:**
3. `awf/metrics.py`: проба схемы (`sqlite_master`) → v1 (`session/part/message`) или v2
   (`session_v2/session_message`); матч проекта по `data LIKE '<путь>'`; токены/стоимость
   из `data` (форма совместима).
4. `awf/api/model_check.py` и плагин: та же проба таблицы; `opencode models` с
   `--standalone` (подтвердить флаг).

**P2 — по ситуации:**
5. `_net.py`: baseURL-цепочка v1 → v2 (`providers.*.settings.baseURL`).
6. `_net.py`: маркеры сетевых ошибок — перепроверить по реальному v2-выводу.
7. `opencode_agents.py` не менять (v1-формат поддерживается).

**Ручная проверка на тестовой машине v2:** `run --standalone` + сигнал; kill группы →
нет орфана; `OPENCODE_CONFIG_CONTENT` deny MCP-инструмента работает; `session_v2.title`
равен `--title`; `opencode debug paths db`; вывод `opencode models`; строки сетевых
ошибок.

**Приёмка рана:** на v2 все e2e/шим-тесты зелёные; kill не оставляет процессов; метрики
показывают новые сессии; preflight работает; на v1 поведение не изменилось (флаг `auto`
не добавляет `--standalone`).
