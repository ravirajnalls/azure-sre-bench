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

from bench.grader import cap_evidence, grade
from bench.heldout import HeldoutKeyError, load_hidden_json, split_of
from bench.sim import COMMANDS_ENV, COMMANDS_FILENAME, LOG_ENV, LOG_FILENAME, SIM_DIR

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
    copied where the agent can read them. `_commands.yaml` is excluded too: it
    configures the simulated CLIs (issue #111) and reaches the agent through
    the shim environment, not as a one-file answer dump in `fixtures/`.
    """
    fixtures_src = Path(task_dir) / "fixtures"
    if fixtures_src.is_dir():
        shutil.copytree(
            fixtures_src,
            Path(workspace) / "fixtures",
            ignore=shutil.ignore_patterns(COMMANDS_FILENAME),
        )
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


def _with_sim_env(env: dict[str, str], task_dir: Path, workspace: Path) -> dict[str, str]:
    """Activate the simulated CLIs when the task ships `fixtures/_commands.yaml`.

    The only runner change needed for issue #111: put `bench/sim` first on the
    agent's PATH so `kubectl`/`az` resolve to the shims, and tell the shims
    where the commands file and the invocation log live.
    """
    commands_file = Path(task_dir) / "fixtures" / COMMANDS_FILENAME
    if not commands_file.is_file():
        return env
    env = dict(env)
    env["PATH"] = os.pathsep.join([str(SIM_DIR), env.get("PATH", os.defpath)])
    env[COMMANDS_ENV] = str(commands_file)
    env[LOG_ENV] = str(Path(workspace) / LOG_FILENAME)
    return env


def _load_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_text(path: Path) -> str:
    """Read a workspace file, or "" when it is absent/unreadable.

    An unreadable command trace is telemetry loss, never a run failure — the
    log only exists when the task simulates CLIs and the agent ran commands.
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _load_rubric(task_dir: Path, task: dict) -> dict:
    """The hidden rubric for a task, decrypting held-out tasks (issue #115).

    A held-out task without a usable HELDOUT_KEY raises `HeldoutKeyError` —
    that is an operator error, never an agent failure, so it is not graded;
    `run_tasks` skips such tasks. Public tasks keep the lenient legacy read.
    """
    if split_of(task) == "heldout":
        return load_hidden_json(task_dir, "expected.json", config=task)
    return _load_json(task_dir / "expected.json")


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
        env = _with_sim_env(
            dict(os.environ, TASK_DIR=str(workspace), TASK_ID=task_id),
            task_dir,
            workspace,
        )
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
        # The command trace must be read before the finally below deletes the
        # workspace — afterwards it is unrecoverable (issue #111).
        commands_log = _read_text(workspace / LOG_FILENAME)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    answer = parse_answer(stdout)
    result = grade(answer, _load_rubric(task_dir, task))
    record = {
        "task": task_id,
        "split": split_of(task),
        "elapsed_s": elapsed_s,
        "answer": cap_evidence(answer),
        "stderr_tail": stderr[-STDERR_TAIL_BYTES:],
        "score": result["score"],
        "passed": result["passed"],
        "detail": result["detail"],
        "timed_out": timed_out,
        "returncode": returncode,
        "commands": [line for line in commands_log.splitlines() if line.strip()],
    }

    out_dir = Path(results_root) / name
    out_dir.mkdir(parents=True, exist_ok=True)
    if commands_log.strip():
        (out_dir / f"{task_id}.commands.log").write_text(commands_log, encoding="utf-8")
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
    skipped = 0
    for task_dir in task_dirs:
        try:
            record = run_task(task_dir, agent_command, name, timeout, results_root)
        except HeldoutKeyError as exc:
            # A held-out task without HELDOUT_KEY cannot be graded; skipping
            # keeps key-less contributors running their public tasks (issue #115).
            print(f"SKIP {task_dir.name}: {exc}")
            skipped += 1
            continue
        mark = "PASS" if record["passed"] else "FAIL"
        print(
            f"{mark} {record['task']} score={record['score']} {record['elapsed_s']}s"
        )
        passed += bool(record["passed"])
    summary = f"{name}: {passed}/{len(task_dirs)} passed"
    if skipped:
        summary += f", {skipped} skipped (locked held-out)"
    print(summary)
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
    if args.split != "all":
        task_dirs = [d for d in task_dirs if split_of(load_task(d)) == args.split]
        if args.task is not None and not task_dirs:
            print(
                f"Task {args.task} is not in the '{args.split}' split.", file=sys.stderr
            )
            return 2
    if not args.all_tasks and args.task is None:
        print("Specify --task <id> or --all.", file=sys.stderr)
        return 2
    return run_tasks(task_dirs, args.agent, args.name, args.timeout)
