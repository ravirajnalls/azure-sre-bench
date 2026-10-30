"""End-to-end tests for the MCP client runner (issue #123).

The mandated mocked benchmark run: a throwaway "MCP agent" script (a real MCP
client speaking to `bench mcp-serve`) is driven through the REAL runner via
`agents/mcp_client_runner.py`, exercising the full chain — harness -> driver ->
agent -> MCP server -> answer file -> stdout -> grading — with no network and
no credentials.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

from bench import run as runner
from bench.cli import main as cli_main

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER = REPO_ROOT / "agents" / "mcp_client_runner.py"
TEST_TASK = REPO_ROOT / "tests" / "data" / "monitoring" / "monitoring-902"
TESTS_DATA_ROOT = REPO_ROOT / "tests" / "data"

# An "MCP-capable agent" for tests: connects to the server command the driver
# publishes in the environment, investigates, probes a mutating command, and
# submits the reference answer via the MCP tools.
FAKE_MCP_AGENT = """
import os, shlex, sys

sys.path.insert(0, {repo_root!r})
from agents.mcp_client_runner import McpStdioClient

with McpStdioClient(shlex.split(os.environ["BENCH_MCP_SERVER_COMMAND"])) as client:
    client.initialize()
    text, err = client.call_tool(
        "run_command", {{"command": "kubectl -n checkout get pods -o json"}}
    )
    assert not err, f"canned command unexpectedly failed: {{text}}"
    blocked, err = client.call_tool(
        "run_command", {{"command": "kubectl delete pod billing-api-0 -n checkout"}}
    )
    assert err, f"mutating command was not blocked: {{blocked}}"

    client.call_tool("submit_answer", {{
        "root_cause": "Redis connection pool exhausted in billing-api "
        "(mutation probe said: {{}})".format(blocked[:40]),
        "evidence": [
            "kubectl-get-pods.txt: CrashLoopBackOff",
            "server command: {{}}".format(
                "mcp-serve" in os.environ["BENCH_MCP_SERVER_COMMAND"]
            ),
        ],
        "fix": "raise the connection limit on the redis pool",
    }})
"""


def fake_agent_command(tmp_path: Path) -> str:
    script = tmp_path / "fake_mcp_agent.py"
    script.write_text(FAKE_MCP_AGENT.format(repo_root=str(REPO_ROOT)), encoding="utf-8")
    return f"{sys.executable} {script}"


def driver_command(agent_command: str) -> str:
    """The agent command `bench run` executes: the driver wrapping the agent."""
    return (
        f"{sys.executable} {DRIVER} "
        f"--agent-command {shlex.quote(agent_command)} "
        f"--tasks-root {shlex.quote(str(TESTS_DATA_ROOT))}"
    )


def test_end_to_end_mocked_benchmark_run(tmp_path):
    """The full chain: harness -> driver -> MCP agent -> server -> grading."""
    record = runner.run_task(
        TEST_TASK, driver_command(fake_agent_command(tmp_path)), results_root=tmp_path
    )

    assert record["passed"] is True, record["stderr_tail"]
    assert record["score"] == 1.0
    answer = record["answer"]
    assert "Redis connection pool" in answer["root_cause"]
    assert "blocked" in answer["root_cause"]  # the mutating probe really errored
    assert answer["evidence"][1] == "server command: True"  # env wiring reached the agent
    assert record["timed_out"] is False


def test_agent_without_submission_grades_as_fail(tmp_path):
    agent = f"{sys.executable} -c 'print(\"investigated, never submitted\")'"
    record = runner.run_task(TEST_TASK, driver_command(agent), results_root=tmp_path)

    assert record["passed"] is False
    assert record["answer"] == {}
    assert "submit_answer" in record["stderr_tail"]


def test_broken_server_command_fails_loudly(tmp_path):
    """A dead MCP server is a clear driver error, not a silent empty result."""
    agent = fake_agent_command(tmp_path)
    command = (
        f"{sys.executable} {DRIVER} "
        f"--agent-command {shlex.quote(agent)} "
        f"--server-command false"
    )
    record = runner.run_task(TEST_TASK, command, results_root=tmp_path)

    assert record["returncode"] == 2
    assert "probe failed" in record["stderr_tail"]
    assert record["passed"] is False


def test_driver_is_wired_through_the_bench_cli(tmp_path, monkeypatch, capsys):
    """`bench run --agent "<driver> ..."` grades the MCP agent like any other."""
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    exit_code = cli_main(
        [
            "run",
            "--agent",
            driver_command(fake_agent_command(tmp_path)),
            "--task",
            "monitoring-902",
            "--name",
            "cli-mcp",
            "--tasks-root",
            "tests/data",
        ]
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert out.startswith("PASS monitoring-902 score=1.0 ")
    assert out.endswith("cli-mcp: 1/1 passed\n")
    stored = json.loads((tmp_path / "results" / "cli-mcp" / "monitoring-902.json").read_text())
    assert stored["passed"] is True
