# Supervisor Flow

**Версия:** 0.2 (draft) · **Дата:** 2026-08-08

## Что

Модель работы supervisor-агента с awf pipeline: от старта сессии до verify.
Описывает supervisor как **диалогового координатора**, активного в ключевых
точках и пассивного между ними — в противовес текущей watchkeeper-модели
(непрерывный poll `awf_wait_for_event`).

Дополняет [agent-ui-plugin.md](agent-ui-plugin.md) (что делают tools) и
[architecture.md](architecture.md) (как устроено) — отвечает на вопрос **как
supervisor должен применять tools** в ходе сессии.

## Почему

Watchkeeper-модель выявила три проблемы на dogfood-сессиях:

1. **Жжёт токены на ожидание.** Execute-стадии идут auto-transition, supervisor
   не нужен, но poll-ит всю сессию.
2. **Смешение слоёв TODO.** Supervisor путает «цель инкремента для пользователя»
   и «задачу для 1-го агента» → микро-менеджемент стадий, кривая нормализация
   skills, неверифицируемый verify.
3. **Перегруженный системный промт.** Один документ на весь flow — supervisor
   держит в голове все шаги сразу, attention размывается.

## Целевой flow (7 шагов)

1. **init = чистый старт.** `awf init` очищает runtime (inbox/outbox/handoff/
   done/state/logs), сохраняет config-слой (roles/pipelines/config). Флаг `--hard`
   для полного сброса.
2. **Цель в начале.** Supervisor узнаёт цель запуска у пользователя → изучает
   проект под цель → рекомендует роли.
3. **Форма с рекомендацией.** `awf_open_project_setup_form` prefill-ится
   recommended-roles от supervisor; пользователь подтверждает/правит.
4. **Нормализация skills.** После формы — обязательный 3-частный checklist:
   адаптация под характер итерации · разруливание перекрытий (`awf_analyze_roles`)
   · handoff-контракты между ролями.
5. **Два слоя TODO.** Сначала **Increment Brief** (цель + критерии успеха для
   пользователя, ~10–20 строк) → approve. Затем **Stage-1 TODO** (задача 1-й
   стадии + handoff-контракт + отсылки к role.md) для агента.
6. **Sleep mode.** После `awf_start` + dashboard open supervisor засыпает.
   Dashboard подсвечивает события (checkpoint / salvage / blocked / verify).
   Пользователь wake-ит сообщением.
7. **Verify по Brief.** Supervisor сверяет результат с Increment Brief (контракт),
   а не с весь вывод pipeline.

## Ключевые сдвиги vs текущей модели

| Аспект | Сейчас | Цель |
|---|---|---|
| init | Осторожный (`force=false` ничего не чистит) | Чистый runtime + сохранённый config |
| Старт сессии | `load_context` → форма | Цель → изучение под цель → рекомендация ролей |
| После формы | Сразу TODO | Нормализация skills (3 части) |
| TODO | Один файл, смешанный | Brief (approve + verify-contract) + Stage-1 TODO |
| Verify | Весь вывод pipeline | Increment Brief |
| Во время pipeline | Watchkeeper poll-ит | Sleep + dashboard wake |
| Промт | Один системный на flow (~600+ строк) | State-Machine Orchestration: awf ведёт, compact промт на шаг (~50–100 строк) |

## Принципы

- **Детерминизм + LLM.** Awf = рельсы (state machine, signals, handoffs, git,
  templates, tools). LLM = поезд. Больше детерминизма в рутине → меньше
  LLM-ошибок → выше качество. LLM остаётся на творческие задачи: понять цель,
  оценить качество, решить неоднозначность.
- **State-Machine Orchestration (вместо больших промтов).** Не «LLM читает
  supervisor.md и решает что делать», а **awf сам ведёт flow**: определяет
  текущий шаг по state, готовит компактный промт для этого шага, передаёт LLM
  только нужный контекст. LLM — исполнитель шагов, не планировщик flow.
- **Автоматизация tool.** Каждая supervisor-задача = candidate tool. Меньше
  свободного LLM-суждения в рутине → выше консистентность.

## Архитектурное решение: State-Machine Orchestration

Углубление принципа. R7 («phase-prompts») изначально мыслился как полумера —
awf подставляет секцию промта, LLM всё равно читает и решает. **State-Machine
Orchestration — структурное решение**: awf = полноправный orchestrator на весь
цикл сессии.

### Ключевая находка

Awf **уже** state-machine, но только для execute-части:
```
plan → analyst → architect → implementer → qa → auditor → verify
```
Здесь awf уже ведёт за руку: определяет стадию, готовит промт (`build_prompt`),
передаёт handoffs, детектит сигналы, переходит дальше. LLM-worker не решает
«какая стадия следующая» — awf это знает.

Решение = **распространить этот же паттерн на setup-часть** (init → goal → form
→ normalize → brief → dispatch). Сейчас setup живёт в `supervisor.md` (для LLM),
должен жить в awf-коде. Это не революция, а **завершение начатого** — единый
state-machine на весь flow, а не половину.

### Граница awf ↔ LLM (детерминированное vs творческое)

| Awf ведёт (детерминированно) | LLM решает (с контекстом) |
|---|---|
| init (clean runtime), form-open, dispatch, start, signal-detection, commit, archive, stage-transitions | Понять цель пользователя |
| Подготовка промта шага, передача контекста | Нормализовать skills (оценить перекрытия) |
| Определение следующего шага по state | Написать Increment Brief |
| Sleep-mode, dashboard wake | Verify — оценка качества работы |

Awf = дирижёр механики. LLM = исполнитель творческих узлов с компактным промтом
+ ссылками на артефакты (Brief, handoffs, vision) — LLM подтягивает нужное.

### Путь реализации

- **Инкрементально, не big-bang.** Переносить по одной секции `supervisor.md` в
  awf-steps, держать оба пути пока новый не стабилен.
- **Первый кандидат — setup-flow** (init → goal → form → normalize → brief).
  Именно там корневые ошибки dogfood-сессии TODO-0003 (смешение слоёв, пропуск
  нормализации). Execute-flow уже автоматизирован — его трогать позже.
- **Escape-hatch'и (ручные переходы, прерывания, jump-to-step) — через dogfood.**
  Сначала linear-flow, edge-cases (стоп-переделай, добавь-на-ходу) обкатаем на
  реальных сессиях, потом заложим в state-machine.

### Аналог

LangGraph / AutoGen — граф переходов задан кодом, LLM только узлы исполняет.
Known-good pattern в agentic-фреймворках.

## Кандидаты на tools (roadmap)

- `awf_open_increment_brief` — слой-A TODO для approve. Закрывает корневую ошибку
  смешения слоёв (Brief = contract для verify).
- `awf_normalize_skills` — 3-частный checklist нормализации `role.md` после формы.
- `awf_supervisor_step` — промт-инъекция по шагу state-machine.
- **Sleep-mode** — убрать auto-poll, добавить idle-состояние + dashboard wake.

## Связанные документы

- [Product Vision](agent-ui-plugin.md) — что делают tools
- [Архитектура](architecture.md) — как устроено
- [BACKLOG](../BACKLOG.md) — открытые задачи

## Статус

Draft v0.2. v0.1 сформулирован по итогам dogfood-сессии TODO-0003
(jira-epic-presenter, 2026-08-08). v0.2 — углубление R7 из «phase-prompts» в
**State-Machine Orchestration** (распространить existing pipeline-state-machine
на setup-flow) по итогам диалогового разбора архитектуры. **Не реализован** —
north star / roadmap для следующих итераций awf. Setup-flow — первый кандидат
на перенос в state-machine.
