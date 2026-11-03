# Contract: Процессная безопасность
Owns: пути сигналов процессам и группам, спавн killable-стадий.
Path: `kill_process_tree`, `kill_pid_tree`, `signal_pid` (`awf/_proc.py`); отсоединённый спавн — `start_in_background` (`awf/api/_background.py`); маршрут сигналов уборки — `awf/worker_reap.py`.
Never: сигнал группе процессов с номером ≤ 1 (`killpg(1, sig)` — SIGTERM/SIGKILL всей сессии, инцидент 2026-09-20); спавн killable-стадии без своей сессии (`start_new_session`); отсоединённый ребёнок (фон, `setsid`) метку стадии не наследует.
Gate: grep -q "def signalable_group" awf/_proc.py && grep -q "or pgid <= 1" awf/_proc.py && grep -q "_forbid_session_kill" tests/conftest.py && python3 scripts/check_process_safety.py awf agent_workflow_ui
