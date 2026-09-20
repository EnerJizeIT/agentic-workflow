# Contract: Процессная безопасность
Owns: все пути, где движок шлёт сигналы процессным группам (kill/killpg), и спавн killable-стадий.
Path: `kill_process_tree` (`awf/_proc.py`)
Never: сигнал группе процессов с номером ≤ 1 (`killpg(1, sig)` ≡ `kill(-1, sig)` — SIGTERM/SIGKILL всей сессии пользователя, инцидент 2026-09-20); спавн killable-стадии без собственной сессии (`start_new_session`).
Gate: [ -f awf/_proc.py ] && [ -f tests/conftest.py ] && grep -q "pid > 1" awf/_proc.py && grep -q "_forbid_session_kill" tests/conftest.py && ! grep -rnE "killpg[[:space:]]*\([[:space:]]*-?1([^0-9]|$)|kill[[:space:]]*\([[:space:]]*-?1([^0-9]|$)" awf agent_workflow_ui --include='*.py' | grep -vE ":[[:space:]]*#"
