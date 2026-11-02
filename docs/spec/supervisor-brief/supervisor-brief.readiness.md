# Готовность: единая инструкция супервизора через `awf_brief`

Статус: READY
Ревизия: rd-5
Входы: supervisor-brief.context.md@ctx-2; supervisor-brief.md@req-11; supervisor-brief.requirements-review.md@reqrev-11; supervisor-brief.architecture.md@arch-5; supervisor-brief.test-spec.md@ts-5; readiness@rd-4
База кода: 40bb6cc (рабочее дерево; `?? docs/spec/supervisor-brief/`, код не менялся)
Проверено: supervisor-brief.context.md=ctx-2; supervisor-brief.md=req-11; supervisor-brief.requirements-review.md=reqrev-11; supervisor-brief.architecture.md=arch-5; supervisor-brief.test-spec.md=ts-5; supervisor-brief.readiness.md=rd-5
Пакет-sha: 771ae0cd82b2af48711f824c6a6d5859a74f25e4582a136869c574be0b4123aa

## Вердикт

READY. Пакет можно отдавать исполнителю: R1–R11, AC1–AC49 и INV1–INV10
покрыты T1–T20, контракты K1–K6 согласованы с решениями D1–D12, план
IMP1–IMP8 исполним по зависимостям, ревизии замкнуты. Открытых BLOCKER и
MAJOR нет; MINOR ниже не меняют поведение и закрываются по маршруту.

Способ: отдельная стадия, свежая сессия, та же модель. Чужие файлы не
правил; тесты не запускал (политика роли). Read-only проверки: реестр и
карта, `count_words`, `parse_spec_header`, `package_fingerprint`,
`spec_gate`, `git show`, `grep`.

## Проверено по существу

- Ревизии: `reqrev-11` — PASS на `req-11`; `arch-5` и `ts-5` называют
  `req-11`; `ctx-2` снят с `40bb6cc`; база у всех шести — `40bb6cc`.
- Требования: PASS @reqrev-11. RR45–RR51 закрыты; FR14/FR15 из rd-4
  закрыты: AC28/AC43 говорят про живой цикл юнита, AC2 запрещает заявку
  «заменяет N вызовов» в любом написании и называет три точных места.
- Архитектура: каждый R имеет решение и точку изменения; K1 даёт поля
  ответа, K2 — карту, K3 — ожидание, K4 — читателей `GATES`/`COMMIT-AUDIT`,
  K5 — хвост лога, K6 — отказ удалённого имени. D7 держит один признак
  «живой цикл юнита» для одиночного `awf start` и забега (AC28).
- Тесты: матрица ts-5 покрывает все AC и INV; у каждого T1–T20 есть
  проверяемая норма и конкретное ожидание; негативные кейсы названы.
- Обратное покрытие: у каждого IMP1–IMP8 есть R и проверка; лишних
  функций нет.
- Машинные факты на базе: реестр 56 (51 `awf_*` + 5 UI),
  `awf_verify_pack` на месте, карта 66 записей (56 MCP + 40 CLI),
  карточка 954 слова, промпт фазы 775 слов, `_audit_record` уже пишет
  `skip_reason`, `GATES` пишется до пробуждения супервизора
  (`pipeline_engine.py:1590`).
- Машинный гейт: `spec_gate` на этой ревизии — READY (шесть файлов,
  `Пакет-sha` совпал, матрица и бюджет в норме).

## Находки

| ID | Важность | Локация / R# / AC# | Проблема и доказательство | Последствие | Что должно измениться | Владелец | Состояние |
|---|---|---|---|---|---|---|---|
| RR52 | MINOR | AC18/AC19; R5 | Список «S0–S13» не называет S1a, хотя R4/AC13 его добавили; K2 и T11 строку S1a держат | В состоянии чужого коммита карта может остаться без строки; T11 ловит | Добавить S1a в AC18/AC19 | analyst | открыт, не блокирует |
| RR53 | MINOR | AC11, AC40; T17 | Шесть docstring'ов удаляемого API вне таблицы поверхностей: `api/__init__.py:23`, `planning.py:4`, `run_plan_read.py:3`, `api/brief.py:31`, `brief.py:134`, `_results.py:475` | После удаления останутся ложные описания «full view»; T17 их не проверяет | Поправить docstring'и в IMP1; добавить пути в AC11/T17 | analyst, architect | открыт, не блокирует |
| RR54 | MINOR | A8; AC43 | A8 говорит «`_audit_record` пишет только status/пути», но `skip_reason` уже есть (`commit_gate.py:443`) | Исполнитель может слить `reason` и `skip_reason` | Сказать: `skip_reason` — для `plan-skip-inconsistent`, `reason` — общий | analyst | открыт, не блокирует |
| FR17 | MINOR | req-11: объявления ID | 35 из 70 ID стоят не с начала строки; `declared_requirement_ids` видит 35, поэтому матрица не может нести остальные строки | Будущая правка может молча потерять AC; поведение не страдает | Разбить объявления по строкам (без смены слов), req-12 | analyst | открыт, не блокирует |
| FR18 | MINOR | A-2; AC11/INV3; T1/T11 | A-2: `inventory_only` «у `helpers` и удаляемых записей», но AC11/INV3/T1 требуют отсутствия снятых имён, а T11 ждёт 64 записи | Два прочтения карты: 64 записи или 66 с архивом | Оставить: снятых имён нет; `inventory_only` — только helper'ы | architect | открыт, не блокирует |
| FR19 | MINOR | K1; T7/T9 | K1: `verify == {}` — «вердикта нет», но T7/T9(e) ждут `status="missing"` | Два вида ответа на «юнит жив, артефакта нет» | Назвать: нет юнита/старый артефакт — `{}`; жив, файла нет — `missing` | architect | открыт, не блокирует |
| FR20 | MINOR | K1/T7; AC26/AC29 | Вердикт `unreadable` (битый JSON) есть в контракте и T7, но не в требованиях; фикстуры в T8 нет | Битый JSON может уронить карточку без проверки | Добавить в AC29 или убрать `unreadable`; фикстура в T8 | analyst, qa | открыт, не блокирует |
| FR21 | SUGGESTION | arch §1 | «коммит-гейт не меняется (INV8)» — INV8 про `DONE.json`/handoff, а A8/IMP3 меняют `_audit_record` | Граница читается шире правды | Сказать: поведение гейта не меняется, меняется запись `reason` | architect | открыт |

## Матрица покрытия

|ID|Источник|Сценарий/контракт|Проверка|
|---|---|---|---|
|R1|supervisor-brief.md#R1|C10/K6|T1/T2/IMP1|
|R2|supervisor-brief.md#R2|C2/C7|T3/T4/IMP2|
|R3|supervisor-brief.md#R3|C9/K6|T16/IMP1|
|R4|supervisor-brief.md#R4|C1/C2|T5/T6/T7/IMP2|
|R5|supervisor-brief.md#R5|C8/K2|T11/IMP4|
|R6|supervisor-brief.md#R6|C2/K3|T14/T15/IMP6|
|R7|supervisor-brief.md#R7|C3/C6|T7/T8/T9/T10/T19/IMP3|
|R8|supervisor-brief.md#R8|C8|T12/IMP5|
|R9|supervisor-brief.md#R9|C2/C5|T13/IMP7|
|R10|supervisor-brief.md#R10|C9|T17/IMP8|
|R11|supervisor-brief.md#R11|C8|T20/IMP5|
|AC1|supervisor-brief.md#R1|C1/K1|T2|
|AC2|supervisor-brief.md#R1|C1/D1|T2|
|AC3|supervisor-brief.md#R1|C1/K1|T2|
|AC4|supervisor-brief.md#R1|C10/K6|T1/IMP1|
|AC5|supervisor-brief.md#R2|C2/D4|T3/IMP2|
|AC9|supervisor-brief.md#R3|C9/K6|T16|
|AC10|supervisor-brief.md#R3|C9/K6|T16/IMP1|
|AC11|supervisor-brief.md#R3|C9/D12|T16/T17/IMP8|
|AC12|supervisor-brief.md#R3|C9/D1|T16/IMP1|
|AC13|supervisor-brief.md#R4|C1/K1|T5/IMP5|
|AC18|supervisor-brief.md#R5|C4/K2|T11/IMP4|
|AC22|supervisor-brief.md#R6|C2/K3|T14/IMP6|
|AC26|supervisor-brief.md#R7|C3/K4|T7/IMP5|
|AC28|supervisor-brief.md#R7|C3/D7|T9/IMP3|
|AC29|supervisor-brief.md#R7|C3/K4|T8/IMP3|
|AC30|supervisor-brief.md#R8|C8/D9|T12/IMP5|
|AC34|supervisor-brief.md#R9|C2/K5|T13/IMP7|
|AC37|supervisor-brief.md#R10|C9/D12|T17/IMP8|
|AC43|supervisor-brief.md#R7|C6/D7|T10/IMP3|
|AC44|supervisor-brief.md#R7|C3/K1|T7/IMP2|
|AC47|supervisor-brief.md#R3|C9/D1|T16/IMP1|
|AC48|supervisor-brief.md#R11|C8/K1|T20/IMP5|
|AC49|supervisor-brief.md#R11|C8/D3|T20/IMP5|
|INV1|supervisor-brief.md#Не должно измениться|C7/K1|T4/IMP5|

Требования объявляют 49 AC и 10 INV, но 35 из 70 ID стоят не с начала
строки (FR17). Матрица даёт строку на каждый R и каждый объявленный ID;
R-строка покрывает свои AC целиком. Остальные ID:

AC6–AC8 → T3; AC14 → T5; AC15–AC17 → T6/T7; AC19–AC21 → T11;
AC23–AC25 → T14/T15; AC27 → T8; AC31–AC33 → T12; AC35–AC36 → T13;
AC38–AC40 → T17; AC41 → T6; AC42 → T4; AC45 → T16; AC46 → T6; INV2 → T12;
INV3 → T11; INV4 → T18; INV5 → T15; INV6 → T17; INV7 → T18; INV8 → T19;
INV9 → T1; INV10 → T6. Поведение от формата объявлений не зависит.

## Условия завершения реализации

1. T1–T20 зелёные; поведенческие (T2–T17, T20) сначала красные на базе,
   proven-red по поведению, а не по импорту.
2. Восемь пинящих тестов обновлены или заменены, не удалены; полный сьют
   зелёный; `ruff check .` чист; K1 проверен по S0–S13 и S1a.
3. Имена: 54 (49 `awf_*` + 5 UI), разница ровно два; снятых имён нет в
   `list_tools`/`tool_names()`/карте/доках; вызов даёт «removed»/«удалён» и
   `awf_brief`; CLI `brief`/`status` дают rc=0; `AGENTS.md` и skill равны
   источникам; числа 54/49.
4. В IMP1 поправить шесть docstring'ов (RR53); в IMP3 держать `reason`
   рядом с `skip_reason` (RR54); в IMP4 — FR18, в IMP5 — FR19/FR20.
5. Поведение коммит-гейта не исполняется: T10 держит запись и чтение
   `COMMIT-AUDIT`, вердикт — оттуда.

## Остаточные риски

- Полный сьют не запускался, артефакта `SUITE-*` на базе нет — факт за
  verify.
- Поведение коммит-гейта не исполнялось: проверены только запись и
  чтение артефакта.
- `run_record.current` правится при чтении; «ран активен, `current` пуст»
  не покрыто.
- Смысл `when` в карте проверяется только на непустоту.
- Бюджет пакета близок к потолку; правки делать со сжатием.
- FR17: машинный разбор объявлений видит 35 из 70 ID.

## Открытые вопросы

Нет.

## Порядок чтения для исполнителя

Требования → архитектура и план (IMP1–IMP8) → тестовая спецификация
(T1–T20); контекст — для источников и рисков. READY не даёт разрешения на
публикацию или внешние изменения.
