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
        if args.command == "analyze-roles":
            from . import cmd_analyze_roles
            return cmd_analyze_roles.run(args)
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

