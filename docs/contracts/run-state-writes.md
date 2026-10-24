# Contract: Состояние забега
Owns: запись и очистку `.agentic/state/run.yaml` (забег) и `current.yaml` (пайплайн) — только через API состояния.
Path: `update_run` (`awf/run_state.py`)
Never: запись/удаление run.yaml или current.yaml в обход API ломает лок. Исключения: pipeline.py (pipeline_pid), lifecycle.py (unlink), _migration.py. Ревизия: тот же ключ и payload — no-op, другой payload — отказ.
Gate: [ -n "$(git ls-files --cached --others --exclude-standard 'awf/*.py' | head -1)" ] && grep -q 'same key with the same payload is a no-op' USAGE.md && ! grep -rnE '/ "current\.yaml"|/ "run\.yaml"' awf --include='*.py' | grep -vE "^(awf/run_state\.py|awf/pipeline_state\.py|awf/api/pipeline\.py|awf/api/lifecycle\.py|awf/infra/state_store/_migration\.py):"
