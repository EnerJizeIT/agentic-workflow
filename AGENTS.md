# AGENTS.md

Перед написанием кода прочитай `docs/contracts/` и плейбуки по своей области.

Ворота качества: `bash scripts/run-all.sh` — контракты, храповики, бюджет
инструкций, тесты, линт. Новый гейт подключаешь только после того, как он
поймал твоё scratch-нарушение (красный → зелёный).

Контракт юнита (опциональный блок verify/gates/prove_red в TODO +
`DONE-<id>.json`): формат в `docs/unit-contract.md`.

## Safety invariants (инцидент 2026-09-20)

- Guard `proc.pid > 1` в `awf/_proc.py` и tripwire `_forbid_session_kill`
  в `tests/conftest.py` — не убирать, не ослаблять. `os.killpg(1, sig)` —
  это `kill(-1, sig)`, вся сессия пользователя.
- Перед полным `pytest tests/` — green-light:
  `grep -n "pid > 1" awf/_proc.py` и `grep -n "_forbid_session_kill" tests/conftest.py`.
- Разбор: `~/Desktop/session-crash-report-2026-09-20.md`.
