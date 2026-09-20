# Contract: Архивы TODO
Owns: перемещение завершённых TODO в `.agentic/done/` и очистка архива.
Path: `archive_todo` (`awf/todos.py`)
Never: удаление каталога архива целиком (`shutil.rmtree` по `done/` или по вложенному `done/<TODO-NNNN>/`) — регрессия commit badf05e удаляла чужие архивы; удаляется только пустой каталог.
Gate: [ -s awf/todos.py ] && ! grep -n "rmtree" awf/todos.py | grep -vE ":[[:space:]]*#"
