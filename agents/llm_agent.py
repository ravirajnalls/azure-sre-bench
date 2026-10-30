#!/usr/bin/env python3
"""LLM baseline agent: one OpenAI-compatible chat call per task (issue #38).

Reads `symptom.md` and every file in `fixtures/` (each truncated to 20 KB),
sends a single prompt to a chat model, and prints the contract JSON the runner
can grade. Config is env-only so any OpenAI-compatible provider works:

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

import requests

FIXTURE_TRUNCATE_BYTES = 20 * 1024  # 20 KB per file, per the issue
SYSTEM_PROMPT = """\
You are an experienced site reliability engineer diagnosing a production incident.
You are given a symptom and a folder of evidence files (CLI output, logs, queries).
Read the evidence carefully — it may contain healthy red herrings.

Respond with ONLY a JSON object, no prose, in exactly this shape:
{"root_cause": "...", "evidence": ["file: observation", "..."], "fix": "..."}

- root_cause: the underlying cause in one paragraph.
- evidence: specific lines/fields from named fixture files that support the diagnosis.
- fix: the concrete remediation."""


def read_truncated(path: Path, limit: int = FIXTURE_TRUNCATE_BYTES) -> str:
    """Read a text file, truncated to `limit` bytes."""
    try:
        data = path.read_bytes()[:limit]
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def collect_evidence(task_dir: Path) -> list[tuple[str, str]]:
    """Return (display name, content) for symptom.md and every fixtures/ file."""
    paths = [task_dir / "symptom.md"]
    fixtures_dir = task_dir / "fixtures"
    if fixtures_dir.is_dir():
        paths.extend(sorted(p for p in fixtures_dir.iterdir() if p.is_file()))

    entries: list[tuple[str, str]] = []
    for path in paths:
        if not path.is_file():
            continue
        rel = path.relative_to(task_dir).as_posix()
        entries.append((rel, read_truncated(path)))
    return entries


def build_user_prompt(entries: list[tuple[str, str]]) -> str:
    parts = []
    for name, content in entries:
        if name == "symptom.md":
            parts.append(f"## Symptom\n\n{content}")
        else:
            parts.append(f"\n## Evidence: {name}\n\n{content}")
    parts.append(
        "\nDiagnose the incident and respond with the JSON object described in "
        "the system prompt."
    )
    return "\n".join(parts)


def strip_fences(text: str) -> str:
    """Remove markdown code fences the model may wrap its answer in."""
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        stripped = stripped[first_newline + 1 :] if first_newline != -1 else ""
    return stripped.removesuffix("```").strip()


def extract_answer(content: str) -> dict:
    """Parse the model's reply into the contract dict; {} when it isn't JSON."""
    try:
        answer = json.loads(strip_fences(content))
    except ValueError:
        return {}
    return answer if isinstance(answer, dict) else {}


def call_llm(messages: list[dict], timeout: int = 120) -> dict:
    """One chat-completion call; returns the full response payload.

    The caller reads the assistant message out of `choices[0].message` and the
    token/cost telemetry out of `usage` (issue #112) — both live in the same
    payload, so one request object serves the answer and its cost.
    """
    base_url = os.environ["LLM_BASE_URL"].rstrip("/")
    response = requests.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {os.environ['LLM_API_KEY']}",
            "Content-Type": "application/json",
        },
        json={
            "model": os.environ["LLM_MODEL"],
            "messages": messages,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


# Provider field names -> the benchmark's usage keys (issue #112). The
# OpenAI-compatible convention is `prompt_tokens`/`completion_tokens`; a
# provider that reports cost natively may use `cost` or `cost_usd`.
_USAGE_ALIASES = {
    "input_tokens": ("input_tokens", "prompt_tokens"),
    "output_tokens": ("output_tokens", "completion_tokens"),
    "cost_usd": ("cost_usd", "cost"),
}


def usage_from_payload(payload: dict) -> dict | None:
    """The normalized usage telemetry in a chat-completion payload, or None.

    Only numeric values survive (booleans and strings are dropped), and a
    payload with no telemetry yields None so the answer carries no empty noise.
    """
    raw = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return None
    usage: dict = {}
    for key, aliases in _USAGE_ALIASES.items():
        for alias in aliases:
            value = raw.get(alias)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                usage[key] = value
                break
    return usage or None


def merge_usage(total: dict | None, delta: dict | None) -> dict | None:
    """Sum one call's usage into a running total (issue #112).

    Multi-call agents (the ReAct loop) accumulate across calls; a None total
    starts the accumulation and a None delta passes the total through.
    """
    if not delta:
        return total
    merged = dict(total or {})
    for key, value in delta.items():
        merged[key] = merged.get(key, 0) + value
    return merged


def main() -> int:
    task_dir = Path(os.environ.get("TASK_DIR", "."))
    entries = collect_evidence(task_dir)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(entries)},
    ]
    try:
        payload = call_llm(messages)
        answer = extract_answer(
            str(payload.get("choices", [{}])[0].get("message", {}).get("content"))
        )
        usage = usage_from_payload(payload)
        if usage is not None:
            answer["usage"] = usage
    except Exception:  # noqa: BLE001 — any provider failure must degrade to a graded FAIL
        answer = {}  # graded as a FAIL result; never crash the runner
    if entries:  # files read while building the prompt (issue #112)
        answer["files_read"] = [rel for rel, _ in entries]
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
