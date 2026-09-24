"""Argparse dispatcher — routes subcommands to cmd_*.py modules."""
from __future__ import annotations

import argparse
import subprocess
import sys

from . import cmd_start, cmd_status
from .api._errors import AwfApiError


def _print_top_level_help(sub) -> int:
    """Print the high-level usage that `awf help` and bare `awf` both show.

    AUD07-08: the command list is generated from the live subparsers
    (``sub._choices_actions`` — the help entries of ``sub.choices``), not a
    hand-maintained string. The old manual list already drifted once:
    ``restore`` was in the parser but missing from the help.
    """
    lines = [
        "awf — Agentic Workflow Framework",
        "",
        "Usage: awf <command> [options]",
        "",
        "Commands:",
    ]
    width = max(len(a.dest) for a in sub._choices_actions)
    for action in sub._choices_actions:
        lines.append(f"  {action.dest:<{width}}  {action.help or ''}")
    lines += [
        f"  {'help':<{width}}  Show this message",
        "",
        "Run 'awf <command> --help' for command-specific options.",
        "",
        "Options for 'start' / 'continue':",
        "  --pipeline <name>    Pipeline to run (default: from config.yaml)",
        "  --from-stage <name>  Start from a specific stage",
        "  --auto               Skip supervisor interactive waits",
        "  --background         Run detached via setsid — writes to",
        "                       .agentic/logs/awf-start.out. Default is",
        "                       foreground; pass --background to detach.",
        "  --timeout <seconds>  Agent timeout (default: 3600)",
        "  --ack <TODO-NNNN>    (continue only) Accept a BLOCKED TODO and resume",
    ]
    print("\n".join(lines))
    return 0


def _add_start_args(parser):
    """Add common args for start/continue subparsers."""
    parser.add_argument(
        "--pipeline",
        default=None,
        help="Pipeline to run (default: from config.yaml)",
    )
    parser.add_argument(
        "--from-stage",
        dest="from_stage",
        default=None,
        help="Start from a specific stage",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Skip supervisor interactive waits",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="Agent timeout in seconds (default: 3600)",
    )
    parser.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )


def main(argv=None):
    # Special-case bare invocation and `awf help`: argparse doesn't have a
    # `help` subcommand by default, but users expect `awf help` to work
    # (it was the canonical invocation in the bash era).
    raw = sys.argv[1:] if argv is None else list(argv)
    if not raw or raw[0] in ("help", "--help", "-h"):
        # `awf help <command>` -> redirect to `<command> --help`
        if len(raw) >= 2 and raw[0] == "help":
            subcmd = raw[1]
            # Re-invoke as `<subcmd> --help`
            return _dispatch_subcommand([subcmd, "--help"])
        # AUD07-08: build the real parser so the command list below comes
        # from the live subparsers (single source of truth).
        _parser, sub = _build_parser()
        return _print_top_level_help(sub)

    return _dispatch_subcommand(raw)


def _dispatch_subcommand(argv):
    parser, _sub = _build_parser()
    args = parser.parse_args(argv)

    return _run_command(args)


def _build_parser():
    """Build the awf argument parser. Returns (parser, subparsers_action).

    AUD07-08: the subparsers action is returned so `awf help` can generate
    its command list from the same source that dispatches the commands.
    """
    parser = argparse.ArgumentParser(
        prog="awf",
        description="Agentic Workflow Framework",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Show current workflow state")
    p_status.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )

    p_start = sub.add_parser("start", help="Start the pipeline from the beginning")
    _add_start_args(p_start)
    p_start.add_argument(
        "--todo",
        default="",
        help="Pin the pipeline to a specific TODO id (e.g. TODO-0015)",
    )
    p_start.add_argument(
        "--background",
        action="store_true",
        help="Run detached (setsid) — don't block the terminal. Default is "
             "foreground. Writes to .agentic/logs/awf-start.out.",
    )

    p_continue = sub.add_parser("continue", help="Resume an interrupted pipeline")
    _add_start_args(p_continue)
    # AUD07-02: continue used to always detach (the API default, designed for
    # the MCP agent) while the help claimed --background was "start only".
    # Now the flag exists on both commands; default is foreground for a
    # human in the terminal.
    p_continue.add_argument(
        "--background",
        action="store_true",
        help="Run detached (setsid) — don't block the terminal. Default is "
             "foreground. Writes to .agentic/logs/awf-start.out.",
    )
    p_restore = sub.add_parser(
        "restore", help="Restore an archived TODO from done/{id}/ to the inbox"
    )
    p_restore.add_argument("todo_id", help="TODO id (e.g. TODO-0015)")
    p_restore.add_argument(
        "--project-dir",
        dest="project_dir",
        default=".",
        help="Project root (default: current directory)",
    )

    # RUN3 #4: stale-closure clearing (a re-issued TODO stays hidden behind
    # an old BLOCKED-<id>.ready until this moves the signals away).
    p_unblock = sub.add_parser(
        "unblock",
        help="Clear stale BLOCKED/ACK closure signals for a TODO (trace in context/)",
    )
    p_unblock.add_argument("todo_id", help="TODO id (e.g. TODO-0018)")
    p_unblock.add_argument(
        "--project-dir",
        dest="project_dir",
        default=".",
        help="Project root (default: current directory)",
    )

    # RUN3 #5: removal of a TODO that never started (trace in done/<id>/).
    p_todo_remove = sub.add_parser(
        "todo-remove",
        help="Remove a TODO that never started (no .ready/signals/progress; trace in done/)",
    )
    p_todo_remove.add_argument("todo_id", help="TODO id (e.g. TODO-0018)")
    p_todo_remove.add_argument(
        "--project-dir",
        dest="project_dir",
        default=".",
        help="Project root (default: current directory)",
    )

    # RUN5 #2: retire a rejected/abandoned TODO that stays "active"
    # (DONE-{id}.{md,json} without the .ready signal — see awf todo-retire).
    p_todo_retire = sub.add_parser(
        "todo-retire",
        help="Retire a rejected/abandoned TODO to done/<id>/ (RETIRED note, no fake signal)",
    )
    p_todo_retire.add_argument("todo_id", help="TODO id (e.g. TODO-0035)")
    p_todo_retire.add_argument(
        "--reason",
        required=True,
        help="Why the TODO is retired (written to the RETIRED note)",
    )
    p_todo_retire.add_argument(
        "--project-dir",
        dest="project_dir",
        default=".",
        help="Project root (default: current directory)",
    )

    # RUN6 #4: reword a not-started TODO, keeping the number (backup in
    # context/; .ready and the baseline are untouched).
    p_todo_update = sub.add_parser(
        "todo-update",
        help="Reword a not-started TODO, keeping the number (backup in context/)",
    )
    p_todo_update.add_argument("todo_id", help="TODO id (e.g. TODO-0001)")
    _content_group = p_todo_update.add_mutually_exclusive_group(required=True)
    _content_group.add_argument(
        "--content", help="New content (short; one-liners)"
    )
    _content_group.add_argument(
        "--content-file",
        dest="content_file",
        help="New content read from a file",
    )
    p_todo_update.add_argument(
        "--reason",
        default="",
        help="Why the content is reworded (written to the log)",
    )
    p_todo_update.add_argument(
        "--project-dir",
        dest="project_dir",
        default=".",
        help="Project root (default: current directory)",
    )

    p_continue.add_argument(
        "--ack",
        default="",
        help=(
            "Resolve a BLOCKED TODO: writes ACK-{todo}.ready and resumes "
            "from the blocked stage (e.g. --ack TODO-0009)"
        ),
    )

    # New commands (Wave 4c)
    p_init = sub.add_parser("init", help="Initialize .agentic/ skeleton in current project")
    # BD-28: --template removed — UI form (project-setup) configures pipeline + roles.
    # MCP-6: --non-interactive — fully deterministic (stack-detect + name-from-dir).
    #        Interactive prompts remain default for human CLI use; agents use MCP tools.
    p_init.add_argument("--force", action="store_true")
    p_init.add_argument("--dry-run", dest="dry_run", action="store_true")
    p_init.add_argument(
        "--non-interactive",
        dest="non_interactive",
        action="store_true",
        help="Skip all prompts; auto-detect stack + derive project name from dir.",
    )
    p_init.add_argument("--project-dir", default=".", help="Project root (default: cwd)")

    # AUD07-03: the help used to promise a gentle "inbox/outbox/logs" clean
    # while the default actually wipes context/state too. The text
    # now mirrors api.reset_runtime verbatim (same source, checked by test).
    p_reset = sub.add_parser(
        "reset",
        help="Clean runtime data (default: inbox/outbox/context/logs + state)",
        description=(
            "Clean runtime data. Every mode also clears state/current.yaml "
            "and state/run.yaml (when present).\n"
            "\n"
            "  default       inbox, outbox, context, logs\n"
            "  --full        default + handoff, inputs, dashboards\n"
            "  --tasks-only  inbox, outbox\n"
            "  --orphans     remove orphan TODOs (asks for confirmation,\n"
            "                --force to skip)"
        ),
    )
    p_reset.add_argument("--tasks-only", dest="tasks_only", action="store_true",
                         help="Clean only inbox + outbox (+ state files)")
    p_reset.add_argument("--full", action="store_true",
                         help="Default clean + handoff/inputs/dashboards")
    p_reset.add_argument("--orphans", action="store_true",
                         help="Remove orphan TODOs (asks for confirmation)")
    p_reset.add_argument("--force", action="store_true",
                         help="Skip the orphans confirmation prompt")
    p_reset.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )

    p_add_role = sub.add_parser("add-role", help="Generate a new role template")
    p_add_role.add_argument("name")
    p_add_role.add_argument("--description", default="")
    p_add_role.add_argument("--model", default="")
    p_add_role.add_argument(
        "--from-skill",
        dest="from_skill",
        default="",
        metavar="NAME",
        help="Create the role from an opencode skill: SKILL.md body "
        "(front-matter stripped) instead of the empty template",
    )
    p_add_role.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the role file if it already exists",
    )
    p_add_role.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )

    p_baseline = sub.add_parser("baseline", help="Create a baseline snapshot")
    p_baseline.add_argument("todo_id")
    p_baseline.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )

    p_prove_red = sub.add_parser(
        "prove-red",
        help="U4: prove declared tests are red on the baseline sha, green now",
    )
    p_prove_red.add_argument(
        "--todo", dest="todo_id", required=True, help="TODO id (e.g. TODO-0021)"
    )
    p_prove_red.add_argument(
        "--tests",
        nargs="*",
        default=None,
        help="Test files and/or file::test ids "
             "(default: prove_red block of the TODO contract)",
    )
    p_prove_red.add_argument(
        "--json", action="store_true", help="Machine-readable output"
    )
    p_prove_red.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )

    p_verify_pack = sub.add_parser(
        "verify-pack",
        help="U5: run the deterministic verify report (GATES-<todo>.md)",
    )
    p_verify_pack.add_argument(
        "--todo", dest="todo_id", required=True, help="TODO id (e.g. TODO-0022)"
    )
    p_verify_pack.add_argument(
        "--json", action="store_true", help="Machine-readable output"
    )
    p_verify_pack.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )

    p_rollback = sub.add_parser("rollback", help="Rollback to baseline")
    p_rollback.add_argument("todo_id")
    p_rollback.add_argument("--hard", action="store_true")
    p_rollback.add_argument("--soft", action="store_true")
    p_rollback.add_argument("--dry-run", dest="dry_run", action="store_true")
    p_rollback.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )

    p_approve = sub.add_parser("approve", help="Approve auto-commit for a TODO in --auto mode")
    p_approve.add_argument("todo_id")
    p_approve.add_argument(
        "--evidence",
        default="",
        # AUD07-04: parity with the MCP awf_approve tool. In run (забег)
        # mode the API REQUIRES evidence — without this flag the command
        # was unusable from the CLI during a run.
        help="Independent-verification evidence (required in run mode): "
             "the commands you actually ran and your verdict.",
    )
    p_approve.add_argument(
        "--verified-sha",
        dest="verified_sha",
        default="",
        # U11 (B5): the tree fingerprint recorded at verify time (awf
        # tree-sha). Without it — behavior as before; with it — the API
        # refuses when the tree moved since verification.
        help="U11: tree fingerprint from verify time (`awf tree-sha`). "
             "Approve is refused if the tree changed after verification.",
    )
    p_approve.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )

    p_report = sub.add_parser("report", help="Show summary report")
    p_report.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )

    # RUN9 #1: the CLI twin of the MCP awf_kill tool — a stuck pipeline
    # is stoppable from the terminal even when the MCP server is down.
    p_kill = sub.add_parser(
        "kill",
        help="Kill the running pipeline AND its stage worker (recovery without MCP)",
    )
    p_kill.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )

    p_metrics = sub.add_parser(
        "metrics", help="U8: token/cost metrics of the work program (report to desktop)"
    )
    p_metrics.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )
    p_metrics.add_argument(
        "--out",
        default="",
        help="Output file or directory (default: metrics.output_dir / ~/Desktop)",
    )
    p_metrics.add_argument(
        "--json", action="store_true", help="Machine-readable output"
    )
    p_metrics.add_argument(
        "--reference-model",
        dest="reference_model",
        default="",
        help="Model for cost conversion "
             "(default: metrics.reference_model / anthropic/claude-sonnet-4-6)",
    )
    p_metrics.add_argument(
        "--since",
        default="",
        help="Start of the statistics window (ISO date/datetime or epoch)",
    )
    p_metrics.add_argument(
        "--refresh-subscriptions",
        dest="refresh_subscriptions",
        action="store_true",
        help="U8b: update the subscriptions table from metrics.subscriptions_url "
             "before computing (fallback to the built-in table on failure)",
    )
    p_metrics.add_argument(
        "--no-mirror",
        dest="no_mirror",
        action="store_true",
        help="U8c: do not copy the report to metrics.mirror_dir for this run",
    )
    p_metrics.add_argument(
        "--all-projects",
        dest="all_projects",
        action="store_true",
        help="RUN10 #3: do not filter sessions by project — collect the whole "
             "shared opencode.db (default: only the current project)",
    )

    # RUN4 #2: feedback contour — friction with awf becomes a report on
    # the owner's desktop (config feedback.dir, default ~/Desktop).
    p_feedback = sub.add_parser(
        "feedback",
        help="RUN4 #2: write a bug/feature report about awf to the owner's desktop",
    )
    p_feedback.add_argument(
        "--type",
        dest="ftype",
        required=True,
        choices=("bug", "feature"),
        help="Report kind: bug or feature",
    )
    p_feedback.add_argument(
        "--title",
        required=True,
        help="One-line title (becomes the file slug)",
    )
    p_feedback.add_argument(
        "--body",
        default="",
        help="Text for the «Что пытался» section",
    )
    p_feedback.add_argument(
        "--expected",
        default="",
        help="Text for the «Ожидал» section (empty = section not printed)",
    )
    p_feedback.add_argument(
        "--got",
        default="",
        help="Text for the «Что получил» section (empty = section not printed)",
    )
    p_feedback.add_argument(
        "--why",
        default="",
        help="Text for the «Почему мешает» section (empty = section not printed)",
    )
    p_feedback.add_argument(
        "--proposal",
        default="",
        help="Text for the «Предложение» section (empty = section not printed)",
    )
    p_feedback.add_argument(
        "--severity",
        default="",
        choices=("", "low", "medium", "high"),
        help="Severity (low|medium|high; empty = no mark)",
    )
    p_feedback.add_argument(
        "--stdout",
        action="store_true",
        help="Print the report instead of writing a file",
    )
    p_feedback.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )

    p_tree_sha = sub.add_parser(
        "tree-sha",
        help="U11: print the working-tree fingerprint (verified-sha for approve)",
    )
    p_tree_sha.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )

    p_mutations = sub.add_parser(
        "mutations",
        help="U11/B6: run scripts/mutations.txt on a quiet tree (supervisor tool)",
    )
    p_mutations.add_argument(
        "--list",
        action="store_true",
        help="List the mutations without running them",
    )
    p_mutations.add_argument(
        "--file",
        default="scripts/mutations.txt",
        help="Mutations file (default: scripts/mutations.txt)",
    )
    p_mutations.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Test-command timeout per mutation, seconds (default: 1800)",
    )
    p_mutations.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )

    p_todo_draft = sub.add_parser(
        "todo-draft",
        help="U11/E3: generate a task-file skeleton from AUDIT-INDEX.md",
    )
    p_todo_draft.add_argument(
        "query", help="Finding id (e.g. AUD02-03) or unit id (e.g. FU-13)"
    )
    p_todo_draft.add_argument(
        "--index",
        default="",
        help="Path to AUDIT-INDEX.md (default: project dir, .agentic/context/)",
    )
    p_todo_draft.add_argument(
        "--out",
        default="",
        help="Write the draft to a file (default: stdout; an existing file "
        "is not overwritten without --force)",
    )
    p_todo_draft.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing --out file",
    )
    p_todo_draft.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )

    # RUN3 #1: named pipelines — create + list (run by name via --pipeline)
    p_pipeline_write = sub.add_parser(
        "pipeline-write",
        help="Write .agentic/pipelines/<name>.yaml from roles (config/supervisor untouched)",
    )
    p_pipeline_write.add_argument(
        "name",
        help="Pipeline name (letters, digits, '_', '.', '-' — no path separators)",
    )
    p_pipeline_write.add_argument(
        "--role",
        action="append",
        required=True,
        help="Worker role for a stage (repeatable; supervisor plan/verify "
             "stages are added automatically, as in the setup form)",
    )
    p_pipeline_write.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing pipeline file",
    )
    p_pipeline_write.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )

    p_pipelines = sub.add_parser(
        "pipelines",
        help="List pipelines in .agentic/pipelines/ (marks the active one)",
    )
    p_pipelines.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )

    # RUN4 #1: supervisor onboarding/recovery card (live state + tool map)
    p_brief = sub.add_parser(
        "brief",
        help="RUN4 #1: supervisor onboarding/recovery card (live state + tool map)",
    )
    p_brief.add_argument(
        "--json", action="store_true", help="Machine-readable output"
    )
    p_brief.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )

    p_analyze = sub.add_parser(
        "analyze-roles",
        help="BD-31: analyze team roles for overlaps, add pipeline-specific disambiguation",
    )
    p_analyze.add_argument(
        "--project-dir",
        default=".",
        help="Path to project root (default: current directory)",
    )
    p_analyze.add_argument(
        "--dry-run",
        action="store_true",
        help="Show proposed patches without writing to role files",
    )

    return parser, sub


def _run_command(args) -> int:
    """AUD07-05: one error handler for every subcommand — mirrors the MCP
    ``_exec`` wrapper. Corrupted inputs (garbage baseline sha, EOF on a
    prompt, non-UTF-8 pipeline) used to leak a raw traceback to a human;
    now they get a clean ``ERROR: <type>: <msg>`` line and rc 1.

    SystemExit (argparse --help / bad args) and KeyboardInterrupt are
    BaseExceptions — intentionally NOT caught here.
    """
    try:
        if args.command == "restore":
            from . import cmd_restore
            return cmd_restore.run(args)
        if args.command == "unblock":
            from . import cmd_unblock
            return cmd_unblock.run(args)
        if args.command == "todo-remove":
            from . import cmd_todo_remove
            return cmd_todo_remove.run(args)
        if args.command == "todo-retire":
            from . import cmd_todo_retire
            return cmd_todo_retire.run(args)
        if args.command == "todo-update":
            from . import cmd_todo_update
            return cmd_todo_update.run(args)
        if args.command == "status":
            return cmd_status.run(args)
        if args.command in ("start", "continue"):
            return cmd_start.run(args)
        if args.command == "init":
            from . import cmd_init
            return cmd_init.run(args)
        if args.command == "reset":
            from . import cmd_reset
            return cmd_reset.run(args)
        if args.command == "add-role":
            from . import cmd_add_role
            return cmd_add_role.run(args)
        if args.command == "baseline":
            from . import cmd_baseline
            return cmd_baseline.run(args)
        if args.command == "prove-red":
            from . import cmd_prove_red
            return cmd_prove_red.run(args)
        if args.command == "verify-pack":
            from . import cmd_verify_pack
            return cmd_verify_pack.run(args)
        if args.command == "rollback":
            from . import cmd_rollback
            return cmd_rollback.run(args)
        if args.command == "approve":
            from . import cmd_approve
            return cmd_approve.run(args)
        if args.command == "report":
            from . import cmd_report
            return cmd_report.run(args)
        if args.command == "kill":
            from . import cmd_kill
            return cmd_kill.run(args)
        if args.command == "pipeline-write":
            from . import cmd_pipeline_write
            return cmd_pipeline_write.run(args)
        if args.command == "pipelines":
            from . import cmd_pipelines
            return cmd_pipelines.run(args)
        if args.command == "analyze-roles":
            from . import cmd_analyze_roles
            return cmd_analyze_roles.run(args)
        if args.command == "metrics":
            from . import cmd_metrics
            return cmd_metrics.run(args)
        if args.command == "feedback":
            from . import cmd_feedback
            return cmd_feedback.run(args)
        if args.command == "brief":
            from . import cmd_brief
            return cmd_brief.run(args)
        if args.command == "tree-sha":
            from . import cmd_tree_sha
            return cmd_tree_sha.run(args)
        if args.command == "mutations":
            from . import cmd_mutations
            return cmd_mutations.run(args)
        if args.command == "todo-draft":
            from . import cmd_todo_draft
            return cmd_todo_draft.run(args)
        return 1
    except AwfApiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except (EOFError, subprocess.CalledProcessError, OSError, UnicodeDecodeError) as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — last line of defense, mirrors _exec
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

