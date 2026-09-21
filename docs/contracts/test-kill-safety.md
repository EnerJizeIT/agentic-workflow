# Contract: Безопасность тестов
Owns: защиту процесса тест-раннера от реальных сигналов с целью ≤ 1 (tripwire + регресс-тест).
Path: `_forbid_session_kill` (`tests/conftest.py`)
Never: реальный `os.kill`/`os.killpg` с целью ≤ 1 в тестах (2026-09-20: полный `pytest tests/` трижды убил сессию владельца через фейк `pid=1`); удаление tripwire или регресс-теста `test_kill_process_tree_never_targets_pid_1`.
Gate: [ -f tests/conftest.py ] && grep -q "def _forbid_session_kill" tests/conftest.py && grep -q "os.kill = _forbid_session_kill" tests/conftest.py && grep -rq "def test_kill_process_tree_never_targets_pid_1(" tests/ --include='*.py'
