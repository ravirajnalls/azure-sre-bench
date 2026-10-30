"""MCP server tests for `bench mcp-serve` (issue #123).

In-process protocol tests drive `handle_message`/`serve` directly; the
round-trip test at the bottom speaks the real protocol over a real stdio
subprocess through `McpStdioClient` (the client the benchmark driver ships).
Uses the synthetic `tests/data/monitoring/run-test-002` task, whose fixtures/
carries a `_commands.yaml` canned-command table.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from agents.mcp_client_runner import McpServerError, McpStdioClient
from bench.mcp_server import (
    COMMAND_TABLE_NAME,
    handle_message,
    is_mutating_command,
    load_command_table,
    normalize_command,
    serve,
    TaskSession,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_TASK = REPO_ROOT / "tests" / "data" / "monitoring" / "monitoring-902"
TESTS_DATA_ROOT = REPO_ROOT / "tests" / "data"
SOLUTION = json.loads((TEST_TASK / "solution.json").read_text())


def make_session(tmp_path: Path, task_dir: Path = TEST_TASK) -> TaskSession:
    return TaskSession(task_dir, tmp_path / "answer.json")


def rpc(session: TaskSession, method: str, params: dict | None = None, msg_id=1):
    message = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return handle_message(session, message)


def tool(session: TaskSession, name: str, arguments: dict | None = None):
    """Call a tool in-process; returns (text, is_error)."""
    response = rpc(session, "tools/call", {"name": name, "arguments": arguments or {}})
    result = response["result"]
    text = "\n".join(
        part["text"] for part in result["content"] if part.get("type") == "text"
    )
    return text, result["isError"]


# --- initialize / ping ------------------------------------------------------


def test_initialize_echoes_a_supported_protocol_version(tmp_path):
    session = make_session(tmp_path)
    response = rpc(session, "initialize", {"protocolVersion": "2024-11-05"})
    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert response["result"]["serverInfo"]["name"] == "azure-sre-bench"
    assert "tools" in response["result"]["capabilities"]
    assert "resources" in response["result"]["capabilities"]


def test_initialize_falls_back_to_latest_for_unknown_version(tmp_path):
    session = make_session(tmp_path)
    response = rpc(session, "initialize", {"protocolVersion": "1999-01-01"})
    assert response["result"]["protocolVersion"] == "2025-06-18"


def test_ping_returns_empty_result(tmp_path):
    response = rpc(make_session(tmp_path), "ping")
    assert response["result"] == {}


def test_unknown_method_returns_method_not_found(tmp_path):
    response = rpc(make_session(tmp_path), "sampling/createMessage")
    assert response["error"]["code"] == -32601


def test_notifications_get_no_response(tmp_path):
    session = make_session(tmp_path)
    assert handle_message(session, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


# --- symptom resource ---------------------------------------------------------


def test_resources_list_exposes_the_symptom(tmp_path):
    response = rpc(make_session(tmp_path), "resources/list")
    resources = response["result"]["resources"]
    assert [r["name"] for r in resources] == ["symptom"]
    assert resources[0]["uri"] == "task://monitoring-902/symptom"


def test_resources_read_returns_the_symptom_text(tmp_path):
    response = rpc(
        make_session(tmp_path), "resources/read", {"uri": "task://monitoring-902/symptom"}
    )
    contents = response["result"]["contents"]
    assert contents[0]["mimeType"] == "text/markdown"
    assert "checkout API" in contents[0]["text"]


def test_resources_read_rejects_unknown_uri(tmp_path):
    response = rpc(make_session(tmp_path), "resources/read", {"uri": "task://nope"})
    assert response["error"]["code"] == -32602


# --- tools/list ----------------------------------------------------------------


def test_tools_list_exposes_the_four_issue_tools(tmp_path):
    response = rpc(make_session(tmp_path), "tools/list")
    assert {t["name"] for t in response["result"]["tools"]} == {
        "list_files",
        "read_file",
        "run_command",
        "submit_answer",
    }
    for tool_def in response["result"]["tools"]:
        assert "inputSchema" in tool_def


# --- list_files / read_file: the information boundary ---------------------------


def test_list_files_hides_the_command_table(tmp_path):
    text, is_error = tool(make_session(tmp_path), "list_files")
    assert not is_error
    names = text.splitlines()
    assert "kubectl-get-pods.txt" in names
    assert COMMAND_TABLE_NAME not in names
    assert "expected.json" not in names
    assert "solution.json" not in names


def test_read_file_returns_fixture_text(tmp_path):
    text, is_error = tool(make_session(tmp_path), "read_file", {"path": "kubectl-get-pods.txt"})
    assert not is_error
    assert "CrashLoopBackOff" in text


def test_read_file_rejects_traversal_and_hidden_files(tmp_path):
    session = make_session(tmp_path)
    for probe in ("../expected.json", "../solution.json", "/etc/passwd", COMMAND_TABLE_NAME):
        _, is_error = tool(session, "read_file", {"path": probe})
        assert is_error, probe


# --- run_command: canned lookups, normalization, mutation blocking ---------------


def test_run_command_returns_canned_output(tmp_path):
    text, is_error = tool(
        make_session(tmp_path), "run_command", {"command": "kubectl get pods -n checkout"}
    )
    assert not is_error
    assert "CrashLoopBackOff" in text


def test_run_command_normalizes_flag_order_and_output_variants(tmp_path):
    session = make_session(tmp_path)
    canonical = normalize_command("kubectl get pods -n checkout")
    assert normalize_command("kubectl -n checkout get pods") == canonical
    assert normalize_command("kubectl get pods -n checkout -o json") == canonical
    assert normalize_command("kubectl get pods -n checkout --output=json") == canonical
    assert normalize_command("kubectl  get   pods -n checkout -o wide") == canonical

    for variant in (
        "kubectl -n checkout get pods",
        "kubectl get pods -n checkout -o json",
        "kubectl get pods -n checkout -o wide",
    ):
        text, is_error = tool(session, "run_command", {"command": variant})
        assert not is_error, variant
        assert "CrashLoopBackOff" in text, variant


def test_run_command_unknown_returns_not_found_not_error(tmp_path):
    text, is_error = tool(
        make_session(tmp_path), "run_command", {"command": "kubectl get configmaps -n checkout"}
    )
    assert not is_error
    assert "not simulated" in text


def test_run_command_blocks_mutating_kubectl(tmp_path):
    session = make_session(tmp_path)
    for command in (
        "kubectl delete pod billing-api-7d9f8b6c5-xk2pq -n checkout",
        "kubectl apply -f deploy.yaml",
        "kubectl patch deployment billing-api -n checkout",
        "kubectl scale deployment billing-api --replicas=2 -n checkout",
        "kubectl set env deployment/billing-api FOO=bar",
        "kubectl rollout undo deployment/billing-api",
        "kubectl exec -it billing-api-7d9f8b6c5-xk2pq -- sh",
    ):
        text, is_error = tool(session, "run_command", {"command": command})
        assert is_error, command
        assert "blocked" in text, command


def test_run_command_blocks_mutating_az(tmp_path):
    session = make_session(tmp_path)
    for command in (
        "az group create --name rg-test --location eastus",
        "az vm delete -g rg-test -n vm1",
        "az keyvault secret set --vault-name kv --name s --value x",
        "az aks update -g rg-test -n cluster1",
    ):
        text, is_error = tool(session, "run_command", {"command": command})
        assert is_error, command
        assert "blocked" in text, command


def test_is_mutating_read_only_negatives():
    for command in (
        "kubectl get pods -n checkout",
        "kubectl describe pod billing-api-7d9f8b6c5-xk2pq -n checkout",
        "kubectl logs deploy/billing-api -n checkout",
        "kubectl top pods -n checkout",
        "kubectl rollout status deployment/billing-api",
        "az network nsg list -g rg-test",
        "az monitor metrics list --resource /subscriptions/x",
        "az aks show -g rg-test -n cluster1",
    ):
        assert not is_mutating_command(command), command


def test_run_command_without_a_table_directs_to_files(tmp_path):
    # run-test-001 has no _commands.yaml: run_command must say so instead of
    # pretending the command ran.
    session = make_session(
        tmp_path, task_dir=REPO_ROOT / "tests" / "data" / "monitoring" / "run-test-001"
    )
    text, is_error = tool(session, "run_command", {"command": "kubectl get pods"})
    assert is_error
    assert "no simulated command table" in text


def test_command_table_rejects_missing_fixture(tmp_path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / COMMAND_TABLE_NAME).write_text(
        "commands:\n  'kubectl get pods': missing.txt\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="missing fixture"):
        load_command_table(fixtures)


# --- submit_answer ---------------------------------------------------------------


def test_submit_answer_writes_the_contract_json(tmp_path):
    session = make_session(tmp_path)
    text, is_error = tool(session, "submit_answer", SOLUTION)
    assert not is_error
    written = json.loads((tmp_path / "answer.json").read_text())
    assert written == SOLUTION  # round-trips exactly, extra keys included


def test_submit_answer_rejects_an_incomplete_answer(tmp_path):
    session = make_session(tmp_path)
    for bad in (
        {"evidence": []},
        {"root_cause": "x", "fix": ""},
        {"root_cause": "", "fix": "y"},
        {"root_cause": "x", "fix": "y", "evidence": "not-a-list"},
        "not even a dict",
    ):
        text, is_error = tool(session, "submit_answer", bad)
        assert is_error, bad
    assert not (tmp_path / "answer.json").exists()


# --- the serve loop ----------------------------------------------------------------


def test_serve_parses_lines_and_answers_until_eof(tmp_path):
    session = make_session(tmp_path)
    requests = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            "this is not json",  # answered with a parse error, loop continues
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ]
    )
    out = io.StringIO()
    assert serve(session, stdin=io.StringIO(requests + "\n"), stdout=out) == 0
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert responses[0]["id"] == 1 and responses[0]["result"] == {}
    assert responses[1]["error"]["code"] == -32700  # parse error, no id
    assert responses[2]["id"] == 2
    assert len(responses[2]["result"]["tools"]) == 4


# --- the mandated protocol round-trip over a real stdio subprocess -----------------


def test_stdio_round_trip_against_a_real_subprocess(tmp_path):
    answer_file = tmp_path / "answer.json"
    command = [
        sys.executable,
        "-m",
        "bench",
        "mcp-serve",
        "--task",
        "monitoring-902",
        "--tasks-root",
        str(TESTS_DATA_ROOT),
        "--answer-file",
        str(answer_file),
    ]
    with McpStdioClient(command, cwd=str(REPO_ROOT)) as client:
        info = client.initialize()
        assert info["protocolVersion"] == "2025-06-18"
        assert info["serverInfo"]["name"] == "azure-sre-bench"

        resources = client.list_resources()
        symptom = client.read_resource(resources[0]["uri"])
        assert "checkout API" in symptom

        tools = client.list_tools()
        assert {t["name"] for t in tools} == {
            "list_files",
            "read_file",
            "run_command",
            "submit_answer",
        }

        text, is_error = client.call_tool(
            "run_command", {"command": "kubectl -n checkout get pods -o json"}
        )
        assert not is_error
        assert "CrashLoopBackOff" in text

        text, is_error = client.call_tool(
            "run_command", {"command": "kubectl delete pod billing-api-7d9f8b6c5-xk2pq"}
        )
        assert is_error and "blocked" in text

        _, is_error = client.call_tool("submit_answer", SOLUTION)
        assert not is_error

    assert client.returncode == 0  # clean exit on stdin EOF
    assert json.loads(answer_file.read_text()) == SOLUTION


def test_client_raises_on_protocol_error(tmp_path):
    command = [
        sys.executable,
        "-m",
        "bench",
        "mcp-serve",
        "--task",
        "monitoring-902",
        "--tasks-root",
        str(TESTS_DATA_ROOT),
        "--answer-file",
        str(tmp_path / "answer.json"),
    ]
    with McpStdioClient(command, cwd=str(REPO_ROOT)) as client:
        client.initialize()
        with pytest.raises(McpServerError, match="unknown resource"):
            client.read_resource("task://nope")
