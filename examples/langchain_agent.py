#!/usr/bin/env python3
"""Example: a LangChain agent benchmarked through the adapter (issue #122).

Demonstrates the wiring the adapter expects — a factory that receives the three
scoped tools and returns a Runnable. The chat model here is a scripted fake
(`FakeMessagesListChatModel` subclass), so the example runs with no API key and
is exercised by pytest; swap in a real model with one line:

    model = ChatOpenAI(model="...", api_key=...)          # langchain_openai
    model = AzureChatOpenAI(deployment_name="...")        # langchain_azure

Run it through the adapter against a task directory:

    TASK_DIR=tasks/monitoring/monitoring-001 \
    LANGCHAIN_AGENT_TARGET=examples.langchain_agent:build_agent \
    python agents/langchain_adapter.py

or standalone (same investigation loop, no adapter):

    TASK_DIR=tasks/monitoring/monitoring-001 python examples/langchain_agent.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MAX_STEPS = 12

SYSTEM_PROMPT = """\
You are an experienced site reliability engineer diagnosing a production incident.
You have three tools:

- read_file({"path": "..."}): read one evidence file; call with no path to list them.
- run_command({"command": "..."}): run a diagnostic command from the task directory.
- submit_answer({"root_cause": ..., "evidence": [...], "fix": ...}): submit the report.

Investigate before answering — evidence may contain healthy red herrings. When
you have enough evidence, submit your answer with the submit_answer tool."""

ANSWER = {
    "root_cause": "expired secret referenced by the liveness probe",
    "evidence": ["fixtures/kubectl-get-pods.txt: probe-api-0 failing probe with 401"],
    "fix": "rotate the secret and redeploy the pod",
}


class ScriptedChatModel(FakeMessagesListChatModel):
    """FakeMessagesListChatModel that accepts bind_tools, as real models do."""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003 — signature mirrors base
        return self


def _scripted_responses() -> list[AIMessage]:
    """A canned investigation: list files, read evidence, run a command, submit."""
    return [
        AIMessage(
            content="",
            tool_calls=[{"name": "read_file", "args": {"path": ""}, "id": "call_1", "type": "tool_call"}],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_file",
                    "args": {"path": "fixtures/kubectl-get-pods.txt"},
                    "id": "call_2",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "run_command",
                    "args": {"command": "echo demo — a real agent would kubectl/az here"},
                    "id": "call_3",
                    "type": "tool_call",
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[{"name": "submit_answer", "args": ANSWER, "id": "call_4", "type": "tool_call"}],
        ),
        AIMessage(content="Investigation complete."),  # no tool calls: the loop stops
    ]


def investigate(model, tools, symptom: str, max_steps: int = MAX_STEPS) -> str:
    """Tool-calling loop: invoke the model, run its tool calls, until it answers."""
    by_name = {tool.name: tool for tool in tools}
    messages: list = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=f"Incident symptom:\n{symptom}"),
    ]
    for _ in range(max_steps):
        message = model.invoke(messages)
        messages.append(message)
        tool_calls = list(getattr(message, "tool_calls", None) or [])
        if not tool_calls:
            break
        for call in tool_calls:
            selected = by_name.get(call["name"])
            if selected is None:
                result = json.dumps({"error": f"unknown tool: {call['name']}"})
            else:
                try:
                    result = selected.invoke(call["args"])
                except Exception as exc:  # noqa: BLE001 — tool failures reach the model
                    result = json.dumps({"error": str(exc)})
            messages.append(ToolMessage(content=result, tool_call_id=call["id"]))
    return str(messages[-1].content)


def build_agent(tools):
    """Factory the adapter imports via LANGCHAIN_AGENT_TARGET."""
    model = ScriptedChatModel(responses=_scripted_responses()).bind_tools(tools)
    return RunnableLambda(lambda symptom: investigate(model, list(tools), symptom))


def main() -> int:
    task_dir = Path(os.environ.get("TASK_DIR", "."))
    try:
        from agents.langchain_adapter import build_tools  # noqa: PLC0415 — script-mode import

        state: dict = {}
        tools = build_tools(task_dir, state)
        model = ScriptedChatModel(responses=_scripted_responses()).bind_tools(tools)
        symptom = _symptom_text(task_dir)
        investigate(model, tools, symptom)
        answer = state.get("answer", {})
    except Exception:  # noqa: BLE001 — any failure must degrade to a graded FAIL
        answer = {}  # graded as a FAIL result; never crash the runner
    print(json.dumps(answer))
    return 0


def _symptom_text(task_dir: Path) -> str:
    """symptom.md if present; otherwise the raw task's `symptom` field.

    `bench run` materializes symptom.md into the agent workspace; a raw task
    directory only carries it inside task.yaml.
    """
    symptom_md = task_dir / "symptom.md"
    if symptom_md.is_file():
        return symptom_md.read_text(encoding="utf-8")
    task_yaml = task_dir / "task.yaml"
    if task_yaml.is_file():
        import yaml  # noqa: PLC0415 — only needed for raw task directories

        task = yaml.safe_load(task_yaml.read_text(encoding="utf-8")) or {}
        return str(task.get("symptom", ""))
    return ""


if __name__ == "__main__":
    sys.exit(main())
