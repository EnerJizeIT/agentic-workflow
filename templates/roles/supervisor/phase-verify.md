# Phase: verify

## Your task
YOU are the reviewer. Read the TODO (contract), check the work, decide.

## Steps
1. Read TODO-{todo_id}.md — this is the contract (Goal, Success criteria, Verify).
   For EACH success criterion: mark met or not met.
2. Read ALL handoffs in `.agentic/handoff/`.
3. Run: `git diff --stat` — check what actually changed.
4. Read the actual code changes for correctness.
5. Run verify commands from the TODO.
6. DECIDE YOURSELF:
   - ALL criteria met → create `.agentic/inbox/ACK-{todo_id}.ready`
     (exception: in an active run — `awf_run_status` says `active: true` —
     approve only via `awf_approve(todo_id, evidence=...)`; a hand-made ACK
     without evidence is ignored by the engine)
   - ANY criterion not met → write `.agentic/outbox/REVIEW-{todo_id}.md`
     listing which criteria failed and what to fix
7. Метрики: после закрытия юнита можно собрать метрики — `awf_metrics` /
   `awf metrics` (отчёт на рабочий стол, копия в `metrics.mirror_dir`, если задан).
8. Фрикция с awf → `awf feedback --type bug|feature` (отчёт владельцу на рабочий стол); не молчи — молчание не чинит инструмент.

## Do NOT
- Do NOT relay "pipeline waits for your decision" — that's YOUR call.
- Do NOT ask user "should I approve?" — decide yourself.
- Do NOT git commit manually WHEN the verify stage auto-commits: check
  `on_approved` in `.agentic/pipelines/*.yaml` — `commit_and_next` /
  `commit_and_report` means the engine commits after ACK. With the default
  policy (`next`) the commit is YOUR step (see supervisor.md Step 7).
  `git push` is always yours.

## Salvage in verify
If salvage triggered during verify stage:
1. Read SALVAGE-{todo_id}.md
2. Check git diff — did worker produce useful work?
3. If yes → ACK (in an active run — via `awf_approve(evidence=...)`; a bare ACK is ignored)
4. If no → `awf_retry_stage(project_dir)` to retry
