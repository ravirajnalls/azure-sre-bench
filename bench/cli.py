"""Command-line interface for Azure SRE Bench.

Issue #1 scaffolded the subcommand surface; `validate` and `list` are live as of
issue #2, `grade` as of issue #3, `run` as of issue #4, `site` as of issue #40,
`leaderboard` as of issue #41, and `catalog` as of issue #42: the full
subcommand surface is implemented.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bench import __version__
from bench.grader import grade
from bench.tasks import difficulty_distribution, load_tasks

SUBCOMMANDS = ("validate", "list", "grade", "run", "site", "leaderboard", "catalog")

IMPLEMENTED = ("validate", "list", "grade", "run", "site", "leaderboard", "catalog")


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
        elif name == "run":
            run_parser = sub.add_parser(
                name, help="run an agent against tasks and record graded results"
            )
            run_parser.add_argument(
                "--agent", required=True, help="agent command string to execute"
            )
            target = run_parser.add_mutually_exclusive_group(required=True)
            target.add_argument("--task", help="task id to run")
            target.add_argument(
                "--all", dest="all_tasks", action="store_true", help="run every task"
            )
            run_parser.add_argument(
                "--name", default="default", help="run name under results/ (default)"
            )
            run_parser.add_argument(
                "--timeout",
                type=int,
                default=600,
                help="per-task timeout in seconds (default 600)",
            )
            run_parser.add_argument(
                "--tasks-root",
                default="tasks",
                help="task discovery root (default: ./tasks)",
            )
        elif name == "leaderboard":
            leaderboard_parser = sub.add_parser(
                name, help="print the markdown leaderboard from results/"
            )
            leaderboard_parser.add_argument(
                "--results-root",
                default="results",
                help="results root directory (default: ./results)",
            )
        elif name == "site":
            site_parser = sub.add_parser(
                name, help="build the static leaderboard site from results/"
            )
            site_parser.add_argument(
                "--results-root",
                default="results",
                help="results directory of run folders (default: results)",
            )
            site_parser.add_argument(
                "--out",
                default="site/index.html",
                help="output HTML path (default: site/index.html)",
            )
        elif name == "validate":
            validate_parser = sub.add_parser(
                name, help="validate every task directory under the root"
            )
            validate_parser.add_argument(
                "root",
                nargs="?",
                default="tasks",
                help="tasks root directory (default: tasks)",
            )
        elif name == "list":
            list_parser = sub.add_parser(name, help="list tasks with difficulty and title")
            list_parser.add_argument(
                "root",
                nargs="?",
                default="tasks",
                help="tasks root directory (default: tasks)",
            )
            list_parser.add_argument(
                "--stats",
                action="store_true",
                help="print the difficulty distribution after the task list",
            )
        elif name == "catalog":
            catalog_parser = sub.add_parser(
                name, help="render the task catalog to a Markdown file"
            )
            catalog_parser.add_argument(
                "root",
                nargs="?",
                default="tasks",
                help="tasks root directory (default: tasks)",
            )
            catalog_parser.add_argument(
                "--output",
                default="docs/TASKS.md",
                help="catalog output path (default: docs/TASKS.md)",
            )
    return parser


def find_task_dir(task_id: str) -> Path:
    """Locate a task directory by ID under ./tasks."""
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


def cmd_validate(root: Path) -> int:
    results = load_tasks(root)
    for result in results:
        if result.errors:
            print(f"FAIL {result.path}")
            for reason in result.errors:
                print(f"  - {reason}")
    for result in results:
        if not result.errors and result.warnings:
            print(f"WARN {result.path}")
            for reason in result.warnings:
                print(f"  - {reason}")
    valid = sum(1 for result in results if not result.errors)
    warning_count = sum(len(result.warnings) for result in results)
    suffix = f", {warning_count} warning{'s' if warning_count != 1 else ''}"
    print(f"{valid}/{len(results)} tasks valid{suffix}")
    if not results:
        print(f"no tasks found under {root}", file=sys.stderr)
        return 1
    return 0 if valid == len(results) else 1


def cmd_list(root: Path, stats: bool = False) -> int:
    results = load_tasks(root)
    for result in sorted(results, key=lambda item: (item.id, str(item.path))):
        config: dict = result.config or {}
        difficulty = config.get("difficulty") or "-"
        title = config.get("title") or "-"
        print(f"{result.id}  {difficulty}  {title}")
    if stats:
        distribution = difficulty_distribution(results)
        total = sum(distribution.values())
        counts = ", ".join(
            f"{count} {level}" for level, count in distribution.items()
        )
        print(f"\n{total} tasks: {counts}")
    return 0


def cmd_leaderboard(args: argparse.Namespace) -> int:
    """Print the markdown leaderboard table from `--results-root` (issue #41)."""
    from bench.leaderboard import leaderboard_markdown  # deferred: keeps --help cheap

    print(leaderboard_markdown(Path(getattr(args, "results_root", "results"))))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command in IMPLEMENTED:
        if args.command == "grade":
            return cmd_grade(args)
        if args.command == "run":
            from bench.run import cmd_run  # deferred: keeps --help cheap

            return cmd_run(args)
        if args.command == "site":
            from bench.site import cmd_site  # deferred: keeps --help cheap

            return cmd_site(args)
        root = Path(getattr(args, "root", "tasks"))
        if args.command == "validate":
            return cmd_validate(root)
        if args.command == "catalog":
            from bench.catalog import cmd_catalog  # deferred: keeps --help cheap

            return cmd_catalog(root, Path(args.output))
        if args.command == "leaderboard":
            return cmd_leaderboard(args)
        return cmd_list(root, stats=bool(getattr(args, "stats", False)))
    print(
        f"'bench {args.command}' is not implemented yet; "
        "all subcommands are implemented; this should be unreachable.",
        file=sys.stderr,
    )
    return 2
