#!/usr/bin/env python3
"""OpenHands adapter — run OpenHands headless in the task workspace (issue #170).

OpenHands gets the workspace directly: the adapter runs it headless with the
symptom as the task text, the task directory as its working directory (so
`fixtures/` and `symptom.md` are in place and the simulated CLIs the runner put
on PATH resolve), and an instruction to write the final answer to
`answer.json`. When OpenHands exits, the adapter reads `answer.json` and maps
it to stdout for `bench run` to grade.

    bench run --agent "python agents/openhands_adapter.py" \
        --task aks-identity-008

Credentials are OpenHands' own concern, passed through the environment as the
tool expects (`LLM_API_KEY`, `LLM_MODEL`); the adapter adds only
`WORKSPACE_BASE` pointing at the task workspace so sandboxed runtimes land in
the same directory the local runtime uses as its cwd.

Contract, same as every bundled agent: the answer JSON on stdout. On any
failure: ``{}`` and exit 0 — the runner grades that as a FAIL, never a crash.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.llm_agent import read_truncated  # noqa: E402

ANSWER_FILENAME = "answer.json"
WORKSPACE_BASE_ENV = "WORKSPACE_BASE"
DEFAULT_COMMAND = "openhands"


def build_prompt(symptom: str) -> str:
    """The headless task text: the symptom plus the answer.json instruction."""
    return (
        f"{symptom}\n\n"
        "You are investigating an SRE incident. The evidence is in the `fixtures/` "
        "directory of your current workspace (list it, read the files, and run any "
        "diagnostic commands you need — kubectl/az resolve to read-only simulated "
        "CLIs). When you have identified the root cause, write your final incident "
        f"report to `{ANSWER_FILENAME}` in the current workspace root as a JSON "
        'object exactly like {"root_cause": "<one paragraph>", "evidence": '
        '["<specific lines/fields from named files>"], "fix": "<the concrete '
        'remediation>"} and stop.'
    )


def build_command(command: str, prompt: str) -> list[str]:
    """The headless invocation: `<openhands> --headless -t <prompt>`."""
    return shlex.split(command) + ["--headless", "-t", prompt]


def collect_answer(task_dir: Path) -> dict:
    """Read `answer.json` from the workspace, or ``{}`` when there is none."""
    try:
        data = json.loads((Path(task_dir) / ANSWER_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_symptom(task_dir: Path) -> str:
    """The symptom text the runner would have fed on stdin, from the workspace."""
    return read_truncated(Path(task_dir) / "symptom.md") or "(no symptom provided)"


def run_openhands(task_dir: Path, command: str, prompt: str) -> dict:
    """Run OpenHands headless in the workspace and map `answer.json` to the answer."""
    env = dict(os.environ, **{WORKSPACE_BASE_ENV: str(Path(task_dir).resolve())})
    proc = subprocess.run(  # noqa: S603 — the command is the operator's OpenHands CLI
        build_command(command, prompt),
        cwd=task_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if proc.stdout:
        print(proc.stdout, file=sys.stderr, end="")
    return collect_answer(task_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openhands_adapter",
        description="Run OpenHands headless in the task workspace (issue #170).",
    )
    parser.add_argument(
        "--command",
        default=DEFAULT_COMMAND,
        help="OpenHands entrypoint to run (default: `openhands` on PATH; "
        "e.g. `python -m openhands.core.main` from a source checkout)",
    )
    args = parser.parse_args(argv)

    task_dir = Path(os.environ.get("TASK_DIR", "."))
    try:
        answer = run_openhands(task_dir, args.command, build_prompt(read_symptom(task_dir)))
        if not answer:
            print(
                f"error: OpenHands finished without a readable {ANSWER_FILENAME}",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 — any adapter failure degrades to a graded FAIL
        print(f"error: openhands adapter failed: {exc}", file=sys.stderr)
        answer = {}
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
