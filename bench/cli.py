"""Command-line interface for Azure SRE Bench.

Issue #1 scaffolded the subcommand surface; `validate` and `list` are live as of
issue #2, `grade` as of issue #3, `run` as of issue #4, `site` as of issue #40,
`leaderboard` as of issue #41, `catalog` as of issue #42, `verify` as of issue
#118, `submit` and `prompt` as of issue #124, `new` as of issue #120,
`mcp-serve` as of issue #123, `sweep` as of issue #125, `export` as of
issue #117, `explain` as of issue #149, and `lint-fixtures` as of issue #151:
the full subcommand surface is implemented. `bench run --release` (issue #116)
checks a run against a frozen release manifest in `releases/`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bench import __version__
import bench.fixture_leaks  # noqa: F401 — registers the leaks/size checkers (issue #154)
import bench.fixture_lint  # noqa: F401 — registers the consistency checker (issue #152)
from bench.export import SUPPORTED_FORMATS
from bench.heldout import ENCRYPTED_SUFFIX, HeldoutKeyError, load_hidden_json
from bench.remediation import grade_for_task
from bench.tasks import difficulty_distribution, load_tasks

SUBCOMMANDS = (
    "validate", "list", "grade", "run", "verify", "submit", "prompt", "site",
    "leaderboard", "catalog", "new", "mcp-serve", "sweep", "export", "explain",
    "lint-fixtures",
)

IMPLEMENTED = SUBCOMMANDS


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
            run_parser.add_argument(
                "--split",
                choices=("public", "heldout", "all"),
                default="all",
                help="restrict the run to one split (default: all)",
            )
            run_parser.add_argument(
                "--repeat",
                type=int,
                default=1,
                metavar="N",
                help=(
                    "run each task N times for run-to-run variance "
                    "(issue #162): records land in results/<name>/<task>.<k>.json "
                    "(default: 1, one <task>.json per task)"
                ),
            )
            run_parser.add_argument(
                "--release",
                metavar="VERSION",
                help=(
                    "run against a frozen release (releases/VERSION.json, "
                    "issue #116): verifies the corpus matches the manifest "
                    "and stamps results with the release name"
                ),
            )
        elif name == "submit":
            submit_parser = sub.add_parser(
                name,
                help="grade and record a closed agent's answer (manual protocol, issue #124)",
            )
            submit_parser.add_argument(
                "--task", required=True, help="task ID, e.g. aks-networking-001"
            )
            submit_parser.add_argument(
                "--answer",
                required=True,
                help="path to the agent's answer JSON file",
            )
            submit_parser.add_argument(
                "--agent",
                required=True,
                help="agent/run name under results/ (the leaderboard entry)",
            )
            submit_parser.add_argument(
                "--results-root",
                default="results",
                help="results root directory (default: ./results)",
            )
        elif name == "prompt":
            prompt_parser = sub.add_parser(
                name,
                help="print the paste-ready manual-protocol prompt for a task (issue #124)",
            )
            prompt_parser.add_argument(
                "--task", required=True, help="task ID, e.g. aks-networking-001"
            )
            prompt_parser.add_argument(
                "--tasks-root",
                default="tasks",
                help="task discovery root (default: ./tasks)",
            )
        elif name == "mcp-serve":
            mcp_parser = sub.add_parser(
                name,
                help="serve one task over the Model Context Protocol (stdio)",
            )
            mcp_parser.add_argument(
                "--task",
                required=True,
                help="task ID to serve, e.g. aks-networking-001",
            )
            mcp_parser.add_argument(
                "--tasks-root",
                default="tasks",
                help="task discovery root (default: ./tasks)",
            )
            mcp_parser.add_argument(
                "--answer-file",
                default=None,
                help=(
                    "where submit_answer writes the contract JSON "
                    "(default: $BENCH_MCP_ANSWER_FILE or ./bench-mcp-answer.json)"
                ),
            )
        elif name == "export":
            export_parser = sub.add_parser(
                name,
                help="export the public dataset, eval-side rubrics, and dataset card (issue #117)",
            )
            export_parser.add_argument(
                "--format",
                dest="format",
                choices=SUPPORTED_FORMATS,
                default="jsonl",
                help="export format (default: jsonl)",
            )
            export_parser.add_argument(
                "--out",
                default="dist",
                help="output directory (default: dist)",
            )
            export_parser.add_argument(
                "--tasks-root",
                default="tasks",
                help="task discovery root (default: ./tasks)",
            )
        elif name == "verify":
            verify_parser = sub.add_parser(
                name,
                help="re-grade stored results in a run folder (issue #118)",
            )
            verify_parser.add_argument(
                "run",
                help="run folder to verify, e.g. results/naive",
            )
            verify_parser.add_argument(
                "--tasks-root",
                default="tasks",
                help="task discovery root (default: ./tasks)",
            )
        elif name == "new":
            new_parser = sub.add_parser(
                name,
                help="scaffold the next free task ID for a category (issue #120)",
            )
            new_parser.add_argument(
                "category",
                help="task category, e.g. aks-networking (see AGENTS.md)",
            )
            new_parser.add_argument(
                "--title",
                required=True,
                help='one-line task title, e.g. "Ingress 502 after cert rotation"',
            )
            new_parser.add_argument(
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
            leaderboard_parser.add_argument(
                "--tasks-root",
                default="tasks",
                help="task discovery root for rubric-hash staleness (default: ./tasks)",
            )
            leaderboard_parser.add_argument(
                "--by",
                choices=["category", "difficulty"],
                help="print a pass-rate breakdown by category or difficulty "
                "(issue #165) instead of the ranking",
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
            site_parser.add_argument(
                "--tasks-root",
                default="tasks",
                help="task discovery root for rubric-hash staleness (default: ./tasks)",
            )
            site_parser.add_argument(
                "--learn-out",
                default="site/learn.html",
                help="output Learn page path (default: site/learn.html)",
            )
        elif name == "explain":
            explain_parser = sub.add_parser(
                name,
                help="print a task's explainer.md (spoiler: full write-up, issue #149)",
            )
            explain_parser.add_argument(
                "--task", required=True, help="task ID, e.g. aks-identity-008"
            )
            explain_parser.add_argument(
                "--tasks-root",
                default="tasks",
                help="task discovery root (default: ./tasks)",
            )
        elif name == "lint-fixtures":
            from bench.lint_fixtures import CHECKS  # registry of check names

            lf_parser = sub.add_parser(
                name,
                help="lint task fixtures with registered checkers (issues #151, #152, #154)",
            )
            lf_parser.add_argument(
                "--check",
                choices=sorted(CHECKS),
                default=None,
                help=(
                    "which checker to run (default: every registered checker; "
                    "'az' validates canned az JSON fixtures, issue #151; "
                    "'leaks' and 'size' are error-severity, issue #154)"
                ),
            )
            lf_parser.add_argument(
                "--max-lines",
                type=int,
                default=None,
                help=(
                    "size-check override: max lines per fixture file "
                    "(default 150, issue #154)"
                ),
            )
            lf_parser.add_argument(
                "--tasks-root",
                default="tasks",
                help="task discovery root (default: ./tasks)",
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
        elif name == "sweep":
            sweep_parser = sub.add_parser(
                name,
                help="run a configured list of agents across the corpus (issue #125)",
            )
            sweep_parser.add_argument(
                "--config",
                default="sweeps/baseline.yaml",
                help="sweep config file (default: sweeps/baseline.yaml)",
            )
            sweep_parser.add_argument(
                "--tasks-root",
                default="tasks",
                help="task discovery root (default: ./tasks)",
            )
            sweep_parser.add_argument(
                "--results-root",
                default="results",
                help="results root directory (default: ./results)",
            )
            sweep_parser.add_argument(
                "--timeout",
                type=int,
                default=None,
                help="per-task timeout override in seconds",
            )
            sweep_parser.add_argument(
                "--dry-run",
                action="store_true",
                help="print the sweep plan without running anything",
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


def find_task_dir(task_id: str, tasks_root: Path | str = "tasks") -> Path:
    """Locate a task directory by ID under the given tasks root.

    Held-out tasks store their rubric encrypted (expected.json.age), so either
    form of the rubric identifies the task (issue #115).
    """
    for candidate in sorted(Path(tasks_root).rglob(task_id)):
        if candidate.is_dir() and (
            (candidate / "expected.json").is_file()
            or (candidate / f"expected.json{ENCRYPTED_SUFFIX}").is_file()
        ):
            return candidate
    raise FileNotFoundError(
        f"no task directory named '{task_id}' with expected.json under {tasks_root}/"
    )


def cmd_grade(args: argparse.Namespace) -> int:
    """Grade `--answer` against the task's expected.json and print the result JSON."""
    try:
        task_dir = find_task_dir(args.task)
        from bench.run import load_task  # deferred: keeps --help cheap

        # A directory without task.yaml (grader-only test trees) is simply public.
        task = load_task(task_dir) if (task_dir / "task.yaml").is_file() else {}
        rubric = load_hidden_json(task_dir, "expected.json", config=task)
        answer = json.loads(Path(args.answer).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, HeldoutKeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(grade_for_task(answer, rubric, task_dir, task), indent=2))
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    """Print a task's explainer.md to stdout (issue #149)."""
    try:
        task_dir = find_task_dir(args.task, Path(args.tasks_root))
        explainer = (task_dir / "explainer.md").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(explainer, end="" if explainer.endswith("\n") else "\n")
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    """Grade a closed agent's answer and record it marked `mode: manual` (#124)."""
    from bench.manual import submit_answer  # deferred: keeps --help cheap

    try:
        answer = json.loads(Path(args.answer).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not isinstance(answer, dict):
        print(f"error: {args.answer} must contain a JSON object", file=sys.stderr)
        return 2
    try:
        record = submit_answer(
            args.task, answer, args.agent, results_root=Path(args.results_root)
        )
    except (FileNotFoundError, ValueError, HeldoutKeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(record, indent=2))
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


def cmd_verify(args: argparse.Namespace) -> int:
    """Re-grade every stored result in `args.run` and report mismatches (issue #118)."""
    from bench.verify import verify_run  # deferred: keeps --help cheap

    report = verify_run(Path(args.run), Path(args.tasks_root))
    for error in report["errors"]:
        print(f"ERROR {error}")
    for mismatch in report["mismatches"]:
        print(f"MISMATCH {mismatch}")
    clean = report["checked"] - len(report["mismatches"])
    print(
        f"{clean}/{report['checked']} results verified; "
        f"{len(report['mismatches'])} mismatch(es); "
        f"{len(report['errors'])} error(s)"
    )
    return 0 if not report["errors"] and not report["mismatches"] else 1


def cmd_export(args: argparse.Namespace) -> int:
    """Export the public dataset, eval-side file, and dataset card (issue #117)."""
    from bench.export import export_dataset  # deferred: keeps --help cheap

    summary = export_dataset(Path(args.tasks_root), Path(args.out))
    for path in summary.files:
        print(f"wrote {path}")
    notes = []
    if summary.heldout_skipped:
        notes.append(
            f"{summary.heldout_skipped} held-out task(s) excluded from the public export"
        )
    if summary.eval_locked:
        notes.append(
            f"{summary.eval_locked} held-out task(s) missing from the eval file "
            "(no usable HELDOUT_KEY)"
        )
    if summary.invalid_skipped:
        notes.append(f"{summary.invalid_skipped} invalid task(s) skipped")
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    print(
        f"exported {summary.public_count} public task(s) and "
        f"{summary.eval_count} eval record(s)"
    )
    return 0


def cmd_leaderboard(args: argparse.Namespace) -> int:
    """Print the markdown leaderboard table from `--results-root` (issue #41)."""
    from bench.leaderboard import (  # deferred: keeps --help cheap
        breakdown_markdown,
        leaderboard_markdown,
    )

    if getattr(args, "by", None):
        # Issue #165: category/difficulty breakdown instead of the ranking.
        print(breakdown_markdown(Path(args.results_root), Path(args.tasks_root), args.by))
        return 0
    print(leaderboard_markdown(Path(args.results_root), Path(args.tasks_root)))
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    """Scaffold the next free task ID for a category (issue #120)."""
    from bench.new import scaffold_task  # deferred: keeps --help cheap

    try:
        task_dir = scaffold_task(Path(args.tasks_root), args.category, args.title)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Scaffolded {task_dir}")
    print("Next steps:")
    print("  1. Replace every <REPLACE-ME-...> placeholder (see CONTRIBUTING.md).")
    print("  2. Check with: python -m bench validate")
    print("  3. Verify integrity with: pytest -q")
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
        if args.command == "sweep":
            from bench.sweep import cmd_sweep  # deferred: keeps --help cheap

            return cmd_sweep(args)
        if args.command == "lint-fixtures":
            from bench.fixture_leaks import set_max_lines
            from bench.lint_fixtures import cmd_lint_fixtures  # deferred: keeps --help cheap

            if args.max_lines is not None:
                set_max_lines(args.max_lines)
                try:
                    return cmd_lint_fixtures(args)
                finally:
                    set_max_lines(None)  # the override is per-invocation
            return cmd_lint_fixtures(args)
        if args.command == "submit":
            return cmd_submit(args)
        if args.command == "explain":
            return cmd_explain(args)
        if args.command == "prompt":
            from bench.manual import SYSTEM_PROMPT, load_task_prompt

            task_dir = find_task_dir(args.task, Path(args.tasks_root))
            print("# system\n")
            print(SYSTEM_PROMPT)
            print("\n# user\n")
            print(load_task_prompt(task_dir))
            return 0
        if args.command == "site":
            from bench.site import cmd_site  # deferred: keeps --help cheap

            return cmd_site(args)
        if args.command == "mcp-serve":
            from bench.mcp_server import cmd_mcp_serve  # deferred: keeps --help cheap

            return cmd_mcp_serve(args)
        if args.command == "export":
            return cmd_export(args)
        root = Path(getattr(args, "root", "tasks"))
        if args.command == "validate":
            return cmd_validate(root)
        if args.command == "new":
            return cmd_new(args)
        if args.command == "catalog":
            from bench.catalog import cmd_catalog  # deferred: keeps --help cheap

            return cmd_catalog(root, Path(args.output))
        if args.command == "leaderboard":
            return cmd_leaderboard(args)
        if args.command == "verify":
            return cmd_verify(args)
        return cmd_list(root, stats=bool(getattr(args, "stats", False)))
    print(
        f"'bench {args.command}' is not implemented yet; "
        "all subcommands are implemented; this should be unreachable.",
        file=sys.stderr,
    )
    return 2
