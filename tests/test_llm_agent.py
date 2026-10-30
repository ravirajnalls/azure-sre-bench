"""Tests for the LLM baseline agent (issue #38).

A local HTTP server stands in for the OpenAI-compatible endpoint, so the mocked
response is proven end to end: the agent runs as a real subprocess and the
contract JSON must reach its stdout.
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

from agents import llm_agent

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_TASK = REPO_ROOT / "tests" / "data" / "monitoring" / "run-test-001"
AGENT = REPO_ROOT / "agents" / "llm_agent.py"

ANSWER = {
    "root_cause": "expired secret referenced by the probe",
    "evidence": ["kubectl-describe-pod.txt: probe failing with 401"],
    "fix": "rotate the secret and redeploy",
}


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    """Returns one canned chat-completion response."""

    content = json.dumps(ANSWER)  # class attribute; tests override per server

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self._respond()

    def _respond(self):
        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": self.content}}]
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence request logging
        pass


def _start_server(handler_class: type) -> HTTPServer:
    """Serve the handler on an ephemeral localhost port."""
    server = HTTPServer(("127.0.0.1", 0), handler_class)
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


def test_contract_json_reaches_stdout():
    """The mocked HTTP response's answer is printed as contract JSON."""
    server = _start_server(_FakeOpenAIHandler)
    try:
        proc = _run_agent(server, TEST_TASK)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ANSWER


def test_markdown_fences_are_stripped():
    """A model that wraps its JSON in fences still grades."""

    class FencedHandler(_FakeOpenAIHandler):
        content = f"```json\n{json.dumps(ANSWER)}\n```"

    server = _start_server(FencedHandler)
    try:
        proc = _run_agent(server, TEST_TASK)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ANSWER


def test_request_carries_model_and_messages(tmp_path):
    """The prompt includes the symptom and fixture content, with the model set."""
    (tmp_path / "symptom.md").write_text(
        "probes failing since 09:00", encoding="utf-8"
    )
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "kubectl-get-pods.txt").write_text(
        "pod probe-api-0 Running", encoding="utf-8"
    )

    seen: dict = {}

    class EchoingHandler(_FakeOpenAIHandler):
        def do_POST(self):
            seen["body"] = json.loads(
                self.rfile.read(int(self.headers["Content-Length"]))
            )
            seen["auth"] = self.headers.get("Authorization")
            self._respond()

    server = _start_server(EchoingHandler)
    try:
        proc = _run_agent(server, tmp_path)
    finally:
        server.shutdown()

    assert proc.returncode == 0, proc.stderr
    assert seen["body"]["model"] == "test-model"
    assert seen["auth"] == "Bearer test-key"
    roles = [m["role"] for m in seen["body"]["messages"]]
    assert roles == ["system", "user"]
    prompt = seen["body"]["messages"][1]["content"]
    assert "## Symptom" in prompt
    assert "probes failing since 09:00" in prompt
    assert "fixtures/kubectl-get-pods.txt" in prompt
    assert "pod probe-api-0 Running" in prompt


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

    class FailingHandler(_FakeOpenAIHandler):
        def do_POST(self):
            self.send_response(500)
            self.end_headers()

    server = _start_server(FailingHandler)
    try:
        proc = _run_agent(server, TEST_TASK)
    finally:
        server.shutdown()

    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}


def test_read_truncated_caps_file_at_20kb(tmp_path):
    big = tmp_path / "big.txt"
    big.write_bytes(b"a" * (llm_agent.FIXTURE_TRUNCATE_BYTES + 1000))
    assert len(llm_agent.read_truncated(big)) == llm_agent.FIXTURE_TRUNCATE_BYTES


def test_collect_evidence_covers_symptom_and_sorted_fixtures(tmp_path):
    (tmp_path / "symptom.md").write_text("symptom", encoding="utf-8")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "fixtures" / "a.txt").write_text("a", encoding="utf-8")

    names = [name for name, _ in llm_agent.collect_evidence(tmp_path)]

    assert names == ["symptom.md", "fixtures/a.txt", "fixtures/b.txt"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ("```json\n{\"a\": 1}\n```", '{"a": 1}'),
        ("```\n{\"a\": 1}\n```", '{"a": 1}'),
        ("no fence", "no fence"),
    ],
)
def test_strip_fences(raw, expected):
    assert llm_agent.strip_fences(raw) == expected


def test_extract_answer_rejects_non_dict():
    assert llm_agent.extract_answer("[]") == {}
    assert llm_agent.extract_answer("not json") == {}
