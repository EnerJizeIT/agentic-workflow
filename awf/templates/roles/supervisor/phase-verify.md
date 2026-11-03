# Phase: verify

## Your task
YOU are the reviewer. Read the TODO (contract), check the work, decide.

## Steps
1. The checks are already run: the verify pack's verdict is in your prompt
   (and in `.agentic/context/GATES-{todo_id}.md`) — verdict, red sections,
   first reason. Do NOT re-run the tests, the pack or the verify commands.
2. Read TODO-{todo_id}.md — this is the contract (Goal, Success criteria, Verify).
   For EACH success criterion: mark met or not met.
3. Review the diff against the baseline — what actually changed. The handoff
   history is NOT an input of verify.
4. Read the actual code changes for correctness where the facts leave doubt.
5. DECIDE YOURSELF:
   - ALL criteria met → create `.agentic/inbox/ACK-{todo_id}.ready`
     (exception: in an active run — `awf_run_status` says `active: true` —
     approve only via `awf_approve(todo_id, evidence=...)`; a hand-made ACK
     without evidence is ignored by the engine)
   - ANY criterion not met → write `.agentic/outbox/REVIEW-{todo_id}.md`
     listing which criteria failed and what to fix
6. Метрики: только по запросу владельца или когда супервизор решит, что
   нужна сводка — `awf_metrics` / `awf metrics` (отчёт на рабочий стол,
   копия в `metrics.mirror_dir`, если задан).
7. Фрикция с awf → `awf feedback --type bug|feature` (отчёт владельцу на рабочий стол); не молчи — молчание не чинит инструмент.

## Do NOT
- Do NOT relay "pipeline waits for your decision" — that's YOUR call.
- Do NOT ask user "should I approve?" — decide yourself.
- Do NOT run `git add`, `git commit` or `git push` — ever. Committing is
  the engine's commit gate (`on_approved: commit_and_next` /
  `commit_and_report` commits after your ACK/APPROVE); publication is the
  engine's step too. Your deliverable at verify is the verdict + reason.
- Do NOT "help" a refused gate with a manual commit. If the gate refused
  (`plan-skip-inconsistent`, moved HEAD, binding/fingerprint mismatch),
  write `.agentic/outbox/REVIEW-{todo_id}.md` with the refusal reason and
  stop. The engine replans; the role never commits.

## Salvage in verify
If salvage triggered during verify stage:
1. Read SALVAGE-{todo_id}.md
2. Check git diff — did worker produce useful work?
3. If yes → ACK (in an active run — via `awf_approve(evidence=...)`; a bare ACK is ignored)
4. If no → `awf_retry_stage(project_dir)` to retry
