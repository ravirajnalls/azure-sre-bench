"""Tests for the tool-using ReAct baseline agent (issue #39).

A local HTTP server stands in for the OpenAI-compatible endpoint, so the mocked
model drives the agent as a real subprocess: the first call returns a tool call,
the later one a final answer, and the contract JSON must reach the agent's
stdout. The TASK_DIR boundary gets both unit tests and an end-to-end attempt by
the mock model to escape it.
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

from agents import react_agent

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_TASK = REPO_ROOT / "tests" / "data" / "monitoring" / "run-test-001"
AGENT = REPO_ROOT / "agents" / "react_agent.py"

ANSWER = {
    "root_cause": "expired secret referenced by the probe",
    "evidence": ["kubectl-describe-pod.txt: probe failing with 401"],
    "fix": "rotate the secret and redeploy",
}


def _assistant_message(content: str | None = None, tool_calls: list | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return message


def _tool_call(name: str, arguments: str, call_id: str = "call_1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class _ScriptedHandler(BaseHTTPRequestHandler):
    """Replays a scripted list of assistant messages, repeating the last one."""

    responses: list[dict] = []
    requests: list[dict] = []

    def do_POST(self):
        self.requests.append(
            json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        )
        index = min(self.server.request_index, len(self.responses) - 1)  # type: ignore[attr-defined]
        self.server.request_index += 1  # type: ignore[attr-defined]
        self._respond(self.responses[index])

    def _respond(self, message: dict) -> None:
        body = json.dumps({"choices": [{"message": message}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence request logging
        pass


def _start_server(handler_class: type) -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), handler_class)
    server.request_index = 0  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _run_agent(server: HTTPServer, task_dir: Path) -> subprocess.CompletedProcess:
    env = dict(
        os.environ,
        LLM_BASE_URL=f"http://127.0.0.1:{server.server_port}/v1",
        LLM_API_KEY="test-key",
        LLM_MODEL="test-model",
        TASK_DIR=str(task_dir),
    )
    return subprocess.run(
        [sys.executable, str(AGENT)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def test_one_tool_call_then_final_answer():
    """list_files round-trips, then the final answer reaches stdout as JSON."""
    _ScriptedHandler.responses = [
        _assistant_message(tool_calls=[_tool_call("list_files", "{}")]),
        _assistant_message(content=json.dumps(ANSWER)),
    ]
    _ScriptedHandler.requests = []
    server = _start_server(_ScriptedHandler)
    try:
        proc = _run_agent(server, TEST_TASK)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ANSWER

    # Request 1 carries the tool schema; request 2 carries the tool result.
    assert "tools" in _ScriptedHandler.requests[0]
    assert [t["function"]["name"] for t in _ScriptedHandler.requests[0]["tools"]] == [
        "list_files",
        "read_file",
    ]
    roles = [m["role"] for m in _ScriptedHandler.requests[1]["messages"]]
    assert "tool" in roles
    tool_message = next(m for m in _ScriptedHandler.requests[1]["messages"] if m["role"] == "tool")
    listed = json.loads(tool_message["content"])["files"]
    assert listed == [
        "fixtures/app-insights-query.kql",
        "fixtures/kubectl-get-pods.txt",
    ]
    # Hidden rubric files must never be listed, even though TEST_TASK is the raw dir.
    assert "expected.json" not in listed
    assert "solution.json" not in listed


def test_read_file_tool_call_returns_fixture_content(tmp_path):
    """A read_file round-trip puts the file's content in the tool result."""
    (tmp_path / "symptom.md").write_text("probes failing", encoding="utf-8")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "kubectl-get-pods.txt").write_text(
        "pod probe-api-0 Running", encoding="utf-8"
    )

    _ScriptedHandler.responses = [
        _assistant_message(
            tool_calls=[
                _tool_call("read_file", json.dumps({"path": "fixtures/kubectl-get-pods.txt"}))
            ]
        ),
        _assistant_message(content=json.dumps(ANSWER)),
    ]
    _ScriptedHandler.requests = []
    server = _start_server(_ScriptedHandler)
    try:
        proc = _run_agent(server, tmp_path)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ANSWER
    second_request = _ScriptedHandler.requests[1]
    tool_message = next(m for m in second_request["messages"] if m["role"] == "tool")
    assert "pod probe-api-0 Running" in tool_message["content"]


def test_model_escape_attempt_is_refused(tmp_path):
    """The mock model tries ../ traversal; the tool result refuses and leaks nothing."""
    (tmp_path / "symptom.md").write_text("symptom", encoding="utf-8")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "a.txt").write_text("evidence", encoding="utf-8")
    secret = tmp_path / "expected.json"
    secret.write_text('{"secret_rubric": "hidden"}', encoding="utf-8")

    _ScriptedHandler.responses = [
        _assistant_message(
            tool_calls=[_tool_call("read_file", json.dumps({"path": "../expected.json"}))]
        ),
        _assistant_message(content=json.dumps(ANSWER)),
    ]
    _ScriptedHandler.requests = []
    server = _start_server(_ScriptedHandler)
    try:
        proc = _run_agent(server, tmp_path)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ANSWER
    sent = json.dumps(_ScriptedHandler.requests)
    assert "secret_rubric" not in sent
    tool_message = next(m for m in _ScriptedHandler.requests[1]["messages"] if m["role"] == "tool")
    assert "outside the task directory" in tool_message["content"]


def test_missing_env_prints_empty_object_and_exits_zero():
    """No credentials configured: {} on stdout, exit 0 — a graded FAIL, not a crash."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LLM_")}
    env["TASK_DIR"] = str(TEST_TASK)
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


def test_http_error_prints_empty_object_and_exits_zero():
    """A 500 from the provider is absorbed: {} on stdout, exit 0."""

    class FailingHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(500)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = _start_server(FailingHandler)
    try:
        proc = _run_agent(server, TEST_TASK)
    finally:
        server.shutdown()

    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}


def test_step_budget_forces_final_answer():
    """A model that never stops calling tools gets 12 steps, then {} exit 0."""
    _ScriptedHandler.responses = [
        _assistant_message(tool_calls=[_tool_call("list_files", "{}")]),
    ]
    _ScriptedHandler.requests = []
    server = _start_server(_ScriptedHandler)
    try:
        proc = _run_agent(server, TEST_TASK)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {}
    assert len(_ScriptedHandler.requests) == react_agent.MAX_STEPS
    assert "tools" not in _ScriptedHandler.requests[-1]  # final call has no tools


def test_resolve_task_path_blocks_escapes(tmp_path):
    inside = tmp_path / "fixtures" / "a.txt"
    inside.parent.mkdir()
    inside.write_text("x", encoding="utf-8")

    assert react_agent.resolve_task_path(tmp_path, "") is None
    assert react_agent.resolve_task_path(tmp_path, "fixtures/a.txt") == inside
    assert react_agent.resolve_task_path(tmp_path, "../expected.json") is None
    assert react_agent.resolve_task_path(tmp_path, "/etc/passwd") is None


def test_symlink_escape_is_refused(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)

    assert react_agent.resolve_task_path(tmp_path, "link.txt") is None


def test_execute_tool_handles_bad_input(tmp_path):
    assert "error" in json.loads(react_agent.execute_tool(tmp_path, "read_file", "not json"))
    assert "error" in json.loads(react_agent.execute_tool(tmp_path, "rm_rf", "{}"))
    result = json.loads(
        react_agent.execute_tool(tmp_path, "read_file", json.dumps({"path": "missing.txt"}))
    )
    assert "refused" in result["error"]


def test_execute_tool_lists_files_sorted(tmp_path):
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "fixtures" / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "symptom.md").write_text("s", encoding="utf-8")

    result = json.loads(react_agent.execute_tool(tmp_path, "list_files", "{}"))

    assert result["files"] == ["fixtures/a.txt", "fixtures/b.txt", "symptom.md"]


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (json.dumps(ANSWER), ANSWER),
        (f"```json\n{json.dumps(ANSWER)}\n```", ANSWER),
        ("not json", {}),
        ("[1, 2]", {}),
        (None, {}),
    ],
)
def test_final_content_parsing(content, expected):
    answer = react_agent.run_agent(
        TEST_TASK,
        call=lambda messages, tools: _assistant_message(content=content),
        max_steps=1,
    )
    assert answer == expected
