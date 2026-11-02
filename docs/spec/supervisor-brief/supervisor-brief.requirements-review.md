# Ревью требований: единая инструкция супервизора через `awf_brief`

Статус: PASS
Ревизия: reqrev-11
Входы: supervisor-brief.md@req-11; supervisor-brief.context.md@ctx-2; TODO-0326 (витки 1–5); TODO-0327, TODO-0329; DONE юнитов 0320–0324; бэклог A24/A26/A29
База кода: 40bb6cc (рабочее дерево; артефакт 85ec91a отстал)
Проверено: supervisor-brief.md=req-11

Режим: ревью требований до проектирования. Способ: отдельная стадия, свежая
сессия; модель та же, что у автора. Несущие факты сверял с кодом и
артефактами, не с отчётом.

Вердикт: PASS. RR45–RR51 закрыты по существу. Три новые MINOR (RR52–RR54) не
мешают дизайну; блокирующих Q# нет. READY не выставляю: это не финальный режим.

## Машинные факты на этой ревизии

Проверял сам, read-only:

- **Имена**: `tool_names()` → 56 MCP (51 `awf_*` + 5 UI); `awf_verify_pack` на
  месте. AC4/INV9 (56 → 54) верны.
- **A31**: `next_action_from_status` (`awf/brief.py:730-741`) отдаёт foreign
  commit выше salvage/blocked; поле есть в `StatusResult` (`_results.py:87`) и
  в `_ACTIONABLE_FIELDS` (`brief.py:682`). Порядок S1a → S1 → S2–S3 совпадает
  с кодом.
- **State**: в `current.yaml` есть `todo_id` и `updated_at`; `current_todo` —
  новое поле статуса по D7.
- **Аудит коммита**: `_audit_record` (`commit_gate.py:416-445`) уже пишет
  `skip_reason` (`:443`), не только `status`/пути. Статусы
  `committed`/`skipped`/`refused`/`error` — из `commit_plan.py:36-39`.
- **Аварийный пакет**: `_write_verify_pack_error_note` пишет `**error**` без
  JSON; обычные вердикты — `ok`/`failed`/`nothing-measured`.
- **Документы, называющие удаляемый API**: `awf/api/__init__.py:23`,
  `awf/api/planning.py:4`, `awf/run_plan_read.py:3`, `awf/api/brief.py:31`,
  `awf/brief.py:134`, `awf/api/_results.py:475`.
- **Бюджет**: требования 2198 слов; пакет 10553 из 11000 (этот отчёт — 822).

## Находки

| ID | Важность | Локация / R# / AC# | Проблема и доказательство | Последствие | Что должно измениться | Владелец | Состояние |
|---|---|---|---|---|---|---|---|
| RR52 | MINOR | AC18; R5; C4 2c | AC18 говорит «для S0–S13», хотя req-11 добавил S1a в R4/AC5/AC13/A2. Для S1a не задан список тулов с «зачем»; C4 2c проверяет только приоритет над salvage. | В состоянии чужого коммита карта тулов может быть пустой или общей; R5 формально зелёный. | Добавить S1a в AC18/AC19 и строку в C4. | analyst | NEW |
| RR53 | MINOR | AC11, AC40, AC47, A11 | Таблица «поверхность × исход» не перечисляет code-docstrings, которые после удаления API останутся врать: `api/__init__.py:23`, `planning.py:4`, `run_plan_read.py:3`, `api/brief.py:31`, `brief.py:134`, `_results.py:475`. AC22 считает docstring носителем. | Правда о входе снова расходится с поведением; проверка AC40 пройдёт, а `run_plan_read` будет звать удалённый «full view». | Добавить эти пути в AC11/AC40 либо исключить с причиной. | analyst | NEW |
| RR54 | MINOR | A8; AC43 | A8 утверждает, что `_audit_record` пишет «только status/пути», но там уже есть `skip_reason` (`commit_gate.py:443`) для `plan-skip-inconsistent`. Общее поле `reason` — новое. | Исполнитель может завести `reason` рядом с `skip_reason` или переиспользовать последнее; читатели разойдутся. | Сказать: `skip_reason` остаётся для своего случая, `reason` — общий. | analyst | NEW |

## Закрытие находок reqrev-10

| ID | Проверка в req-11 | Статус |
|---|---|---|
| RR45 | S1a `foreign_commit` есть в R4, AC5, AC13, AC17, AC46, C4, A2, INV10; приоритет выше salvage; совет `git log -1`/`awf rollback`/повтор, «не salvage» | VERIFIED |
| RR46 | AC1: «START of a new session» только у брифа, «START of every interaction» нет ни у кого; совет статуса равен брифу, не отсылка | VERIFIED |
| RR47 | AC26 знает `error`; AC29 — аварийный отчёт без JSON; C3 2g | VERIFIED |
| RR48 | AC47/A11: публичный Python-API удаляется, export снят, helper'ы остаются; Q6 закрыт | VERIFIED |
| RR49 | AC16: «каким тулом или чем закрыть» | VERIFIED |
| RR50 | AC5 ссылается на AC42, не INV1 | VERIFIED |
| RR51 | AC28: state `todo_id`, в статусе `current_todo` (D7) | VERIFIED |

## Покрытие требований

| R# | AC | Сценарий | Источник |
|---|---|---|---|
| R1 | AC1–AC4 | C10 | TODO п.1, CTX1 |
| R2 | AC5–AC8, AC42 | C2, C3, C7 | TODO п.1, CTX1/2 |
| R3 | AC9–AC12, AC45, AC47 | C9 | TODO п.6, CTX12/13/18 |
| R4 | AC13–AC17, AC41, AC46 | C1–C7 | TODO п.2, A31 |
| R5 | AC18–AC21 | C8 | TODO п.3, CTX7 |
| R6 | AC22–AC25 | C2 | TODO п.4, CTX3 |
| R7 | AC26–AC29, AC43, AC44 | C3, C6 | TODO п.5, CTX6, A29 |
| R8 | AC30–AC33 | C8 | TODO п.2, CTX4 |
| R9 | AC34–AC36 | C2, C5 | TODO п.2, CTX5, A24 |
| R10 | AC37–AC40 | C9 | TODO «Границы», CTX11/17 |
| R11 | AC48–AC49 | C8 | TODO виток 4 п.2, CTX14 |

## Границы проверки

- READY не выставлял: архитектуру, тест-спеку и план как пакет не оцениваю.
- Тесты не запускал (политика роли). Живые пробы — read-only: `tool_names`,
  `parse_spec_header`, `count_words`, `grep`, `git show`.
- Runtime-поведение карточки не проверял — предмет тестовой спеки и verify.
- Архитектура (`arch-4`) и тест-спека (`ts-4`) ещё называют req-9, обёртку и
  `deprecated`; их перезапускают spec-design и spec-tests. Это не дефект
  требований.

## Маршрут

1. `agent-architector` переписывает архитектуру под удаление, R11 и состояния
   S0–S13 + S1a; закрывает RR52–RR54 в части контракта.
2. `agent-qa-review` обновляет тест-спеку под req-11: T2 (маркеры), T9/T10
   (живой цикл и оба режима), T1/T3 (54 имени, отказ удалённого), проверка
   S1a и docstrings RR53.
3. Readiness после дизайна и тестов; требования не редактировал.
