"""Command-line interface for Azure SRE Bench.

Subcommand surface declared in issue #1. `grade` lands with issue #3; the rest
remain stubs until the loader/runner (issues #2 and #4).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bench import __version__
from bench.grader import grade

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
        if name == "grade":
            grade_parser = sub.add_parser(
                name, help="grade an answer against a task's hidden rubric"
            )
            grade_parser.add_argument(
                "--task", required=True, help="task ID, e.g. aks-networking-001"
            )
            grade_parser.add_argument(
                "--answer",
                required=True,
                help="path to the agent's answer JSON file",
            )
        else:
            sub.add_parser(name)
    return parser


def find_task_dir(task_id: str) -> Path:
    """Locate a task directory by ID under ./tasks (loader arrives with issue #2)."""
    tasks_root = Path("tasks")
    for candidate in sorted(tasks_root.rglob(task_id)):
        if candidate.is_dir() and (candidate / "expected.json").is_file():
            return candidate
    raise FileNotFoundError(
        f"no task directory named '{task_id}' with expected.json under {tasks_root}/"
    )


def cmd_grade(args: argparse.Namespace) -> int:
    """Grade `--answer` against the task's expected.json and print the result JSON."""
    try:
        task_dir = find_task_dir(args.task)
        rubric = json.loads((task_dir / "expected.json").read_text(encoding="utf-8"))
        answer = json.loads(Path(args.answer).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(grade(answer, rubric), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "grade":
        return cmd_grade(args)
    print(
        f"'bench {args.command}' is not implemented yet; "
        "see issues #2 and #4 for the planned subcommands.",
        file=sys.stderr,
    )
    return 2
