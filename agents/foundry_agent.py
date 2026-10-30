#!/usr/bin/env python3
"""Azure AI Foundry baseline agent — same investigation loop as `react_agent` (issue #121).

Instead of the raw chat-completions surface, this baseline speaks the **Foundry
Agents API** on a Foundry project endpoint: it creates an agent (deployment +
SRE instructions + the two investigation tools), a thread, posts the symptom,
and drives a run — polling it and feeding `submit_tool_outputs` whenever the
model asks for a tool — until the assistant message carries the contract JSON.

Config (env-only):

    FOUNDRY_ENDPOINT  project endpoint, e.g.
                      https://<resource>.services.ai.azure.com/api/projects/<project>
    FOUNDRY_MODEL     model deployment name
    FOUNDRY_API_KEY   optional project API key (`api-key` header). When unset,
                      Entra ID is used via DefaultAzureCredential
                      (azure-identity package, token scope https://ai.azure.com/.default)
                      — the documented setup needs the "Azure AI Developer"/
                      project-level data-plane role, e.g. Azure AI Project Owner.
    FOUNDRY_API_VERSION              optional, default 2025-05-01
    FOUNDRY_POLL_INTERVAL_SECONDS    optional run-poll interval, default 1.0

RBAC: API-key auth needs no role; Entra ID callers need a project data-plane
role (see https://learn.microsoft.com/azure/ai-foundry/agents/quickstart).

On any error it prints `{}` and exits 0 — the runner grades that as a FAIL
result, never a crash. Credentials are never printed: keys and tokens exist
only inside request headers and are never logged.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.llm_agent import extract_answer  # noqa: E402
from agents.react_agent import (  # noqa: E402
    SYSTEM_PROMPT,
    TOOLS,
    build_user_prompt,
    execute_tool,
)

AGENT_NAME = "azure-sre-bench-foundry"
DEFAULT_API_VERSION = "2025-05-01"
MAX_STEPS = 12  # tool-output rounds before the run is abandoned


def poll_interval() -> float:
    return float(os.environ.get("FOUNDRY_POLL_INTERVAL_SECONDS", "1"))


def auth_headers() -> dict[str, str]:
    """Auth headers for the Foundry project endpoint: API key or Entra ID.

    The chosen credential is returned only as a header dict — never logged.
    Entra ID needs the `azure-identity` package; its absence is a config error
    the caller degrades to a graded FAIL.
    """
    api_key = os.environ.get("FOUNDRY_API_KEY")
    if api_key:
        return {"api-key": api_key, "Content-Type": "application/json"}
    from azure.identity import DefaultAzureCredential  # deferred: optional dependency

    token = DefaultAzureCredential().get_token("https://ai.azure.com/.default")
    return {"Authorization": f"Bearer {token.token}", "Content-Type": "application/json"}


def call_api(method: str, path: str, body: dict | None = None) -> dict:
    """One Foundry Agents API call; returns the parsed JSON body."""
    endpoint = os.environ["FOUNDRY_ENDPOINT"].rstrip("/")
    api_version = os.environ.get("FOUNDRY_API_VERSION", DEFAULT_API_VERSION)
    response = requests.request(
        method,
        f"{endpoint}{path}",
        params={"api-version": api_version},
        headers=auth_headers(),
        json=body,
        timeout=120,
    )
    response.raise_for_status()
    return response.json()


def create_agent() -> str:
    """Create the throwaway agent; returns its id."""
    payload = call_api(
        "POST",
        "/assistants",
        {
            "name": AGENT_NAME,
            "model": os.environ["FOUNDRY_MODEL"],
            "instructions": SYSTEM_PROMPT,
            "tools": TOOLS,
        },
    )
    return str(payload["id"])


def delete_agent(agent_id: str) -> None:
    """Best-effort cleanup — the API creates persistent agents per run."""
    try:
        call_api("DELETE", f"/assistants/{agent_id}")
    except Exception:  # noqa: BLE001 — cleanup failure must not fail a graded run
        pass


def create_thread() -> str:
    return str(call_api("POST", "/threads", {})["id"])


def add_message(thread_id: str, content: str) -> None:
    call_api("POST", f"/threads/{thread_id}/messages", {"role": "user", "content": content})


def create_run(thread_id: str, agent_id: str) -> dict:
    return call_api("POST", f"/threads/{thread_id}/runs", {"assistant_id": agent_id})


def get_run(thread_id: str, run_id: str) -> dict:
    return call_api("GET", f"/threads/{thread_id}/runs/{run_id}")


def submit_tool_outputs(thread_id: str, run_id: str, tool_outputs: list[dict]) -> dict:
    return call_api(
        "POST", f"/threads/{thread_id}/runs/{run_id}/submit_tool_outputs", {"tool_outputs": tool_outputs}
    )


def latest_assistant_text(thread_id: str) -> str:
    """Concatenate the newest assistant message's text segments."""
    payload = call_api("GET", f"/threads/{thread_id}/messages")
    for message in payload.get("data", []):
        if message.get("role") != "assistant":
            continue
        return "".join(
            segment.get("text", {}).get("value", "")
            for segment in message.get("content", [])
            if segment.get("type") == "text"
        )
    return ""


def run_agent(task_dir: Path) -> dict:
    """Investigation loop; returns the contract dict or {} when the agent fails."""
    agent_id = create_agent()
    try:
        thread_id = create_thread()
        add_message(thread_id, build_user_prompt(task_dir))

        run = create_run(thread_id, agent_id)
        for _ in range(MAX_STEPS):
            while run.get("status") in ("queued", "in_progress"):
                time.sleep(poll_interval())
                run = get_run(str(run["thread_id"]), str(run["id"]))

            if run.get("status") != "requires_action":
                break

            tool_calls = run["required_action"]["submit_tool_outputs"]["tool_calls"]
            tool_outputs = [
                {
                    "tool_call_id": str(tool_call["id"]),
                    "output": execute_tool(
                        task_dir,
                        str(tool_call.get("function", {}).get("name", "")),
                        str(tool_call.get("function", {}).get("arguments") or ""),
                    ),
                }
                for tool_call in tool_calls
            ]
            run = submit_tool_outputs(str(run["thread_id"]), str(run["id"]), tool_outputs)
        else:  # step budget exhausted without a terminal status
            return {}

        if run.get("status") != "completed":
            return {}
        return extract_answer(latest_assistant_text(thread_id))
    finally:
        delete_agent(agent_id)


def main() -> int:
    task_dir = Path(os.environ.get("TASK_DIR", "."))
    try:
        answer = run_agent(task_dir)
    except Exception:  # noqa: BLE001 — any provider failure must degrade to a graded FAIL
        answer = {}  # graded as a FAIL result; never crash the runner
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
