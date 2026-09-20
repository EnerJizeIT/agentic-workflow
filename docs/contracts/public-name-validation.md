# Contract: Имена с публичного входа
Owns: валидацию имён (роль, пайплайн, агент, TODO) на входе публичных API — до того, как имя стало частью пути на диске.
Path: `resolve_pipeline_file` (`awf/pipeline.py`)
Never: использование имени из аргумента API без валидации: `../` уходит за пределы `.agentic/` (AUD14-04: имя лога worker из аргумента; AUD14-05: pipeline_name читал файл за пределами `.agentic/pipelines/`). Доказательство: `TestWorkerLogNameSanitized` и `TestPipelineNameValidation` в `tests/negative/test_audit14_incidents.py`.
Gate: [ -n "$(git ls-files --cached --others --exclude-standard 'tests/*.py' | head -1)" ] && grep -q "def resolve_pipeline_file(" awf/pipeline.py && grep -q "class TestPipelineNameValidation:" tests/negative/test_audit14_incidents.py && grep -q "class TestWorkerLogNameSanitized:" tests/negative/test_audit14_incidents.py
