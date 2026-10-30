#!/usr/bin/env python3
"""AutoGen multi-agent adapter — investigator + reviewer team (issue #173).

Two AutoGen agents share the task boundary as a `RoundRobinGroupChat`:

- **investigator** — holds the scoped tools (`list_files`, `read_file`,
  `run_command`, `submit_answer`) and drives the diagnosis.
- **reviewer** — holds no tools; each round it challenges the investigator's
  hypothesis against the evidence (the red herring included) before the
  investigator finalizes.

The team measures whether a critic improves distractor resistance versus the
single-agent ReAct baseline. The model client is one OpenAI-compatible endpoint
configured from the shared baseline env (`LLM_BASE_URL`, `LLM_API_KEY`,
`LLM_MODEL`); `AUTOGEN_AGENT_TARGET` overrides the wiring with a factory:
`module:attribute` (or `module.attribute`) returning a
`ChatCompletionClient` — see ``examples/autogen_team.py`` for a replay client
that runs with no API key.

Tool boundary, same as every bundled agent: only `symptom.md` and `fixtures/`
are readable, and commands run with the task directory as their working
directory inside the runner's isolated workspace.

Contract, same as every bundled agent: the JSON object
``{"root_cause": ..., "evidence": [...], "fix": ...}`` on stdout. On any
failure: ``{}`` and exit 0 — the runner grades that as a FAIL, never a crash.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core.models import ChatCompletionClient
from autogen_ext.models.openai import OpenAIChatCompletionClient

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.llm_agent import extract_answer, read_truncated
from agents.react_agent import agent_visible_files, resolve_task_path

COMMAND_TIMEOUT_S = 30
MAX_COMMAND_TIMEOUT_S = 60
OUTPUT_LIMIT_CHARS = 20_000
# Stop when the investigator posts the final report (the contract JSON's
# `root_cause` field appears — the reviewer is prompted with the prose spelling
# "root cause" so it cannot trip the sentinel) or the message cap is reached.
FINAL_REPORT_MENTION = "root_cause"
MAX_TEAM_MESSAGES = 8  # investigator → reviewer rounds before the loop is cut


def _clip(text: str) -> str:
    """Truncate tool output so one chatty command cannot blow up the context."""
    if len(text) <= OUTPUT_LIMIT_CHARS:
        return text
    return text[:OUTPUT_LIMIT_CHARS] + "\n... (truncated)"


def build_tools(task_dir: Path, state: dict[str, Any]) -> list[Callable]:
    """Build the scoped tools around one task directory.

    Plain callables — AutoGen infers the tool schema from type hints and
    docstrings. `state` is a shared capture dict: `submit_answer` stores the
    answer under `"answer"` for `run_agent` to read after the team finishes.
    """
    task_dir = Path(task_dir)

    def visible() -> set[Path]:
        return set(agent_visible_files(task_dir))

    def list_files() -> str:
        """List the evidence files available in the task directory."""
        files = sorted(p.relative_to(task_dir).as_posix() for p in visible())
        return json.dumps({"files": files})

    def read_file(path: str) -> str:
        """Read one evidence file (max 20 KB), path relative to the task directory. Files outside the task directory cannot be read."""
        resolved = resolve_task_path(task_dir, path)
        if resolved is None or resolved not in visible():
            return json.dumps({"error": "refused: path is outside the task directory"})
        return json.dumps({"content": read_truncated(resolved)})

    def run_command(command: str, timeout: float = COMMAND_TIMEOUT_S) -> str:
        """Run a shell command from the task directory and capture stdout, stderr, and the return code. Use for diagnostic commands (kubectl, az, curl); non-zero exits are reported, not raised."""
        capped = min(timeout, MAX_COMMAND_TIMEOUT_S)
        try:
            proc = subprocess.run(
                command,
                shell=True,
                check=False,
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

    def submit_answer(root_cause: str, evidence: list[str], fix: str) -> str:
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

    return [list_files, read_file, run_command, submit_answer]


def resolve_model_client() -> ChatCompletionClient:
    """Build the OpenAI-compatible model client from the shared baseline env vars.

    `model_info` is declared explicitly: custom base URLs (Azure AI Foundry's
    OpenAI-compatible surface included) are not in AutoGen's known-model list,
    and an AssistantAgent with tools refuses a client that cannot call them.
    """
    return OpenAIChatCompletionClient(
        model=os.environ["LLM_MODEL"],
        base_url=os.environ.get("LLM_BASE_URL") or None,
        api_key=os.environ.get("LLM_API_KEY") or "missing",
        model_info={
            "vision": False,
            "function_calling": True,
            "json_output": True,
            "structured_output": True,
            "family": "unknown",
        },
    )


def load_target(target: str) -> Callable[[], ChatCompletionClient]:
    """Import ``module:attribute`` (or ``module.attribute``) and return the attribute."""
    module_name, _, attribute = target.partition(":")
    if not attribute:
        module_name, _, attribute = module_name.rpartition(".")
        if not module_name:
            raise ValueError(
                f"AUTOGEN_AGENT_TARGET must be 'module:attribute' or 'module.attribute', "
                f"got {target!r}"
            )
    return getattr(importlib.import_module(module_name), attribute)


async def _run_agent_async(task_dir: Path, client: ChatCompletionClient) -> dict:
    state: dict[str, Any] = {}
    tools = build_tools(task_dir, state)

    investigator = AssistantAgent(
        name="investigator",
        model_client=client,
        tools=tools,
        system_message=INVESTIGATOR_PROMPT,
    )
    reviewer = AssistantAgent(
        name="reviewer",
        model_client=client,
        system_message=REVIEWER_PROMPT,
    )
    team = RoundRobinGroupChat(
        [investigator, reviewer],
        termination_condition=(
            TextMentionTermination(FINAL_REPORT_MENTION)
            | MaxMessageTermination(MAX_TEAM_MESSAGES)
        ),
    )
    result = await team.run()

    answer = state.get("answer")
    if answer is None:
        # The investigator may answer with bare contract JSON in its last turn.
        for message in reversed(result.messages):
            answer = extract_answer(str(message.content))
            if answer:
                break
    return answer if isinstance(answer, dict) else {}


def run_agent(task_dir: Path, target_name: str | None = None) -> dict:
    """Run the AutoGen team on one task directory and return the contract dict."""
    task_dir = Path(task_dir)
    client = load_target(target_name)() if target_name else resolve_model_client()
    return asyncio.run(_run_agent_async(task_dir, client))


INVESTIGATOR_PROMPT = """\
You are an experienced site reliability engineer diagnosing a production incident.
You are given a symptom and access to a folder of evidence files (CLI output,
logs, queries) through tools: list_files(), read_file(path), and
run_command(command) for diagnostic commands.

Investigate before answering — evidence may contain healthy red herrings. Read
the files that matter rather than guessing. State your hypothesis with the
evidence you found; a reviewer will challenge it. When your hypothesis holds
(or the reviewer's challenge changed it), respond with ONLY a JSON object, no
prose, in exactly this shape:
{"root_cause": "...", "evidence": ["file: observation", "..."], "fix": "..."}
or submit it with submit_answer(root_cause, evidence, fix)."""

REVIEWER_PROMPT = """\
You are a skeptical incident review partner. The investigator proposes a root
cause; your job is to challenge it against the evidence before it is accepted:
which red herrings rule it out, which fixture line contradicts it, what
alternative explanation fits better? Be specific and brief. If the hypothesis
survives your challenge, say so and ask for the final report."""


def main() -> int:
    task_dir = Path(os.environ.get("TASK_DIR", "."))
    try:
        answer = run_agent(task_dir, os.environ.get("AUTOGEN_AGENT_TARGET"))
    except Exception:  # noqa: BLE001 — any adapter failure must degrade to a graded FAIL
        answer = {}  # graded as a FAIL result; never crash the runner
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
