"""Agent runner for `bench run` (issue #4).

Executes an agent command against each task in an isolated workspace: the agent
sees only `fixtures/` and `symptom.md` — never the hidden `expected.json` or
`solution.json`. The runner feeds the symptom on stdin, enforces a timeout,
parses whatever JSON the agent printed, grades it against the hidden rubric, and
records one result file per task.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from bench.grader import grade

TASKS_DIR = Path("tasks")
RESULTS_DIR = Path("results")
HIDDEN_FILES = ("expected.json", "solution.json")
STDERR_TAIL_BYTES = 2048
DEFAULT_NAME = "default"
DEFAULT_TIMEOUT_S = 600


def load_task(task_dir: Path) -> dict:
    """Read a task's `task.yaml` (metadata only; validation is issue #2's scope)."""
    with open(Path(task_dir) / "task.yaml", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def discover_tasks(tasks_root: Path) -> list[Path]:
    """Every directory under `tasks_root` holding a `task.yaml`, ordered by task id."""
    task_dirs = [path.parent for path in Path(tasks_root).rglob("task.yaml")]
    return sorted(task_dirs, key=lambda d: (str(load_task(d).get("id", d.name)), d.name))


def prepare_workspace(task_dir: Path, workspace: Path) -> None:
    """Copy only the agent-visible files into the workspace.

    The information boundary: `expected.json` and `solution.json` must never be
    copied where the agent can read them.
    """
    fixtures_src = Path(task_dir) / "fixtures"
    if fixtures_src.is_dir():
        shutil.copytree(fixtures_src, Path(workspace) / "fixtures")
    task = load_task(task_dir)
    title = task.get("title") or task.get("id") or "Incident"
    symptom_md = (
        f"# {title}\n\n{task.get('symptom', '')}\n\nEvidence is in `fixtures/`.\n"
    )
    (Path(workspace) / "symptom.md").write_text(symptom_md, encoding="utf-8")


def resolve_command(command: str) -> list[str]:
    """Split an agent command, resolving relative file paths to absolute.

    The agent runs with cwd = its workspace, so a relative path in the command
    (e.g. `agents/naive.py`) would otherwise break. Paths are resolved against
    the caller's cwd; flags and non-existent tokens are left untouched.
    """
    resolved = []
    for token in shlex.split(command):
        if (
            not token.startswith("-")
            and not os.path.isabs(token)
            and os.path.isfile(token)
        ):
            token = os.path.abspath(token)
        resolved.append(token)
    return resolved


def parse_answer(stdout: str) -> dict:
    """Parse the agent's answer from stdout.

    Whole output as JSON first; otherwise the last line that parses to a JSON
    object — so an agent that logs before answering still gets graded. Returns
    `{}` when nothing parseable is found.
    """
    try:
        obj = json.loads(stdout)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return {}


def _load_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def run_task(
    task_dir: Path,
    agent_command: str,
    name: str = DEFAULT_NAME,
    timeout: int = DEFAULT_TIMEOUT_S,
    results_root: Path | None = None,
) -> dict:
    """Run one agent against one task and write `results/<name>/<task-id>.json`."""
    results_root = Path(results_root or RESULTS_DIR)
    task_dir = Path(task_dir)
    task = load_task(task_dir)
    task_id = str(task.get("id") or task_dir.name)

    workspace = Path(tempfile.mkdtemp(prefix=f"bench-{task_id}-"))
    try:
        prepare_workspace(task_dir, workspace)
        env = dict(os.environ, TASK_DIR=str(workspace), TASK_ID=task_id)
        symptom = str(task.get("symptom", ""))

        start = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(
                resolve_command(agent_command),
                cwd=workspace,
                env=env,
                input=symptom,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            # A timeout is a graded FAIL result, never a crash.
            timed_out = True
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            returncode = None
        except OSError as exc:
            # Spawn failure (missing interpreter, non-executable script, ...) is
            # also a graded FAIL result, never a crash.
            stdout = ""
            stderr = str(exc)
            returncode = None
        elapsed_s = round(time.monotonic() - start, 3)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    answer = parse_answer(stdout)
    result = grade(answer, _load_json(task_dir / "expected.json"))
    record = {
        "task": task_id,
        "elapsed_s": elapsed_s,
        "answer": answer,
        "stderr_tail": stderr[-STDERR_TAIL_BYTES:],
        "score": result["score"],
        "passed": result["passed"],
        "detail": result["detail"],
        "timed_out": timed_out,
        "returncode": returncode,
    }

    out_dir = Path(results_root) / name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{task_id}.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )
    return record


def run_tasks(
    task_dirs: list[Path],
    agent_command: str,
    name: str = DEFAULT_NAME,
    timeout: int = DEFAULT_TIMEOUT_S,
    results_root: Path | None = None,
) -> int:
    """Run a sequence of tasks, printing per-task lines and a final summary."""
    results_root = Path(results_root or RESULTS_DIR)
    passed = 0
    for task_dir in task_dirs:
        record = run_task(task_dir, agent_command, name, timeout, results_root)
        mark = "PASS" if record["passed"] else "FAIL"
        print(
            f"{mark} {record['task']} score={record['score']} {record['elapsed_s']}s"
        )
        passed += bool(record["passed"])
    print(f"{name}: {passed}/{len(task_dirs)} passed")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """CLI entry for `bench run`."""
    task_dirs = discover_tasks(Path(args.tasks_root))
    if args.task is not None:
        task_dirs = [
            d
            for d in task_dirs
            if d.name == args.task or str(load_task(d).get("id")) == args.task
        ]
        if not task_dirs:
            print(f"Unknown task: {args.task}", file=sys.stderr)
            return 2
    if not args.all_tasks and args.task is None:
        print("Specify --task <id> or --all.", file=sys.stderr)
        return 2
    return run_tasks(task_dirs, args.agent, args.name, args.timeout)
