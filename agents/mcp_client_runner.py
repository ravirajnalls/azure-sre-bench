#!/usr/bin/env python3
"""MCP client runner: drive any MCP-capable agent through the benchmark (issue #123).

`bench run` executes an agent command; this driver turns an MCP-capable agent
(Claude Code, Goose, Copilot agents, ...) into that command with no custom
benchmark code:

    bench run --agent "python agents/mcp_client_runner.py \
        --agent-command 'claude --print --mcp-config mcp.json'" \
        --task aks-identity-008

The driver:
1. spawns the MCP stdio server (`bench mcp-serve --task <id>`) and probes it —
   initialize, resources/list, tools/list — failing fast with a clear stderr
   message if the server is broken, instead of leaving the agent to flounder;
2. spawns the agent command with two env vars:
   `BENCH_MCP_SERVER_COMMAND` — the server command line the agent should spawn
   with its own MCP client (the standard wiring for MCP-capable agents) — and
   `BENCH_MCP_ANSWER_FILE` — where the server's `submit_answer` tool persists
   the contract JSON;
3. waits for the agent to exit, prints the submitted answer JSON on stdout
   (the runner parses and grades it), and forwards everything else to stderr.

If the agent never calls `submit_answer`, the driver prints nothing on stdout,
so the runner grades an empty answer — a FAIL result, never a crash.

The bundled `McpStdioClient` is a minimal MCP client (newline-delimited
JSON-RPC 2.0 over stdio, stdlib only). It exists so the driver's probe and the
test suite can speak the protocol; real MCP-capable agents bring their own
client and only need the two env vars above.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import __version__  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

SERVER_ENV_VAR = "BENCH_MCP_SERVER_COMMAND"
ANSWER_ENV_VAR = "BENCH_MCP_ANSWER_FILE"

MCP_PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "azure-sre-bench-mcp-client-runner"
PROBE_SHUTDOWN_TIMEOUT_S = 10


class McpServerError(RuntimeError):
    """The MCP server broke protocol or closed its stream."""


class McpStdioClient:
    """Minimal MCP client over a spawned stdio server.

    Speaks newline-delimited JSON-RPC 2.0 — the MCP stdio transport. Blocking
    reads by design: the benchmark harness applies the overall timeout.
    """

    def __init__(self, command: list[str], env: dict | None = None, cwd=None):
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
            cwd=cwd,
        )
        self._next_id = 0

    # -- protocol plumbing ---------------------------------------------------

    def _send(self, payload: dict) -> None:
        if self._proc.stdin is None or self._proc.stdin.closed:
            raise McpServerError("MCP server stdin is closed")
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def _receive(self) -> dict:
        if self._proc.stdout is None:
            raise McpServerError("MCP server stdout is closed")
        line = self._proc.stdout.readline()
        if not line:
            raise McpServerError("MCP server closed the protocol stream")
        try:
            return json.loads(line)
        except ValueError as exc:
            raise McpServerError(
                f"MCP server sent a non-JSON line: {line[:200]!r}"
            ) from exc

    def request(self, method: str, params: dict | None = None) -> dict:
        """Send a request and wait for the matching response."""
        self._next_id += 1
        msg_id = self._next_id
        self._send(
            {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}
        )
        while True:
            message = self._receive()
            if message.get("id") == msg_id and ("result" in message or "error" in message):
                if "error" in message:
                    error = message["error"]
                    raise McpServerError(
                        f"{method} failed: {error.get('message')} "
                        f"(code {error.get('code')})"
                    )
                result = message["result"]
                return result if isinstance(result, dict) else {}
            # Anything else is a server-initiated notification — ignore.

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # -- MCP surface -----------------------------------------------------------

    def initialize(self) -> dict:
        result = self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": __version__},
            },
        )
        self.notify("notifications/initialized")
        return result

    def list_resources(self) -> list[dict]:
        result = self.request("resources/list")
        resources = result.get("resources", [])
        return resources if isinstance(resources, list) else []

    def read_resource(self, uri: str) -> str:
        result = self.request("resources/read", {"uri": uri})
        contents = result.get("contents", [])
        if not contents:
            return ""
        return str(contents[0].get("text", ""))

    def list_tools(self) -> list[dict]:
        result = self.request("tools/list")
        tools = result.get("tools", [])
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict | None = None) -> tuple[str, bool]:
        """Call a tool; returns (joined text content, is_error)."""
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        text = "\n".join(
            part.get("text", "")
            for part in result.get("content", [])
            if isinstance(part, dict) and part.get("type") == "text"
        )
        return text, bool(result.get("isError"))

    # -- lifecycle ---------------------------------------------------------------

    @property
    def returncode(self) -> int | None:
        """The server process's exit code (None while still running)."""
        return self._proc.returncode

    def close(self, timeout: float = PROBE_SHUTDOWN_TIMEOUT_S) -> int | None:
        """Close stdin (the stdio shutdown signal), then escalate to kill."""
        try:
            if self._proc.stdin and not self._proc.stdin.closed:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=timeout)
        return self._proc.returncode

    def __enter__(self) -> "McpStdioClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def build_server_command(task: str, tasks_root: Path, answer_file: Path) -> str:
    """The default MCP server command line handed to the agent.

    Runs the server as a script (not `python -m bench`) so it works from any
    working directory the agent spawns it in — the harness sets the agent's
    cwd to the per-task workspace, where `bench` is not importable.
    """
    return shlex.join(
        [
            sys.executable,
            str(REPO_ROOT / "bench" / "mcp_server.py"),
            "mcp-serve",
            "--task",
            task,
            "--tasks-root",
            str(tasks_root),
            "--answer-file",
            str(answer_file),
        ]
    )


def _probe(server_command: str, cwd) -> None:
    """Handshake with the server once so a broken setup fails loudly here."""
    with McpStdioClient(shlex.split(server_command), cwd=cwd) as client:
        info = client.initialize()
        resources = client.list_resources()
        tools = client.list_tools()
        print(
            f"[mcp_client_runner] probe ok: server="
            f"{info.get('serverInfo', {}).get('name')} "
            f"protocol={info.get('protocolVersion')} "
            f"resources={[r.get('uri') for r in resources]} "
            f"tools={[t.get('name') for t in tools]}",
            file=sys.stderr,
            flush=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp_client_runner",
        description="Drive any MCP-capable agent through `bench run` (issue #123).",
    )
    parser.add_argument(
        "--agent-command",
        required=True,
        help="command line of the MCP-capable agent to benchmark",
    )
    parser.add_argument(
        "--server-command",
        default=None,
        help="MCP server command line (default: <repo>/bench/mcp_server.py mcp-serve ...)",
    )
    parser.add_argument(
        "--task",
        default=os.environ.get("TASK_ID"),
        help="task ID to serve (default: $TASK_ID from the bench runner)",
    )
    parser.add_argument(
        "--tasks-root",
        default=None,
        help="task discovery root for the default server command "
        "(default: <repo>/tasks)",
    )
    parser.add_argument(
        "--answer-file",
        default=None,
        help="answer file path (default: a private temp file, deleted after use)",
    )
    args = parser.parse_args(argv)
    if not args.task:
        parser.error("--task is required (or run under `bench run`, which sets $TASK_ID)")
        return 2

    tasks_root = (
        Path(args.tasks_root) if args.tasks_root else REPO_ROOT / "tasks"
    )
    if args.answer_file:
        answer_file = Path(args.answer_file)
        owned_answer = False
    else:
        handle, name = tempfile.mkstemp(prefix="bench-mcp-answer-", suffix=".json")
        os.close(handle)
        answer_file = Path(name)
        owned_answer = True

    server_command = args.server_command or build_server_command(
        args.task, tasks_root, answer_file
    )

    try:
        _probe(server_command, cwd=REPO_ROOT)
    except Exception as exc:
        print(f"error: MCP server probe failed: {exc}", file=sys.stderr)
        if owned_answer:
            answer_file.unlink(missing_ok=True)
        return 2

    env = dict(
        os.environ,
        **{SERVER_ENV_VAR: server_command, ANSWER_ENV_VAR: str(answer_file)},
    )
    print(
        f"[mcp_client_runner] running agent: {args.agent_command}",
        file=sys.stderr,
        flush=True,
    )
    try:
        proc = subprocess.run(
            shlex.split(args.agent_command),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        print(f"error: agent command failed to spawn: {exc}", file=sys.stderr)
        if owned_answer:
            answer_file.unlink(missing_ok=True)
        return 2
    if proc.stdout:
        print(proc.stdout, file=sys.stderr, end="")

    answer: dict = {}
    try:
        data = json.loads(answer_file.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            answer = data
    except (OSError, ValueError) as exc:
        print(f"error: answer file unreadable: {exc}", file=sys.stderr)
    if owned_answer:
        answer_file.unlink(missing_ok=True)

    if answer:
        print(json.dumps(answer))
        return 0
    print(
        "error: agent finished without calling submit_answer",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
