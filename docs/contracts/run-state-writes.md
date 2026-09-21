# Contract: Состояние забега
Owns: запись и очистку `.agentic/state/run.yaml` (забег) и `current.yaml` (пайплайн) — только через API состояния.
Path: `update_run` (`awf/run_state.py`)
Never: прямая запись/удаление run.yaml или current.yaml в обход `run_state`/`pipeline_state` — обход ломает лок read→merge→write (AUD05-05) и атомарность записи (KAUD-6). Документированные исключения: `awf/api/pipeline.py` (прямая запись для удаления ключа pipeline_pid — API не умеет) и `awf/api/lifecycle.py` (unlink при reset).
Gate: [ -n "$(git ls-files --cached --others --exclude-standard 'awf/*.py' | head -1)" ] && ! grep -rnE '/ "current\.yaml"|/ "run\.yaml"' awf --include='*.py' | grep -vE "^(awf/run_state\.py|awf/pipeline_state\.py|awf/api/pipeline\.py|awf/api/lifecycle\.py):"
