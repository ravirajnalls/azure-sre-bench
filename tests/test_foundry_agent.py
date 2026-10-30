"""Tests for the Azure AI Foundry baseline agent (issue #121).

The house mocked-HTTP pattern from `test_react_agent.py`/`test_llm_agent.py`
(PR #87), adapted to the Foundry Agents API's multi-route surface: a local HTTP
server answers /assistants, /threads, run polling, and submit_tool_outputs, so
the mock Foundry project drives the agent as a real subprocess. The mock never
sees a credential beyond the literal test key, and nothing the agent prints
contains one.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from agents import foundry_agent

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_TASK = REPO_ROOT / "tests" / "data" / "monitoring" / "run-test-001"
AGENT = REPO_ROOT / "agents" / "foundry_agent.py"

ANSWER = {
    "root_cause": "expired secret referenced by the probe",
    "evidence": ["kubectl-describe-pod.txt: probe failing with 401"],
    "fix": "rotate the secret and redeploy",
}
TEST_KEY = "test-key"


class FoundryMock(BaseHTTPRequestHandler):
    """Route-matched mock of the Foundry Agents API.

    `routes` maps (method, path regex) to a callable(request body) ->
    (status, payload dict). `state` is a mutable dict the route callables read
    and write, so tests can script poll sequences and capture tool outputs.
    """

    routes: dict = {}
    requests: list[dict] = []

    def _handle(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(raw) if raw else None
        path = urlparse(self.path).path
        FoundryMock.requests.append(
            {"method": self.command, "path": path, "body": body, "headers": dict(self.headers)}
        )
        for (method, pattern), handler in self.routes.items():
            if method == self.command and re.fullmatch(pattern, path):
                status, payload = handler(body)
                self._respond(status, payload)
                return
        self._respond(404, {"error": f"no route: {self.command} {path}"})

    do_GET = do_POST = do_DELETE = _handle

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence request logging
        pass


def _start_server(routes: dict, state: dict) -> HTTPServer:
    FoundryMock.routes = routes
    FoundryMock.requests = []
    server = HTTPServer(("127.0.0.1", 0), FoundryMock)
    server.state = state  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _run_agent(server: HTTPServer, task_dir: Path) -> subprocess.CompletedProcess:
    env = dict(
        os.environ,
        FOUNDRY_ENDPOINT=f"http://127.0.0.1:{server.server_port}",
        FOUNDRY_MODEL="test-deployment",
        FOUNDRY_API_KEY=TEST_KEY,
        FOUNDRY_POLL_INTERVAL_SECONDS="0.01",
        TASK_DIR=str(task_dir),
    )
    env.pop("LLM_API_KEY", None)
    return subprocess.run(
        [sys.executable, str(AGENT)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def _standard_routes(state: dict) -> dict:
    """Routes for the happy path: one tool round, then a completed run."""

    def assistant(body):
        state["create_agent_body"] = body
        return 200, {"id": "asst_1"}

    def thread(body):
        return 200, {"id": "thread_1"}

    def message(body):
        state["symptom_message"] = body
        return 200, {"id": "msg_1"}

    def create_run(body):
        state["create_run_body"] = body
        return 200, {"id": "run_1", "thread_id": "thread_1", "status": "queued"}

    def get_run(body):
        statuses = state["get_statuses"]
        if state["poll_index"] < len(statuses):
            status = statuses[state["poll_index"]]
            state["poll_index"] += 1
        else:
            status = state["final_status"]
        run = {"id": "run_1", "thread_id": "thread_1", "status": status}
        if status == "requires_action":
            run["required_action"] = {
                "type": "submit_tool_outputs",
                "submit_tool_outputs": {"tool_calls": [state["tool_call"]]},
            }
        return 200, run

    def submit(body):
        state["tool_outputs"].append(body["tool_outputs"])
        return 200, {"id": "run_1", "thread_id": "thread_1", "status": "in_progress"}

    def messages(body):
        return 200, {
            "data": [
                {"role": "user", "content": [{"type": "text", "text": {"value": "symptom"}}]},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": {"value": state["assistant_text"]}}],
                },
            ]
        }

    def delete(body):
        state["deleted"] = True
        return 200, {"deleted": True}

    return {
        ("POST", "/assistants"): assistant,
        ("POST", "/threads"): thread,
        ("POST", "/threads/thread_1/messages"): message,
        ("POST", "/threads/thread_1/runs"): create_run,
        ("GET", "/threads/thread_1/runs/run_1"): get_run,
        ("POST", "/threads/thread_1/runs/run_1/submit_tool_outputs"): submit,
        ("GET", "/threads/thread_1/messages"): messages,
        ("DELETE", "/assistants/asst_1"): delete,
    }


def _happy_state(assistant_text: str, get_statuses: list[str]) -> dict:
    default_tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "list_files", "arguments": "{}"},
    }
    return {
        "poll_index": 0,
        "get_statuses": get_statuses,
        "final_status": "completed",
        "tool_outputs": [],
        "assistant_text": assistant_text,
        "tool_call": default_tool_call,
        "deleted": False,
    }


def test_list_files_tool_round_then_final_answer():
    """One tool round via submit_tool_outputs, then the contract JSON on stdout."""
    state = _happy_state(json.dumps(ANSWER), ["requires_action"])
    routes = _standard_routes(state)
    server = _start_server(routes, state)
    try:
        proc = _run_agent(server, TEST_TASK)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ANSWER
    assert TEST_KEY not in proc.stdout and TEST_KEY not in proc.stderr

    # Request order: create agent → thread → symptom message → run → poll → submit → messages → delete.
    sent = FoundryMock.requests
    sequence = [(r["method"], r["path"]) for r in sent]
    assert sequence == [
        ("POST", "/assistants"),
        ("POST", "/threads"),
        ("POST", "/threads/thread_1/messages"),
        ("POST", "/threads/thread_1/runs"),
        ("GET", "/threads/thread_1/runs/run_1"),
        ("POST", "/threads/thread_1/runs/run_1/submit_tool_outputs"),
        ("GET", "/threads/thread_1/runs/run_1"),  # post-submit poll
        ("GET", "/threads/thread_1/messages"),
        ("DELETE", "/assistants/asst_1"),
    ]

    # Agent creation carries the deployment, SRE instructions, and both tools.
    create_body = state["create_agent_body"]
    assert create_body["model"] == "test-deployment"
    assert [t["function"]["name"] for t in create_body["tools"]] == ["list_files", "read_file"]
    assert "site reliability engineer" in create_body["instructions"]

    # The run references the agent; the symptom message went to the thread.
    assert state["create_run_body"] == {"assistant_id": "asst_1"}
    assert "## Symptom" in state["symptom_message"]["content"]
    assert state["symptom_message"]["role"] == "user"

    # The tool round submitted a list_files result for the model's call id.
    (tool_outputs,) = state["tool_outputs"]
    assert tool_outputs[0]["tool_call_id"] == "call_1"
    listed = json.loads(tool_outputs[0]["output"])["files"]
    assert listed == [
        "fixtures/app-insights-query.kql",
        "fixtures/kubectl-get-pods.txt",
    ]
    assert "expected.json" not in listed and "solution.json" not in listed
    assert state["deleted"]  # the throwaway agent is cleaned up


def test_read_file_tool_round_returns_fixture_content(tmp_path):
    """A read_file round-trip delivers the fixture text as the tool output."""
    (tmp_path / "symptom.md").write_text("probes failing", encoding="utf-8")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "kubectl-get-pods.txt").write_text(
        "pod probe-api-0 Running", encoding="utf-8"
    )

    state = _happy_state(json.dumps(ANSWER), ["requires_action"])
    state["tool_call"] = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": json.dumps({"path": "fixtures/kubectl-get-pods.txt"}),
        },
    }
    routes = _standard_routes(state)
    server = _start_server(routes, state)
    try:
        proc = _run_agent(server, tmp_path)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ANSWER
    (tool_outputs,) = state["tool_outputs"]
    assert "pod probe-api-0 Running" in tool_outputs[0]["output"]


def test_task_directory_escape_is_refused(tmp_path):
    """The mock model tries ../expected.json; the tool refuses and leaks nothing."""
    (tmp_path / "symptom.md").write_text("symptom", encoding="utf-8")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "a.txt").write_text("evidence", encoding="utf-8")
    (tmp_path / "expected.json").write_text('{"secret_rubric": "hidden"}', encoding="utf-8")

    state = _happy_state(json.dumps(ANSWER), ["requires_action"])
    state["tool_call"] = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "read_file", "arguments": json.dumps({"path": "../expected.json"})},
    }
    routes = _standard_routes(state)
    server = _start_server(routes, state)
    try:
        proc = _run_agent(server, tmp_path)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ANSWER
    assert "secret_rubric" not in json.dumps(state["tool_outputs"])
    assert "outside the task directory" in state["tool_outputs"][0][0]["output"]


def test_missing_env_prints_empty_object_and_exits_zero():
    """No Foundry config at all: {} on stdout, exit 0 — a graded FAIL, not a crash."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("FOUNDRY_")}
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


def test_entra_id_fallback_fails_closed_without_azure_identity():
    """No API key and no azure-identity package: graceful {} — the credential gap."""
    env = dict(
        os.environ,
        FOUNDRY_ENDPOINT="http://127.0.0.1:1",
        FOUNDRY_MODEL="test-deployment",
        TASK_DIR=str(TEST_TASK),
    )
    env.pop("FOUNDRY_API_KEY", None)
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


def test_provider_500_prints_empty_object_and_exits_zero():
    """A 500 from the Foundry project is absorbed: {} on stdout, exit 0."""

    def failing(body):
        return 500, {"error": {"code": "500", "message": "server error"}}

    server = _start_server({("POST", "/assistants"): failing}, {})
    try:
        proc = _run_agent(server, TEST_TASK)
    finally:
        server.shutdown()

    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}
    assert TEST_KEY not in proc.stdout and TEST_KEY not in proc.stderr


def test_failed_run_prints_empty_object():
    """A run that reaches `failed` degrades to {} — no answer, no crash."""
    state = _happy_state("never reached", [])
    state["final_status"] = "failed"
    routes = _standard_routes(state)
    server = _start_server(routes, state)
    try:
        proc = _run_agent(server, TEST_TASK)
    finally:
        server.shutdown()

    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}


def test_tool_loop_budget_is_capped():
    """A model that demands tool outputs forever stops at MAX_STEPS rounds."""
    state = _happy_state("never reached", [])
    state["final_status"] = "requires_action"
    routes = _standard_routes(state)
    server = _start_server(routes, state)
    try:
        proc = _run_agent(server, TEST_TASK)
    finally:
        server.shutdown()

    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}
    submits = [r for r in FoundryMock.requests if "submit_tool_outputs" in r["path"]]
    assert len(submits) == foundry_agent.MAX_STEPS


def test_fenced_answer_is_extracted():
    """The assistant may wrap its JSON in markdown fences."""
    state = _happy_state(f"```json\n{json.dumps(ANSWER)}\n```", [])
    routes = _standard_routes(state)
    server = _start_server(routes, state)
    try:
        proc = _run_agent(server, TEST_TASK)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ANSWER


def test_api_key_header_selection(monkeypatch):
    """API key -> `api-key` header; without one -> Entra ID bearer token."""
    monkeypatch.setenv("FOUNDRY_API_KEY", TEST_KEY)
    headers = foundry_agent.auth_headers()
    assert headers["api-key"] == TEST_KEY
    assert "Authorization" not in headers

    fake_identity = types.ModuleType("azure.identity")

    class FakeCredential:
        def get_token(self, scope):
            assert scope == "https://ai.azure.com/.default"
            return types.SimpleNamespace(token="entra-token")

    fake_identity.DefaultAzureCredential = FakeCredential
    monkeypatch.setitem(sys.modules, "azure.identity", fake_identity)
    monkeypatch.delenv("FOUNDRY_API_KEY")
    headers = foundry_agent.auth_headers()
    assert headers["Authorization"] == "Bearer entra-token"
    assert "api-key" not in headers


def test_latest_assistant_text_skips_user_messages():
    """Only the newest assistant message's text segments are concatenated."""
    payload = {
        "data": [
            {"role": "user", "content": [{"type": "text", "text": {"value": "symptom"}}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": {"value": "first "}},
                    {"type": "text", "text": {"value": "answer"}},
                ],
            },
        ]
    }
    monkeypatched = {"calls": []}

    def fake_call(method, path, body=None):
        monkeypatched["calls"].append(path)
        return payload

    original = foundry_agent.call_api
    foundry_agent.call_api = fake_call
    try:
        assert foundry_agent.latest_assistant_text("thread_1") == "first answer"
    finally:
        foundry_agent.call_api = original

    assert monkeypatched["calls"] == ["/threads/thread_1/messages"]


@pytest.mark.parametrize(
    ("run_statuses", "final_status"),
    [
        (["requires_action"], "completed"),
        (["requires_action", "requires_action"], "completed"),
    ],
)
def test_polling_rounds_before_completion(run_statuses, final_status):
    """Multi-round tool use converges: each round submits, then completes."""
    state = _happy_state(json.dumps(ANSWER), run_statuses)
    state["final_status"] = final_status
    routes = _standard_routes(state)
    server = _start_server(routes, state)
    try:
        proc = _run_agent(server, TEST_TASK)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ANSWER
    assert len(state["tool_outputs"]) == len(run_statuses)
