# Orchestrator — Спецификация

Orchestrator — это ядро фреймворка. Он читает конфиг pipeline и последовательно запускает стадии, передавая данные между ролями через файловую шину.

---

## Архитектура orchestrator'а

```
┌─────────────────────────────────────────────┐
│  awf start                                   │
│        │                                     │
│        ▼                                     │
│  load_config() → load_pipeline()             │
│        │                                     │
│        ▼                                     │
│  for stage in pipeline.stages:               │
│    │                                         │
│    ├── execute_stage(stage)                  │
│    │     │                                   │
│    │     ├── role == "supervisor"            │
│    │     │   └── run_supervisor_stage()      │
│    │     │       (текущая сессия)            │
│    │     │                                   │
│    │     └── role != "supervisor"            │
│    │       └── run_agent_stage(role)         │
│    │           (opencode run --agent)        │
│    │                                         │
│    ├── wait_for_signal(stage)                │
│    │     └── polling outbox/ на .ready       │
│    │                                         │
│    └── resolve_transition(signal, stage)     │
│          ├── "next" → следующая стадия       │
│          ├── "rollback_to:X" → стадия X      │
│          ├── "escalate" → supervisor         │
│          ├── "stop" → остановка              │
│          └── retry → повтор той же стадии    │
│                                                │
│  generate_report()                             │
└─────────────────────────────────────────────┘
```

---

## `execute_stage()` — запуск стадии

### Supervisor-стадия

Supervisor — это текущая сессия. Orchestrator **не запускает** отдельный процесс, а выводит инструкции для пользователя:

```
═══════════════════════════════════════════
  STAGE: plan (supervisor)
═══════════════════════════════════════════

Инструкции в: .agentic/roles/supervisor.md
План проекта: .agentic/phases/MVP-PHASE.md

Что нужно сделать:
  1. Изучить текущее состояние проекта
  2. Определить следующий шаг из плана
  3. Создать TODO и положить в .agentic/inbox/
  4. Назначить: TODO-{NNNN}.md + TODO-{NNNN}.ready

По завершении нажмите Enter для продолжения...
```

После нажатия Enter orchestrator проверяет появление `.ready` файла и переходит к следующей стадии.

### Agent-стадия (worker, reviewer, tester)

Orchestrator запускает отдельный процесс opencode:

```bash
opencode run --auto \
  --agent <role.agent_name> \
  --file .agentic/roles/<role>.md \
  --file <input_files...> \
  "<generated_prompt>"
```

Генерируемый промпт зависит от `action` стадии:

| Action | Промпт |
|---|---|
| `execute_todo` | "Реализуй все Task из TODO-{NNNN} через edit tool. Запусти verify после каждой задачи. Напиши DONE или BLOCKED." |
| `review_code` | "Проведи код-ревью изменений по TODO-{NNNN}. Проверь соответствие задаче и качество. Напиши REVIEW-APPROVED или REVIEW-REJECTED." |
| `run_tests` | "Запусти тесты и проверки качества. Команды в конфиге. Сравни с baseline. Напиши TEST-PASSED или TEST-FAILED." |

---

## `wait_for_signal()` — ожидание результата

Orchestrator опрашивает `outbox/` на появление `.ready` файла:

```bash
# Ожидание с таймаутом
POLL_INTERVAL=5    # секунд между проверками
TIMEOUT=3600       # 1 час макс.

while true; do
  if ls "$OUTBOX"/*-${TASK_ID}.ready 1>/dev/null 2>&1; then
    SIGNAL_FILE=$(ls -t "$OUTBOX"/*-${TASK_ID}.ready | head -1)
    echo "Signal received: $SIGNAL_FILE"
    break
  fi
  
  sleep "$POLL_INTERVAL"
  
  # Проверка таймаута
  ELAPSED=$(( $(date +%s) - START_TIME ))
  if [[ $ELAPSED -gt $TIMEOUT ]]; then
    echo "TIMEOUT: Stage did not complete within ${TIMEOUT}s"
    write_blocked "${TASK_ID}" "Stage timeout"
    break
  fi
done
```

---

## `resolve_transition()` — переход между стадиями

На основе сигнала из `outbox/` и правил из конфига pipeline определяет следующее действие:

```
Сигнал из outbox:
├── DONE-{NNNN}.ready
│   └── on_approved из конфига:
│       ├── "next" → следующая стадия
│       ├── "commit_and_next" → git commit → следующая
│       └── "commit_and_report" → git commit → генерация отчёта
│
├── BLOCKED-{NNNN}.ready
│   └── on_blocked из конфига:
│       ├── "escalate" → supervisor стадия
│       ├── "rollback_to:<stage>" → указанная стадия
│       └── "stop" → остановка, ожидание пользователя
│
├── REVIEW-APPROVED-{NNNN}.ready
│   └── on_approved → следующая стадия
│
├── REVIEW-REJECTED-{NNNN}.ready
│   └── on_rejected → rollback или replan
│
├── TEST-PASSED-{NNNN}.ready
│   └── on_passed → следующая стадия
│
└── TEST-FAILED-{NNNN}.ready
    └── on_failed → rollback
```

### Retry логика

Если сигнал требует повторной попытки:

```
Попытка 1: worker → BLOCKED
  ↓ max_retries = 3? Да
Попытка 2: supervisor создаёт исправленный TODO → worker → DONE
  ↓ success
Переход к следующей стадии
```

Если превышен `max_retries`:

```
Попытка 3: worker → BLOCKED
  ↓ max_retries = 3? Превышено
Остановка pipeline
  → уведомление пользователя
  → запись в логи
```

---

## Генерация отчёта

По завершении pipeline orchestrator генерирует `.agentic/reports/status.md`:

```markdown
# Workflow Report

**Project:** {project.name}
**Pipeline:** {pipeline.name}
**Generated:** {timestamp}

## Summary

| Metric | Value |
|---|---|
| Stages completed | {N}/{total} |
| Tasks executed | {N} |
| Blockers encountered | {N} |
| Rollbacks | {N} |
| Total time | {duration} |

## Tasks

| ID | Status | Stage | Time |
|---|---|---|---|
| TODO-0001 | ✅ DONE | implement | 12m |
| TODO-0002 | ✅ DONE | implement → review → test | 25m |
| TODO-0003 | 🔄 IN PROGRESS | implement | — |

## Files Changed

{git diff --stat}

## Test Results

{последний результат тестов}

## Notes

{замечания supervisor'а, если есть}
```

---

## Логирование

Каждая итерация логируется в `.agentic/logs/orchestrator.log`:

```
2026-07-07T15:00:00Z [orchestrator] Pipeline started: default
2026-07-07T15:00:00Z [orchestrator] Stage: plan (supervisor)
2026-07-07T15:05:00Z [orchestrator] Signal: TODO-0001.ready
2026-07-07T15:05:01Z [orchestrator] Stage: implement (worker)
2026-07-07T15:17:00Z [orchestrator] Signal: DONE-0001.ready
2026-07-07T15:17:01Z [orchestrator] Transition: next → verify
2026-07-07T15:17:01Z [orchestrator] Stage: verify (supervisor)
...
```
