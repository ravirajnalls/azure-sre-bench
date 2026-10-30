#!/usr/bin/env python3
"""Native tool-use baseline agent for Claude and GPT (issue #174).

Same investigation loop, tools, and step budget as the ReAct baseline
(`agents/react_agent.py`), but instead of the OpenAI-compatible chat shim it
speaks each provider's native tool-calling API directly:

- **Anthropic Messages API** (`/v1/messages`) — tools as `input_schema`
  objects, tool results fed back as `tool_result` content blocks.
- **OpenAI Responses API** (`/v1/responses`) — flat function tools, tool
  results fed back as `function_call_output` input items.

Both surfaces get the identical `list_files`/`read_file` tool set and the
identical 12-step budget as the ReAct baseline, so the leaderboard rows are
directly comparable across Claude, GPT, and the OpenAI-compatible shim.

Config (env-only):

    NATIVE_PROVIDER   "anthropic" or "openai" — which native API to speak

    Anthropic:
        ANTHROPIC_API_KEY    API key (sent as the x-api-key header)
        ANTHROPIC_MODEL      e.g. a claude-... model name
        ANTHROPIC_BASE_URL   optional, default https://api.anthropic.com
        ANTHROPIC_MAX_TOKENS optional, default 4096

    OpenAI:
        OPENAI_API_KEY       bearer token
        OPENAI_MODEL         e.g. a gpt-... model name
        OPENAI_BASE_URL      optional, default https://api.openai.com/v1

On any error it prints `{}` and exits 0 — the runner grades that as a FAIL
result, never a crash. Credentials are never printed: keys exist only inside
request headers and are never logged.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.llm_agent import extract_answer, merge_usage, usage_from_payload  # noqa: E402
from agents.react_agent import (  # noqa: E402
    MAX_STEPS,
    SYSTEM_PROMPT,
    TOOLS,
    build_user_prompt,
    execute_tool,
)

REQUEST_TIMEOUT = 120  # seconds per provider call
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_ANTHROPIC_MAX_TOKENS = 4096
FORCE_FINAL_PROMPT = "You must answer now. Respond with only the JSON object."


# --- normalized conversation events -------------------------------------------
#
# The loop records what happened as provider-neutral events and each request
# builder translates the full event log into its provider's wire format. That
# keeps the investigation loop identical across providers and the builders
# pure — both are unit-testable without HTTP.


@dataclass
class ToolUse:
    """One tool invocation the model asked for, arguments as a JSON string."""

    id: str
    name: str
    arguments: str


@dataclass
class Turn:
    """A parsed provider response: final text, tool calls, and usage."""

    text: str | None
    tool_uses: list[ToolUse]
    usage: dict | None = None


@dataclass
class UserText:
    """A user-role message (the symptom prompt, or the forced final answer)."""

    text: str


@dataclass
class AssistantTurn:
    """What the model produced: final text and/or tool calls."""

    text: str | None
    tool_uses: list[ToolUse]


@dataclass
class ToolResult:
    """The outcome of one executed tool call, keyed by the model's call id."""

    tool_use_id: str
    content: str


Event = UserText | AssistantTurn | ToolResult


def parse_arguments(arguments: str) -> dict:
    """Parse a tool-call arguments JSON string into a dict; {} when invalid."""
    try:
        parsed = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --- wire-format converters (one source of truth: react_agent.TOOLS) ----------


def to_anthropic_tools(tools: list[dict]) -> list[dict]:
    """Chat-completions tool schema -> Anthropic `input_schema` tool schema."""
    return [
        {
            "name": tool["function"]["name"],
            "description": tool["function"]["description"],
            "input_schema": tool["function"]["parameters"],
        }
        for tool in tools
    ]


def to_openai_responses_tools(tools: list[dict]) -> list[dict]:
    """Chat-completions tool schema -> Responses API flat function tools."""
    return [
        {
            "type": "function",
            "name": tool["function"]["name"],
            "description": tool["function"]["description"],
            "parameters": tool["function"]["parameters"],
        }
        for tool in tools
    ]


def anthropic_request(events: list[Event], model: str, tools: list[dict] | None) -> dict:
    """Build a Messages API request body from the normalized event log.

    Adjacent user-role content is merged into one message: tool results and a
    forced final-answer prompt would otherwise produce consecutive user
    messages, which the Messages API rejects.
    """
    messages: list[dict] = []

    def append_user(blocks: list[dict]) -> None:
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": "user", "content": blocks})

    for event in events:
        if isinstance(event, UserText):
            append_user([{"type": "text", "text": event.text}])
        elif isinstance(event, AssistantTurn):
            blocks: list[dict] = []
            if event.text:
                blocks.append({"type": "text", "text": event.text})
            for use in event.tool_uses:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": use.id,
                        "name": use.name,
                        "input": parse_arguments(use.arguments),
                    }
                )
            if blocks:  # never send an empty assistant message
                messages.append({"role": "assistant", "content": blocks})
        elif isinstance(event, ToolResult):
            append_user(
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": event.tool_use_id,
                        "content": event.content,
                    }
                ]
            )

    body: dict = {
        "model": model,
        "max_tokens": int(
            os.environ.get("ANTHROPIC_MAX_TOKENS", str(DEFAULT_ANTHROPIC_MAX_TOKENS))
        ),
        "system": SYSTEM_PROMPT,
        "messages": messages,
    }
    if tools is not None:
        body["tools"] = to_anthropic_tools(tools)
    return body


def openai_request(events: list[Event], model: str, tools: list[dict] | None) -> dict:
    """Build a Responses API request body from the normalized event log.

    The system prompt rides in the top-level `instructions` field; assistant
    turns become `function_call` input items; tool results become
    `function_call_output` items keyed by `call_id`.
    """
    input_items: list[dict] = []
    for event in events:
        if isinstance(event, UserText):
            input_items.append({"role": "user", "content": event.text})
        elif isinstance(event, AssistantTurn):
            if event.text:
                input_items.append({"role": "assistant", "content": event.text})
            for use in event.tool_uses:
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": use.id,
                        "name": use.name,
                        "arguments": use.arguments,
                    }
                )
        elif isinstance(event, ToolResult):
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": event.tool_use_id,
                    "output": event.content,
                }
            )

    body: dict = {"model": model, "instructions": SYSTEM_PROMPT, "input": input_items}
    if tools is not None:
        body["tools"] = to_openai_responses_tools(tools)
    return body


def anthropic_turn(payload: dict) -> Turn:
    """Parse a Messages API response into a Turn."""
    content = payload.get("content") or []
    text = "".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
    tool_uses = [
        ToolUse(
            id=str(block.get("id", "")),
            name=str(block.get("name", "")),
            arguments=json.dumps(block.get("input") or {}),
        )
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ]
    return Turn(text=text or None, tool_uses=tool_uses, usage=usage_from_payload(payload))


def openai_turn(payload: dict) -> Turn:
    """Parse a Responses API response into a Turn."""
    text_parts: list[str] = []
    tool_uses: list[ToolUse] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            text_parts.extend(
                str(part.get("text", ""))
                for part in item.get("content") or []
                if isinstance(part, dict) and part.get("type") == "output_text"
            )
        elif item.get("type") == "function_call":
            tool_uses.append(
                ToolUse(
                    id=str(item.get("call_id") or item.get("id") or ""),
                    name=str(item.get("name", "")),
                    arguments=str(item.get("arguments") or "{}"),
                )
            )
    return Turn(text="".join(text_parts) or None, tool_uses=tool_uses, usage=usage_from_payload(payload))


# --- transports ---------------------------------------------------------------


def anthropic_endpoint() -> tuple[str, dict]:
    """Base URL + headers for the Messages API; key read from the env."""
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": ANTHROPIC_API_VERSION,
        "Content-Type": "application/json",
    }
    return f"{base}/v1/messages", headers


def openai_endpoint() -> tuple[str, dict]:
    """Base URL + headers for the Responses API; key read from the env."""
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    headers = {
        "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
        "Content-Type": "application/json",
    }
    return f"{base}/responses", headers


def make_call(provider: str) -> Callable[..., Turn]:
    """Bind one provider's endpoint, model, builder, and parser into a call.

    The returned callable has the loop-neutral signature
    `call(events, tools) -> Turn`; a missing env var raises KeyError here so
    `main` degrades to the graded-FAIL `{}` before any request is made.
    """
    if provider == "anthropic":
        url, headers = anthropic_endpoint()
        model = os.environ["ANTHROPIC_MODEL"]
        build, parse = anthropic_request, anthropic_turn
    elif provider == "openai":
        url, headers = openai_endpoint()
        model = os.environ["OPENAI_MODEL"]
        build, parse = openai_request, openai_turn
    else:
        raise ValueError(f"unknown NATIVE_PROVIDER: {provider!r}")

    def call(events: list[Event], tools: list[dict] | None, timeout: int = REQUEST_TIMEOUT) -> Turn:
        response = requests.post(
            url, headers=headers, json=build(events, model, tools), timeout=timeout
        )
        response.raise_for_status()
        return parse(response.json())

    return call


# --- investigation loop -------------------------------------------------------


def count_served_read(use: ToolUse, result: str) -> str | None:
    """The path a read_file call served, or None when it wasn't a served read.

    Refusals and argument errors are not reads (issue #112 telemetry).
    """
    if use.name != "read_file":
        return None
    try:
        served = json.loads(result)
    except ValueError:
        return None
    if not (isinstance(served, dict) and "content" in served):
        return None
    args = parse_arguments(use.arguments)
    return str(args["path"]) if "path" in args else None


def run_agent(
    task_dir: Path, call: Callable[..., Turn], max_steps: int = MAX_STEPS
) -> dict:
    """Investigation loop; returns the contract dict or {} when the agent fails.

    Structurally identical to `react_agent.run_agent` — same system prompt,
    same two tools, same step budget, same forced final answer — so results
    stay comparable with the OpenAI-compatible baseline. The only difference
    is that `call` speaks one provider's native tool API over a normalized
    event log, which is rebuilt into wire format on every request.
    """
    events: list[Event] = [UserText(build_user_prompt(task_dir))]
    usage_total: dict | None = None
    files_read: list[str] = []

    for step in range(max_steps):
        final = step == max_steps - 1
        if final:  # force an answer: no tools left to call
            events.append(UserText(FORCE_FINAL_PROMPT))
        try:
            turn = call(events, tools=None if final else TOOLS)
        except Exception:  # noqa: BLE001 — any provider failure must degrade to a graded FAIL
            return {}

        usage_total = merge_usage(usage_total, turn.usage)
        events.append(AssistantTurn(text=turn.text, tool_uses=turn.tool_uses))

        if not final and turn.tool_uses:
            for use in turn.tool_uses:
                result = execute_tool(task_dir, use.name, use.arguments)
                served = count_served_read(use, result)
                if served is not None:
                    files_read.append(served)
                events.append(ToolResult(tool_use_id=use.id, content=result))
            continue

        answer = extract_answer(str(turn.text or ""))
        if answer:
            if usage_total:
                answer["usage"] = usage_total
            if files_read:
                # Dedupe, keeping first-read order: the list names the files
                # read, not the read events (the runner counts unique paths).
                answer["files_read"] = list(dict.fromkeys(files_read))
            return answer

    return {}  # never converged within the step budget: a graded FAIL


def main() -> int:
    task_dir = Path(os.environ.get("TASK_DIR", "."))
    try:
        call = make_call(os.environ["NATIVE_PROVIDER"])
        answer = run_agent(task_dir, call)
    except Exception:  # noqa: BLE001 — any provider failure must degrade to a graded FAIL
        answer = {}  # graded as a FAIL result; never crash the runner
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
