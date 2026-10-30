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

from bench import GRADER_VERSION, RESULT_SCHEMA_VERSION
from bench.attribution import ANSWER_CONTRACT_BLOCK as ATTRIBUTION_BLOCK
from bench.attribution import is_attribution
from bench.fixture_lint import LINTIGNORE_FILENAME
from bench.grader import cap_evidence, extract_files_read, extract_usage, grade, rubric_sha256
from bench.heldout import HeldoutKeyError, load_hidden_json, split_of
from bench.preincident import is_pre_incident_review
from bench.releases import load_release, verify_release
from bench.remediation import grade_for_task, is_remediation
from bench.sim import COMMANDS_ENV, COMMANDS_FILENAME, LOG_ENV, LOG_FILENAME, SIM_DIR

TASKS_DIR = Path("tasks")
RESULTS_DIR = Path("results")
HIDDEN_FILES = ("expected.json", "solution.json", "explainer.md")
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
    copied where the agent can read them. `explainer.md` is hidden too (issue
    #149): it names the root cause and the red herring outright, so it exists
    for humans on the Learn page, never in the agent's workspace.
    `_commands.yaml` is excluded too: it
    configures the simulated CLIs (issue #111) and reaches the agent through
    the shim environment, not as a one-file answer dump in `fixtures/`.
    `.lintignore` is excluded as well (issue #152): it documents which fixture
    inconsistencies are intentional — i.e. which of them is the planted bug.
    """
    fixtures_src = Path(task_dir) / "fixtures"
    if fixtures_src.is_dir():
        shutil.copytree(
            fixtures_src,
            Path(workspace) / "fixtures",
            ignore=shutil.ignore_patterns(COMMANDS_FILENAME, LINTIGNORE_FILENAME),
        )
    task = load_task(task_dir)
    title = task.get("title") or task.get("id") or "Incident"
    symptom_md = (
        f"# {title}\n\n{task.get('symptom', '')}\n\nEvidence is in `fixtures/`.\n"
    )
    if is_pre_incident_review(task):
        # Issue #147: a pre-incident review's answer contract is a findings
        # list, not a diagnosis — this is the runner's only agent-facing
        # surface, so it must carry the shape.
        symptom_md += (
            "\nThis is a pre-incident-review task: review the proposed change "
            "in `fixtures/` and include a `findings` list in your answer — at "
            'most 5 objects, each `{"file": str, "line": int, "issue": str, '
            '"fix": str}` — one per change that would cause an incident: the '
            "file (as named in the diff) and its 1-based line, what will break "
            "when it ships, and the fix to make before merge. Report only "
            "changes that would cause an incident; healthy changes are not "
            "findings.\n"
        )
    elif is_remediation(task):
        # The runner's only agent-facing surface: a remediation task (issue
        # #110) must tell agents the patch half of the answer contract.
        symptom_md += (
            "\nThis is a remediation task: include a `patched_files` object in "
            "your answer, mapping `fixtures/<file>` paths to the full new file "
            "content that implements your fix.\n"
        )
    elif is_attribution(task):
        # Same agent-facing surface: a change-attribution task (issue #144)
        # must tell agents the culprit/reasoning/fix answer contract.
        symptom_md += ATTRIBUTION_BLOCK
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


def load_release_checked(
    name: str, tasks_root: Path | str = "tasks"
) -> tuple[dict, list[dict]]:
    """Load a release manifest and verify the live corpus against it (issue #116).

    Returns `(manifest, frozen_tasks)`. Any drift against the manifest's tasks
    — a rubric edited since the freeze, a manifest task removed, a grader bump
    — raises `ValueError` with the full problem list. New corpus tasks the
    manifest does not name are allowed: a release run must stay reproducible
    against the tasks the manifest froze, while the corpus grows toward the
    next release.
    """
    manifest = load_release(name)
    problems = verify_release(manifest, tasks_root=tasks_root)
    if problems:
        detail = "\n".join(f"  - {problem}" for problem in problems)
        raise ValueError(
            f"corpus does not match release {name!r} ({len(problems)} problem(s)):\n{detail}"
        )
    return manifest, manifest.get("tasks", [])


def run_task(
    task_dir: Path,
    agent_command: str,
    name: str = DEFAULT_NAME,
    timeout: int = DEFAULT_TIMEOUT_S,
    results_root: Path | None = None,
    release: str | None = None,
    repeat_index: int | None = None,
) -> dict:
    """Run one agent against one task and write `results/<name>/<task-id>.json`.

    With `repeat_index` (issue #162), the record lands in
    `results/<name>/<task-id>.<k>.json` instead and carries a `repeat` stamp —
    one file per repeat, so single-run results stay byte-compatible.
    """
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
    # One grading entry point for every surface: remediation tasks run their
    # patch checks here too (issue #110).
    rubric = _load_rubric(task_dir, task)
    result = grade_for_task(answer, rubric, task_dir, task)
    record = {
        "task": task_id,
        "split": split_of(task),
        # Result-schema extension (issue #95): every record is self-describing
        # about the schema shape, the grading semantics, and the exact rubric
        # it was scored against — renderers rank only same-version results.
        # Held-out rubrics hash as decrypted, but stay version-only for
        # staleness: their on-disk form is ciphertext, so no current hash
        # exists to compare against (positive-mismatch-only by design).
        "schema_version": RESULT_SCHEMA_VERSION,
        "grader_version": GRADER_VERSION,
        "rubric_sha256": rubric_sha256(rubric),
        "agent": name,
        "elapsed_s": elapsed_s,
        "answer": cap_evidence(answer),
        "stderr_tail": stderr[-STDERR_TAIL_BYTES:],
        "score": result["score"],
        "passed": result["passed"],
        "detail": result["detail"],
        # Issue #113: unsafe-fix matches are recorded at the top level so the
        # leaderboard's safety column and `bench verify` read them directly.
        "safety_violations": result["detail"]["safety_violations"],
        "timed_out": timed_out,
        "returncode": returncode,
        "commands": [line for line in commands_log.splitlines() if line.strip()],
    }
    usage = extract_usage(answer)
    if usage is not None:
        record["usage"] = usage
    # Issue #116: a `bench run --release` run stamps the release name so
    # leaderboard versions can group results by the corpus they scored.
    if release is not None:
        record["release"] = release
    # Issue #162: repeated runs stamp their index so the loaders can group
    # `<task>.<k>.json` files into one task's run set without parsing names.
    if repeat_index is not None:
        record["repeat"] = repeat_index
    # Efficiency metrics (issue #112): the sim-CLI count comes from the
    # runner's own command trace; the file-read count from the agent's own
    # accounting (`files_read` in the answer). Either may be absent.
    efficiency: dict = {}
    if record["commands"]:
        efficiency["commands_run"] = len(record["commands"])
    files_read = extract_files_read(answer)
    if files_read is not None:
        efficiency["files_read"] = files_read
    if efficiency:
        record["efficiency"] = efficiency

    out_dir = Path(results_root) / name
    out_dir.mkdir(parents=True, exist_ok=True)
    # Repeats (issue #162) suffix the run index so every repeat keeps its own
    # record and command trace; the single-run names are untouched.
    stem = task_id if repeat_index is None else f"{task_id}.{repeat_index}"
    if commands_log.strip():
        (out_dir / f"{stem}.commands.log").write_text(commands_log, encoding="utf-8")
    (out_dir / f"{stem}.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )
    return record


def run_tasks(
    task_dirs: list[Path],
    agent_command: str,
    name: str = DEFAULT_NAME,
    timeout: int = DEFAULT_TIMEOUT_S,
    results_root: Path | None = None,
    release: str | None = None,
    repeat: int = 1,
) -> int:
    """Run a sequence of tasks, printing per-task lines and a final summary.

    With `repeat > 1` (issue #162) every task runs that many times, each run
    writing its own `<task>.<k>.json` record; per-run lines carry a `[run k]`
    suffix so repeat runs read unambiguously.
    """
    results_root = Path(results_root or RESULTS_DIR)
    repeat = max(1, repeat)
    passed = 0
    skipped = 0
    for task_dir in task_dirs:
        try:
            records = [
                run_task(
                    task_dir,
                    agent_command,
                    name,
                    timeout,
                    results_root,
                    release,
                    repeat_index=k if repeat > 1 else None,
                )
                for k in range(repeat)
            ]
        except HeldoutKeyError as exc:
            # A held-out task without HELDOUT_KEY cannot be graded; skipping
            # keeps key-less contributors running their public tasks (issue #115).
            # One line per task, not per repeat — the key check is repeat-independent.
            print(f"SKIP {task_dir.name}: {exc}")
            skipped += 1
            continue
        for k, record in enumerate(records):
            mark = "PASS" if record["passed"] else "FAIL"
            suffix = f" [run {k}]" if repeat > 1 else ""
            print(f"{mark} {record['task']} score={record['score']} {record['elapsed_s']}s{suffix}")
            passed += bool(record["passed"])
    summary = f"{name}: {passed}/{len(task_dirs) * repeat} passed"
    if repeat > 1:
        summary += f" ({repeat} repeats per task)"
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
    release_name = getattr(args, "release", None)
    if release_name is not None:
        # Issue #116: a release run must be reproducible against the frozen
        # corpus. Verify before the first task executes; refuse to label a
        # drifted corpus with the release.
        try:
            manifest, _frozen = load_release_checked(release_name, args.tasks_root)
        except (FileNotFoundError, ValueError) as exc:
            print(f"Release check failed: {exc}", file=sys.stderr)
            return 2
        frozen_ids = {task["id"] for task in manifest["tasks"]}
        task_dirs = [d for d in task_dirs if str(load_task(d).get("id")) in frozen_ids]
        print(f"Release {release_name}: {len(frozen_ids)} tasks in manifest")
    repeat = getattr(args, "repeat", 1)
    if repeat is None or repeat < 1:
        print("--repeat must be at least 1", file=sys.stderr)
        return 2
    return run_tasks(
        task_dirs, args.agent, args.name, args.timeout, release=release_name, repeat=repeat
    )
