"""Argparse dispatcher — routes subcommands to cmd_*.py modules."""
import argparse
import sys

from . import cmd_start
from . import cmd_status


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

    p_continue = sub.add_parser("continue", help="Resume an interrupted pipeline")
    _add_start_args(p_continue)

    args = parser.parse_args(argv)

    if args.command == "status":
        return cmd_status.run(args)
    if args.command in ("start", "continue"):
        return cmd_start.run(args)

    print(
        f"awf: '{args.command}' not implemented in Python yet",
        file=sys.stderr,
    )
    return 2
