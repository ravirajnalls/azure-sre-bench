#!/usr/bin/env python3
"""Goose adapter — benchmark Block's Goose through the benchmark MCP server (issue #169).

Goose is an MCP-capable agent, so no benchmark-specific code runs on the Goose
side: the adapter generates a throwaway Goose config that registers the bundled
MCP stdio server (`bench mcp-serve --task <id>`, from issue #123) as a
command-line extension, then runs Goose headless with the symptom as the task
text. Goose investigates through the server's scoped tools — `list_files`,
`read_file`, `run_command`, `submit_answer` — and the adapter reads the
`submit_answer` result back from the answer file and prints it on stdout for
`bench run` to grade.

    bench run --agent "python agents/goose_adapter.py" \
        --task aks-identity-008

The generated config is per-run (a temp directory handed to Goose through
`GOOSE_CONFIG_DIR`), so a benchmark run never touches the operator's real
`~/.config/goose/config.yaml`. Provider and model come from the environment as
Goose expects: `GOOSE_PROVIDER` and `GOOSE_MODEL` must be set (Goose refuses to
start without them; the adapter surfaces Goose's error rather than guessing a
default model).

Contract, same as every bundled agent: the JSON object submitted via
`submit_answer` on stdout. On any failure: ``{}`` and exit 0 — the runner
grades that as a FAIL, never a crash.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from agents.llm_agent import read_truncated  # noqa: E402
from agents.mcp_client_runner import build_server_command  # noqa: E402

EXTENSION_NAME = "bench-mcp"
GOOSE_CONFIG_DIR_ENV = "GOOSE_CONFIG_DIR"
CONFIG_FILENAME = "config.yaml"
EXTENSION_TIMEOUT_S = 600


def build_extension(server_command: str) -> dict:
    """One stdio extension entry registering the benchmark MCP server."""
    cmd, *args = shlex.split(server_command)
    return {
        "type": "stdio",
        "name": EXTENSION_NAME,
        "cmd": cmd,
        "args": args,
        "enabled": True,
        "timeout": EXTENSION_TIMEOUT_S,
    }


def build_goose_config(server_command: str, config_dir: Path) -> Path:
    """Write a minimal Goose `config.yaml` exposing the benchmark server.

    Returns the config file path; the parent directory is what `GOOSE_CONFIG_DIR`
    should point at. The only entry is the MCP extension — provider and model
    stay in the environment, where Goose itself looks for them.
    """
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / CONFIG_FILENAME
    config_path.write_text(
        yaml.safe_dump(
            {"extensions": {EXTENSION_NAME: build_extension(server_command)}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_path


def build_prompt(symptom: str) -> str:
    """The headless task text: the symptom plus the MCP-tool instructions."""
    return (
        f"{symptom}\n\n"
        f"You are investigating an SRE incident. The `{EXTENSION_NAME}` extension "
        "exposes this incident's evidence: use its `list_files` and `read_file` "
        "tools to inspect the files (paths are relative to the task directory) and "
        "`run_command` for any diagnostics it offers. When you have identified the "
        "root cause, call `submit_answer` exactly once with the final incident "
        "report: `root_cause` (one paragraph), `evidence` (specific lines/fields "
        "from named files), and `fix` (the concrete remediation)."
    )


def collect_answer(answer_file: Path) -> dict:
    """Read the submitted contract JSON back, or ``{}`` when there is none."""
    try:
        data = json.loads(Path(answer_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_symptom(task_dir: Path) -> str:
    """The symptom text the runner would have fed on stdin, from the workspace."""
    return read_truncated(Path(task_dir) / "symptom.md") or "(no symptom provided)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="goose_adapter",
        description="Benchmark Block's Goose through the benchmark MCP server (issue #169).",
    )
    parser.add_argument(
        "--goose-command",
        default="goose",
        help="Goose CLI to run (default: `goose` on PATH)",
    )
    parser.add_argument(
        "--task",
        default=os.environ.get("TASK_ID"),
        help="task ID to serve (default: $TASK_ID from the bench runner)",
    )
    parser.add_argument(
        "--tasks-root",
        default=None,
        help="task discovery root for the MCP server (default: <repo>/tasks)",
    )
    parser.add_argument(
        "--answer-file",
        default=None,
        help="answer file path (default: a private temp file, deleted after use)",
    )
    args = parser.parse_args(argv)
    if not args.task:
        parser.error("--task is required (or run under `bench run`, which sets $TASK_ID)")
        return 2

    task_dir = Path(os.environ.get("TASK_DIR", "."))
    tasks_root = Path(args.tasks_root) if args.tasks_root else Path(__file__).resolve().parent.parent / "tasks"

    if args.answer_file:
        answer_file = Path(args.answer_file)
        owned_answer = False
    else:
        handle, name = tempfile.mkstemp(prefix="bench-goose-answer-", suffix=".json")
        os.close(handle)
        answer_file = Path(name)
        owned_answer = True

    try:
        server_command = build_server_command(args.task, tasks_root, answer_file)
        config_dir = Path(tempfile.mkdtemp(prefix="bench-goose-config-"))
        build_goose_config(server_command, config_dir)

        env = dict(os.environ, **{GOOSE_CONFIG_DIR_ENV: str(config_dir)})
        print(
            f"[goose_adapter] running: {args.goose_command} run --headless",
            file=sys.stderr,
            flush=True,
        )
        run_args = ["run", "--headless", "-t", build_prompt(read_symptom(task_dir))]
        proc = subprocess.run(  # noqa: S603 — the command is the operator's Goose CLI
            shlex.split(args.goose_command) + run_args,
            cwd=task_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if proc.stdout:
            print(proc.stdout, file=sys.stderr, end="")

        answer = collect_answer(answer_file)
        if not answer:
            print("error: Goose finished without calling submit_answer", file=sys.stderr)
        print(json.dumps(answer))
        return 0
    except Exception as exc:  # noqa: BLE001 — any adapter failure degrades to a graded FAIL
        print(f"error: goose adapter failed: {exc}", file=sys.stderr)
        print(json.dumps({}))
        return 0
    finally:
        if owned_answer:
            answer_file.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
