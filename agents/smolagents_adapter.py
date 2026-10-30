#!/usr/bin/env python3
"""smolagents adapter — Hugging Face ToolCallingAgent on the task boundary (issue #171).

The adapter builds the scoped tool set around one task directory —
`list_files`, `read_file`, `run_command`, and `submit_answer` — and hands it to
a `ToolCallingAgent`. By default the agent's model is a smolagents
`LiteLLMModel` pointed at any OpenAI-compatible endpoint through the shared
baseline env (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`; LiteLLM prefixes the
model id with `openai/`). `SMOLAGENTS_AGENT_TARGET` overrides the wiring with a
factory: `module:attribute` (or `module.attribute`) receiving the tool list and
returning a ready agent — see ``examples/smolagents_agent.py`` for a scripted
fake model that runs with no API key.

Tool boundary, same as every bundled agent: only `symptom.md` and `fixtures/`
are readable, and commands run with the task directory as their working
directory inside the runner's isolated workspace.

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
from typing import Any, Callable

from smolagents import LiteLLMModel, Tool, ToolCallingAgent, tool

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.llm_agent import extract_answer, read_truncated  # noqa: E402
from agents.react_agent import agent_visible_files, resolve_task_path  # noqa: E402

COMMAND_TIMEOUT_S = 30
MAX_COMMAND_TIMEOUT_S = 60
OUTPUT_LIMIT_CHARS = 20_000
MAX_STEPS = 12  # investigation steps before the agent is forced to answer


def _clip(text: str) -> str:
    """Truncate tool output so one chatty command cannot blow up the context."""
    if len(text) <= OUTPUT_LIMIT_CHARS:
        return text
    return text[:OUTPUT_LIMIT_CHARS] + "\n... (truncated)"


def build_tools(task_dir: Path, state: dict[str, Any]) -> list[Tool]:
    """Build the scoped tools around one task directory.

    `state` is a shared capture dict: `submit_answer` stores the answer under
    `"answer"` for `run_agent` to read after the agent finishes. smolagents'
    `final_answer` tool is added by the agent itself and never used here.
    """
    task_dir = Path(task_dir)

    def visible() -> set[Path]:
        return set(agent_visible_files(task_dir))

    @tool
    def list_files() -> str:
        """List the evidence files available in the task directory."""
        files = sorted(p.relative_to(task_dir).as_posix() for p in visible())
        return json.dumps({"files": files})

    @tool
    def read_file(path: str) -> str:
        """Read one evidence file, path relative to the task directory.

        Args:
            path: Path of the file to read, relative to the task directory.

        Returns:
            The file content (max 20 KB) as JSON.
        """
        resolved = resolve_task_path(task_dir, path)
        if resolved is None or resolved not in visible():
            return json.dumps({"error": "refused: path is outside the task directory"})
        return json.dumps({"content": read_truncated(resolved)})

    @tool
    def run_command(command: str, timeout: float = COMMAND_TIMEOUT_S) -> str:
        """Run a shell command from the task directory and capture stdout, stderr, and the return code. Use for diagnostic commands (kubectl, az, curl); non-zero exits are reported, not raised.

        Args:
            command: The shell command to run.
            timeout: Seconds to wait before the command is killed (capped at 60).

        Returns:
            stdout, stderr, and returncode as JSON.
        """
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
    def submit_answer(root_cause: str, evidence: list, fix: str) -> str:
        """Submit the final incident report: root_cause (one paragraph), evidence (specific lines/fields from named fixture files), and fix (the concrete remediation). This ends the investigation.

        Args:
            root_cause: The underlying cause in one paragraph.
            evidence: Specific lines/fields from named fixture files supporting the diagnosis.
            fix: The concrete remediation.

        Returns:
            Submission status as JSON.
        """
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

    return [list_files, read_file, run_command, submit_answer]


def resolve_model() -> LiteLLMModel:
    """Build the LiteLLM model from the shared baseline env vars.

    LiteLLM routes OpenAI-compatible endpoints through the `openai/` provider
    prefix, so the same LLM_BASE_URL/LLM_API_KEY/LLM_MODEL trio as the ReAct
    baseline works unchanged (Azure AI Foundry included).
    """
    return LiteLLMModel(
        model_id=f"openai/{os.environ['LLM_MODEL']}",
        api_base=os.environ["LLM_BASE_URL"],
        api_key=os.environ["LLM_API_KEY"],
    )


def load_target(target: str) -> Callable[[list[Tool]], Any]:
    """Import ``module:attribute`` (or ``module.attribute``) and return the attribute."""
    module_name, _, attribute = target.partition(":")
    if not attribute:
        module_name, _, attribute = module_name.rpartition(".")
        if not module_name:
            raise ValueError(
                f"SMOLAGENTS_AGENT_TARGET must be 'module:attribute' or 'module.attribute', "
                f"got {target!r}"
            )
    return getattr(importlib.import_module(module_name), attribute)


def run_agent(task_dir: Path, target_name: str | None = None) -> dict:
    """Run the smolagents agent on one task directory and return the contract dict."""
    task_dir = Path(task_dir)
    state: dict[str, Any] = {}
    tools = build_tools(task_dir, state)

    if target_name:
        agent = load_target(target_name)(tools)
    else:
        agent = ToolCallingAgent(
            tools=tools,
            model=resolve_model(),
            max_steps=MAX_STEPS,
            instructions=SYSTEM_PROMPT,
            verbosity_level=-1,
        )

    symptom = read_truncated(task_dir / "symptom.md") or "(no symptom provided)"
    result = agent.run(symptom)

    answer = state.get("answer")
    if answer is None:
        answer = extract_answer(str(result))
    return answer if isinstance(answer, dict) else {}


SYSTEM_PROMPT = """\
You are an experienced site reliability engineer diagnosing a production incident.
You are given a symptom and access to a folder of evidence files (CLI output,
logs, queries) through tools: list_files(), read_file(path), and
run_command(command) for diagnostic commands.

Investigate before answering — evidence may contain healthy red herrings. Read
the files that matter rather than guessing. When you have enough evidence,
submit the report with submit_answer(root_cause, evidence, fix)."""


def main() -> int:
    task_dir = Path(os.environ.get("TASK_DIR", "."))
    try:
        answer = run_agent(task_dir, os.environ.get("SMOLAGENTS_AGENT_TARGET"))
    except Exception:  # noqa: BLE001 — any adapter failure must degrade to a graded FAIL
        answer = {}  # graded as a FAIL result; never crash the runner
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
