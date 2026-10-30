#!/usr/bin/env python3
"""Example: a smolagents ToolCallingAgent benchmarked through the adapter (issue #171).

Demonstrates the wiring the adapter expects — a factory that receives the four
scoped tools and returns a ready agent. The chat model here is a scripted fake
(subclass of smolagents' abstract `Model`), so the example runs with no API key
and is exercised by pytest; swap in a real model with one line:

    model = LiteLLMModel(model_id="openai/<deployment>", api_base=..., api_key=...)
    agent = ToolCallingAgent(tools=tools, model=model, max_steps=12)

Run it through the adapter against a task directory:

    TASK_DIR=tasks/monitoring/monitoring-001 \\
    SMOLAGENTS_AGENT_TARGET=examples.smolagents_agent:build_agent \\
    python agents/smolagents_adapter.py

or standalone (same investigation loop, no adapter):

    TASK_DIR=tasks/monitoring/monitoring-001 \\
    SMOLAGENTS_AGENT_TARGET=examples.smolagents_agent:build_agent \\
    python examples/smolagents_agent.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from smolagents import Tool, ToolCallingAgent
from smolagents.models import (
    ChatMessage,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    MessageRole,
    Model,
)

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MAX_STEPS = 12

ANSWER = {
    "root_cause": "expired secret referenced by the liveness probe",
    "evidence": ["fixtures/kubectl-get-pods.txt: probe-api-0 failing probe with 401"],
    "fix": "rotate the secret and redeploy the pod",
}


class ScriptedModel(Model):
    """Minimal smolagents model: pops one scripted step per generate() call."""

    def __init__(self, script: list[dict]) -> None:
        super().__init__()
        self.script = list(script)

    def generate(self, messages, stop_sequences=None, tools_to_call_from=None, **kwargs):  # noqa: ANN001, ANN003 — signature mirrors the base
        step = self.script.pop(0) if self.script else {"name": "final_answer", "args": {"answer": "done"}}
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="",
            tool_calls=[
                ChatMessageToolCall(
                    id="call_1",
                    type="function",
                    function=ChatMessageToolCallFunction(
                        name=step["name"], arguments=json.dumps(step["args"])
                    ),
                )
            ],
        )


def _scripted_steps() -> list[dict]:
    """A canned investigation: list files, read evidence, run a command, submit."""
    return [
        {"name": "list_files", "args": {}},
        {"name": "read_file", "args": {"path": "fixtures/kubectl-get-pods.txt"}},
        {"name": "run_command", "args": {"command": "echo demo — a real agent would kubectl/az here"}},
        {"name": "submit_answer", "args": ANSWER},
    ]


def build_agent(tools: list[Tool]) -> ToolCallingAgent:
    """Factory the adapter imports via SMOLAGENTS_AGENT_TARGET."""
    return ToolCallingAgent(
        tools=list(tools),
        model=ScriptedModel(script=_scripted_steps()),
        max_steps=MAX_STEPS,
        verbosity_level=-1,
    )


def main() -> int:
    task_dir = Path(os.environ.get("TASK_DIR", "."))
    try:
        from agents.smolagents_adapter import build_tools, load_target  # noqa: PLC0415 — script-mode import

        target = load_target(os.environ["SMOLAGENTS_AGENT_TARGET"])
        state: dict = {}
        agent = target(build_tools(task_dir, state))
        symptom_md = task_dir / "symptom.md"
        symptom = symptom_md.read_text(encoding="utf-8") if symptom_md.is_file() else "(no symptom provided)"
        agent.run(symptom)
        answer = state.get("answer", {})
    except Exception:  # noqa: BLE001 — any failure must degrade to a graded FAIL
        answer = {}  # graded as a FAIL result; never crash the runner
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
