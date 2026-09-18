"""Argparse dispatcher — routes subcommands to cmd_*.py modules."""
from __future__ import annotations

import argparse
import sys

from . import cmd_start, cmd_status


def _print_top_level_help() -> int:
    """Print the high-level usage that `awf help` and bare `awf` both show."""
    print(
        "awf — Agentic Workflow Framework\n"
        "\n"
        "Usage: awf <command> [options]\n"
        "\n"
        "Commands:\n"
        "  init              Initialize .agentic/ in current project\n"
        "  start             Start the pipeline from the beginning\n"
        "  continue          Resume an interrupted pipeline\n"
        "  status            Show current workflow state\n"
        "  report            Show summary report\n"
        "  add-role <name>   Generate a template for a new role\n"
        "  approve <id>      Approve auto-commit for a TODO in --auto mode\n"
        "  baseline <id>     Create a baseline snapshot\n"
        "  rollback <id>     Rollback to baseline\n"
        "  reset             Clean runtime data (inbox/outbox/logs)\n"
        "  analyze-roles     BD-31: analyze team roles, add disambiguation patches\n"
        "  help              Show this message\n"
        "\n"
        "Run 'awf <command> --help' for command-specific options.\n"
        "\n"
        "Options for 'start' / 'continue':\n"
        "  --pipeline <name>    Pipeline to run (default: from config.yaml)\n"
        "  --from-stage <name>  Start from a specific stage\n"
        "  --auto               Skip supervisor interactive waits\n"
        "  --background         (start only) Run detached via setsid — writes\n"
        "                       to .agentic/logs/awf-start.out\n"
        "  --timeout <seconds>  Agent timeout (default: 3600)\n"
        "  --ack <TODO-NNNN>    (continue only) Accept a BLOCKED TODO and resume"
    )
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
        return _print_top_level_help()

    return _dispatch_subcommand(raw)


def _dispatch_subcommand(argv):
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
        "--background",
        action="store_true",
        help="Run detached (setsid) — don't block the terminal. Writes to "
             ".agentic/logs/awf-start.out.",
    )

    p_continue = sub.add_parser("continue", help="Resume an interrupted pipeline")
    _add_start_args(p_continue)
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

    p_reset = sub.add_parser("reset", help="Clean runtime data")
    p_reset.add_argument("--tasks-only", dest="tasks_only", action="store_true")
    p_reset.add_argument("--full", action="store_true")
    p_reset.add_argument("--orphans", action="store_true")
    p_reset.add_argument("--force", action="store_true")
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

    args = parser.parse_args(argv)

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

