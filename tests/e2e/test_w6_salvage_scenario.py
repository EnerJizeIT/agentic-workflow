"""W6.6 NEG-4: scenario-driven opencode shim + engine scenario tests.

The shim (tests/infra/opencode_shim/opencode) stands in for the real
opencode/qwen so the subprocess container of the engine is testable
deterministically:

1. salvage — worker exits silently (no signal) → the engine classifies
   salvage (SALVAGE note + state), then the retry (shim switched to
   do-step, `awf continue --from-stage`) drives the unit to verify;
2. blocked — worker writes BLOCKED-<id>.md → the engine stops by
   policy, state and hint are correct;
3. hang — worker hangs past the stage timeout → the engine kills the
   process group on the hard timeout and no shim process is left.

All runs are on a scratch project in tmp_path with `opencode` resolved
from the shim dir (front of PATH); no real opencode/qwen process is
spawned, every run is bounded in time.
"""
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from conftest import _git_init, run_awf

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHIM_DIR = REPO_ROOT / "tests" / "infra" / "opencode_shim"

PIPELINE_YAML = (
    'name: "default"\n'
    'description: "W6 scratch pipeline (plan → worker → verify)"\n\n'
    'stages:\n'
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - name: "implement"\n'
    '    role: "worker"\n'
    '    on_blocked: "stop"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
)

CONFIG_YAML = (
    "project:\n"
    "  name: w6-scratch\n"
    "automation:\n"
    "  preflight_timeout_seconds: 0\n"
    "  no_output_timeout_seconds: 0\n"
    "  verify_pack: false\n"
)


@pytest.fixture
def scratch_project(tmp_path, awf_env) -> Path:
    """Throwaway git project with the .agentic skeleton the engine needs.

    plan → implement(worker) → verify pipeline; TODO-0001 pre-created in
    the inbox. config.yaml disables preflight / the no-output watchdog /
    the verify pack so the ONLY timer in the runs is the CLI --timeout
    (the hard stage timeout) — the hang scenario must be governed by it.
    """
    proj = tmp_path / "scratch"
    proj.mkdir()
    _git_init(proj)
    ag = proj / ".agentic"
    for sub in ("pipelines", "roles", "inbox", "outbox", "context", "logs", "handoff"):
        (ag / sub).mkdir(parents=True)
    # .agentic/ is runtime data, not work — in a real project it is
    # gitignored. Without this, the engine's own artifacts (worker logs,
    # state files) look like untracked "work" and the auto-DONE gate
    # synthesizes a fake DONE for a silent worker (NEG-4 B1 surface).
    (proj / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=proj, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "ignore .agentic"], cwd=proj, check=True
    )
    (ag / "pipelines" / "default.yaml").write_text(PIPELINE_YAML, encoding="utf-8")
    (ag / "roles" / "worker.md").write_text("# Worker\nExecute the TODO.\n", encoding="utf-8")
    (ag / "roles" / "supervisor.md").write_text("# Supervisor\nPlan and verify.\n", encoding="utf-8")
    (ag / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    inbox = ag / "inbox"
    (inbox / "TODO-0001.md").write_text("# TODO-0001\nstub task\n", encoding="utf-8")
    (inbox / "TODO-0001.ready").write_text("", encoding="utf-8")
    return proj


def _env(awf_env: dict, mode: str, sup_mode: str | None = None,
         state_dir: Path | None = None) -> dict:
    """Env for one engine run: shim first in PATH + the scenario modes."""
    env = dict(awf_env)
    env["PATH"] = f"{SHIM_DIR}:{env['PATH']}"
    env["AWF_SHIM_MODE"] = mode
    env.pop("AWF_TEST_OPENCODE_BEHAVIOR", None)  # old-stub control, shim ignores it
    if sup_mode is not None:
        env["AWF_SHIM_SUPERVISOR_MODE"] = sup_mode
    if state_dir is not None:
        state_dir.mkdir(parents=True, exist_ok=True)
        env["AWF_SHIM_STATE_DIR"] = str(state_dir)
    return env


def _state(proj: Path) -> dict:
    p = proj / ".agentic" / "state" / "current.yaml"
    if not p.is_file():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _invocations(state_dir: Path) -> list[list[str]]:
    log = state_dir / "invocation.log"
    if not log.is_file():
        return []
    return [line.split() for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _log_text(proj: Path) -> str:
    p = proj / ".agentic" / "logs" / "orchestrator.log"
    return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""


def _pid_dead(pid: int, tries: int = 10, delay: float = 0.5) -> bool:
    """True when pid is gone — a ZOMBIE counts as dead (it does no work)."""
    for _ in range(tries):
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OverflowError):
            return True
        except OSError:
            return True
        try:
            stat = Path(f"/proc/{pid}/stat").read_bytes().decode("ascii", "replace")
            rpar = stat.rfind(")")
            state = stat[rpar + 2:].split()[0]
            if state in ("Z", "X"):
                return True
        except OSError:
            return True
        time.sleep(delay)
    return False


def _group_dead(pgid: int, tries: int = 10, delay: float = 0.5) -> bool:
    """True when the process group has no live members left."""
    for _ in range(tries):
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, OverflowError):
            return True
        except OSError:
            return True
        try:
            for name in os.listdir("/proc"):
                if not name.isdigit():
                    continue
                stat = Path(f"/proc/{name}/stat").read_bytes().decode("ascii", "replace")
                fields = stat[stat.rfind(")") + 2:].split()
                if int(fields[2]) == pgid and fields[0] not in ("Z", "X"):
                    break
            else:
                return True
        except (OSError, ValueError, IndexError):
            pass
        time.sleep(delay)
    return False


# ─── scenario 1: salvage ────────────────────────────────────────────────


@pytest.mark.timeout(300)
def test_salvage_silent_exit_then_retry_drives_to_verify(
    scratch_project: Path, awf_bin: str, awf_env: dict, tmp_path: Path
):
    """Silent worker exit → salvage classification; the do-step retry via
    `awf continue --from-stage` drives the unit to verify and finishes."""
    proj = scratch_project
    state1 = tmp_path / "state1"
    env1 = _env(awf_env, "silent-exit", sup_mode="approve", state_dir=state1)

    run1 = run_awf(
        awf_bin, ["start", "--auto", "--todo", "TODO-0001"],
        cwd=proj, env=env1, input_data=b"", timeout=240,
    )
    out1 = (run1.stdout + run1.stderr).decode(errors="replace")

    # Engine stopped at the salvage failure (auto: no work + no signal).
    assert run1.returncode != 0, f"run1 rc={run1.returncode}\n{out1}"

    # Salvage classification artifacts.
    salvage_file = proj / ".agentic" / "inbox" / "SALVAGE-TODO-0001.md"
    assert salvage_file.is_file(), f"SALVAGE note missing\n{out1}\n{_log_text(proj)}"
    st1 = _state(proj)
    assert st1.get("salvage_needed") is True, f"state={st1}"
    assert st1.get("salvage_stage") == "implement", f"state={st1}"
    assert st1.get("todo_id") == "TODO-0001", f"state={st1}"

    # No fake DONE was synthesized for the silent worker.
    outbox = proj / ".agentic" / "outbox"
    assert not (outbox / "DONE-TODO-0001.ready").exists(), "false DONE after silent exit"
    assert "salvage" in _log_text(proj), _log_text(proj)[-2000:]

    # The worker was run 3 times (initial + 2 silent-retry continue-pushes),
    # each in silent-exit mode; the plan stage saw one supervisor call.
    calls1 = _invocations(state1)
    workers1 = [c for c in calls1 if c[1] == "worker" and c[2] == "silent-exit"]
    assert len(workers1) == 3, f"expected 3 silent-exit worker calls, got {calls1}"
    assert any(c[1] == "supervisor" for c in calls1), f"no supervisor call in {calls1}"

    # Retry: switch the shim to the success mode and continue from the
    # salvaged stage (the pinned unit comes from the state file).
    state2 = tmp_path / "state2"
    env2 = _env(awf_env, "do-step", sup_mode="approve", state_dir=state2)
    run2 = run_awf(
        awf_bin, ["continue", "--auto", "--from-stage", "implement"],
        cwd=proj, env=env2, input_data=b"", timeout=120,
    )
    out2 = (run2.stdout + run2.stderr).decode(errors="replace")

    assert run2.returncode == 0, f"run2 rc={run2.returncode}\n{out2}\n{_log_text(proj)}"
    assert "Pipeline complete!" in out2, out2

    # The unit reached verify, was accepted and archived: DONE report lives
    # in the done/ archive (the outbox copy is consumed on the transition),
    # work evidence from the do-step attempt is in the tree.
    assert (proj / ".agentic" / "done" / "TODO-0001" / "DONE.md").is_file(), \
        f"no archived DONE report; outbox={list(outbox.iterdir())}"
    assert not (proj / ".agentic" / "inbox" / "TODO-0001.md").exists(), \
        "TODO-0001 not archived"
    assert (proj / "src" / "shim-TODO-0001-work.txt").is_file(), "no work evidence"
    st2 = _state(proj)
    assert st2.get("phase") == "done", f"state={st2}"
    assert st2.get("salvage_needed") in (None, False), f"state={st2}"

    calls2 = _invocations(state2)
    workers2 = [c for c in calls2 if c[1] == "worker"]
    assert [c[2] for c in workers2] == ["do-step"], f"worker calls={calls2}"
    sups2 = [c for c in calls2 if c[1] == "supervisor"]
    assert [c[3] for c in sups2] == ["awf-supervisor-verify"], f"supervisor calls={calls2}"


# ─── scenario 2: BLOCKED ────────────────────────────────────────────────


def test_blocked_stops_with_correct_state_and_hint(
    scratch_project: Path, awf_bin: str, awf_env: dict, tmp_path: Path
):
    """Worker writes BLOCKED-<id>.md → engine stops by policy (on_blocked:
    stop), the BLOCKED report and the state hint are correct."""
    proj = scratch_project
    state_dir = tmp_path / "state"
    env = _env(awf_env, "write-blocked", state_dir=state_dir)

    run = run_awf(
        awf_bin, ["start", "--auto", "--todo", "TODO-0001"],
        cwd=proj, env=env, input_data=b"", timeout=90,
    )
    out = (run.stdout + run.stderr).decode(errors="replace")

    assert run.returncode != 0, f"rc={run.returncode}\n{out}"
    assert "Pipeline stopped by policy." in out, out

    # The BLOCKED report (the "why") is kept in the outbox.
    blocked_md = proj / ".agentic" / "outbox" / "BLOCKED-TODO-0001.md"
    assert blocked_md.is_file(), "BLOCKED report missing"
    assert "write-blocked" in blocked_md.read_text(encoding="utf-8")

    # State: the stop reason is recorded; the unit is NOT closed — it waits
    # for the supervisor's answer (ACK/replan), visible in awf status.
    st = _state(proj)
    assert st.get("last_signal") == "BLOCKED-TODO-0001", f"state={st}"
    assert st.get("stage_name") == "implement", f"state={st}"

    status = run_awf(awf_bin, ["status"], cwd=proj, env=env, input_data=b"", timeout=30)
    status_out = status.stdout.decode(errors="replace")
    assert "Active: TODO-0001" in status_out, status_out


# ─── scenario 3: hang ───────────────────────────────────────────────────


@pytest.mark.timeout(180)
def test_hang_killed_by_hard_timeout_no_processes_left(
    scratch_project: Path, awf_bin: str, awf_env: dict, tmp_path: Path
):
    """Worker hangs past the stage timeout → the engine kills the process
    group on the hard timeout; neither the shim nor its child survive."""
    proj = scratch_project
    state_dir = tmp_path / "state"
    env = _env(awf_env, "hang", state_dir=state_dir)
    env["AWF_SHIM_HANG_SECONDS"] = "300"

    run = run_awf(
        awf_bin, ["start", "--auto", "--todo", "TODO-0001", "--timeout", "8"],
        cwd=proj, env=env, input_data=b"", timeout=120,
    )
    out = (run.stdout + run.stderr).decode(errors="replace")

    # Stage ended on the timeout, the pipeline stopped (crash death).
    assert run.returncode != 0, f"rc={run.returncode}\n{out}"
    log = _log_text(proj)
    assert "hard timeout" in log, log[-3000:]

    # The shim group is gone: main pid, its child, and the whole group.
    pids_file = state_dir / "hang-pids"
    assert pids_file.is_file(), "hang mode did not record its pids"
    lines = [ln for ln in pids_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2, f"hang-pids={lines}"
    main_pid, child_pid = int(lines[0]), int(lines[1])
    assert _pid_dead(main_pid), f"shim pid {main_pid} survived the hard timeout"
    assert _pid_dead(child_pid), f"shim child {child_pid} survived the hard timeout"
    assert _group_dead(main_pid), f"process group {main_pid} still has live members"

    # No fake DONE for a worker that never signalled.
    assert not (proj / ".agentic" / "outbox" / "DONE-TODO-0001.ready").exists()


# ─── bonus: crash fails loud ────────────────────────────────────────────


def test_crash_fails_loud(
    scratch_project: Path, awf_bin: str, awf_env: dict, tmp_path: Path
):
    """Worker exits non-zero without a signal → loud failure, no fake DONE,
    the exit code lands in the log."""
    proj = scratch_project
    env = _env(awf_env, "crash", state_dir=tmp_path / "state")

    run = run_awf(
        awf_bin, ["start", "--auto", "--todo", "TODO-0001"],
        cwd=proj, env=env, input_data=b"", timeout=90,
    )
    out = (run.stdout + run.stderr).decode(errors="replace")

    assert run.returncode != 0, f"rc={run.returncode}\n{out}"
    # The RuntimeError text (with the exit code) lands in the orchestrator log.
    assert "exited with code 3" in _log_text(proj), _log_text(proj)[-2000:]
    assert not (proj / ".agentic" / "outbox" / "DONE-TODO-0001.ready").exists()
    assert "salvage" not in _state(proj), "a crash is a crash, not a salvage"
