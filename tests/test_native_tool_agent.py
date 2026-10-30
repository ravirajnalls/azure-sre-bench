"""Tests for the native tool-use baseline agent (issue #174).

A local HTTP server stands in for each provider's native endpoint — Anthropic
Messages API and OpenAI Responses API — so the mocked model drives the agent
as a real subprocess: the first call returns a tool call in the provider's
native wire format, the later one a final answer, and the contract JSON must
reach stdout. The TASK_DIR boundary and the step budget get the same
treatment as the ReAct baseline's tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agents import native_tool_agent, react_agent

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_TASK = REPO_ROOT / "tests" / "data" / "monitoring" / "run-test-001"
AGENT = REPO_ROOT / "agents" / "native_tool_agent.py"

ANSWER = {
    "root_cause": "expired secret referenced by the probe",
    "evidence": ["kubectl-describe-pod.txt: probe failing with 401"],
    "fix": "rotate the secret and redeploy",
}

PROVIDERS = ["anthropic", "openai"]


# --- scripted provider responses -----------------------------------------------


def anthropic_tool_use(call_id: str, name: str, arguments: dict) -> list[dict]:
    return [
        {"type": "tool_use", "id": call_id, "name": name, "input": arguments},
    ]


def anthropic_text(text: str) -> list[dict]:
    return [{"type": "text", "text": text}]


def openai_function_call(call_id: str, name: str, arguments: str) -> list[dict]:
    return [
        {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments},
    ]


def openai_message(text: str) -> list[dict]:
    return [{"type": "message", "content": [{"type": "output_text", "text": text}]}]


class _ScriptedHandler(BaseHTTPRequestHandler):
    """Replays a scripted list of provider payloads, repeating the last one.

    The payload shapes are provider-native (`content` blocks for Anthropic,
    `output` items for OpenAI) so the agent's parsers are exercised for real.
    """

    responses: list[dict] = []
    requests: list[dict] = []
    request_paths: list[str] = []
    request_headers: list[dict] = []

    def do_POST(self):
        self.requests.append(
            json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        )
        self.request_paths.append(self.path)
        self.request_headers.append(dict(self.headers))
        index = min(self.server.request_index, len(self.responses) - 1)  # type: ignore[attr-defined]
        self.server.request_index += 1  # type: ignore[attr-defined]
        self._respond(self.responses[index])

    def _respond(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence request logging
        pass


class _FailingHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(500)
        self.end_headers()

    def log_message(self, *args):
        pass


def _start_server(handler_class: type) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), handler_class)
    server.request_index = 0  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _tool_turn(provider: str, call_id: str, name: str, arguments: dict | str) -> dict:
    """One scripted provider payload carrying a single tool call.

    Anthropic's `input` is a JSON object on the wire; the Responses API takes
    `arguments` as a JSON string — the test hands in either and this normalizes.
    """
    if provider == "anthropic":
        input_value = arguments if isinstance(arguments, dict) else json.loads(arguments)
        return {
            "content": anthropic_tool_use(call_id, name, input_value),
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    arguments_str = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {
        "output": openai_function_call(call_id, name, arguments_str),
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _text_turn(provider: str, text: str) -> dict:
    """One scripted provider payload carrying the final answer."""
    if provider == "anthropic":
        return {"content": anthropic_text(text)}
    return {"output": openai_message(text)}


def _run_agent(server: HTTPServer, provider: str, task_dir: Path) -> subprocess.CompletedProcess:
    port = server.server_port
    env = dict(os.environ, TASK_DIR=str(task_dir))
    env = {k: v for k, v in env.items() if not k.startswith(("ANTHROPIC_", "OPENAI_"))}
    env.pop("NATIVE_PROVIDER", None)
    env["NATIVE_PROVIDER"] = provider
    if provider == "anthropic":
        env.update(
            ANTHROPIC_BASE_URL=f"http://127.0.0.1:{port}",
            ANTHROPIC_API_KEY="test-key",
            ANTHROPIC_MODEL="claude-test-model",
        )
    else:
        env.update(
            OPENAI_BASE_URL=f"http://127.0.0.1:{port}/v1",
            OPENAI_API_KEY="test-key",
            OPENAI_MODEL="gpt-test-model",
        )
    return subprocess.run(
        [sys.executable, str(AGENT)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


# --- end-to-end (mocked provider, real subprocess) -------------------------------


def test_anthropic_tool_round_trip_then_final_answer():
    """list_files round-trips through tool_use/tool_result blocks; JSON on stdout."""
    _ScriptedHandler.responses = [
        _tool_turn("anthropic", "toolu_1", "list_files", {}),
        _text_turn("anthropic", json.dumps(ANSWER)),
    ]
    _ScriptedHandler.requests = []
    _ScriptedHandler.request_paths = []
    _ScriptedHandler.request_headers = []
    server = _start_server(_ScriptedHandler)
    try:
        proc = _run_agent(server, "anthropic", TEST_TASK)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    # The tool call's turn usage (10/5) accumulates into the answer (issue #112).
    assert json.loads(proc.stdout) == {
        **ANSWER,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }

    # Anthropic Messages API surface: /v1/messages, x-api-key, api version.
    assert _ScriptedHandler.request_paths[0].endswith("/v1/messages")
    assert _ScriptedHandler.request_headers[0]["x-api-key"] == "test-key"
    assert "anthropic-version" in _ScriptedHandler.request_headers[0]

    first = _ScriptedHandler.requests[0]
    assert "system" in first and "site reliability engineer" in first["system"]
    assert [t["name"] for t in first["tools"]] == ["list_files", "read_file"]
    assert all("input_schema" in t and "parameters" not in t for t in first["tools"])

    # Request 2 carries the tool result back as a tool_result block. Two user
    # messages: the symptom, then the tool result (tool result + the forced
    # final answer would merge, but there is no force-final in this script).
    second = _ScriptedHandler.requests[1]
    user_messages = [m for m in second["messages"] if m["role"] == "user"]
    assert len(user_messages) == 2
    tool_results = [
        b for m in user_messages for b in m["content"] if b["type"] == "tool_result"
    ]
    assert tool_results and tool_results[0]["tool_use_id"] == "toolu_1"
    listed = json.loads(tool_results[0]["content"])["files"]
    assert listed == [
        "fixtures/app-insights-query.kql",
        "fixtures/kubectl-get-pods.txt",
    ]
    # Hidden rubric files must never be listed.
    assert "expected.json" not in listed
    assert "solution.json" not in listed


def test_openai_function_call_round_trip_then_final_answer():
    """function_call/function_call_output round-trip through the Responses API."""
    _ScriptedHandler.responses = [
        _tool_turn("openai", "call_1", "list_files", {}),
        _text_turn("openai", json.dumps(ANSWER)),
    ]
    _ScriptedHandler.requests = []
    _ScriptedHandler.request_paths = []
    _ScriptedHandler.request_headers = []
    server = _start_server(_ScriptedHandler)
    try:
        proc = _run_agent(server, "openai", TEST_TASK)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    # The tool call's turn usage (10/5) accumulates into the answer (issue #112).
    assert json.loads(proc.stdout) == {
        **ANSWER,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }

    # Responses API surface: /responses, bearer token, flat function tools.
    assert _ScriptedHandler.request_paths[0].endswith("/responses")
    assert _ScriptedHandler.request_headers[0]["Authorization"] == "Bearer test-key"
    first = _ScriptedHandler.requests[0]
    assert "instructions" in first and "site reliability engineer" in first["instructions"]
    assert [t["name"] for t in first["tools"]] == ["list_files", "read_file"]
    assert all(t["type"] == "function" and "parameters" in t for t in first["tools"])

    # Request 2 carries the function_call and its output.
    second = _ScriptedHandler.requests[1]
    kinds = [item.get("type") for item in second["input"]]
    assert "function_call" in kinds
    assert "function_call_output" in kinds
    output = next(i for i in second["input"] if i.get("type") == "function_call_output")
    assert output["call_id"] == "call_1"
    listed = json.loads(output["output"])["files"]
    assert "expected.json" not in listed
    assert "solution.json" not in listed


@pytest.mark.parametrize("provider", PROVIDERS)
def test_read_file_round_trip_reports_files_read(provider):
    """A served read_file lands in files_read on either provider."""
    _ScriptedHandler.responses = [
        _tool_turn(provider, "c1", "read_file", {"path": "fixtures/kubectl-get-pods.txt"}),
        _text_turn(provider, json.dumps(ANSWER)),
    ]
    _ScriptedHandler.requests = []
    server = _start_server(_ScriptedHandler)
    try:
        proc = _run_agent(server, provider, TEST_TASK)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        **ANSWER,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "files_read": ["fixtures/kubectl-get-pods.txt"],
    }


@pytest.mark.parametrize("provider", PROVIDERS)
def test_model_escape_attempt_is_refused(provider, tmp_path):
    """The mock model tries ../ traversal; the tool result refuses and leaks nothing."""
    (tmp_path / "symptom.md").write_text("symptom", encoding="utf-8")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "a.txt").write_text("evidence", encoding="utf-8")
    secret = tmp_path / "expected.json"
    secret.write_text('{"secret_rubric": "hidden"}', encoding="utf-8")

    _ScriptedHandler.responses = [
        _tool_turn(provider, "c1", "read_file", {"path": "../expected.json"}),
        _text_turn(provider, json.dumps(ANSWER)),
    ]
    _ScriptedHandler.requests = []
    server = _start_server(_ScriptedHandler)
    try:
        proc = _run_agent(server, provider, tmp_path)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        **ANSWER,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    sent = json.dumps(_ScriptedHandler.requests)
    assert "secret_rubric" not in sent
    assert "outside the task directory" in sent


@pytest.mark.parametrize("provider", PROVIDERS)
def test_missing_env_prints_empty_object_and_exits_zero(provider):
    """No credentials configured: {} on stdout, exit 0 — a graded FAIL, not a crash."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("LLM_", "ANTHROPIC_", "OPENAI_", "NATIVE_"))
    }
    env["TASK_DIR"] = str(TEST_TASK)
    env["NATIVE_PROVIDER"] = provider
    proc = subprocess.run(
        [sys.executable, str(AGENT)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )

    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}


def test_unknown_provider_prints_empty_object_and_exits_zero():
    """A bad NATIVE_PROVIDER degrades to {} instead of crashing the runner."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ANTHROPIC_", "OPENAI_", "NATIVE_"))
    }
    env["TASK_DIR"] = str(TEST_TASK)
    env["NATIVE_PROVIDER"] = "not-a-provider"
    proc = subprocess.run(
        [sys.executable, str(AGENT)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )

    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_http_error_prints_empty_object_and_exits_zero(provider):
    """A 500 from the provider is absorbed: {} on stdout, exit 0."""
    server = _start_server(_FailingHandler)
    try:
        proc = _run_agent(server, provider, TEST_TASK)
    finally:
        server.shutdown()

    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_step_budget_forces_final_answer(provider):
    """A model that never stops calling tools gets 12 steps, then {} exit 0."""
    _ScriptedHandler.responses = [_tool_turn(provider, "c1", "list_files", {})]
    _ScriptedHandler.requests = []
    server = _start_server(_ScriptedHandler)
    try:
        proc = _run_agent(server, provider, TEST_TASK)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {}
    assert len(_ScriptedHandler.requests) == react_agent.MAX_STEPS
    assert "tools" not in _ScriptedHandler.requests[-1]  # final call has no tools


# --- unit tests: converters, builders, loop internals ----------------------------


def test_step_budget_matches_react_baseline():
    """Identical limits by construction, so rows stay comparable."""
    assert native_tool_agent.MAX_STEPS == react_agent.MAX_STEPS


def test_tool_converters_preserve_the_react_baseline_tools():
    """Both wire converters carry the same names, descriptions, and parameters."""
    for converted in (
        native_tool_agent.to_anthropic_tools(react_agent.TOOLS),
        native_tool_agent.to_openai_responses_tools(react_agent.TOOLS),
    ):
        assert [t["name"] for t in converted] == [
            t["function"]["name"] for t in react_agent.TOOLS
        ]
        assert [t["description"] for t in converted] == [
            t["function"]["description"] for t in react_agent.TOOLS
        ]


def test_anthropic_request_merges_adjacent_user_events():
    """Tool results + forced final answer become one user message."""
    events = [
        native_tool_agent.UserText("symptom"),
        native_tool_agent.AssistantTurn(
            text=None,
            tool_uses=[native_tool_agent.ToolUse("c1", "list_files", "{}")],
        ),
        native_tool_agent.ToolResult("c1", '{"files": []}'),
        native_tool_agent.UserText("You must answer now."),
    ]

    body = native_tool_agent.anthropic_request(events, "claude-test", tools=None)

    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user"]
    final = body["messages"][-1]["content"]
    assert [b["type"] for b in final] == ["tool_result", "text"]
    assert body["system"] == react_agent.SYSTEM_PROMPT
    assert "tools" not in body


def test_openai_request_shape():
    """instructions carries the system prompt; events map to input items."""
    events = [
        native_tool_agent.UserText("symptom"),
        native_tool_agent.AssistantTurn(
            text=None,
            tool_uses=[native_tool_agent.ToolUse("c1", "list_files", "{}")],
        ),
        native_tool_agent.ToolResult("c1", '{"files": []}'),
    ]

    body = native_tool_agent.openai_request(events, "gpt-test", tools=react_agent.TOOLS)

    assert body["model"] == "gpt-test"
    assert body["instructions"] == react_agent.SYSTEM_PROMPT
    kinds = [item.get("type") for item in body["input"]]
    assert body["input"][0]["role"] == "user"  # user items have no type key
    assert "function_call" in kinds
    assert "function_call_output" in kinds
    assert [t["type"] for t in body["tools"]] == ["function", "function"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (json.dumps(ANSWER), ANSWER),
        (f"```json\n{json.dumps(ANSWER)}\n```", ANSWER),
        ("not json", {}),
        ("[1, 2]", {}),
        (None, {}),
    ],
)
def test_final_content_parsing(text, expected):
    """The same contract-JSON extraction as the ReAct baseline."""
    answer = native_tool_agent.run_agent(
        TEST_TASK,
        call=lambda events, tools: native_tool_agent.Turn(text=text, tool_uses=[]),
        max_steps=1,
    )
    assert answer == expected


def test_usage_accumulates_across_calls_and_files_read(tmp_path):
    """Per-call provider usage sums into one answer total; served reads count."""
    (tmp_path / "symptom.md").write_text("probes failing", encoding="utf-8")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "a.log").write_text("log line", encoding="utf-8")

    _ScriptedHandler.responses = [
        _tool_turn("anthropic", "c1", "read_file", {"path": "fixtures/a.log"}),
        _tool_turn("anthropic", "c2", "read_file", {"path": "fixtures/a.log"}),
        _text_turn("anthropic", json.dumps(ANSWER)),
    ]
    _ScriptedHandler.requests = []
    server = _start_server(_ScriptedHandler)
    try:
        proc = _run_agent(server, "anthropic", tmp_path)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    # Tool turns carry usage; the final text turn here carries none.
    assert out["usage"] == {"input_tokens": 20, "output_tokens": 10}
    assert out["files_read"] == ["fixtures/a.log"]


def test_refused_read_is_not_counted_as_files_read(tmp_path):
    """A read the tool refused is an attempt, not a file read."""
    _ScriptedHandler.responses = [
        _tool_turn("openai", "c1", "read_file", {"path": "../expected.json"}),
        _text_turn("openai", json.dumps(ANSWER)),
    ]
    server = _start_server(_ScriptedHandler)
    try:
        proc = _run_agent(server, "openai", tmp_path)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert "files_read" not in out


def test_count_served_read_handles_bad_input():
    """Malformed arguments and non-read tools never count as served reads."""
    use = native_tool_agent.ToolUse("c1", "read_file", "not json")
    assert native_tool_agent.count_served_read(use, '{"content": "x"}') is None
    list_use = native_tool_agent.ToolUse("c2", "list_files", "{}")
    assert native_tool_agent.count_served_read(list_use, '{"content": "x"}') is None
    read = native_tool_agent.ToolUse("c3", "read_file", '{"path": "fixtures/a.log"}')
    assert native_tool_agent.count_served_read(read, '{"content": "x"}') == "fixtures/a.log"
