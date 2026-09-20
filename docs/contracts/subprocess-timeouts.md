# Contract: Время исполнения
Owns: все внешние вызовы (subprocess.run / Popen) в движке awf.
Path: `run_tree` (`awf/_proc.py`)
Never: внешний вызов без timeout в модулях `awf/**`, кроме явного перечня исключений в `scripts/check-subprocess-timeouts.py` (run_tree с timeout в communicate, фоновый пайплайн, worker с deadline-циклом). Сейчас в перечне есть НАХОДКА: rollback `git diff`/`git reset` в `awf/api/pipeline.py` без timeout.
Gate: [ -n "$(git ls-files --cached --others --exclude-standard 'awf/*.py' | head -1)" ] && python3 scripts/check-subprocess-timeouts.py
