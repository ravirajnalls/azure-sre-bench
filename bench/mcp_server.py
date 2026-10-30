"""MCP stdio server exposing one benchmark task (issue #123).

`bench mcp-serve --task <id>` speaks the Model Context Protocol over stdio
(newline-delimited JSON-RPC 2.0, no third-party dependencies). It exposes:

- a `symptom` resource (`task://<task-id>/symptom`) — what on-call sees;
- `list_files` / `read_file` — read-only access to the task's `fixtures/`;
- `run_command` — canned kubectl/az commands from the task's
  `fixtures/_commands.yaml` (the simulated-CLI command table designed in
  issue #111; mutating verbs are blocked, unknown commands answer not-found);
- `submit_answer` — validates and writes the contract JSON
  (`root_cause`, `evidence`, `fix`) to the answer file, where the driver
  (`agents/mcp_client_runner.py`) picks it up for grading.

Information boundary: only files under `fixtures/` are listable and readable,
and `_commands.yaml` itself is hidden from both — the command table would
reveal the investigation path. Hidden rubric files (`expected.json`,
`solution.json`) live outside `fixtures/` and are never served. Nothing an
agent submits or runs is ever executed on the host: `run_command` only looks
up canned output.

Client-side counterpart: `agents/mcp_client_runner.McpStdioClient` implements
the same protocol and is what the round-trip tests (and the driver's probe)
speak.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

import yaml

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import __version__  # noqa: E402
from bench.run import load_task  # noqa: E402

MCP_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")

SERVER_NAME = "azure-sre-bench"
COMMAND_TABLE_NAME = "_commands.yaml"
# Same visibility cap the ReAct agent's read_file applies (agents/react_agent.py).
READ_FILE_MAX_BYTES = 20 * 1024

# JSON-RPC / MCP error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Mutating verbs that the simulated CLIs refuse (issue #111: "any mutating verb
# (apply, delete, patch, az ... create/update/delete)"). Kubectl also blocks
# session verbs (exec/attach/port-forward/...) — nothing an agent can do with
# them is read-only. Extend as tasks demand; unknown commands simply miss the
# canned table and answer not-found, so over-blocking costs an investigation
# path, not correctness.
KUBECTL_MUTATING_VERBS = frozenset(
    {
        "apply", "create", "delete", "edit", "patch", "replace", "scale",
        "set", "label", "annotate", "cordon", "uncordon", "drain", "taint",
        "exec", "attach", "port-forward", "proxy", "cp", "debug",
        "undo", "restart",
    }
)
AZ_MUTATING_VERBS = frozenset(
    {
        "create", "update", "delete", "set", "new", "purge",
        "assign", "remove", "import", "restore",
    }
)

# Flags whose next token is a value, for token normalization. `--flag=value`
# forms are handled structurally; unknown bare flags are treated as boolean.
FLAGS_TAKING_VALUES = frozenset(
    {
        "-n", "--namespace", "--context", "--kubeconfig", "-o", "--output",
        "-l", "--selector", "--field-selector", "-g", "--resource-group",
        "--subscription", "--vault-name", "--name", "--resource", "--cluster",
        "--token", "--as", "--user", "--image", "--timeout", "--revision",
        "--from", "--to", "--workspace", "--api-version", "--query", "-c",
    }
)
# The output-format flag is dropped entirely: `-o yaml|json|wide` variants must
# normalize to the same canned fixture (issue #111).
DROPPED_FLAGS = frozenset({"-o", "--output"})


class RpcError(Exception):
    """A JSON-RPC error to send back to the client."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _Tokens:
    """A command split into positionals and normalized flag tokens."""

    def __init__(self, positionals: list[str], flags: list[str]):
        self.positionals = positionals
        self.flags = flags

    def canonical(self, binary: str) -> str:
        parts = [binary, *self.positionals, *sorted(self.flags)]
        return " ".join(part for part in parts if part)


def _split_tokens(tokens: list[str]) -> _Tokens:
    """Value-aware token scan: `-n x`/`--namespace=x` never read as positionals."""
    positionals: list[str] = []
    flags: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token == "--":
            break  # everything after `--` is command args; irrelevant for lookups
        if token.startswith("-"):
            if token in DROPPED_FLAGS:
                skip_next = True
                continue
            if token in FLAGS_TAKING_VALUES:
                flags.append(f"{token} ")
                skip_next = True
                continue
            if "=" in token:
                name, _, value = token.partition("=")
                if name in DROPPED_FLAGS:
                    continue
                flags.append(f"{name}={value}")
                continue
            flags.append(token)
            continue
        positionals.append(token)
    return _Tokens(positionals, flags)


def _binary_of(tokens: list[str]) -> str:
    return Path(tokens[0]).name if tokens else ""


def normalize_command(command: str) -> str:
    """Canonical lookup key for a command line.

    Collapses whitespace, strips `-o/--output` variants, and reorders flags so
    `kubectl -n x get pods -o json` and `kubectl get pods -n x` produce the
    same key. Unquotable input degrades to whitespace-collapsing, which can
    only miss the table (never execute anything).
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return " ".join(command.split())
    if not tokens:
        return ""
    return _split_tokens(tokens[1:]).canonical(_binary_of(tokens))


def is_mutating_command(command: str) -> bool:
    """True when the command mutates the (simulated) cluster or resource.

    Kubectl: any positional in the mutating set (covers `kubectl rollout undo`
    via the subverb). Az: any positional after the noun group, matching the
    issue's `az ... create/update/delete` shape.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False
    binary = _binary_of(tokens)
    positionals = _split_tokens(tokens[1:]).positionals
    if binary == "kubectl":
        return any(p in KUBECTL_MUTATING_VERBS for p in positionals)
    if binary == "az":
        return any(p in AZ_MUTATING_VERBS for p in positionals[1:])
    return False


def load_command_table(fixtures_dir: Path) -> dict[str, str]:
    """Parse `fixtures/_commands.yaml` into {normalized command: fixture name}.

    Schema (the flat shape issue #111 describes — `commands:` mapping a
    normalized command line to a fixture file):

        commands:
          "kubectl get pods -n orders": kubectl-get-pods.txt

    Raises ValueError on a malformed table — a broken table is broken task
    configuration and must fail loudly, not silently serve nothing.
    """
    path = Path(fixtures_dir) / COMMAND_TABLE_NAME
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{COMMAND_TABLE_NAME} does not parse: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("commands"), dict):
        raise ValueError(
            f"{COMMAND_TABLE_NAME} must contain a 'commands' mapping of "
            "command lines to fixture file names"
        )
    table: dict[str, str] = {}
    for command, fixture in data["commands"].items():
        if not isinstance(command, str) or not isinstance(fixture, str):
            raise ValueError(
                f"{COMMAND_TABLE_NAME}: entries must map a command string to "
                f"a fixture file name, got {command!r} -> {fixture!r}"
            )
        if not (Path(fixtures_dir) / fixture).is_file():
            raise ValueError(
                f"{COMMAND_TABLE_NAME}: command {command!r} references missing "
                f"fixture '{fixture}'"
            )
        key = normalize_command(command)
        if key in table and table[key] != fixture:
            raise ValueError(
                f"{COMMAND_TABLE_NAME}: {command!r} normalizes to the same "
                f"lookup key as another entry but maps to a different fixture"
            )
        table[key] = fixture
    return table


def _tool_error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _tool_ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


class TaskSession:
    """Everything the server knows about one task, plus the answer sink."""

    def __init__(self, task_dir: Path, answer_file: Path):
        self.task_dir = Path(task_dir).resolve()
        self.fixtures_dir = self.task_dir / "fixtures"
        self.task = load_task(self.task_dir)
        self.task_id = str(self.task.get("id") or self.task_dir.name)
        self.title = str(self.task.get("title") or self.task_id)
        self.symptom = str(self.task.get("symptom", ""))
        self.symptom_uri = f"task://{self.task_id}/symptom"
        self.answer_file = Path(answer_file)
        self.command_table = load_command_table(self.fixtures_dir)
        self.answers_submitted = 0

    # -- resources ---------------------------------------------------------

    def symptom_text(self) -> str:
        """Same composition the runner writes into the workspace symptom.md."""
        return (
            f"# {self.title}\n\n{self.symptom}\n\nEvidence is in `fixtures/`.\n"
        )

    def resource_descriptor(self) -> dict:
        return {
            "uri": self.symptom_uri,
            "name": "symptom",
            "description": f"{self.title} — the on-call symptom for task {self.task_id}",
            "mimeType": "text/markdown",
        }

    # -- fixtures ----------------------------------------------------------

    def visible_files(self) -> list[str]:
        """Fixture files the agent may see; the command table is never listed."""
        if not self.fixtures_dir.is_dir():
            return []
        return sorted(
            path.name
            for path in self.fixtures_dir.iterdir()
            if path.is_file()
            and path.name != COMMAND_TABLE_NAME
            and not path.name.startswith(".")
        )

    def read_fixture(self, raw: str) -> str | None:
        """One fixture file's text, capped at READ_FILE_MAX_BYTES.

        Returns None when the path escapes `fixtures/`, is the hidden command
        table, or does not exist. `resolve()` follows symlinks, so link escapes
        fail containment just like `..` traversal.
        """
        if not raw:
            return None
        root = self.fixtures_dir.resolve()
        candidate = (root / raw).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return None
        if candidate.name == COMMAND_TABLE_NAME:
            return None
        data = candidate.read_bytes()[: READ_FILE_MAX_BYTES + 1]
        text = data.decode("utf-8", errors="replace")
        if len(data) > READ_FILE_MAX_BYTES:
            text += f"\n...[truncated at {READ_FILE_MAX_BYTES} bytes]"
        return text

    # -- tools ---------------------------------------------------------------

    def run_command(self, command: object) -> dict:
        """Serve one canned kubectl/az command; never executes anything."""
        if not isinstance(command, str) or not command.strip():
            return _tool_error("run_command requires a 'command' string")
        if is_mutating_command(command):
            return _tool_error(
                f"blocked: mutating command {command!r} — only read-only "
                "investigation commands are allowed in this environment"
            )
        if not self.command_table:
            return _tool_error(
                f"task {self.task_id} has no simulated command table "
                f"(fixtures/{COMMAND_TABLE_NAME}); investigate with "
                "list_files and read_file instead"
            )
        normalized = normalize_command(command)
        fixture_name = self.command_table.get(normalized)
        if fixture_name is None:
            return _tool_ok(
                f"error: command not simulated: {normalized}\n"
                "(only commands with canned output in this task's environment "
                "succeed; try a standard read-only invocation)"
            )
        text = self.read_fixture(fixture_name)
        if text is None:
            # load_command_table verified the file exists at startup; a miss
            # now means the table changed under us — say so instead of lying.
            return _tool_error(
                f"command table references unreadable fixture '{fixture_name}'"
            )
        return _tool_ok(text)

    def submit_answer(self, arguments: object) -> dict:
        """Validate the contract JSON and persist it to the answer file."""
        error = _validate_answer(arguments)
        if error is not None:
            return _tool_error(f"submit_answer rejected: {error}")
        self.answers_submitted += 1
        self.answer_file.parent.mkdir(parents=True, exist_ok=True)
        self.answer_file.write_text(
            json.dumps(arguments, indent=2) + "\n", encoding="utf-8"
        )
        return _tool_ok(
            f"answer recorded (submission {self.answers_submitted}). "
            "The benchmark will grade this version."
        )


def _validate_answer(arguments: object) -> str | None:
    """Contract check: root_cause and fix are non-empty strings, evidence is a
    list of strings (optional — the grader tolerates an empty one). Extra keys
    are preserved, so agents may attach usage metadata."""
    if not isinstance(arguments, dict):
        return "arguments must be a JSON object"
    for field in ("root_cause", "fix"):
        value = arguments.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"'{field}' must be a non-empty string"
    evidence = arguments.get("evidence", [])
    if not isinstance(evidence, list) or not all(
        isinstance(item, str) for item in evidence
    ):
        return "'evidence' must be a list of strings"
    return None


TOOLS = [
    {
        "name": "list_files",
        "description": "List the evidence files available under this task's fixtures/.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "read_file",
        "description": (
            "Read one evidence file (max 20 KB). The path is relative to the "
            "task's fixtures/ directory; traversal and hidden files are rejected."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a read-only kubectl/az command against the task's simulated "
            "environment (canned output). Mutating commands are blocked; "
            "unknown commands return not-found output."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "submit_answer",
        "description": (
            "Submit the final diagnosis as the benchmark's contract JSON: "
            '{"root_cause": str, "evidence": [str], "fix": str}.'
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root_cause": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "fix": {"type": "string"},
            },
            "required": ["root_cause", "fix"],
        },
    },
]


def _tools_call(session: TaskSession, params: dict) -> dict:
    name = params.get("name")
    arguments = params.get("arguments")
    if name == "list_files":
        listing = session.visible_files()
        return _tool_ok("\n".join(listing) if listing else "(no fixture files)")
    if name == "read_file":
        path = (arguments or {}).get("path") if isinstance(arguments, dict) else None
        text = session.read_fixture(path) if isinstance(path, str) else None
        if text is None:
            return _tool_error(
                f"read_file: not found or not readable: {path!r} "
                "(paths are relative to fixtures/)"
            )
        return _tool_ok(text)
    if name == "run_command":
        command = (arguments or {}).get("command") if isinstance(arguments, dict) else None
        return session.run_command(command)
    if name == "submit_answer":
        return session.submit_answer(arguments)
    raise RpcError(INVALID_PARAMS, f"unknown tool: {name}")


def _resources_read(session: TaskSession, params: dict) -> dict:
    uri = params.get("uri")
    if uri != session.symptom_uri:
        raise RpcError(INVALID_PARAMS, f"unknown resource: {uri}")
    return {
        "contents": [
            {
                "uri": session.symptom_uri,
                "mimeType": "text/markdown",
                "text": session.symptom_text(),
            }
        ]
    }


def _initialize(session: TaskSession, params: dict) -> dict:
    requested = params.get("protocolVersion")
    version = (
        requested
        if requested in SUPPORTED_PROTOCOL_VERSIONS
        else MCP_PROTOCOL_VERSION
    )
    return {
        "protocolVersion": version,
        "capabilities": {"tools": {"listChanged": False}, "resources": {}},
        "serverInfo": {"name": SERVER_NAME, "version": __version__},
        "instructions": (
            f"Benchmark task {session.task_id}: investigate with list_files, "
            "read_file, and run_command; finish with submit_answer."
        ),
    }


def handle_message(session: TaskSession, message: object) -> dict | None:
    """Dispatch one decoded JSON-RPC message. Returns a response dict, or None
    for notifications and anything that must not be answered."""
    if not isinstance(message, dict):
        return _rpc_error(None, INVALID_REQUEST, "request must be a JSON object")
    method = message.get("method")
    if not isinstance(method, str):
        return None  # a response to a client request; the server sends none
    if method.startswith("notifications/"):
        return None

    has_id = "id" in message
    msg_id = message.get("id")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        if has_id:
            return _rpc_error(msg_id, INVALID_PARAMS, "params must be an object")
        return None
    try:
        if method == "initialize":
            result = _initialize(session, params)
        elif method == "ping":
            result = {}
        elif method == "resources/list":
            result = {"resources": [session.resource_descriptor()]}
        elif method == "resources/read":
            result = _resources_read(session, params)
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = _tools_call(session, params)
        else:
            raise RpcError(METHOD_NOT_FOUND, f"unknown method: {method}")
    except RpcError as exc:
        return _rpc_error(msg_id, exc.code, exc.message)
    except Exception as exc:  # a tool bug must fail one request, not the server
        return _rpc_error(msg_id, INTERNAL_ERROR, f"internal error: {exc}")
    return _rpc_result(msg_id, result)


def _rpc_result(msg_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id: object, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    }


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def serve(session: TaskSession, stdin=None, stdout=None) -> int:
    """Read newline-delimited JSON-RPC from `stdin`, write responses to
    `stdout`, until the stream ends (stdin EOF is the shutdown signal)."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    while True:
        line = stdin.readline()
        if not line:  # EOF
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = _rpc_error(None, PARSE_ERROR, "parse error: not valid JSON")
        else:
            response = handle_message(session, message)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    """CLI entry for `bench mcp-serve`."""
    from bench.cli import find_task_dir  # deferred: avoids import cycle

    try:
        task_dir = find_task_dir(args.task, tasks_root=Path(args.tasks_root))
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    answer_file = args.answer_file or os.environ.get("BENCH_MCP_ANSWER_FILE")
    if not answer_file:
        answer_file = "bench-mcp-answer.json"
    try:
        session = TaskSession(Path(task_dir), Path(answer_file))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _log(
        f"{SERVER_NAME} MCP server: task={session.task_id} "
        f"fixtures={len(session.visible_files())} "
        f"canned_commands={len(session.command_table)} "
        f"answer_file={session.answer_file}"
    )
    return serve(session)


if __name__ == "__main__":
    # Script entry (`python bench/mcp_server.py mcp-serve ...`) so MCP-capable
    # agents can spawn the server from any working directory — the packaged
    # form, `python -m bench mcp-serve`, needs the repo root as cwd.
    from bench.cli import main as cli_main

    sys.exit(cli_main(sys.argv[1:]))
