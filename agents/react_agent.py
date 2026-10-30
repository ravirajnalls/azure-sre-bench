#!/usr/bin/env python3
"""Tool-using ReAct baseline agent (issue #39).

Same env-only config as the LLM baseline (`agents/llm_agent.py`), but instead of
dumping every fixture into one prompt, the model gets two tools — `list_files`
and `read_file` — and loops up to 12 steps, investigating before it answers.
The leaderboard compares this against the evidence-dumping baseline to show
whether on-demand investigation beats a single context-stuffed call.

Config (any OpenAI-compatible provider):

    LLM_BASE_URL   e.g. https://<resource>.openai.azure.com/v1 (Azure AI Foundry)
    LLM_API_KEY    bearer token for the endpoint
    LLM_MODEL      model/deployment name

On any error it prints `{}` and exits 0 — the runner grades that as a FAIL
result, never a crash.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Callable

import requests

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.llm_agent import extract_answer, merge_usage, read_truncated, usage_from_payload  # noqa: E402

MAX_STEPS = 12  # investigation steps before the model is forced to answer

SYSTEM_PROMPT = """\
You are an experienced site reliability engineer diagnosing a production incident.
You are given a symptom and access to a folder of evidence files (CLI output,
logs, queries) through two tools:

- list_files(): lists the available evidence files.
- read_file({"path": "..."}): reads one file, path relative to the task directory.

Investigate before answering — evidence may contain healthy red herrings. Read
the files that matter rather than guessing. When you have enough evidence,
stop calling tools and respond with ONLY a JSON object, no prose, in exactly
this shape:
{"root_cause": "...", "evidence": ["file: observation", "..."], "fix": "..."}

- root_cause: the underlying cause in one paragraph.
- evidence: specific lines/fields from named fixture files that support the diagnosis.
- fix: the concrete remediation."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the evidence files available in the task directory.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read one evidence file (max 20 KB), path relative to the task "
                "directory. Files outside the task directory cannot be read."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
]


def resolve_task_path(task_dir: Path, raw: str) -> Path | None:
    """Resolve `raw` against `task_dir`, or None if it escapes the directory.

    Absolute paths, `..` traversal, and symlink escapes all fail the containment
    check because `resolve()` follows links to the real location.
    """
    if not raw:
        return None
    task_root = task_dir.resolve()
    candidate = (task_dir / raw).resolve()
    return candidate if candidate.is_relative_to(task_root) else None


def agent_visible_files(task_dir: Path) -> list[Path]:
    """Files the agent may see: symptom.md plus everything under fixtures/.

    Mirrors the runner's workspace copy (bench/run.py), so hidden rubric files
    (`expected.json`, `solution.json`) are invisible to list_files and unreadable
    by read_file even when TASK_DIR points at the raw task directory.
    """
    paths = [task_dir / "symptom.md"]
    fixtures_dir = task_dir / "fixtures"
    if fixtures_dir.is_dir():
        paths.extend(sorted(p for p in fixtures_dir.iterdir() if p.is_file()))
    return [p for p in paths if p.is_file()]


def execute_tool(task_dir: Path, name: str, arguments: str) -> str:
    """Run one tool call and return the JSON string the model sees as its result."""
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        return json.dumps({"error": "arguments must be a JSON object"})

    if name == "list_files":
        files = sorted(p.relative_to(task_dir).as_posix() for p in agent_visible_files(task_dir))
        return json.dumps({"files": files})

    if name == "read_file":
        path = resolve_task_path(task_dir, str(args.get("path", "")))
        if path is None or path not in agent_visible_files(task_dir):
            return json.dumps(
                {"error": "refused: path is outside the task directory"}
            )
        return json.dumps({"content": read_truncated(path)})

    return json.dumps({"error": f"unknown tool: {name}"})


def chat(messages: list[dict], tools: list[dict] | None, timeout: int = 120) -> dict:
    """One chat-completion call; returns the full response payload.

    The caller reads the assistant message out of `choices[0].message` and the
    token/cost telemetry out of `usage` (issue #112) — multi-call accumulation
    happens in `run_agent`.
    """
    base_url = os.environ["LLM_BASE_URL"].rstrip("/")
    body: dict = {"model": os.environ["LLM_MODEL"], "messages": messages}
    if tools is not None:
        body["tools"] = tools
    response = requests.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def build_user_prompt(task_dir: Path) -> str:
    """Seed the conversation with the symptom; files come via tools."""
    symptom = read_truncated(task_dir / "symptom.md") or "(no symptom provided)"
    return (
        f"## Symptom\n\n{symptom}\n\n"
        "Use the list_files and read_file tools to investigate the evidence, "
        "then diagnose the incident and respond with the JSON object described "
        "in the system prompt."
    )


def run_agent(
    task_dir: Path, call: Callable[..., dict] = chat, max_steps: int = MAX_STEPS
) -> dict:
    """Investigation loop; returns the contract dict or {} when the agent fails.

    The returned answer also carries the efficiency telemetry issue #112 asks
    for when it is known: `usage` (token/cost totals summed across calls) and
    `files_read` (paths successfully served by `read_file`). Both keys are
    omitted when there is nothing to report.
    """
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(task_dir)},
    ]
    usage_total: dict | None = None
    files_read: list[str] = []

    for step in range(max_steps):
        final = step == max_steps - 1
        if final:  # force an answer: no tools left to call
            messages.append(
                {
                    "role": "user",
                    "content": "You must answer now. Respond with only the JSON object.",
                }
            )
        try:
            payload = call(messages, tools=None if final else TOOLS)
            message = payload["choices"][0]["message"]
        except Exception:  # noqa: BLE001 — any provider failure must degrade to a graded FAIL
            return {}

        usage_total = merge_usage(usage_total, usage_from_payload(payload))
        messages.append(message)
        tool_calls = message.get("tool_calls") or []
        if not final and tool_calls:
            for tool_call in tool_calls:
                function = tool_call.get("function", {})
                result = execute_tool(
                    task_dir,
                    str(function.get("name", "")),
                    str(function.get("arguments") or ""),
                )
                # Count a file read only when the tool actually served content
                # (issue #112): refusals and argument errors are not reads.
                try:
                    served = json.loads(result)
                except ValueError:
                    served = {}
                if (
                    str(function.get("name", "")) == "read_file"
                    and isinstance(served, dict)
                    and "content" in served
                ):
                    try:
                        path = str(json.loads(str(function.get("arguments") or ""))["path"])
                    except (ValueError, KeyError, TypeError):
                        path = "?"
                    files_read.append(path)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id", ""),
                        "content": result,
                    }
                )
            continue

        answer = extract_answer(str(message.get("content") or ""))
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
        answer = run_agent(task_dir)
    except Exception:  # noqa: BLE001 — any provider failure must degrade to a graded FAIL
        answer = {}  # graded as a FAIL result; never crash the runner
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
