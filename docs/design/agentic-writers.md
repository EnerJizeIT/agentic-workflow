# Писатели и удаляторы под `.agentic` — инвентарь модулей `awf/`

Документ перечисляет **модули `awf/**/*.py`, где есть записи и удаления под
`.agentic`**, и по каждому сканируемому сайту (плюс сайты, названные аудитом)
даёт строку: поверхность, проба, ожидание, вердикт. Строка получает `barrier`,
если сайт доказан тестом отказа; `exception+<причина>`, если запись разрешена
политикой; `unit-0239`, если сайт ещё ждёт перевода на managed-примитив.
Таблица — единственный источник обещаний по сайтам: проза ниже объясняет
решения, но новых сайтов не обещает.

Граница скана: Python-модули `awf/`. Shell-хелперы и тесты вне `awf/`
(например, `scripts/suite-canonical.sh` пишет `SUITE-*.json` под
`.agentic/context`) в таблицу не входят и её полноту не расширяют. Единственная
строка вне скана — `docs/design.md`: это `policy-source` со ссылкой на этот
документ, а не файловая операция. Так же вне таблицы — записи через
managed-открыватели: путь проверяет сам примитив
(`open_managed` — например, `awf/run_state.py` → `.agentic/state/.lock`;
`open_append_log` — например, `awf/signal_watch.py` →
`.agentic/logs/<stage>.log`, им же пишет логгер `awf/_log.py`). Такие сайты
не входят ни в шесть операций скана, ни в строки таблицы: полнота здесь
держится на примитиве, а не на инвентаре.

Машинная проверка считает полноту по модулям **и классам операций**: для
каждого модуля дерева тест определяет, какие из классов `write`, `touch`,
`mkdir`, `unlink`, `move`, `rename`, `rmtree` в нём есть, и требует строку на
каждый присутствующий класс. Класс `write` — это голые `atomic_write_text`,
`write_text`, `write_bytes`; managed-писатель (`managed_write_text`) в него не
входит: его путь держит примитив, и он описан барьерными строками. Класс
`rename` — `Path.rename` и `Path.replace(dst)`; `os.replace` в машинный класс
не входит: внутри примитивов (`_atomic`, `managed_fs`) это их собственная
уборка, а живой сайт вне примитивов один — `plan_checkpoint.py` (consume
решения), он несёт барьерную строку. Так же не считаются managed-открыватели.
Класс строки виден в её компоненте или в выводе пробы; барьерная строка
называет класс в тексте сайта, потому что селектор строк не печатает.
Граница честная: полнота по модулям и классам, а новый сайт уже известного
класса ловится ревью, а не этим тестом.
Отдельными тестами проверяются класс `.touch()` (включая строки `.ready`),
голое `open(..., "w"/"a"/"x"/"+")` (класс «голое создание») и оба сайта
win32-ветки `_lock.py` (`mkdir` и файл блокировки).
`pytest tests/negative/test_design_agentic_writers.py -q` вычисляет список
модулей по операциям скана и отдельно классы каждого модуля, требует строку
на каждый модуль и класс, разбирает таблицу и запускает каждую пробу. Строка, которая перестала совпадать с деревом, роняет
тест.

**Ожидание — литеральный токен, а не обещание.** Колонка `Ожидание` содержит
подстроку, которая обязана присутствовать в выводе пробы символ в символ; она
различает именно этот сайт. Тест проверяет вхождение в stdout, требует
уникальности токенов внутри модуля и краснеет, если подменить ожидание на
токен соседней строки того же модуля.

Если проба матчит несколько строк (общий шаблон вроде `md_path.unlink`), токен
обязан встречаться ровно в одной строке вывода: иначе удаление соседнего сайта
останется зелёным. Строка `outside-surface` зелёная только тогда, когда у
модуля нет прямой записи в `.agentic` — ни default-присваивания пути под
`.agentic`, ни строки с операцией записи и литералом `.agentic` на ней;
нашлась такая — нужна строка, документирующая эту поверхность.

Барьерная строка требует строки `<имя теста> ... PASSED` в выводе селектора:
пропущенный (`SKIPPED`) тест отказа ничего не доказывает и краснит строку.

## Политика платформы

**Fail-closed для всех записей состояния и разрушения, кроме двух названных
исключений: init-bootstrap и housekeeping. `awf init` проходит обычным
атомарным писателем, следующий за ним setup (project-setup form) отказывает
на не-POSIX с человеческим текстом; уборка stale pid (`api/_liveness.py`, A4)
молча пропускает отказ и продолжает — вердикт строки `exception+housekeeping`.**
Ослабленная реализация молча не включается. Источник и полный текст —
[`docs/design.md`](../design.md): строка «Запись состояния — fail-closed» и
абзац «Платформа». Платформенный запрет закрывает путь записи состояния; при
этом прямые вызовы `locked()` (`api/hygiene.py`, `infra/state_store/_lock.py`)
до win32-ветки `_lock.py` доходят — она живая, её вердикт `unit-0239`
(перевод на managed-примитив или явный отказ, не удаление).

Правило листа (юнит 0235, N3): для разрушающей операции проверяются **все**
компоненты пути, включая лист. Симлинк-лист — отказ, даже если `unlink` сам
снял бы ссылку, а не цель.

## Как читать строки

- **`barrier`** — проба это pytest-селектор существующего теста отказа
  (например, симлинк на `.agentic` или на поверхность). Селектор обязан
  проходить, а `Ожидание` — имя теста: прогон `pytest -v` печатает его со
  словом `PASSED`. `grep` рядом с барьером ничего не доказывает и запрещён.
- **`unit-NNNN`** — проба это `grep -n` по живому сайту (файл + шаблон), а
  `Ожидание` — литеральный фрагмент найденной строки с маркером этого сайта
  (выход пробы обязан его содержать). Пропавший сайт или проба, съехавшая на
  соседа, делает строку красной, а номер юнита в вердикте — адрес, где сайт
  переведут на managed-примитив.
  Три подкласса: голый писатель/удалятор (`atomic_write_text`, `write_text`,
  `unlink`, `touch`); managed-писатель без перевода `ManagedPathError` в
  `AwfApiError` (рядом такой перевод уже есть); голый `mkdir(parents=True)`
  после барьера — каталог создаётся вне managed-пути. Строки S1/S2 (service
  pipeline writer, service approve evidence) помечены `red-with-owner`:
  владелец — тот же юнит.
- **`exception+<причина>`** — названное исключение из fail-closed. Проба —
  читающая команда (`grep`, `test`, `ls`), которая показывает поверхность
  исключения; `Ожидание` — тот же литеральный токен поверхности. Сейчас их восемь видов: `outside-surface` (путь вне `.agentic`:
  Desktop, `opencode.json`, `.gitignore` проекта, временный worktree, CLI
  `--out`); `generic-primitive` (`_atomic` — сам примитив атомарной записи);
  `init-bootstrap` (скелет каталогов при `awf init`); `platform-gate`
  (`require_posix` — платформенный отказ, а не проверка пути);
  `housekeeping` (уборка stale pid молча пропускает отказ и продолжает —
  политика сайта A4, код в дизайн-юните не меняется); `policy-source`
  (строка-ссылка на `docs/design.md`); `false-positive` (regex совпал с
  комментарием, файловой операции нет); `test-tooling` (`mutations` пишет
  исходники намеренно — это инструмент мутационной проверки). Список видов
  закрыт: неизвестный вид тест отвергает, опечатка не выключает проверку
  `outside-surface`.

## Инвентарь

| Компонент | Путь (поверхность → корень/лист) | Проба | Ожидание | Вердикт |
|---|---|---|---|---|
| `awf/agent_stage.py` · handoff стадии | `.agentic/handoff/<stage>-<id>.md` | `grep -n "atomic_write_text(out_file" awf/agent_stage.py` | `atomic_write_text(out_file` | `unit-0239` |
| `awf/agent_stage.py` · `mkdir` каталога handoff | `.agentic/handoff/` (каталог) | `grep -n "handoff_dir.mkdir(parents=True" awf/agent_stage.py` | `handoff_dir.mkdir(parents=True` | `unit-0239` |
| `awf/agent_stage.py` · очистка сигналов стадии | `.agentic/outbox/*-<id>.ready` | `tests/negative/test_miniwave_m3d_unlinks.py::test_agent_stage_refuses_symlinked_outbox` | `test_agent_stage_refuses_symlinked_outbox` | `barrier` |
| `awf/agent_stage.py` · `require_posix` горячего пути | `.agentic/**` (платформенный гейт) | `grep -n "managed_fs.require_posix" awf/agent_stage.py` | `managed_fs.require_posix` | `exception+platform-gate: отказывает только на не-POSIX, путь не проверяет` |
| `awf/infra/managed_fs.py` · `managed_write_text` | `.agentic/**` (поверхность → лист, no-follow) | `tests/negative/test_roadmap_r1_safety.py::TestOtherManagedWriters::test_managed_write_text_refuses_symlinked_agentic` | `test_managed_write_text_refuses_symlinked_agentic` | `barrier` |
| `awf/infra/managed_fs.py` · компонент пути: `os.mkdir` | `.agentic/**` (каталог через dir_fd) | `grep -n "os.mkdir(part, 0o700, dir_fd=fd)" awf/infra/managed_fs.py` | `os.mkdir(part, 0o700, dir_fd=fd)` | `exception+generic-primitive: примитив создаёт компонент через dir_fd после no-follow проверки` |
| `awf/infra/managed_fs.py` · временный файл: `os.unlink` | `.agentic/**` (временный файл примитива) | `grep -n "os.unlink(tmp_name, dir_fd=dir_fd)" awf/infra/managed_fs.py` | `os.unlink(tmp_name, dir_fd=dir_fd)` | `exception+generic-primitive: примитив убирает временный файл через dir_fd` |
| `awf/api/_helpers.py` · `managed_write_api_text` (`write_text`) и `refuse_unsafe_surfaces` | `.agentic/**` (поверхность и лист через API) | `tests/negative/test_miniwave_m3c_writers.py::test_barrier_refuses_internal_symlink_component` | `test_barrier_refuses_internal_symlink_component` | `barrier` |
| `awf/api/_background.py` · pid фонового запуска | `.agentic/state/*.pid` | `tests/negative/test_roadmap_r1_safety.py::TestOtherManagedWriters::test_managed_write_text_refuses_symlinked_agentic` | `test_managed_write_text_refuses_symlinked_agentic` | `barrier` |
| `awf/_engine_liveness.py` · heartbeat движка | `.agentic/state/engine.heartbeat` | `grep -n "json.dumps(payload, ensure_ascii=False)" awf/_engine_liveness.py` | `json.dumps(payload, ensure_ascii=False)` | `exception+generic-primitive: запись идёт через managed_write_text (no-follow), отказ деградирует в лог` |
| `awf/_engine_liveness.py` · engine-exit | `.agentic/context/ENGINE-EXIT-<ts>.md` | `grep -n "_managed_write(target" awf/_engine_liveness.py` | `_managed_write(target` | `exception+generic-primitive: запись идёт через managed_write_text (no-follow), отказ деградирует в лог` |
| `awf/api/dashboard.py` · состояние дашборда | `.agentic/state/dashboard_*` | `tests/negative/test_roadmap_r1_safety.py::TestOtherManagedWriters::test_managed_write_text_refuses_symlinked_agentic` | `test_managed_write_text_refuses_symlinked_agentic` | `barrier` |
| `awf/api/dashboard_server.py` · boot-marker: `mkdir` и запись | `.agentic/state/dashboard_boot` | `grep -n -A1 "^    f.parent.mkdir(parents=True, exist_ok=True)" awf/api/dashboard_server.py` | `f.write_text(` | `unit-0239` |
| `awf/api/dashboard_server.py` · port: `mkdir` до записи | `.agentic/state/` (каталог) | `grep -n "^        f.parent.mkdir(parents=True, exist_ok=True)" awf/api/dashboard_server.py` | `f.parent.mkdir` | `unit-0239` |
| `awf/api/dashboard_server.py` · port: запись | `.agentic/state/dashboard_port` | `grep -n "f.write_text(str(port)" awf/api/dashboard_server.py` | `f.write_text(str(port)` | `unit-0239` |
| `awf/api/dispatch.py` · managed-запись задачи | `.agentic/inbox/<id>.md`, `.agentic/inbox/<id>.ready` | `tests/negative/test_roadmap_r1_no_bypass.py::test_dispatch_refuses_symlinked_inbox` | `test_dispatch_refuses_symlinked_inbox` | `barrier` |
| `awf/api/dispatch.py` · preflight `done` до первой записи | `.agentic/done/<id>/` (назначение, до резервации) | `tests/negative/test_miniwave_m3b_movers.py::test_dispatch_refuses_symlinked_done_before_any_write` | `test_dispatch_refuses_symlinked_done_before_any_write` | `barrier` |
| `awf/api/dispatch.py` · managed без перевода `ManagedPathError` (файл задачи) | `.agentic/inbox/<id>.md` | `grep -n "managed_fs.managed_write_text(md_path" awf/api/dispatch.py` | `managed_fs.managed_write_text(md_path` | `unit-0239` |
| `awf/api/dispatch.py` · managed без перевода `ManagedPathError` (.ready) | `.agentic/inbox/<id>.ready` | `grep -n "managed_fs.managed_write_text(ready_path" awf/api/dispatch.py` | `managed_fs.managed_write_text(ready_path` | `unit-0239` |
| `awf/api/dispatch.py` · голый `unlink` при закрытом DONE | `.agentic/inbox/<id>.md` | `grep -n -A2 "^        md_path.unlink(missing_ok=True)" awf/api/dispatch.py` | `outbox has a DONE closure` | `unit-0239` |
| `awf/api/dispatch.py` · голый `unlink` при сбое baseline | `.agentic/inbox/<id>.md` | `grep -n -B1 "^        md_path.unlink(missing_ok=True)" awf/api/dispatch.py` | `if baseline fails (prevents orphan` | `unit-0239` |
| `awf/api/hygiene.py` · managed-запись note (no-follow доказан) | `.agentic/context/RETIRED-*.md` | `tests/negative/test_roadmap_r1_safety.py::TestOtherManagedWriters::test_managed_write_text_refuses_symlinked_agentic` | `test_managed_write_text_refuses_symlinked_agentic` | `barrier` |
| `awf/api/hygiene.py` · baseline `unlink` в remove | `.agentic/context/BASELINE-<id>.*` | `grep -n "(project_dir / rel).unlink()" awf/api/hygiene.py` | `(project_dir / rel).unlink()` | `unit-0239` |
| `awf/api/hygiene.py` · backup `unlink` | `.agentic/context/<id>.md.bak-*` | `grep -n "backup.unlink()" awf/api/hygiene.py` | `backup.unlink()` | `unit-0239` |
| `awf/api/hygiene.py` · перенос закрытий в trace | `.agentic/context/` | `grep -n "shutil.move(str(f), str(trace / f.name))" awf/api/hygiene.py` | `shutil.move(str(f), str(trace / f.name))` | `unit-0239` |
| `awf/api/hygiene.py` · remove: перенос файла задачи | `.agentic/done/<id>/` | `grep -n "shutil.move(str(md), str(trace))" awf/api/hygiene.py` | `shutil.move(str(md), str(trace))` | `unit-0239` |
| `awf/api/hygiene.py` · remove: перенос `.ready` | `.agentic/done/<id>/` | `grep -n "shutil.move(str(ready), str(ready_target))" awf/api/hygiene.py` | `shutil.move(str(ready), str(ready_target))` | `unit-0239` |
| `awf/api/hygiene.py` · archive: перенос закрытий | `.agentic/done/<id>/` | `grep -n "shutil.move(str(src), str(target))" awf/api/hygiene.py` | `shutil.move(str(src), str(target))` | `unit-0239` |
| `awf/api/hygiene.py` · тот же сайт: `ManagedPathError` не переведён | `.agentic/context/RETIRED-*.md` | `grep -n "managed_fs.managed_write_text(" awf/api/hygiene.py` | `managed_fs.managed_write_text(` | `unit-0239` |
| `awf/api/hygiene.py` · `mkdir` trace-каталога | `.agentic/context/unblock-<id>-<ts>/` (каталог) | `grep -n "trace.mkdir(parents=True" awf/api/hygiene.py` | `trace.mkdir(parents=True` | `unit-0239` |
| `awf/api/hygiene.py` · remove: `mkdir` каталога done | `.agentic/done/<id>/` (каталог) | `grep -n -B3 "^        done_dir.mkdir" awf/api/hygiene.py` | `has PROGRESS — it was started` | `unit-0239` |
| `awf/api/hygiene.py` · archive: `mkdir` каталога done | `.agentic/done/<id>/` (каталог) | `grep -n -B4 "^        done_dir.mkdir" awf/api/hygiene.py` | `sorted(outbox.glob(f"REVIEW-{tid}.*")` | `unit-0239` |
| `awf/api/_lease.py` · lease запуска (`_publish_claim`) | `.agentic/logs/awf-launch.lease` | `grep -n "dir_fd = os.open(" awf/api/_lease.py` | `dir_fd = os.open(` | `unit-0239` |
| `awf/api/_lease.py` · `os.link` lease-заявки | `.agentic/logs/awf-launch.lease` (публикация) | `grep -n "os.link($" awf/api/_lease.py` | `os.link(` | `unit-0239` |
| `awf/api/_lease.py` · `os.unlink` | `.agentic/logs/` (lease) | `grep -n "os.unlink(str(path))" awf/api/_lease.py` | `os.unlink(str(path))` | `unit-0239` |
| `awf/api/_lease.py` · `path.unlink` | `.agentic/logs/` (lease) | `grep -n "path.unlink()" awf/api/_lease.py` | `path.unlink()` | `unit-0239` |
| `awf/api/_lease.py` · `mkdir` каталога logs | `.agentic/logs/` (каталог) | `grep -n "logs.mkdir(parents=True" awf/api/_lease.py` | `logs.mkdir(parents=True` | `unit-0239` |
| `awf/api/lifecycle.py` · re-init bootstrap `mkdir` | `.agentic/*` (скелет) | `grep -n -B1 "(agentic / d).mkdir(parents=True" awf/api/lifecycle.py` | `for d in missing:` | `exception+init-bootstrap: скелет создаётся при init, политика платформы это разрешает` |
| `awf/api/lifecycle.py` · fresh-init bootstrap `mkdir` | `.agentic/*` (скелет) | `grep -n -B1 "(agentic / d).mkdir(parents=True" awf/api/lifecycle.py` | `for d in _FRESH_DIRS:` | `exception+init-bootstrap: скелет создаётся при init, политика платформы это разрешает` |
| `awf/api/lifecycle.py` · force-init preflight | `.agentic/**` (перед `rmtree`) | `tests/negative/test_miniwave_m3c_writers.py::test_force_init_refuses_symlinked_agentic` | `test_force_init_refuses_symlinked_agentic` | `barrier` |
| `awf/api/lifecycle.py` · restore-перенос | `.agentic/inbox/` (восстановление задачи) | `grep -n "shutil.move(str(md), str(target))" awf/api/lifecycle.py` | `shutil.move(str(md), str(target))` | `unit-0239` |
| `awf/api/lifecycle.py` · restore `.ready` `touch` | `.agentic/inbox/<id>.ready` | `grep -n '{todo_id}.ready").touch()' awf/api/lifecycle.py` | `{todo_id}.ready").touch()` | `unit-0239` |
| `awf/api/lifecycle.py` · restore `mkdir` inbox | `.agentic/inbox/` (каталог) | `grep -n "inbox.mkdir(parents=True" awf/api/lifecycle.py` | `inbox.mkdir(parents=True` | `unit-0239` |
| `awf/api/lifecycle.py` · `rmdir` пустого done | `.agentic/done/<id>/` | `grep -n "done_dir.rmdir()" awf/api/lifecycle.py` | `done_dir.rmdir()` | `unit-0239` |
| `awf/api/lifecycle.py` · reset runtime: `unlink` файла | `.agentic/*` (файл после preflight) | `grep -n "f.unlink()" awf/api/lifecycle.py` | `f.unlink()` | `unit-0239` |
| `awf/api/lifecycle.py` · reset runtime: `rmtree` каталога | `.agentic/*` (каталог после preflight) | `grep -n "shutil.rmtree(d)" awf/api/lifecycle.py` | `shutil.rmtree(d)` | `unit-0239` |
| `awf/api/lifecycle.py` · bootstrap-писатель вне POSIX | `.agentic/config.yaml`, `.agentic/supervisor.md` | `grep -n "atomic_write_text(path, content, mode=mode)" awf/api/lifecycle.py` | `atomic_write_text(path, content, mode=mode)` | `exception+init-bootstrap: на POSIX идёт через managed_write_api_text, голая ветка — только разрешённый bootstrap на не-POSIX` |
| `awf/api/_liveness.py` · stale pid | `.agentic/logs/*.pid` | `grep -n "pid_file.unlink()" awf/api/_liveness.py` | `pid_file.unlink()` | `exception+housekeeping: молча пропустить отказ и продолжить (A4)` |
| `awf/api/pipeline.py` · RUN-EVIDENCE | `.agentic/context/RUN-EVIDENCE-<id>.md` | `tests/negative/test_miniwave_m3c_writers.py::test_run_evidence_write_refuses_symlinked_context` | `test_run_evidence_write_refuses_symlinked_context` | `barrier` |
| `awf/api/pipeline.py` · baseline (поверхность) | `.agentic/context/BASELINE-<id>.sha` | `tests/negative/test_miniwave_m3d_unlinks.py::test_baseline_refuses_symlinked_context` | `test_baseline_refuses_symlinked_context` | `barrier` |
| `awf/api/pipeline.py` · baseline-файлы (лист) | `.agentic/context/BASELINE-<id>.*` | `grep -n "atomic_write_text(path, content, errors=" awf/api/pipeline.py` | `atomic_write_text(path, content, errors=` | `unit-0239` |
| `awf/api/pipeline.py` · REVIEW-файл: запись | `.agentic/outbox/REVIEW-<id>.md` | `grep -n -A2 "atomic_write_text($" awf/api/pipeline.py` | `review_file, f"# REVIEW` | `unit-0239` |
| `awf/api/pipeline.py` · ACK-сигнал | `.agentic/inbox/ACK-<id>.ready` | `grep -n "atomic_write_text(ack_path" awf/api/pipeline.py` | `atomic_write_text(ack_path` | `unit-0239` |
| `awf/api/pipeline.py` · APPROVE `.ready` `touch` | `.agentic/inbox/APPROVE-<id>.ready` | `grep -n "signal.touch()" awf/api/pipeline.py` | `signal.touch()` | `unit-0239` |
| `awf/api/pipeline.py` · APPROVE-бинд: запись | `.agentic/inbox/APPROVE-<id>.ready` | `grep -n "atomic_write_text(signal, json.dumps(binding" awf/api/pipeline.py` | `atomic_write_text(signal, json.dumps(binding` | `unit-0242` |
| `awf/api/pipeline.py` · ACK `.ready` `touch` | `.agentic/inbox/ACK-<id>.ready` | `grep -n 'ACK-{ack}.ready").touch()' awf/api/pipeline.py` | `ACK-{ack}.ready").touch()` | `unit-0239` |
| `awf/api/pipeline.py` · last-kill запись | `.agentic/state/last-kill.json` | `grep -n "LAST_KILL_FILE, json.dumps(record)" awf/api/pipeline.py` | `LAST_KILL_FILE, json.dumps(record)` | `unit-0239` |
| `awf/api/pipeline.py` · reconcile ACK (A2) | `.agentic/inbox/ACK-*.ready` | `grep -n "ack.unlink()" awf/api/pipeline.py` | `ack.unlink()` | `unit-0239` |
| `awf/api/pipeline.py` · pending closure | `.agentic/inbox/CLOSE-*.ready` | `tests/negative/test_miniwave_m3d_unlinks.py::test_resolve_pending_closure_refuses_symlinked_inbox` | `test_resolve_pending_closure_refuses_symlinked_inbox` | `barrier` |
| `awf/api/pipeline.py` · unblock preflight | `.agentic/outbox/BLOCKED-<id>.{ready,md}` → `.agentic/context/` | `tests/negative/test_miniwave_m3d_unlinks.py::test_unblock_todo_preflights_outbox_and_context` | `test_unblock_todo_preflights_outbox_and_context` | `barrier` |
| `awf/api/pipeline.py` · approve evidence `unlink` | `.agentic/context/RUN-EVIDENCE-<id>.md` | `tests/negative/test_miniwave_m3d_unlinks.py::test_approve_evidence_unlink_is_guarded` | `test_approve_evidence_unlink_is_guarded` | `barrier` |
| `awf/api/pipeline.py` · note «BLOCKED cleared» (A3) | `.agentic/context/NOTE-*.md` | `grep -n "BLOCKED cleared" awf/api/pipeline.py` | `BLOCKED cleared` | `unit-0239` |
| `awf/api/pipeline.py` · `mkdir` context (A1) | `.agentic/context/` (каталог) | `grep -n "context_dir.mkdir(parents=True" awf/api/pipeline.py` | `context_dir.mkdir(parents=True` | `unit-0239` |
| `awf/api/pipeline.py` · `mkdir` inbox (M3c #2) | `.agentic/inbox/` (каталог) | `tests/negative/test_inventory_batch1.py::test_approve_refuses_symlinked_inbox` | `test_approve_refuses_symlinked_inbox` | `barrier` |
| `awf/api/pipeline.py` · `mkdir` ACK-inbox | `.agentic/inbox/` (ACK-поверхность) | `grep -n "ack_inbox.mkdir(parents=True" awf/api/pipeline.py` | `ack_inbox.mkdir(parents=True` | `unit-0239` |
| `awf/api/pipeline.py` · `mkdir` outbox | `.agentic/outbox/` (каталог) | `grep -n "outbox.mkdir(parents=True" awf/api/pipeline.py` | `outbox.mkdir(parents=True` | `unit-0239` |
| `awf/api/pipeline.py` · `mkdir` state | `.agentic/state/` (каталог) | `grep -n "state_dir.mkdir(parents=True" awf/api/pipeline.py` | `state_dir.mkdir(parents=True` | `unit-0239` |
| `awf/api/pipeline.py` · salvage `unlink` | `.agentic/inbox/SALVAGE-*.md` | `grep -n -B2 "^            p.unlink()" awf/api/pipeline.py` | `for p in salvage_notes` | `unit-0239` |
| `awf/api/roles.py` · adopt/discard черновика | `.agentic/roles/draft/<slug>.md` | `tests/negative/test_miniwave_m3d_unlinks.py::test_role_adopt_refuses_symlinked_draft` | `test_role_adopt_refuses_symlinked_draft` | `barrier` |
| `awf/api/run.py` · RUN-REPORT: запись | `.agentic/outbox/RUN-REPORT-<ts>.md` | `tests/negative/test_inventory_batch1.py::test_run_report_write_refuses_symlinked_outbox` | `test_run_report_write_refuses_symlinked_outbox` | `barrier` |
| `awf/api/run.py` · SERVICE-RUN-REPORT: запись | `.agentic/outbox/SERVICE-RUN-REPORT-<ts>.md` | `tests/negative/test_inventory_batch1.py::test_service_run_report_write_refuses_symlinked_outbox` | `test_service_run_report_write_refuses_symlinked_outbox` | `barrier` |
| `awf/api/run.py` · RUN-REPORT `unlink` | `.agentic/outbox/RUN-REPORT-<ts>.md` | `tests/negative/test_inventory_batch1.py::test_run_report_unlink_refuses_symlinked_outbox` | `test_run_report_unlink_refuses_symlinked_outbox` | `barrier` |
| `awf/api/run.py` · `.ready` `unlink` | `.agentic/inbox/<id>.ready` | `tests/negative/test_inventory_batch1.py::test_ready_unlink_refuses_symlinked_inbox` | `test_ready_unlink_refuses_symlinked_inbox` | `barrier` |
| `awf/api/run.py` · service pipeline (S1, red-with-owner) | `.agentic/pipelines/role-draft-<slug>.yaml` | `grep -n "atomic_write_text(out, header" awf/api/run.py` | `atomic_write_text(out, header` | `unit-0239` |
| `awf/api/run.py` · service evidence (S2, red-with-owner): запись | `.agentic/context/RUN-EVIDENCE-<id>.md` | `grep -n -A2 "atomic_write_text($" awf/api/run.py` | `f"# Service run evidence` | `unit-0239` |
| `awf/api/run.py` · `.ready` `touch` | `.agentic/inbox/<id>.ready` | `tests/negative/test_inventory_batch1.py::test_ready_touch_refuses_symlinked_inbox` | `test_ready_touch_refuses_symlinked_inbox` | `barrier` |
| `awf/api/run.py` · service APPROVE `.ready` `touch` | `.agentic/inbox/APPROVE-<id>.ready` | `grep -n "signal.touch()" awf/api/run.py` | `signal.touch()` | `unit-0239` |
| `awf/api/run.py` · service-rollback inbox | `.agentic/inbox/<id>.md`, `.agentic/inbox/<id>.ready` | `grep -n '(inbox / f"{todo_id}{suffix}").unlink()' awf/api/run.py` | `(inbox / f"{todo_id}{suffix}").unlink()` | `unit-0239` |
| `awf/api/run.py` · service-rollback baseline | `.agentic/context/BASELINE-<id>.*` | `grep -n '(ctx / name).unlink()' awf/api/run.py` | `(ctx / name).unlink()` | `unit-0239` |
| `awf/api/run.py` · service-rollback pipeline | `.agentic/pipelines/role-draft-*.yaml` | `grep -n '"pipelines" / f"{pipe_name}.yaml").unlink()' awf/api/run.py` | `"pipelines" / f"{pipe_name}.yaml").unlink()` | `unit-0239` |
| `awf/api/run.py` · `mkdir` pipelines | `.agentic/pipelines/` (каталог) | `grep -n "out.parent.mkdir(parents=True" awf/api/run.py` | `out.parent.mkdir(parents=True` | `unit-0239` |
| `awf/api/run.py` · `mkdir` context | `.agentic/context/` (каталог) | `grep -n "ctx.mkdir(parents=True" awf/api/run.py` | `ctx.mkdir(parents=True` | `unit-0239` |
| `awf/api/run.py` · `mkdir` approve inbox | `.agentic/inbox/` (APPROVE-поверхность) | `grep -n "inbox.mkdir(parents=True" awf/api/run.py` | `inbox.mkdir(parents=True` | `unit-0239` |
| `awf/api/setup.py` · backup конфига | `.agentic/config.yaml.bak`, `.agentic/config.yaml.<ts>.bak` | `tests/negative/test_miniwave_m3c_writers.py::test_config_backup_barrier_refusal_keeps_update` | `test_config_backup_barrier_refusal_keeps_update` | `barrier` |
| `awf/api/setup.py` · ротация backup | `.agentic/config.yaml.*.bak` | `grep -n "old.unlink(missing_ok=True)" awf/api/setup.py` | `old.unlink(missing_ok=True)` | `unit-0239` |
| `awf/api/setup.py` · `rename` pipeline в backup | `.agentic/pipelines/<name>.yaml.bak` | `grep -n -A1 "target.rename(backup)" awf/api/setup.py` | `Backed up existing pipeline to` | `unit-0239` |
| `awf/api/setup.py` · `rename` quick pipeline в backup | `.agentic/pipelines/<name>.yaml.bak` | `grep -n -A1 "target.rename(backup)" awf/api/setup.py` | `Backed up existing quick pipeline to` | `unit-0239` |
| `awf/api/_templates.py` · `.gitignore`: первый блок | `.gitignore` (корень проекта, вне `.agentic`) | `grep -n -A1 "^            gitignore.write_text($" awf/api/_templates.py` | `content + "\n# Agentic workflow runtime files` | `exception+outside-surface: файл в корне проекта, не состояние .agentic` |
| `awf/api/_templates.py` · `.gitignore`: обновление handoff/state | `.gitignore` (корень проекта, вне `.agentic`) | `grep -n -A1 "^            gitignore.write_text($" awf/api/_templates.py` | `content + "\n# Agentic workflow runtime (updated)` | `exception+outside-surface: файл в корне проекта, не состояние .agentic` |
| `awf/api/_templates.py` · `.gitignore`: блок ui | `.gitignore` (корень проекта, вне `.agentic`) | `grep -n -A1 "^            gitignore.write_text($" awf/api/_templates.py` | `content + "\n# agent-workflow-ui runtime` | `exception+outside-surface: файл в корне проекта, не состояние .agentic` |
| `awf/api/_templates.py` · `.gitignore`: файла ещё нет | `.gitignore` (корень проекта, вне `.agentic`) | `grep -n -A1 "^        gitignore.write_text($" awf/api/_templates.py` | `"# Agentic workflow runtime files\n" + gitignore_block` | `exception+outside-surface: файл в корне проекта, не состояние .agentic` |
| `awf/_atomic.py` · `atomic_write_text` | `произвольный путь (примитив)` | `grep -n "def atomic_write_text" awf/_atomic.py` | `def atomic_write_text` | `exception+generic-primitive: сам примитив атомарной записи, цель задаёт вызывающий` |
| `awf/_atomic.py` · родительский каталог примитива (`mkdir`) | `каталог цели (примитив)` | `grep -n "path.parent.mkdir(parents=True, exist_ok=True)" awf/_atomic.py` | `path.parent.mkdir(parents=True, exist_ok=True)` | `exception+generic-primitive: каталог цели создаёт сам примитив атомарной записи` |
| `awf/_atomic.py` · уборка временного файла (`unlink`) | `временный файл рядом с целью` | `grep -n "os.unlink(tmp_path)" awf/_atomic.py` | `os.unlink(tmp_path)` | `exception+generic-primitive: примитив убирает свой временный файл после отказа` |
| `awf/candidate_verify.py` · managed evidence (`write_text`) | `.agentic/context/candidate-*` | `tests/negative/test_roadmap_r1_safety.py::TestOtherManagedWriters::test_managed_write_text_refuses_symlinked_agentic` | `test_managed_write_text_refuses_symlinked_agentic` | `barrier` |
| `awf/candidate_verify.py` · временный worktree и index | `/tmp/opencode/**` (вне проекта) | `grep -n "tempfile.mkdtemp(prefix=" awf/candidate_verify.py` | `tempfile.mkdtemp(prefix=` | `exception+outside-surface: одноразовый worktree и git-index вне проекта` |
| `awf/candidate_verify.py` · worktree: `mkdir` | `/tmp/opencode/**` (вне проекта) | `grep -n "dest.mkdir(parents=True, exist_ok=True)" awf/candidate_verify.py` | `dest.mkdir(parents=True, exist_ok=True)` | `exception+outside-surface: одноразовый worktree вне проекта` |
| `awf/candidate_verify.py` · worktree index: `unlink` | временный git-index (вне проекта) | `grep -n "git read-tree creates the index" awf/candidate_verify.py` | `git read-tree creates the index` | `exception+outside-surface: одноразовый git-index вне проекта` |
| `awf/candidate_verify.py` · worktree: `rmtree` | `/tmp/opencode/**` (вне проекта) | `grep -n "shutil.rmtree(dest, ignore_errors=True)" awf/candidate_verify.py` | `shutil.rmtree(dest, ignore_errors=True)` | `exception+outside-surface: одноразовый worktree вне проекта` |
| `awf/claim_changes.py` · запись claim-плана | `.agentic/context/CLAIM-*` | `grep -n -A1 "^    atomic_write_text($" awf/claim_changes.py` | `path, json.dumps(plan.to_dict()` | `unit-0239` |
| `awf/claim_changes.py` · claim-ссылка | `.agentic/context/BASELINE-<id>.claim` | `grep -n "claim_link_path(project_dir, todo_id), f" awf/claim_changes.py` | `claim_link_path(project_dir, todo_id), f"{source_todo}` | `unit-0239` |
| `awf/claim_changes.py` · снятие claim-ссылки | `.agentic/context/BASELINE-<id>.claim` | `grep -n "claim_link_path(Path(project_dir), todo_id).unlink" awf/claim_changes.py` | `claim_link_path(Path(project_dir), todo_id).unlink` | `unit-0239` |
| `awf/commit_delta.py` · одноразовый git-index | `/tmp/opencode/**` (вне проекта) | `grep -n "tempfile.mkstemp" awf/commit_delta.py` | `tempfile.mkstemp` | `exception+outside-surface: изолированный git-index вне проекта` |
| `awf/commit_delta.py` · index: `unlink` | временный git-index (вне проекта) | `grep -n "os.unlink(index_path)" awf/commit_delta.py` | `os.unlink(index_path)` | `exception+outside-surface: изолированный git-index вне проекта` |
| `awf/commit_gate.py` · managed запись публикации | `.agentic/state/`, `.agentic/context/` | `tests/negative/test_roadmap_r1_safety.py::TestOtherManagedWriters::test_managed_write_text_refuses_symlinked_agentic` | `test_managed_write_text_refuses_symlinked_agentic` | `barrier` |
| `awf/commit_gate.py` · одноразовый git-index | `/tmp/opencode/**` (вне проекта) | `grep -n "tempfile.mkstemp" awf/commit_gate.py` | `tempfile.mkstemp` | `exception+outside-surface: изолированный git-index вне проекта` |
| `awf/commit_gate.py` · index: `unlink` | временный git-index (вне проекта) | `grep -n "os.unlink(index_path)" awf/commit_gate.py` | `os.unlink(index_path)` | `exception+outside-surface: изолированный git-index вне проекта` |
| `awf/doctrine.py` · seed доктрины, POSIX-ветка (managed) | `.agentic/doctrine/*.md` | `tests/negative/test_miniwave_m3c_writers.py::test_init_refuses_symlinked_doctrine` | `test_init_refuses_symlinked_doctrine` | `barrier` |
| `awf/doctrine.py` · seed доктрины вне POSIX: голый `atomic_write_text` | `.agentic/doctrine/*.md` | `grep -n "atomic_write_text(target, text, mode=mode)" awf/doctrine.py` | `atomic_write_text(target, text, mode=mode)` | `exception+init-bootstrap: awf init сеет доктрину обычным атомарным писателем на не-POSIX — та же политика, что lifecycle._bootstrap_write` |
| `awf/doctrine.py` · `DOCTRINE-ASSEMBLED.md` | `.agentic/context/DOCTRINE-ASSEMBLED.md` | `grep -n "atomic_write_text(out, text)" awf/doctrine.py` | `atomic_write_text(out, text)` | `unit-0239` |
| `awf/doctrine.py` · `mkdir` в не-POSIX ветке | `.agentic/doctrine/` (каталог) | `grep -n "dst.mkdir(parents=True" awf/doctrine.py` | `dst.mkdir(parents=True` | `exception+init-bootstrap: каталог сеет awf init на не-POSIX — та же политика, что lifecycle init-bootstrap` |
| `awf/doctrine.py` · `mkdir` context | `.agentic/context/` (каталог) | `grep -n "ctx.mkdir(parents=True" awf/doctrine.py` | `ctx.mkdir(parents=True` | `unit-0239` |
| `awf/feedback.py` · отчёт обратной связи | `~/Desktop` (вне проекта) | `grep -n 'Path.home() / "Desktop"' awf/feedback.py` | `Path.home() / "Desktop"` | `exception+outside-surface: отчёт кладётся на рабочий стол владельца` |
| `awf/feedback.py` · отчёт: `write_text` | `~/Desktop` (вне проекта) | `grep -n "atomic_write_text(dest, report)" awf/feedback.py` | `atomic_write_text(dest, report)` | `exception+outside-surface: отчёт кладётся на рабочий стол владельца` |
| `awf/feedback.py` · отчёт: `mkdir` каталога | `~/Desktop` (вне проекта) | `grep -n "target.mkdir(parents=True, exist_ok=True)" awf/feedback.py` | `target.mkdir(parents=True, exist_ok=True)` | `exception+outside-surface: каталог отчёта на рабочем столе владельца` |
| `awf/include_untracked.py` · audit-ссылка baseline | `.agentic/context/BASELINE-<id>.include` | `grep -n "atomic_write_text(" awf/include_untracked.py` | `atomic_write_text(` | `unit-0239` |
| `awf/include_untracked.py` · снятие audit-ссылки | `.agentic/context/BASELINE-<id>.include` | `grep -n "include_file_path(project_dir, todo_id).unlink" awf/include_untracked.py` | `include_file_path(project_dir, todo_id).unlink` | `unit-0239` |
| `awf/infra/state_store/_io.py` · запись состояния (`write_text`) | `.agentic/state/**` | `tests/negative/test_miniwave_m3_managedfs_round2.py::test_put_record_and_control_refuse_symlinked_state` | `test_put_record_and_control_refuse_symlinked_state` | `barrier` |
| `awf/infra/state_store/_projection.py` · запись (`write_text`) и удаление (`unlink`) проекции | `.agentic/state/*.json` | `tests/negative/roadmap_r2/test_roadmap_r2_publish_qa.py::test_projection_writer_no_follow_barrier` | `test_projection_writer_no_follow_barrier` | `barrier` |
| `awf/infra/state_store/_records.py` · GC записей (`unlink`) | `.agentic/state/records/**` | `tests/negative/test_miniwave_m3b_movers.py::test_gc_records_refuses_symlinked_records_and_keeps_canary` | `test_gc_records_refuses_symlinked_records_and_keeps_canary` | `barrier` |
| `awf/infra/state_store/_wipe.py` · reset состояния (`unlink`/`rmtree`) | `.agentic/state/**` | `tests/negative/test_miniwave_m3_managedfs.py::test_wipe_refuses_symlinked_state_and_keeps_outside_canary` | `test_wipe_refuses_symlinked_state_and_keeps_outside_canary` | `barrier` |
| `awf/_lock.py` · win32 `mkdir` состояния (достижим: прямые `locked()` в `api/hygiene.py`, `infra/state_store/_lock.py`) | `.agentic/state/` (каталог блокировки) | `grep -n "state_dir.mkdir(parents=True, exist_ok=True)" awf/_lock.py` | `state_dir.mkdir(parents=True, exist_ok=True)` | `unit-0239` |
| `awf/_lock.py` · win32 файл блокировки (достижим: прямые `locked()` в `api/hygiene.py`, `infra/state_store/_lock.py`) | `.agentic/state/.lock` | `grep -n 'open(lock_file, "a+b")' awf/_lock.py` | `open(lock_file, "a+b")` | `unit-0239` |
| `awf/_log.py` · ротация лога | `.agentic/logs/*.log` | `grep -n "old.unlink()" awf/_log.py` | `old.unlink()` | `unit-0239` |
| `awf/_log.py` · `rename` лога в архив | `.agentic/logs/*.log` | `grep -n "log_file.rename(archive)" awf/_log.py` | `log_file.rename(archive)` | `unit-0239` |
| `awf/_log.py` · `mkdir` корня логов | `.agentic/` (корень при отсутствии) | `grep -n "root.mkdir(parents=True" awf/_log.py` | `root.mkdir(parents=True` | `unit-0239` |
| `awf/metrics.py` · кэш цен моделей: запись | `.agentic/state/metrics_models_cache.json` | `tests/negative/test_inventory_batch1.py::test_metrics_models_cache_refuses_symlinked_state` | `test_metrics_models_cache_refuses_symlinked_state` | `barrier` |
| `awf/metrics.py` · кэш подписок: запись | `.agentic/state/metrics_subscriptions.json` | `tests/negative/test_inventory_batch1.py::test_metrics_subscriptions_cache_refuses_symlinked_state` | `test_metrics_subscriptions_cache_refuses_symlinked_state` | `barrier` |
| `awf/metrics.py` · отчёт метрик: `write_text` | `~/Desktop` (вне проекта) | `grep -n "out_path.write_text" awf/metrics.py` | `out_path.write_text(` | `exception+outside-surface: отчёт кладётся на рабочий стол владельца` |
| `awf/metrics.py` · `mkdir` зеркала отчёта | `metrics.mirror_dir` (вне проекта) | `grep -n "dest_dir.mkdir(parents=True, exist_ok=True)" awf/metrics.py` | `dest_dir.mkdir(parents=True, exist_ok=True)` | `exception+outside-surface: архивное зеркало вне проекта` |
| `awf/mutations.py` · подмена исходника | `файлы проекта (инструмент мутаций)` | `grep -n "target.write_text(original.replace" awf/mutations.py` | `target.write_text(original.replace(m.find, m.repl, 1)` | `exception+test-tooling: мутационная проверка намеренно пишет исходник и возвращает байты` |
| `awf/mutations.py` · восстановление исходника | `файлы проекта (инструмент мутаций)` | `grep -n "target.write_text(original, encoding" awf/mutations.py` | `target.write_text(original, encoding="utf-8")` | `exception+test-tooling: мутационная проверка намеренно пишет исходник и возвращает байты` |
| `awf/opencode_agents.py` · `opencode.json` (apply) | `opencode.json` (конфиг вне `.agentic`) | `grep -n -A2 "^        atomic_write_text($" awf/opencode_agents.py` | `json.dumps(d, indent=2, ensure_ascii=False),` | `exception+outside-surface: конфиг opencode вне .agentic` |
| `awf/opencode_agents.py` · `opencode.json` (MCP-блок) | `opencode.json` (конфиг вне `.agentic`) | `grep -n -A2 "^    atomic_write_text($" awf/opencode_agents.py` | `ensure_ascii=False) + "\n"` | `exception+outside-surface: конфиг opencode вне .agentic` |
| `awf/orchestrator.py` · `PIPELINE-<id>.yaml` | `.agentic/context/PIPELINE-<id>.yaml` | `grep -n "atomic_write_text(_pipeline_snapshot_file" awf/orchestrator.py` | `atomic_write_text(_pipeline_snapshot_file` | `unit-0239` |
| `awf/orchestrator.py` · `mkdir` каталогов запуска | `.agentic/*` (каталоги) | `grep -n "d.mkdir(parents=True" awf/orchestrator.py` | `d.mkdir(parents=True` | `unit-0239` |
| `awf/_path_link.py` · совпадение в комментарии | `файловых операций нет` | `grep -n "atomic_write_text" awf/_path_link.py` | `atomic_write_text` | `exception+false-positive: regex совпал с комментарием, записи нет` |
| `awf/pipeline_engine.py` · GATES крэш-фолбэк | `.agentic/context/GATES-<id>.md` | `grep -n "report.write_text(" awf/pipeline_engine.py` | `report.write_text(` | `unit-0239` |
| `awf/pipeline_engine.py` · BASELINE sha | `.agentic/context/BASELINE-<id>.sha` | `grep -n "atomic_write_text(sha_file" awf/pipeline_engine.py` | `atomic_write_text(sha_file` | `unit-0239` |
| `awf/pipeline_engine.py` · SALVAGE-заметка | `.agentic/inbox/SALVAGE-<id>.md` | `tests/negative/test_miniwave_m3d_unlinks.py::test_salvage_notes_refuse_symlinked_inbox` | `test_salvage_notes_refuse_symlinked_inbox` | `barrier` |
| `awf/pipeline_engine.py` · consume verify decision | `.agentic/inbox/{ACK,APPROVE}-<id>.ready` | `tests/negative/test_miniwave_m3d_unlinks.py::test_engine_verify_decision_refuses_symlinked_inbox` | `test_engine_verify_decision_refuses_symlinked_inbox` | `barrier` |
| `awf/pipeline_engine.py` · consume stage signal | `.agentic/outbox/DONE-<id>.ready` | `tests/negative/test_miniwave_m3d_unlinks.py::test_engine_transition_refuses_symlinked_outbox` | `test_engine_transition_refuses_symlinked_outbox` | `barrier` |
| `awf/pipeline_engine.py` · unsafe stage stop | `.agentic/state/*`, `.agentic/inbox/SALVAGE-<id>.md` | `tests/negative/test_miniwave_m3d_unlinks.py::test_engine_unsafe_stage_stop_is_logged_and_salvaged` | `test_engine_unsafe_stage_stop_is_logged_and_salvaged` | `barrier` |
| `awf/pipeline_engine.py` · `mkdir` context | `.agentic/context/` (каталог) | `tests/negative/test_inventory_batch1.py::test_unblock_todo_refuses_context_swapped_after_preflight` | `test_unblock_todo_refuses_context_swapped_after_preflight` | `barrier` |
| `awf/pipeline_engine.py` · перенос BLOCKED-закрытий | `.agentic/context/BLOCKED-<id>.{md,ready}` | `grep -n "src.replace(dst)" awf/pipeline_engine.py` | `src.replace(dst)` | `unit-0239` |
| `awf/pipeline_engine.py` · хвостовой `unlink` | `.agentic/inbox/`, `.agentic/outbox/` | `grep -n "p.unlink()" awf/pipeline_engine.py` | `p.unlink()` | `unit-0239` |
| `awf/paths.py` · общая temp-база: `mkdir` | `/tmp/opencode/**` (вне проекта) | `grep -n "base.mkdir(parents=True, exist_ok=True)" awf/paths.py` | `base.mkdir(parents=True, exist_ok=True)` | `exception+outside-surface: общая база временных файлов awf вне проекта` |
| `awf/plan_checkpoint.py` · решение и кэш | `.agentic/context/CHECKPOINT-*`, `.agentic/context/PLAN-*.hash` | `tests/negative/test_miniwave_m3c_writers.py::test_checkpoint_hash_cache_refuses_symlinked_context` | `test_checkpoint_hash_cache_refuses_symlinked_context` | `barrier` |
| `awf/plan_checkpoint.py` · consume решения (os.replace) | `.agentic/context/CHECKPOINT-DECISION-*.json` | `tests/negative/test_miniwave_m3d_unlinks.py::test_checkpoint_consume_refuses_symlinked_context` | `test_checkpoint_consume_refuses_symlinked_context` | `barrier` |
| `awf/plan_checkpoint.py` · уборка stale решения | `.agentic/context/CHECKPOINT-*` | `grep -n "Path(stale).unlink()" awf/plan_checkpoint.py` | `Path(stale).unlink()` | `unit-0239` |
| `awf/plan_checkpoint.py` · временная HTML-форма | `временный каталог (вне .agentic)` | `grep -n "Path(html_path).unlink()" awf/plan_checkpoint.py` | `Path(html_path).unlink()` | `exception+outside-surface: временный HTML формы лежит вне .agentic` |
| `awf/plan_progress.py` · PROGRESS-заметка | `.agentic/outbox/PROGRESS-<id>.md` | `tests/negative/test_roadmap_r1_safety.py::TestOtherManagedWriters::test_managed_write_text_refuses_symlinked_agentic` | `test_managed_write_text_refuses_symlinked_agentic` | `barrier` |
| `awf/prove_red.py` · одноразовый worktree | `/tmp/opencode/**` (вне проекта) | `grep -n "tempfile.mkdtemp" awf/prove_red.py` | `tempfile.mkdtemp` | `exception+outside-surface: baseline-прогон идёт в одноразовом worktree` |
| `awf/prove_red.py` · worktree: `mkdir` | `/tmp/opencode/**` (вне проекта) | `grep -n "base.mkdir(parents=True, exist_ok=True)" awf/prove_red.py` | `base.mkdir(parents=True, exist_ok=True)` | `exception+outside-surface: каталог baseline-прогона вне проекта` |
| `awf/prove_red.py` · worktree: `rmtree` | `/tmp/opencode/**` (вне проекта) | `grep -n -B1 "shutil.rmtree(worktree, ignore_errors=True)" awf/prove_red.py` | `if r.returncode != 0:` | `exception+outside-surface: уборка одноразового worktree вне проекта` |
| `awf/prove_red.py` · bootstrap: `write_text` | `/tmp/opencode/**` (вне проекта) | `grep -n "bootstrap.write_text(_BOOTSTRAP_SOURCE" awf/prove_red.py` | `bootstrap.write_text(_BOOTSTRAP_SOURCE` | `exception+outside-surface: bootstrap-файл baseline-прогона вне проекта` |
| `awf/publish.py` · факты публикации | `.agentic/state/`, `.agentic/context/` | `tests/negative/roadmap_r2/test_roadmap_r2_publish.py::test_managed_refusal_on_publish_path_is_a_typed_outcome` | `test_managed_refusal_on_publish_path_is_a_typed_outcome` | `barrier` |
| `awf/publish.py` · удаление факта | `.agentic/state/*`, `.agentic/context/*` | `tests/negative/roadmap_r2/test_roadmap_r2_publish.py::test_projection_remove_no_follow_barrier` | `test_projection_remove_no_follow_barrier` | `barrier` |
| `awf/reject_files.py` · carry-over-ссылка | `.agentic/context/BASELINE-<id>.carry_over` | `grep -n "atomic_write_text(" awf/reject_files.py` | `atomic_write_text(` | `unit-0239` |
| `awf/reject_files.py` · снятие carry-over-ссылки (`unlink`) | `.agentic/context/BASELINE-<id>.carry_over` | `grep -n "carry_over_link_path(Path(project_dir), todo_id).unlink" awf/reject_files.py` | `carry_over_link_path(Path(project_dir), todo_id).unlink` | `unit-0239` |
| `awf/reject_files.py` · REJECT-файл | `.agentic/context/REJECT-<id>.files` | `grep -n "reject_files_path(project_dir, todo_id).write_text(" awf/reject_files.py` | `reject_files_path(project_dir, todo_id).write_text(` | `unit-0239` |
| `awf/signals.py` · `clean_stage_signals` (правило N3) | `.agentic/outbox/*-<id>.ready` | `tests/negative/test_miniwave_m3d_unlinks.py::test_clean_stage_signals_refuses_symlink_leaf` | `test_clean_stage_signals_refuses_symlink_leaf` | `barrier` |
| `awf/todo_draft.py` · `--out` черновика | `путь, выбранный пользователем (вне .agentic)` | `grep -n "out_path.write_text(draft" awf/todo_draft.py` | `out_path.write_text(draft` | `exception+outside-surface: CLI пишет туда, куда указал пользователь` |
| `awf/todo_draft.py` · `mkdir` каталога `--out` | `путь, выбранный пользователем (вне .agentic)` | `grep -n "out_path.parent.mkdir(parents=True, exist_ok=True)" awf/todo_draft.py` | `out_path.parent.mkdir(parents=True, exist_ok=True)` | `exception+outside-surface: каталог выбирает пользователь` |
| `awf/todos.py` · архивация задачи | `.agentic/done/<id>`, `.agentic/inbox`, `.agentic/outbox` | `tests/negative/test_miniwave_m3b_movers.py::test_archive_todo_refuses_symlinked_destination_leaf` | `test_archive_todo_refuses_symlinked_destination_leaf` | `barrier` |
| `awf/todos.py` · `mkdir` каталога архива | `.agentic/done/<id>` (каталог) | `grep -n "dest.mkdir(parents=True" awf/todos.py` | `dest.mkdir(parents=True` | `unit-0239` |
| `awf/todos.py` · потребление сигналов inbox | `.agentic/inbox/*.ready` | `grep -n "p.unlink()" awf/todos.py` | `p.unlink()` | `unit-0239` |
| `awf/todos.py` · перенос в архив | `.agentic/done/<id>` | `grep -n "shutil.move(str(winner), str(dst))" awf/todos.py` | `shutil.move(str(winner), str(dst))` | `unit-0239` |
| `awf/todos.py` · `mkdir` каталога архива (dst_dir) | `.agentic/done/<id>/` (каталог) | `grep -n "dst_dir.mkdir(parents=True" awf/todos.py` | `dst_dir.mkdir(parents=True` | `unit-0239` |
| `awf/todos.py` · `rmdir` пустого архива | `.agentic/done/<id>/` | `grep -n "dest.rmdir()" awf/todos.py` | `dest.rmdir()` | `unit-0239` |
| `awf/verify_pack.py` · GATES в дереве | `.agentic/context/GATES-<id>.md` | `tests/negative/test_roadmap_r1_safety.py::TestOtherManagedWriters::test_managed_write_text_refuses_symlinked_agentic` | `test_managed_write_text_refuses_symlinked_agentic` | `barrier` |
| `awf/verify_pack.py` · `out=` вне проекта | `путь, выбранный пользователем (вне .agentic)` | `grep -n "report_path.relative_to(project)" awf/verify_pack.py` | `report_path.relative_to(project)` | `exception+outside-surface: явный out= — выбор вызывающего` |
| `awf/verify_pack.py` · `mkdir` при `out=` вне проекта | `путь, выбранный пользователем (вне .agentic)` | `grep -n "report_path.parent.mkdir(parents=True" awf/verify_pack.py` | `report_path.parent.mkdir(parents=True` | `exception+outside-surface: каталог отчёта создаётся только для явного out= вне проекта` |
| `awf/verify.py` · лог verify | `.agentic/logs/` | `tests/negative/test_roadmap_r1_safety.py::TestOtherManagedWriters::test_managed_write_text_refuses_symlinked_agentic` | `test_managed_write_text_refuses_symlinked_agentic` | `barrier` |
| `awf/verify.py` · DONE-"синтез" | `.agentic/outbox/DONE-<id>.md` | `grep -n "atomic_write_text(done_md" awf/verify.py` | `atomic_write_text(done_md` | `unit-0239` |
| `awf/verify.py` · синтезированный `.ready` `touch` | `.agentic/outbox/DONE-<id>.ready` | `grep -n 'DONE-{todo_id}.ready").touch()' awf/verify.py` | `DONE-{todo_id}.ready").touch()` | `unit-0239` |
| `docs/design.md` · политика ссылается на инвентарь | `docs/design.md` (политика) | `grep -n "Запись состояния — fail-closed" docs/design.md` | `agentic-writers.md` | `exception+policy-source: документ-источник политики, не операция записи` |
| `awf/cmd_blocked_draft.py` · `--out` внутри `.agentic`: отказ | `.agentic/**` (симлинки разрешаются) | `tests/negative/test_miniwave_m5_sourceref.py::test_c_blocked_draft_out_inside_agentic_refused` | `test_c_blocked_draft_out_inside_agentic_refused` | `barrier` |
| `awf/cmd_blocked_draft.py` · `--out`: `write_text` | `--out <файл>` (вне `.agentic`) | `grep -n "write_text(result.review_text" awf/cmd_blocked_draft.py` | `write_text(result.review_text` | `exception+outside-surface: пишет только в явно переданный --out; путь внутри .agentic отвергается` |

## Формат пробы

Барьерная строка: проба — pytest-селектор `tests/...py::test_имя` (для теста
внутри класса — `tests/...py::TestКласс::test_имя`). Тест-обёртка запускает
его через текущий интерпретатор (`python -m pytest -v`) и требует код возврата
0 и имя теста со словом `PASSED` в выводе — имя и есть `Ожидание`. `grep` в
барьерной строке запрещён: он доказывает текст, а не отказ. Селектор теста
примитива `managed_write_text` допустим только для модуля, который сам
вызывает `managed_write_text(...)`; иначе это проба соседней поверхности, и
тест краснеет.

Строка `unit-NNNN`: проба — `grep -n` по живому сайту (обязательно файл
`.py`), `Ожидание` — литеральный фрагмент найденной строки с маркером сайта.
Если строка заявляет файловую операцию, проба попадает в саму строку
операции (`atomic_write_text(...)`, `p.unlink()`), а не в соседнюю строку
построения пути: иначе удаление писателя оставит строку зелёной. Тест это
проверяет; исключение — строка A3 про текст note, файловой операции она не
заявляет. Пропавший сайт и проба, съехавшая на соседа, краснят строку;
номер юнита — адрес будущей правки. Токены разных строк одного модуля не
совпадают — тест это проверяет.

Строка `exception+<причина>`: проба — `grep`, `test` или `ls`, `Ожидание` —
литеральный токен поверхности исключения. Для `outside-surface` проба
показывает поверхность вне `.agentic` (Desktop, конфиг, временный каталог);
для остальных причин — сайт исключения и его границу.

Разрешены только читающие команды: `grep`, `test`, `ls`. `git` и `python3`
запрещены намеренно — обе умеют писать (`git checkout`, `python3 -c
"open(...,'w')"`), а проба не имеет права менять дерево. Метасимволы `>`,
`<`, `|`, `&` (включая `&&`), `;`, `$(` и обратные кавычки запрещены.

## Что меняет следующий юнит

Юнит 0239 читает таблицу сверху вниз и переводит каждую строку с вердиктом
`unit-0239` на managed-примитив. Отдельно решаются вопросы, которые дизайн
только фиксирует:

- **A1 (mkdir после барьера).** Голые `mkdir(parents=True)` на
  managed-поверхности — все строки таблицы с `mkdir` и вердиктом `unit-0239`;
  перечень здесь не повторяется, таблица — единственный источник. Нужен
  managed-примитив создания каталога; его проектирование входит в границы
  `unit-0239` (там же перевод этих строк), отдельного carry в
  `.agentic/context/CARRIES.md` нет — T3 относится к `unlink`/`move`.
  Исключение — `init`-bootstrap:
  политика разрешает обычный `mkdir` в `api/lifecycle.py` и в не-POSIX ветке
  `awf/doctrine.py` (обе строки — `exception+init-bootstrap`).
- **`_lock.py` win32 — мёртвый резерв только в пути записи состояния.**
  `run_state._ensure_lock_file` отказывает до `locked()`, но прямые вызовы
  `locked()` (`api/hygiene.py`,
  `infra/state_store/_lock.py::project_lock`) входят в win32-ветку и создают
  `.agentic/state/.lock` голыми `mkdir` + `open` до любого POSIX-отказа. Это
  нарушение fail-closed, а не разрешённое исключение: обе строки несут
  `unit-0239` — ветку переводят на managed-примитив или отказывают явно;
  удаление не выбирается.
- **A2/A3 (reconcile и note).** `_reconcile` проверяет листья внутри цикла
  удаления (A2); note пишет «BLOCKED cleared» после best-effort unblock без
  сверки исхода (A3). Оба места получают адрес в 0239.
- **A4 (два ответа на уборку).** `api/_liveness.py` молча пропускает отказ
  stale pid, `api/pipeline.py` в том же классе бросает `AwfApiError`. Политика
  по сайту зафиксирована здесь (строка `housekeeping`), код не меняется.
- **Live-дыры вне прошлых границ.** `api/run.py` service-rollback (inbox,
  BASELINE-артефакты, сгенерированный pipeline), `api/hygiene.py`
  remove_todo/backup, `api/lifecycle.py` restore-todo, `api/todos.py`
  archive/todos, `api/dashboard_server.py` state-маркеры, `awf/_log.py`
  ротация — все вошли строками `unit-0239`, чтобы инвентарь не обещал
  полноты, которой нет.
- **Переносы и удаления каталогов.** `rename`/`replace` (`api/setup.py`,
  `pipeline_engine.py`, `_log.py`) и `rmdir` (`todos.py`, `api/lifecycle.py`)
  тоже строки `unit-0239`: машинный скан эти классы не считает, но политика
  fail-closed распространяется и на них.
