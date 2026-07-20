"""Argparse dispatcher — routes subcommands to cmd_*.py modules."""
import argparse
import sys

from . import cmd_status


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

    args = parser.parse_args(argv)

    if args.command == "status":
        return cmd_status.run(args)

    print(
        f"awf: '{args.command}' not implemented in Python yet",
        file=sys.stderr,
    )
    return 2
