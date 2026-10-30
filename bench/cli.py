"""Command-line interface for Azure SRE Bench.

Stub for issue #1: `--help` works and the subcommand surface is declared, but the
subcommands themselves land with the loader/grader/runner (issues #2-#4).
"""

from __future__ import annotations

import argparse
import sys

from bench import __version__

SUBCOMMANDS = ("validate", "list", "grade", "run", "site", "leaderboard", "catalog")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bench",
        description="Azure SRE Bench: evaluate AI agents on Azure/AKS incident tasks.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command")
    for name in SUBCOMMANDS:
        sub.add_parser(name)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    print(
        f"'bench {args.command}' is not implemented yet; "
        "see issues #2-#4 for the planned subcommands.",
        file=sys.stderr,
    )
    return 2
