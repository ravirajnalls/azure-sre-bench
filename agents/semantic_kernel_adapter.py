#!/usr/bin/env python3
"""Semantic Kernel adapter — Microsoft-native agent on the task boundary (issue #172).

The adapter builds a Semantic Kernel plugin around one task directory —
`list_files`, `read_file`, `run_command`, and `submit_answer` — and hands it to
a `ChatCompletionAgent`. The chat service is Microsoft-native by default: an
`AzureChatCompletion` against an Azure OpenAI / Foundry deployment when
`AZURE_OPENAI_ENDPOINT` is set, otherwise an OpenAI-compatible endpoint through
the shared baseline env (`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`).
`SEMANTIC_KERNEL_AGENT_TARGET` overrides the wiring with a factory:
`module:attribute` (or `module.attribute`) returning a
`ChatCompletionClientBase` — see ``examples/semantic_kernel_agent.py`` for a
scripted fake service that runs with no API key.

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
from pathlib import Path
from typing import Any, Callable

from semantic_kernel.agents import ChatCompletionAgent
from semantic_kernel.connectors.ai.chat_completion_client_base import ChatCompletionClientBase
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion, OpenAIChatCompletion
from semantic_kernel.functions import kernel_function

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


class SREPlugin:
    """Kernel plugin: the scoped task-boundary tools, same as every adapter.

    `state` is a shared capture dict: `submit_answer` stores the answer under
    `"answer"` for `run_agent` to read after the agent finishes.
    """

    def __init__(self, task_dir: Path, state: dict[str, Any]) -> None:
        self.task_dir = Path(task_dir)
        self.state = state

    def _visible(self) -> set[Path]:
        return set(agent_visible_files(self.task_dir))

    @kernel_function(description="List the evidence files available in the task directory.")
    def list_files(self) -> str:
        files = sorted(p.relative_to(self.task_dir).as_posix() for p in self._visible())
        return json.dumps({"files": files})

    @kernel_function(
        description=(
            "Read one evidence file, path relative to the task directory "
            "(max 20 KB). Files outside the task directory cannot be read."
        )
    )
    def read_file(self, path: str) -> str:
        resolved = resolve_task_path(self.task_dir, path)
        if resolved is None or resolved not in self._visible():
            return json.dumps({"error": "refused: path is outside the task directory"})
        return json.dumps({"content": read_truncated(resolved)})

    @kernel_function(
        description=(
            "Run a shell command from the task directory and capture stdout, "
            "stderr, and the return code. Use for diagnostic commands (kubectl, "
            "az, curl); non-zero exits are reported, not raised."
        )
    )
    def run_command(self, command: str, timeout: float = COMMAND_TIMEOUT_S) -> str:
        capped = min(timeout, MAX_COMMAND_TIMEOUT_S)
        try:
            proc = subprocess.run(  # noqa: S602 — the runner isolates the workspace
                command,
                shell=True,
                cwd=self.task_dir,
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

    @kernel_function(
        description=(
            "Submit the final incident report: root_cause (one paragraph), "
            "evidence (specific lines/fields from named fixture files), and fix "
            "(the concrete remediation). This ends the investigation."
        )
    )
    def submit_answer(self, root_cause: str, evidence: list[str], fix: str) -> str:
        if not root_cause or not fix:
            return json.dumps({"error": "root_cause and fix are required"})
        if not isinstance(evidence, list) or not all(
            isinstance(item, str) for item in evidence
        ):
            return json.dumps({"error": "evidence must be a list of strings"})
        self.state["answer"] = {
            "root_cause": root_cause,
            "evidence": list(evidence),
            "fix": fix,
        }
        return json.dumps({"status": "submitted"})


def resolve_service() -> ChatCompletionClientBase:
    """Build the chat service: Azure OpenAI/Foundry, or any OpenAI-compatible endpoint.

    Azure path: `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_API_KEY` (or
    `AZURE_OPENAI_API_KEY` alone when Azure defaults carry the endpoint), with
    the deployment from `AZURE_OPENAI_CHAT_DEPLOYMENT_NAME` or `LLM_MODEL`.
    Non-Azure path: the shared LLM_* trio against an OpenAI-compatible
    endpoint (Azure AI Foundry's OpenAI-compatible surface included).
    """
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    if endpoint:
        return AzureChatCompletion(
            deployment_name=os.environ.get(
                "AZURE_OPENAI_CHAT_DEPLOYMENT_NAME", os.environ.get("LLM_MODEL", "")
            ),
            endpoint=endpoint,
            api_key=os.environ.get("AZURE_OPENAI_API_KEY", ""),
        )
    from openai import AsyncOpenAI  # imported lazily: only needed on the non-Azure path

    client = AsyncOpenAI(
        base_url=os.environ.get("LLM_BASE_URL") or None,
        api_key=os.environ.get("LLM_API_KEY") or "missing",
    )
    return OpenAIChatCompletion(ai_model_id=os.environ["LLM_MODEL"], async_client=client)


def load_target(target: str) -> Callable[[], ChatCompletionClientBase]:
    """Import ``module:attribute`` (or ``module.attribute``) and return the attribute."""
    module_name, _, attribute = target.partition(":")
    if not attribute:
        module_name, _, attribute = module_name.rpartition(".")
        if not module_name:
            raise ValueError(
                f"SEMANTIC_KERNEL_AGENT_TARGET must be 'module:attribute' or 'module.attribute', "
                f"got {target!r}"
            )
    return getattr(importlib.import_module(module_name), attribute)


async def _run_agent_async(task_dir: Path, service: ChatCompletionClientBase) -> dict:
    state: dict[str, Any] = {}
    plugin = SREPlugin(task_dir, state)
    agent = ChatCompletionAgent(
        service=service,
        name="sre-investigator",
        instructions=SYSTEM_PROMPT,
        plugins=[plugin],
    )

    symptom = read_truncated(task_dir / "symptom.md") or "(no symptom provided)"
    last_content = ""
    async for message in agent.invoke(messages=symptom):
        last_content = str(message.content)

    answer = state.get("answer")
    if answer is None:
        answer = extract_answer(last_content)
    return answer if isinstance(answer, dict) else {}


def run_agent(task_dir: Path, target_name: str | None = None) -> dict:
    """Run the Semantic Kernel agent on one task directory and return the contract dict."""
    task_dir = Path(task_dir)
    service = load_target(target_name)() if target_name else resolve_service()
    return asyncio.run(_run_agent_async(task_dir, service))


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
        answer = run_agent(task_dir, os.environ.get("SEMANTIC_KERNEL_AGENT_TARGET"))
    except Exception:  # noqa: BLE001 — any adapter failure must degrade to a graded FAIL
        answer = {}  # graded as a FAIL result; never crash the runner
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
