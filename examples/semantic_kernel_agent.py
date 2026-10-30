#!/usr/bin/env python3
"""Example: a Semantic Kernel agent benchmarked through the adapter (issue #172).

Demonstrates the wiring the adapter expects — a `ChatCompletionAgent` service.
The chat service here is a scripted fake (subclass of SK's
`ChatCompletionClientBase`), so the example runs with no API key and is
exercised by pytest; swap in a real service with one line:

    service = AzureChatCompletion(deployment_name="...", endpoint="...", api_key=...)

Run it through the adapter against a task directory:

    TASK_DIR=tasks/monitoring/monitoring-001 \\
    SEMANTIC_KERNEL_AGENT_TARGET=examples.semantic_kernel_agent:build_service \\
    python agents/semantic_kernel_adapter.py

or standalone (same investigation loop, no adapter):

    TASK_DIR=tasks/monitoring/monitoring-001 \\
    SEMANTIC_KERNEL_AGENT_TARGET=examples.semantic_kernel_agent:build_service \\
    python examples/semantic_kernel_agent.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, AsyncGenerator

from pydantic import Field

from semantic_kernel.agents import ChatCompletionAgent
from semantic_kernel.connectors.ai.chat_completion_client_base import ChatCompletionClientBase
from semantic_kernel.contents import (
    AuthorRole,
    ChatMessageContent,
    FunctionCallContent,
    StreamingChatMessageContent,
)

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MAX_STEPS = 12

ANSWER = {
    "root_cause": "expired secret referenced by the liveness probe",
    "evidence": ["fixtures/kubectl-get-pods.txt: probe-api-0 failing probe with 401"],
    "fix": "rotate the secret and redeploy the pod",
}


class ScriptedChatCompletion(ChatCompletionClientBase):
    """Minimal SK chat service: pops one scripted step per completion call."""

    SUPPORTS_FUNCTION_CALLING = True

    script: list = Field(default_factory=list)
    calls: int = 0

    def __init__(self, script: list[dict], **kwargs) -> None:
        super().__init__(ai_model_id="scripted", script=script, **kwargs)

    async def _inner_get_chat_message_contents(self, chat_history, settings) -> list["ChatMessageContent"]:
        object.__setattr__(self, "calls", self.calls + 1)
        step = self.script.pop(0) if self.script else {"type": "text", "content": "done"}
        if step["type"] == "call":
            item = FunctionCallContent(
                id=f"call_{self.calls}", name=step["name"], arguments=json.dumps(step["args"])
            )
            return [ChatMessageContent(role=AuthorRole.ASSISTANT, content="", items=[item])]
        return [ChatMessageContent(role=AuthorRole.ASSISTANT, content=step["content"])]

    async def _inner_get_streaming_chat_message_contents(
        self, chat_history, settings, function_invoke_attempt: int = 0
    ) -> AsyncGenerator[list["StreamingChatMessageContent"], Any]:
        yield []


def _scripted_steps() -> list[dict]:
    """A canned investigation: list files, read evidence, run a command, submit."""
    return [
        {"type": "call", "name": "list_files", "args": {}},
        {"type": "call", "name": "read_file", "args": {"path": "fixtures/kubectl-get-pods.txt"}},
        {"type": "call", "name": "run_command", "args": {"command": "echo demo — a real agent would kubectl/az here"}},
        {"type": "call", "name": "submit_answer", "args": ANSWER},
        {"type": "text", "content": "Investigation complete."},
    ]


def build_service() -> ChatCompletionClientBase:
    """Factory the adapter imports via SEMANTIC_KERNEL_AGENT_TARGET."""
    return ScriptedChatCompletion(script=_scripted_steps())


def main() -> int:
    task_dir = Path(os.environ.get("TASK_DIR", "."))
    try:
        from agents.semantic_kernel_adapter import SREPlugin, load_target  # noqa: PLC0415 — script-mode import

        state: dict = {}
        service = load_target(os.environ["SEMANTIC_KERNEL_AGENT_TARGET"])()
        symptom_md = task_dir / "symptom.md"
        symptom = symptom_md.read_text(encoding="utf-8") if symptom_md.is_file() else "(no symptom provided)"

        async def investigate() -> None:
            agent = ChatCompletionAgent(
                service=service,
                name="sre-investigator",
                instructions="Investigate, then submit_answer.",
                plugins=[SREPlugin(task_dir, state)],
            )
            async for _ in agent.invoke(messages=symptom):
                pass

        asyncio.run(investigate())
        answer = state.get("answer", {})
    except Exception:  # noqa: BLE001 — any failure must degrade to a graded FAIL
        answer = {}  # graded as a FAIL result; never crash the runner
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
