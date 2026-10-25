"""R1.3 (roadmap R1): StateStore — единый владелец состояния, CAS, миграция.

На исходной версии файл КРАСНЫЙ: модуля ``awf.infra.state_store`` нет;
импорт ленивый, поэтому падают утверждения, а не сбор.
Строки приёмки R1.3: crash до/после публикации (recovery, без двойного
эффекта); битый/новее control.json (отказ, без отката к пустому state);
миграция legacy и повтор (no_change, исходники на месте); stale reconcile
после нового поколения (snapshot не изменён). Плюс lock/CAS на двух
реальных процессах и матрица reservations.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _store():
    """Ленивый импорт: на базовом дереве символа нет. Провал — утверждение
    «модуля нет», а не import-ошибка: так baseline даёт настоящий красный,
    а не broken-runner."""
    try:
        from awf.infra import state_store
    except ImportError as e:
        raise AssertionError(f"R1.3 StateStore module is missing: {e}") from e
    return state_store


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".agentic" / "state").mkdir(parents=True)
    return proj


# ─── snapshot: missing | corrupt | unsupported ───────────────────────────


def test_missing_snapshot_is_not_corrupt(tmp_path):
    ss = _store()
    proj = _project(tmp_path)
    snap = ss.read_snapshot(proj)
    assert snap.status == "missing"
    assert snap.revision == 0
    assert not ss.control_path(proj).exists()


def test_corrupt_snapshot_refuses_mutation_without_fallback(tmp_path):
    ss = _store()
    proj = _project(tmp_path)
    ss.control_path(proj).write_text("{ this is not json", encoding="utf-8")

    snap = ss.read_snapshot(proj)
    assert snap.status == "corrupt"

    with pytest.raises(ss.StateCorrupt):
        ss.transact(proj, expected_revision=0, operation_id="op-c", mutate=lambda s: s)

    # не откатились к пустому state и не переписали битый файл
    assert ss.control_path(proj).read_text(encoding="utf-8") == "{ this is not json"


def test_newer_schema_refused(tmp_path):
    ss = _store()
    proj = _project(tmp_path)
    ss.control_path(proj).write_text(
        json.dumps({"schema_version": ss.SCHEMA_VERSION + 1, "revision": 5}),
        encoding="utf-8",
    )
    assert ss.read_snapshot(proj).status == "unsupported"
    with pytest.raises(ss.StateUnsupported) as ei:
        ss.transact(proj, expected_revision=0, operation_id="op-v", mutate=lambda s: s)
    assert ei.value.outcome == "refused"


# ─── CAS на двух реальных процессах ──────────────────────────────────────


def test_cas_two_processes_exactly_one_winner(tmp_path):
    ss = _store()
    proj = _project(tmp_path)
    go = tmp_path / "go"
    script = (
        "import json,sys,time\n"
        "from pathlib import Path\n"
        "from awf.infra import state_store as ss\n"
        "proj=Path(sys.argv[1]); go=Path(sys.argv[2])\n"
        "while not go.exists():\n"
        "    time.sleep(0.005)\n"
        "try:\n"
        "    out=ss.transact(proj, expected_revision=0, operation_id='child-op',\n"
        "                    mutate=lambda s: s)\n"
        "    print(json.dumps({'status':'applied','revision':out['revision']}))\n"
        "except ss.StateStoreError as e:\n"
        "    print(json.dumps({'status':e.outcome}))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(proj), str(go)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(REPO_ROOT),
    )
    try:
        baseline = ss.read_snapshot(proj)
        assert baseline.revision == 0
        go.write_text("go", encoding="utf-8")
        try:
            parent = ss.transact(proj, expected_revision=0, operation_id="parent-op", mutate=lambda s: s)
            parent_status = "applied"
        except ss.StateStoreError as e:
            parent_status = e.outcome
        out, err = child.communicate(timeout=30)
        assert child.returncode == 0, err
        child_status = json.loads(out.strip().splitlines()[-1])["status"]
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()

    assert sorted([parent_status, child_status]) == ["applied", "stale_operation"]
    assert ss.read_snapshot(proj).revision == 1


# ─── operation journal: replay / conflict / recovery ─────────────────────


def test_operation_replay_is_idempotent_and_conflict_refused(tmp_path):
    ss = _store()
    proj = _project(tmp_path)
    ss.transact(proj, expected_revision=0, operation_id="op-a", mutate=lambda s: {**s, "counters": {"done": 1}}, args={"n": 1})
    replay = ss.transact(proj, expected_revision=0, operation_id="op-a", mutate=lambda s: {**s, "counters": {"done": 99}}, args={"n": 1})
    assert replay["replayed"] is True
    assert replay["revision"] == 1
    assert ss.read_snapshot(proj).data["counters"]["done"] == 1

    with pytest.raises(ss.OperationConflict):
        ss.transact(proj, expected_revision=1, operation_id="op-a", mutate=lambda s: s, args={"n": 2})


def test_reused_operation_id_with_omitted_args_is_refused(tmp_path):
    """TODO-0217: a reused id whose retry OMITS ``args`` is refused, not
    replayed (the recorded digest is non-None, the retry's is None)."""
    ss = _store()
    proj = _project(tmp_path)
    ss.transact(proj, expected_revision=0, operation_id="op-reuse",
                mutate=lambda s: {**s, "counters": {"done": 1}}, args={"n": 1})

    with pytest.raises(ss.OperationConflict):
        ss.transact(proj, expected_revision=1, operation_id="op-reuse",
                    mutate=lambda s: {**s, "counters": {"done": 99}})


def test_reused_operation_id_with_added_args_is_refused(tmp_path):
    """TODO-0217, inverse: first call OMITS args, retry ADDS them — conflict.
    An honest None-vs-None replay stays valid (asserted at the end)."""
    ss = _store()
    proj = _project(tmp_path)
    ss.transact(proj, expected_revision=0, operation_id="op-add",
                mutate=lambda s: {**s, "counters": {"done": 1}})

    with pytest.raises(ss.OperationConflict):
        ss.transact(proj, expected_revision=1, operation_id="op-add",
                    mutate=lambda s: {**s, "counters": {"done": 99}}, args={"n": 1})

    # None vs None (both calls omit args) — an honest replay, not a conflict
    ss.transact(proj, expected_revision=1, operation_id="op-noargs", mutate=lambda s: s)
    replay = ss.transact(proj, expected_revision=1, operation_id="op-noargs", mutate=lambda s: s)
    assert replay["replayed"] is True


def test_conflicting_reuse_leaves_journal_and_snapshot_intact(tmp_path):
    """The args guard fires BEFORE the ``reserved`` write: a conflicting
    retry leaves both the snapshot and the recorded outcome untouched."""
    ss = _store()
    proj = _project(tmp_path)
    ss.transact(proj, expected_revision=0, operation_id="op-keep",
                mutate=lambda s: {**s, "counters": {"done": 1}}, args={"n": 1})

    with pytest.raises(ss.OperationConflict):
        ss.transact(proj, expected_revision=1, operation_id="op-keep",
                    mutate=lambda s: {**s, "counters": {"done": 99}}, args={"n": 2})

    snap = ss.read_snapshot(proj)
    assert snap.revision == 1
    assert snap.data["counters"]["done"] == 1
    assert ss.read_operation(proj, "op-keep")["status"] == "applied"

    replay = ss.transact(proj, expected_revision=1, operation_id="op-keep",
                         mutate=lambda s: s, args={"n": 1})
    assert replay["replayed"] is True and replay["revision"] == 1


def test_crash_before_publish_requires_recovery(tmp_path):
    ss = _store()
    proj = _project(tmp_path)
    # симулируем: запись reserved сделана, публикации не было
    ss.write_operation(proj, "op-crash", status="reserved", revision_before=0)
    with pytest.raises(ss.OperationRecoveryRequired):
        ss.transact(proj, expected_revision=0, operation_id="op-crash", mutate=lambda s: s)
    # двойного эффекта нет: snapshot так и не появился
    assert ss.read_snapshot(proj).status == "missing"


def test_crash_after_publish_recovers_without_double_effect(tmp_path, monkeypatch):
    ss = _store()
    from awf.infra.state_store import _transact as tx

    proj = _project(tmp_path)
    real = tx.write_operation

    def flaky(project_dir, operation_id, **fields):
        if fields.get("status") == "applied":
            raise OSError("simulated disk failure after publish")
        return real(project_dir, operation_id, **fields)

    monkeypatch.setattr(tx, "write_operation", flaky)
    with pytest.raises(ss.OperationRecoveryRequired):
        ss.transact(proj, expected_revision=0, operation_id="op-after",
                    mutate=lambda s: {**s, "counters": {"done": 7}})

    snap = ss.read_snapshot(proj)
    assert snap.valid and snap.revision == 1
    assert snap.data["counters"]["done"] == 7  # эффект лёг

    # повтор отвергается (reserved без finish) — второй раз эффект не применяется
    with pytest.raises(ss.OperationRecoveryRequired):
        ss.transact(proj, expected_revision=1, operation_id="op-after",
                    mutate=lambda s: {**s, "counters": {"done": 99}})
    assert ss.read_snapshot(proj).data["counters"]["done"] == 7


def test_crash_after_publish_with_non_oserror_recovers(tmp_path, monkeypatch):
    """T2b: the finish-write contract must not rest on ``except OSError``.

    The finish record is written AFTER the snapshot is published, so ANY
    failure there — not only OSError — leaves the effect landed but
    unrecorded: recovery_required, never a raw escape. RED before the fix.
    """
    ss = _store()
    from awf.infra.state_store import _transact as tx
    proj = _project(tmp_path)
    real = tx.write_operation

    def flaky(project_dir, operation_id, **fields):
        if fields.get("status") == "applied":
            raise RuntimeError("simulated non-OSError after publish")
        return real(project_dir, operation_id, **fields)

    monkeypatch.setattr(tx, "write_operation", flaky)
    with pytest.raises(ss.OperationRecoveryRequired):
        ss.transact(proj, expected_revision=0, operation_id="op-nonos",
                    mutate=lambda s: {**s, "counters": {"done": 5}})
    assert ss.read_snapshot(proj).data["counters"]["done"] == 5  # эффект лёг
    with pytest.raises(ss.OperationRecoveryRequired):  # reserved без finish
        ss.transact(proj, expected_revision=1, operation_id="op-nonos",
                    mutate=lambda s: {**s, "counters": {"done": 99}})
    assert ss.read_snapshot(proj).data["counters"]["done"] == 5


# ─── immutable records ───────────────────────────────────────────────────


def test_records_are_digest_addressed_and_gc_by_reachability(tmp_path):
    ss = _store()
    proj = _project(tmp_path)
    d1 = ss.put_record(proj, "evidence", {"a": 1})
    assert d1 == ss.put_record(proj, "evidence", {"a": 1})
    assert ss.get_record(proj, d1, "evidence") == {"a": 1}

    (ss.records_dir(proj) / "evidence" / f"{d1}.json").write_text('{"a": 2}', encoding="utf-8")
    assert ss.get_record(proj, d1, "evidence") is None  # подмена обнаружена

    ss.transact(proj, expected_revision=0, operation_id="op-rec",
                mutate=lambda s: s, records={"evidence": {"a": 1}})
    orphan = ss.put_record(proj, "findings", {"unreachable": True})
    removed = ss.gc_records(proj, dry_run=True)
    assert any(orphan in p for p in removed)
    assert not any(d1 in p for p in removed)
    ss.gc_records(proj, dry_run=False)
    assert ss.get_record(proj, orphan, "findings") is None


def test_record_kind_traversal_is_refused(tmp_path):
    """B: ``kind`` is public input used verbatim as a path component; a
    traversal kind must be refused and no file may land outside records/."""
    ss = _store()
    proj = _project(tmp_path)

    with pytest.raises(ss.StateStoreError):
        ss.put_record(proj, "../../evil", {"x": 1})
    assert not (proj / ".agentic" / "evil").exists()

    ss.put_record(proj, "evidence", {"a": 1})
    with pytest.raises(ss.StateStoreError):
        ss.get_record(proj, ss.payload_digest({"a": 1}), "../../evil")

    with pytest.raises(ss.StateStoreError):
        ss.transact(proj, expected_revision=0, operation_id="op-badkind",
                    mutate=lambda s: s, records={"../escape": {"x": 1}})
    assert not (proj / ".agentic" / "escape").exists()
    assert ss.read_snapshot(proj).status == "missing"


# ─── reservations matrix ─────────────────────────────────────────────────


def test_reservations_matrix_and_ownership(tmp_path):
    ss = _store()
    proj = _project(tmp_path)
    ss.reserve(proj, "worker", "w1", operation_id="res-1", expected_revision=0)
    data = ss.read_snapshot(proj).data

    for action in ("write", "start", "reset"):
        assert not ss.action_permitted(data, action)
        with pytest.raises(ss.ReservationDenied):
            ss.check_action(proj, action)
    for action in ("read", "heartbeat", "stop", "recovery"):
        assert ss.action_permitted(data, action)
    ss.check_action(proj, "read")  # не блокируется собственным worker'ом

    with pytest.raises(ss.ReservationDenied):
        ss.reserve(proj, "writer", "w2", operation_id="res-2", expected_revision=1)
    with pytest.raises(ss.ReservationDenied):
        ss.release_reservation(proj, "worker", owner="w2", operation_id="res-3", expected_revision=1)

    with pytest.raises(ss.StateStoreError):
        ss.takeover_reservation(proj, "worker", new_owner="w3", evidence="", operation_id="res-4", expected_revision=1)
    ss.takeover_reservation(proj, "worker", new_owner="w3", evidence="pid 123 gone", operation_id="res-5", expected_revision=1)
    assert ss.read_snapshot(proj).data["reservations"]["worker"]["owner"] == "w3"

    ss.release_reservation(proj, "worker", owner="w3", operation_id="res-6", expected_revision=2)
    assert ss.action_permitted(ss.read_snapshot(proj).data, "write")


def test_lock_is_not_reentrant(tmp_path):
    ss = _store()
    proj = _project(tmp_path)
    with ss.project_lock(proj):
        with pytest.raises(ss.StateStoreError):
            with ss.project_lock(proj):
                pass


def test_release_unheld_reservation_is_no_change(tmp_path):
    """F3: releasing a kind nobody holds is a no-op — the revision must not
    move and no ``applied`` journal line is written. RED before the fix (the
    old code published revision+1 and journaled an ``applied`` for a no-op,
    so a concurrent CAS saw a phantom StaleState)."""
    ss = _store()
    proj = _project(tmp_path)
    ss.transact(proj, expected_revision=0, operation_id="seed", mutate=lambda s: s)
    before = ss.read_snapshot(proj).revision

    out = ss.release_reservation(
        proj, "worker", owner="w1", operation_id="rel-noop", expected_revision=before
    )
    assert out == before
    assert ss.read_snapshot(proj).revision == before
    assert ss.read_operation(proj, "rel-noop") is None


def test_check_action_rejects_unknown_action_without_snapshot(tmp_path):
    """F4: an unknown action is refused identically whether the snapshot is
    missing or valid — one input, one behavior. RED before the fix (the
    missing branch returned early and let ``bogus`` pass silently)."""
    ss = _store()
    proj = _project(tmp_path)  # no control.json → missing
    with pytest.raises(ss.StateStoreError):
        ss.check_action(proj, "bogus")

    ss.transact(proj, expected_revision=0, operation_id="seed", mutate=lambda s: s)
    with pytest.raises(ss.StateStoreError):
        ss.check_action(proj, "bogus")


# ─── migration ───────────────────────────────────────────────────────────


def test_migration_preview_publish_and_idempotent_repeat(tmp_path):
    ss = _store()
    proj = _project(tmp_path)
    state = proj / ".agentic" / "state"
    (state / "run.yaml").write_text("run_id: r1\nrun_generation: 2\n", encoding="utf-8")
    (state / "current.yaml").write_text("stage_name: execute\n", encoding="utf-8")

    preview = ss.migration_preview(proj)
    assert preview["requires_reconciliation"] is False
    assert preview["old_fields"]["active_run"]["run_id"] == "r1"

    result = ss.migrate(proj)
    assert result["status"] == "applied" and result["revision"] == 1
    snap = ss.read_snapshot(proj)
    assert snap.valid
    assert snap.data["active_run"]["run_id"] == "r1"
    assert snap.data["run_generation"] == 2
    assert (state / "run.yaml").is_file()  # legacy сохранён
    assert ss.manifest_path(proj).is_file()

    repeat = ss.migrate(proj)
    assert repeat["status"] == "no_change"
    assert repeat["revision"] == 1
    assert ss.read_snapshot(proj).revision == 1


def test_migration_ambiguous_done_dir_requires_reconciliation(tmp_path):
    ss = _store()
    proj = _project(tmp_path)
    state = proj / ".agentic" / "state"
    (state / "run.yaml").write_text("run_id: r1\n", encoding="utf-8")
    (proj / ".agentic" / "done" / "UNIT-0001").mkdir(parents=True)

    assert ss.migration_preview(proj)["requires_reconciliation"] is True
    with pytest.raises(ss.MigrationReconciliationRequired) as ei:
        ss.migrate(proj)
    assert ei.value.outcome == "migration_requires_reconciliation"
    assert not ss.control_path(proj).exists()  # автолечения нет


def test_migration_corrupt_legacy_requires_reconciliation(tmp_path):
    ss = _store()
    proj = _project(tmp_path)
    state = proj / ".agentic" / "state"
    (state / "run.yaml").write_text("run_id: [broken\n", encoding="utf-8")

    assert ss.migration_preview(proj)["requires_reconciliation"] is True
    with pytest.raises(ss.StateCorrupt):
        ss.read_projection(proj, "run")
    with pytest.raises(ss.MigrationReconciliationRequired):
        ss.migrate(proj)
    assert not ss.control_path(proj).exists()  # a corrupt legacy is not auto-healed


def test_stale_reconcile_after_new_generation(tmp_path):
    ss = _store()
    proj = _project(tmp_path)
    ss.transact(proj, expected_revision=0, operation_id="new-gen",
                mutate=lambda s: {**s, "run_generation": 1})

    with pytest.raises(ss.StaleState) as ei:
        ss.transact(proj, expected_revision=0, operation_id="old-reconcile",
                    mutate=lambda s: {**s, "execution": {"stale": True}})
    assert ei.value.outcome == "stale_operation"

    snap = ss.read_snapshot(proj)
    assert snap.revision == 1
    assert snap.data.get("execution") is None  # новый snapshot не изменён


def test_migration_cli_reports_recovery_required(tmp_path, capsys):
    """C: the CLI must surface ``recovery_required``, not a raw traceback,
    when ``migrate`` meets a stale ``state-migrate-2`` reservation.

    ``OperationRecoveryRequired`` is an ``AwfApiError`` but NOT a
    ``StateStoreError``; the CLI caught only the latter and let the former
    escape as a traceback. RED before the fix."""
    ss = _store()
    proj = _project(tmp_path)
    migrate_args = {"migration": ss.SCHEMA_VERSION, "legacy": []}
    ss.write_operation(
        proj, "state-migrate-2", status="reserved",
        args_digest=ss.payload_digest(migrate_args), revision_before=0,
    )

    rc = ss.main(["migrate", str(proj)])
    out = capsys.readouterr().out
    assert rc == 1
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["status"] == "recovery_required"
    assert not ss.control_path(proj).exists()


def test_migration_cli_handles_oversized_legacy_pid(tmp_path, capsys):
    """F1: an oversized legacy ``pipeline_pid`` (outside the OS pid range)
    must not escape as a raw OverflowError from a bespoke ``os.kill``. The
    liveness probe is the shared one, so the CLI returns clean JSON. RED
    before the fix (the bare ``os.kill(pid, 0)`` raised OverflowError)."""
    ss = _store()
    proj = _project(tmp_path)
    state = proj / ".agentic" / "state"
    (state / "current.yaml").write_text("pipeline_pid: 2147483648\n", encoding="utf-8")

    rc = ss.main(["migrate", str(proj)])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["status"] == "applied"


def test_migration_liveness_probe_is_shared(tmp_path, monkeypatch):
    """F1: ``_live_pid`` delegates to the shared ``_pid_alive`` guard (one
    liveness semantics) and keeps the ``pid <= 1`` guard before delegating."""
    from awf.infra.state_store import _migration as mig

    calls: list[int] = []
    monkeypatch.setattr(mig, "_pid_alive", lambda pid: calls.append(pid) or True)

    assert mig._live_pid(4242) is True
    assert calls == [4242]
    assert mig._live_pid(1) is False  # pid <= 1 is never probed
    assert calls == [4242]


def test_migration_post_commit_failure_requests_recovery(tmp_path, monkeypatch):
    """D: a failure AFTER the snapshot commit point (the manifest write) must
    surface ``recovery_required`` with diagnostics — not a raw exception.
    The effect stays published; a repeat is an idempotent ``no_change``."""
    ss = _store()
    proj = _project(tmp_path)
    state = proj / ".agentic" / "state"
    (state / "run.yaml").write_text("run_id: r1\n", encoding="utf-8")

    from awf.infra.state_store import _migration as mig

    def flaky(path, data):
        raise OSError("simulated manifest write failure after publish")

    monkeypatch.setattr(mig, "atomic_write_json", flaky)
    with pytest.raises(ss.OperationRecoveryRequired):
        ss.migrate(proj)

    snap = ss.read_snapshot(proj)
    assert snap.valid and snap.revision == 1  # эффект лёг
    assert snap.data["active_run"]["run_id"] == "r1"
    monkeypatch.undo()
    assert ss.migrate(proj)["status"] == "no_change"


def test_older_schema_snapshot_is_not_silently_accepted(tmp_path):
    """E: a snapshot of an OLDER schema must not be read as current and
    silently skip migration (reader/guard mismatch). RED before the fix."""
    ss = _store()
    proj = _project(tmp_path)
    ss.control_path(proj).write_text(
        json.dumps({"schema_version": 1, "revision": 4}), encoding="utf-8"
    )
    assert ss.read_snapshot(proj).status == "unsupported"
    with pytest.raises(ss.StateUnsupported):
        ss.migrate(proj)


def test_state_publish_path_is_durable(tmp_path, monkeypatch):
    """T3a (durability bar): control.json, immutable records and the operation
    journal must publish through ``durable=True`` (fsync file + parent dir).

    A ``reserved`` journal line lost to a HOST crash while the unit's
    inbox file survives would let a replay double the effect; the durable path is the
    fix. A regression that silently drops the flag reverts the bar, so this
    pins it. RED if any state write publishes non-durable."""
    ss = _store()
    from awf.infra import managed_fs

    seen: list[bool] = []
    real = managed_fs.managed_write_text

    def spy(path, content, *, root, mode=0o600, encoding="utf-8", durable=False):
        seen.append(durable)
        return real(
            path, content, root=root, mode=mode, encoding=encoding,
            durable=durable,
        )

    monkeypatch.setattr(managed_fs, "managed_write_text", spy)
    proj = _project(tmp_path)
    ss.put_record(proj, "evidence", {"a": 1})
    ss.transact(proj, expected_revision=0, operation_id="op-durable",
                mutate=lambda s: s, records={"evidence": {"a": 1}})

    assert seen, "no state write happened — the spy missed the publish path"
    assert all(seen), f"a state write published without durable=True: {seen}"


# ─── QA-217: migration robustness (fault path) ───────────────────────────


def test_migration_cli_surfaces_a_write_fault_as_json_not_a_traceback(
    tmp_path, capsys, monkeypatch
):
    """QA-217 (class of finding C, still open): the C fix broadened the CLI
    ``except`` only to ``AwfApiError``. A plain write fault on the snapshot
    publish is not an ``AwfApiError`` — ``_transact`` re-raises the raw
    ``OSError`` — so it escapes ``main`` as a traceback instead of a
    structured outcome. RED now (ValueError/OSError escapes)."""
    ss = _store()
    from awf.infra.state_store import _transact as tx

    proj = _project(tmp_path)
    (proj / ".agentic" / "state" / "run.yaml").write_text("run_id: r1\n", encoding="utf-8")
    real = tx.atomic_write_json

    def flaky(path, data):
        if path.name == "control.json":
            raise OSError("simulated snapshot write fault")
        return real(path, data)

    monkeypatch.setattr(tx, "atomic_write_json", flaky)
    rc = ss.main(["migrate", str(proj)])  # must return, not raise
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] in {"error", "failed", "refused", "recovery_required"}
    assert rc == 1


def test_preview_cli_surfaces_unserializable_legacy_as_json_not_traceback(
    tmp_path, capsys
):
    """QA-217: the fault class ``migrate`` now surfaces as JSON also reaches
    the SIBLING ``preview`` subcommand. A legacy field YAML parses into a
    non-JSON type (``started: 2026-10-17`` -> ``datetime.date``), so
    ``json.dumps(migration_preview(...))`` raised TypeError and escaped
    ``main`` as a traceback — the exact defect Q1 closed one branch over.
    RED before the fix (``main`` raises), GREEN after (structured JSON)."""
    ss = _store()
    proj = _project(tmp_path)
    (proj / ".agentic" / "state" / "run.yaml").write_text(
        "run_id: r1\nstarted: 2026-10-17\n", encoding="utf-8"
    )

    rc = ss.main(["preview", str(proj)])  # must return, not raise
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] == "failed"
    assert "date" in payload["error"]
    assert rc == 1


def test_failed_migration_is_retryable(tmp_path, monkeypatch):
    """QA-217: a migration that failed WITHOUT landing an effect is
    journaled ``failed`` under the FIXED ``state-migrate-2`` id. The CLI
    offers no other id, so a retry must re-attempt the migration (the
    operation had no effect) — not replay the stale failure forever and
    leave the project unmigratable. RED now (the retry raises the recorded
    ``failed`` error)."""
    ss = _store()
    from awf.infra.state_store import _transact as tx

    proj = _project(tmp_path)
    (proj / ".agentic" / "state" / "run.yaml").write_text("run_id: r1\n", encoding="utf-8")
    real = tx.atomic_write_json
    state = {"fault": True}

    def flaky(path, data):
        if path.name == "control.json" and state["fault"]:
            state["fault"] = False
            raise OSError("transient snapshot write fault")
        return real(path, data)

    monkeypatch.setattr(tx, "atomic_write_json", flaky)
    with pytest.raises(Exception):
        ss.migrate(proj)
    monkeypatch.undo()

    out = ss.migrate(proj)  # retry — the first attempt left no effect
    assert out["status"] == "applied"
    assert ss.read_snapshot(proj).valid
