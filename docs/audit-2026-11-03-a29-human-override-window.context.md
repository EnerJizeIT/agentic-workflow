# Аудит A29: окно перекрытия человеческого и авто-вердикта

Scope: TODO-0364 (A29, повтор) — окно `wait.human_override_seconds` между
авто-вердиктом стадии verify и вердиктом владельца. Широкий срез вокруг
изменения: переносимость, стыки с соседними юнитами, шероховатости.
Ревизия: ctx-1
База кода: commit `cda51ac` + незакоммиченные файлы юнита
(`awf/arbiter.py`, `awf/supervisor.py`, `awf/config_keys.py`,
`tests/conftest.py`, `tests/unit/_signal_fakes.py`, два новых тест-файла,
две правки старых тестов); дерево `82a2d62…`.
Статус: REVIEW — контекст и находки собраны; пределы проверки названы в
конце.

## Краткий итог

Окно делает то, что просили: авто-REVIEW больше не применяется сразу,
человеческий APPROVE, написанный через секунды, побеждает, авто-REVIEW
выметается и клона юнита не появляется. Обратное направление (авто-APPROVE
и человеческий REVIEW) идёт тем же кодом и покрыто тестом QA.

Три вещи прошлые стадии не назвали:

1. **Окно срабатывает на любой авто-сигнал verify, включая ACK.**
   Наблюдал вживую: в `tests/negative/test_stage_assignment_outputs.py`
   verify дал `ACK-TODO-0001` в 19:15:47, движок применил его в 19:16:47 —
   ровно 60 с окна. Тест целиком занимает 69.9 с, из них 60 — окно.
2. **Проверка привязки в окне считается дважды за тик** и столько же раз
   пишет строку «ignored» в лог: до 120 git-отпечатков и до 60 одинаковых
   строк на один отвергнутый APPROVE.
3. **Короткое окно в тестах выставлено только фикстуре `initialized_project`.**
   Negative-тесты собирают конфиг сами и платят дефолтные 60 с за каждый
   прогон, доходящий до verify.

## Карта затронутых путей

- авто-вердикт: `awf/supervisor.py:run_supervisor_via_subprocess` →
  watcher-путь (`:1776`) или fallback (`:1820`) → `_prefer_human_verdict`
  (`:1466`) → `await_human_override` (`awf/arbiter.py:330`) →
  `human_verify_decision(..., require_current_binding=True)`
  (`awf/arbiter.py:242`) → `discard_overruled` → сигнал наружу.
- чтение окна: `_human_override_seconds` (`awf/supervisor.py:1429`), ключ
  `wait.human_override_seconds` (`awf/config_keys.py:83`).
- проверка привязки: `_approve_is_current` (`awf/arbiter.py:198`) →
  `commit_plan.preexisting_approval_status` (`awf/commit_plan.py:992`) →
  `verified_fingerprint_refusal` (`:900`) → `git_utils.tree_fingerprint`.
- интерактивная стадия (`auto=False`) идёт через
  `_wait_for_verify_decision`/`wait_for_supervisor_signal` — авто-вердикта
  там нет, окно не участвует.
- писатели вердикта с меткой человека: только `awf/api/pipeline.py:1079`
  (approve) и `:1221` (reject, через `arbiter.mark_human_review`).

## Переиспользованные контракты и соглашения

- «Один арбитр»: окно не вводит второго определения «вердикт человека
  считается» — оно спрашивает `human_verify_decision` с флагом.
- `commit_plan.preexisting_approval_status` — тот же валидатор, что у
  коммит-гейта; привязанный APPROVE, чьё дерево уехало, не перекрывает.
- Политика числовых ключей `wait.*`: 0 — выключено, отрицательное или не
  число — дефолт с логом; копия `_verify_grace_seconds`.
- Метка источника: `{"source": "human"}` у APPROVE и HTML-комментарий
  `<!-- awf-source: human -->` у REVIEW (`awf/arbiter.py:36-47`).

## Baseline и средства проверки

| Проверка | Каталог | Результат | Что наблюдалось |
|---|---|---|---|
| канонический сьют, дерево `82a2d62…` (артефакт QA) | корень репо | PASS (не перезапускал) | 6967 passed, 0 failed, 0 errors, 1415 с |
| `pytest tests/unit/test_verify_human_override.py tests/unit/test_verify_human_override_window.py tests/unit/test_one_arbiter.py tests/negative/test_u6b_stale_decision.py -q` | корень репо | PASS | 43 passed in 3.93s |
| `ruff check .` | корень репо | PASS | All checks passed |
| `sh scripts/ratchet.sh` | корень репо | PASS | counts within baseline |
| `sh scripts/check-contracts.sh` | корень репо | PASS | ok (8 contracts, 6 unit contracts, 21 path tokens, 8 gates) |
| `sh scripts/instruction-gates.sh` | корень репо | PASS | ok (доктрина 250/250) |
| `pytest tests/negative/test_stage_assignment_outputs.py -q --durations=6` | корень репо | PASS, но медленно | 10 passed in 77.19s; `test_stage_prompt_carries_task` — 69.91s |
| один и тот же тест + `--basetemp` и разбор лога | корень репо | PASS | лог: ACK в 19:15:47, «consumed» в 19:16:47 — 60 с окна |
| `tree_fingerprint` × 5 на этом дереве | корень репо | — | 22.5–27.4 мс за вызов |
| `git rev-parse HEAD` / `tree_fingerprint` | корень репо | — | `cda51ac…` / `82a2d62…` = sha артефакта QA |

## Находки в scope

| ID | Важность | Файл / символ | Факт и доказательство | Влияние | Следующий шаг / владелец | Уверенность |
|---|---|---|---|---|---|---|
| CTX1 | MINOR | `awf/supervisor.py:1491` (`window = … if auto_signal else 0.0`) | Окно включается на любом непустом авто-сигнале verify, включая `ACK-<id>`, который тоже означает «принято». Воспроизведено: прогон `test_stage_prompt_carries_task` с `--basetemp`, лог `orchestrator.log`: `19:15:47 Supervisor verify produced signal (fallback): 'ACK-TODO-0001'` → `19:16:47 U6b: recorded accepted verify decision`. Тест целиком длится 69.91 с, 60 из них — окно. | Каждый полностью автоматический прогон (CI, шим, `awf start --auto` без человека) платит 60 с за вердикт, который человеку перекрывать нечего. В TODO названы только REVIEW и APPROVE; ACK — третий случай, его никто не обсуждал. | Решить продуктово: ACK в окне нужен или нет. Если нет — гейт по `auto_signal.startswith(("APPROVE-", "REVIEW-"))`; если да — назвать это в TODO-докстроке и в USAGE. Владелец — architect. | подтверждено запуском |
| CTX2 | MINOR | `awf/arbiter.py:289` и `:297` + `:226` `_approve_tree_moved` | На каждом тике окна `human_verify_decision` считает `current = _approve_is_current(...)`, а затем `_approve_tree_moved` вызывает `_approve_is_current` ещё раз. Плюс ветка `elif … tree_moved` и обычная `elif` пишут строку «ignored» на каждом тике. До 120 git-отпечатков (22–27 мс каждый здесь) и до 60 одинаковых строк за одно отвергнутое решение. QA-5 отметила шум для привязанного APPROVE; дубль проверки — отдельный факт. | Лог оркестратора забивается повторами, работа считается дважды. На большом репозитории это уже секунды CPU на окно. | Передавать уже посчитанный `current` в `_approve_tree_moved` (или схлопнуть в `tree_moved = bool(verified_sha) and not current`) и логировать «ignored» один раз за окно. Владелец — implementer. | высокая (по коду) |
| CTX3 | SUGGESTION | `tests/conftest.py:272-283` против `tests/negative/*`, `tests/e2e/*` | Короткое окно (`human_override_seconds: 2`) выставляется только фикстуре `initialized_project`. Negative-тесты строят `.agentic/config.yaml` сами (`tests/negative/test_stage_assignment_outputs.py:30`, `test_pipeline_snapshot.py:40`) и получают дефолтные 60 с. Отсюда 69.91 с у одного теста; при этом его внутренний `timeout=180` на `awf start` остаётся выше, так что тест не падает, а просто ест время. | Сьют зелёный, но часть его времени — это окно, а не работа. Полный прогон QA: 1415 с. В одном тесте 60 с из 70 — пауза; добавление ещё пары verify-прогонов в negative-тесты упрётся в их таймауты. | Один общий короткий `wait.human_override_seconds` для тестовых проектов (helper/фикстура в `tests/negative/conftest.py` или в `_make_project`), а не только в `initialized_project`. Владелец — QA/implementer. | подтверждено запуском |
| CTX4 | SUGGESTION | `awf/config_keys.py:83`, `USAGE.md`/`USAGE.ru.md` | Ключ зарегистрирован и читается, но в USAGE его нет. Там же документирован соседний `wait.cap_seconds` (`USAGE.ru.md:797`, `USAGE.md:1051`). У `wait.verify_grace_seconds` документации тоже нет — то есть соглашение соблюдается непоследовательно. | Владелец не узнает из доков, почему после каждого авто-вердикта появилась пауза и как её сократить. Диагностика инцидента «движок завис» снова начнётся с чтения кода. | Добавить строку в раздел про ожидания USAGE (и/или в RELEASE-NOTES): дефолт 60 с, `0` выключает, ключ `wait.human_override_seconds`. Владелец — architect/docs. | высокая |
| CTX5 | SUGGESTION | `awf/supervisor.py:1429` и `:1400` | `_human_override_seconds` — копия `_verify_grace_seconds` (тот же разбор числа, тот же лог, та же политика «минус → дефолт»). Общий помощник не выделен. | Третий такой ключ снова скопирует разбор; расхождение политик (например, кламп сверху) появится в одной копии из трёх. | Вынести `_config_seconds(config, key, default, logs)` и переиспользовать в обоих местах. Владелец — implementer. | высокая |
| CTX6 | SUGGESTION | `tests/conftest.py:278-283` | Фикстура дописывает в конец `config.yaml` вторую секцию `wait:` сырым текстом. Сейчас `awf init` секцию `wait` не пишет (проверено на живом `awf init` в `/tmp/opencode/audit-init`), поэтому дубля нет. Но если шаблон её получит, PyYAML молча возьмёт последнюю секцию и потеряет значения из шаблона. | Латентная ловушка: «настройка из init не применилась» через месяц после изменения шаблона. | Писать ключ через существующий патчер конфига (`awf/config_write.py`) или вставлять в уже существующую секцию. Владелец — QA. | высокая (риск), дефекта сейчас нет |

## Стыки, которые проверены и держат

- Оба выхода авто-вердикта передают `config` в `_prefer_human_verdict`
  (`awf/supervisor.py:1776`, `:1820`) — клетка «watcher-путь» закрыта
  чтением кода. Отдельного теста на watcher-путь нет (см. ограничения).
- Пре-спавновая проверка (`awf/supervisor.py:1544`) не тронута: сигнал
  человека на диске вообще не спавнит субпроцесс, окна нет.
- Интерактивная стадия окна не касается: там `_wait_for_verify_decision`,
  авто-вердикт не создаётся.
- Метку `human` пишут только `awf_approve` и `awf_reject`; второго
  писателя в дереве нет (grep).
- `discard_overruled` вызывается до возврата человеческого вердикта на
  обоих путях — авто-REVIEW удаляется, повода для replan нет (A32 держится).
- Ожидание окна не делает движок «мёртвым»: heartbeat пишет отдельный поток,
  порог устаревания — не меньше 120 с (`awf/_engine_liveness.py:127`),
  окно 60 с укладывается.
- Окно считается по дедлайну (`time.monotonic`), и после последнего сна
  проверка выполняется ещё раз — вердикт, записанный за секунду до конца,
  виден.

## Несвязанные находки

| ID | Важность | Файл | Факт | Следующий шаг / владелец |
|---|---|---|---|---|
| CTX7 | MINOR | `scripts/suite-canonical.sh:192-206` против `:213` | `--force --background` отдаёт кэш: фоновая ветка сначала проверяет `artifact_exit` и выходит, а `--force` передаётся только дочернему процессу. QA-8 назвала это как обход «удалить артефакт»; подтверждаю чтением скрипта. Полный прогон для проверки не запускал. | Перенести проверку `force` выше ветки background (как в foreground). Владелец — implementer/tooling. |

## Ограничения проверки

- Полный канонический сьют не перезапускал (политика роли). Опираюсь на
  артефакт QA на том же дереве `82a2d62…`: 6967 passed, 1415 с.
- Watcher-путь окна покрыт только чтением кода: новые тесты идут через
  fallback (`run_subprocess_until_signal` в них не выставляет
  `signal_holder`). Симметрия путей очевидна (одинаковый вызов), но теста
  на неё нет.
- Исходный инцидент 03.11 не воспроизводил живьём (реальный `awf_approve`
  в реальном забеге). Позитивный путь привязанного approve проверен тестом
  QA на настоящем git-репозитории, а не мной.
- Цифру «~10 с до изменения» для `test_stage_prompt_carries_task` взял из
  суммарного времени файла (77 с на 10 тестов) и 60-секундной паузы в логе,
  а не из прогона на старом дереве.
- Windows не проверял: проект POSIX-only (`killpg`).
