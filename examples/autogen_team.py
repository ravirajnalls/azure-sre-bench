#!/usr/bin/env python3
"""Example: an AutoGen investigator + reviewer team benchmarked through the adapter (issue #173).

Demonstrates the wiring the adapter expects — a `ChatCompletionClient`. The
client here is AutoGen's replay client (a scripted sequence, no network), so
the example runs with no API key and is exercised by pytest; swap in a real
client with one line:

    client = OpenAIChatCompletionClient(model="...", base_url="...", api_key="...")

Run it through the adapter against a task directory:

    TASK_DIR=tasks/monitoring/monitoring-001 \\
    AUTOGEN_AGENT_TARGET=examples.autogen_team:build_client \\
    python agents/autogen_adapter.py

or standalone (same two-agent loop, no adapter):

    TASK_DIR=tasks/monitoring/monitoring-001 \\
    AUTOGEN_AGENT_TARGET=examples.autogen_team:build_client \\
    python examples/autogen_team.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from autogen_core.models import ChatCompletionClient
from autogen_ext.models.replay import ReplayChatCompletionClient

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ANSWER = {
    "root_cause": "expired secret referenced by the liveness probe",
    "evidence": ["fixtures/kubectl-get-pods.txt: probe-api-0 failing probe with 401"],
    "fix": "rotate the secret and redeploy the pod",
}

MODEL_INFO = {
    "vision": False,
    "function_calling": True,
    "json_output": True,
    "structured_output": True,
    "family": "unknown",
}


def _scripted_completions() -> list[str]:
    """Hypothesis → reviewer challenge → final contract JSON, as the team hears them."""
    return [
        (
            "Hypothesis: the liveness probe's secret expired — kubectl-get-pods.txt "
            "shows probe-api-0 failing its probe with 401."
        ),
        (
            "Challenge: 401 on the probe could also be an RBAC change or a rotated "
            "identity. But the event stream confirms the probe reads the expired "
            "secret. Hypothesis holds — finalize with the exact evidence line."
        ),
        json.dumps(ANSWER),
    ]


def build_client() -> ChatCompletionClient:
    """Factory the adapter imports via AUTOGEN_AGENT_TARGET."""
    return ReplayChatCompletionClient(_scripted_completions(), model_info=MODEL_INFO)


def main() -> int:
    task_dir = Path(os.environ.get("TASK_DIR", "."))
    try:
        from agents.autogen_adapter import load_target

        client = load_target(os.environ["AUTOGEN_AGENT_TARGET"])()
        answer = asyncio.run(_standalone_run(task_dir, client))
    except Exception:  # noqa: BLE001 — any failure must degrade to a graded FAIL
        answer = {}  # graded as a FAIL result; never crash the runner
    print(json.dumps(answer))
    return 0


async def _standalone_run(task_dir: Path, client: ChatCompletionClient) -> dict:
    """The adapter's team loop, inlined for standalone runs."""
    from agents.autogen_adapter import build_tools

    state: dict = {}
    tools = build_tools(task_dir, state)
    from autogen_agentchat.agents import AssistantAgent
    from autogen_agentchat.conditions import MaxMessageTermination
    from autogen_agentchat.teams import RoundRobinGroupChat

    investigator = AssistantAgent(
        name="investigator",
        model_client=client,
        tools=tools,
        system_message="Investigate, then answer with the contract JSON.",
    )
    reviewer = AssistantAgent(
        name="reviewer",
        model_client=client,
        system_message="Challenge the hypothesis against the red herrings.",
    )
    team = RoundRobinGroupChat(
        [investigator, reviewer],
        termination_condition=MaxMessageTermination(3),
    )
    result = await team.run()
    answer = state.get("answer")
    if answer is None:
        for message in reversed(result.messages):
            answer = _extract(str(message.content))
            if answer:
                break
    return answer if isinstance(answer, dict) else {}


def _extract(content: str) -> dict | None:
    from agents.llm_agent import extract_answer

    answer = extract_answer(content)
    return answer if isinstance(answer, dict) and answer else None


if __name__ == "__main__":
    sys.exit(main())
