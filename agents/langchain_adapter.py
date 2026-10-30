#!/usr/bin/env python3
"""LangChain adapter — bridge the benchmark's task workspace to LangChain (issue #122).

The adapter never talks to a model. It builds the three scoped tools around one
task directory — `read_file` (empty path lists the files), `run_command`, and
`submit_answer` — and hands them to a target you point at through
`LANGCHAIN_AGENT_TARGET`:

- ``pkg.module:factory`` — a callable receiving the tool list, returning a
  Runnable the adapter invokes with the symptom text (see
  ``examples/langchain_agent.py``).
- ``pkg.module:runnable`` — a Runnable instance invoked with the symptom text
  directly; it must submit the answer itself or return the contract JSON.

Whatever the target runs internally (AgentExecutor, LangGraph, a hand-rolled
loop) is invisible here. The tool boundary is the same one the ReAct baseline
enforces: only ``symptom.md`` and ``fixtures/`` are readable, and commands run
with the task directory as their working directory inside the runner's isolated
workspace.

Contract, same as every bundled agent: the JSON object
``{"root_cause": ..., "evidence": [...], "fix": ...}`` on stdout. On any
failure: ``{}`` and exit 0 — the runner grades that as a FAIL, never a crash.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from langchain_core.runnables import Runnable
from langchain_core.tools import Tool, tool

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.llm_agent import extract_answer, read_truncated  # noqa: E402
from agents.react_agent import agent_visible_files, resolve_task_path  # noqa: E402

COMMAND_TIMEOUT_S = 30
MAX_COMMAND_TIMEOUT_S = 60
OUTPUT_LIMIT_CHARS = 20_000


def _clip(text: str) -> str:
    """Truncate tool output so one chatty command cannot blow up the context."""
    if len(text) <= OUTPUT_LIMIT_CHARS:
        return text
    return text[:OUTPUT_LIMIT_CHARS] + "\n... (truncated)"


def build_tools(task_dir: Path, state: dict[str, Any]) -> list[Tool]:
    """Build the three scoped tools around one task directory.

    `state` is a shared capture dict: `submit_answer` stores the answer under
    `"answer"` for `run_agent` to read after the runnable finishes.
    """
    task_dir = Path(task_dir)

    def visible() -> set[Path]:
        return set(agent_visible_files(task_dir))

    @tool
    def read_file(path: str = "") -> str:
        """Read one evidence file, path relative to the task directory. Call with no path to list the available files. Files outside the task directory cannot be read."""
        if not path:
            files = sorted(p.relative_to(task_dir).as_posix() for p in visible())
            return json.dumps({"files": files})
        resolved = resolve_task_path(task_dir, path)
        if resolved is None or resolved not in visible():
            return json.dumps({"error": "refused: path is outside the task directory"})
        return json.dumps({"content": read_truncated(resolved)})

    @tool
    def run_command(command: str, timeout: float = COMMAND_TIMEOUT_S) -> str:
        """Run a shell command from the task directory and capture stdout, stderr, and the return code. Use for diagnostic commands (kubectl, az, curl); non-zero exits are reported, not raised."""
        capped = min(timeout, MAX_COMMAND_TIMEOUT_S)
        try:
            proc = subprocess.run(  # noqa: S602 — the runner isolates the workspace
                command,
                shell=True,
                cwd=task_dir,
                capture_output=True,
                text=True,
                timeout=capped,
            )
        except subprocess.TimeoutExpired:
            return json.dumps({"error": f"command timed out after {capped}s"})
        except OSError as exc:
            return json.dumps({"error": str(exc)})
        return json.dumps(
            {
                "stdout": _clip(proc.stdout),
                "stderr": _clip(proc.stderr),
                "returncode": proc.returncode,
            }
        )

    @tool
    def submit_answer(root_cause: str = "", evidence: list[str] | None = None, fix: str = "") -> str:
        """Submit the final incident report: root_cause (one paragraph), evidence (specific lines/fields from named fixture files), and fix (the concrete remediation). This ends the investigation."""
        if not root_cause or not fix:
            return json.dumps({"error": "root_cause and fix are required"})
        if not isinstance(evidence, list) or not all(
            isinstance(item, str) for item in evidence
        ):
            return json.dumps({"error": "evidence must be a list of strings"})
        state["answer"] = {
            "root_cause": root_cause,
            "evidence": list(evidence),
            "fix": fix,
        }
        return json.dumps({"status": "submitted"})

    return [read_file, run_command, submit_answer]


def load_target(target: str) -> Any:
    """Import ``module:attribute`` (or ``module.attribute``) and return the attribute."""
    module_name, _, attribute = target.partition(":")
    if not attribute:
        module_name, _, attribute = module_name.rpartition(".")
        if not module_name:
            raise ValueError(
                f"LANGCHAIN_AGENT_TARGET must be 'module:attribute' or 'module.attribute', "
                f"got {target!r}"
            )
    return getattr(importlib.import_module(module_name), attribute)


def run_agent(task_dir: Path, target_name: str) -> dict:
    """Load the target, hand it the scoped tools, and return the contract dict."""
    target = load_target(target_name)
    task_dir = Path(task_dir)
    state: dict[str, Any] = {}
    tools = build_tools(task_dir, state)

    if isinstance(target, Runnable):
        agent = target
    elif callable(target):  # factory: builds the runnable around the tools
        agent = target(tools)
    else:
        raise TypeError(
            f"target must be a Runnable or a factory(tools) callable, got {type(target)}"
        )

    symptom = read_truncated(task_dir / "symptom.md") or "(no symptom provided)"
    result = agent.invoke(symptom)

    answer = state.get("answer")
    if answer is None:
        answer = extract_answer(str(result))
    return answer if isinstance(answer, dict) else {}


def main() -> int:
    task_dir = Path(os.environ.get("TASK_DIR", "."))
    try:
        answer = run_agent(task_dir, os.environ["LANGCHAIN_AGENT_TARGET"])
    except Exception:  # noqa: BLE001 — any adapter failure must degrade to a graded FAIL
        answer = {}  # graded as a FAIL result; never crash the runner
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
