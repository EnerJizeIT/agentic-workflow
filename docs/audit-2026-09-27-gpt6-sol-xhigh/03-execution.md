# 03 — Исполнение и переход стадии

**Роль:** инженер рантайма. **Проверка:** A-02/A-14, владение foreground/background, обработка отказа commit gate.

**A-02:** `start_pipeline` берёт launch lease для обычного foreground и background вызова, под lease проверяет живой процесс, `finally` освобождает право; background child узнаётся отдельно (`awf/api/pipeline.py:884`, `awf/api/_lease.py`). Тесты одновременных foreground, background и mixed запусков прошли. **A-14:** отказанный `CommitOutcome` останавливает переход execute стадии, `skipped` продолжает по контракту (`awf/pipeline_engine.py`, `test_audit25_a14_execute_refusal.py`).

Проверка выявила новое расхождение между типом запуска и успехом: `start_pipeline` возвращает `run_mode="foreground"` с ненулевым `exit_code` при ошибке, а `run_next` считает любой режим, кроме `noop/error`, успешным. Изолированный сценарий получил `action=started`, `index=1` после `exit_code=1` (V-01). При прямом исключении из `start_pipeline` действует V-02. Оба пути относятся к исполнению очереди, а не к паре конкурентных стартов A-02.

**Вывод:** исходные инварианты A-02/A-14 подтверждены; для `run_next` требуется единый смысл результата запуска. См. [слой 12](12-recovery-reliability.md).
