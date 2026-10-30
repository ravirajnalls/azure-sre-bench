"""End-to-end tests for the Goose adapter (issue #169).

No real Goose install: the fake "Goose" script below does what the real CLI
would — reads the generated config from `GOOSE_CONFIG_DIR`, spawns the
extension's stdio command as an MCP server, investigates, and calls
`submit_answer`. That exercises the full chain — harness -> adapter -> config ->
MCP server -> answer file -> stdout -> grading — with no network, no
credentials, and no model.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

from agents import goose_adapter
from bench import run as runner
from bench.cli import main as cli_main

REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTER = REPO_ROOT / "agents" / "goose_adapter.py"
TEST_TASK = REPO_ROOT / "tests" / "data" / "monitoring" / "monitoring-902"
TESTS_DATA_ROOT = REPO_ROOT / "tests" / "data"

ANSWER = {
    "root_cause": "Redis connection pool exhausted in billing-api",
    "evidence": ["kubectl-get-pods.txt: CrashLoopBackOff"],
    "fix": "raise the connection limit on the redis pool",
}

# A fake Goose: loads the generated config, brings up the extension's server,
# proves the headless prompt arrived, and submits via the MCP tools.
FAKE_GOOSE = """
import os, shlex, sys

import yaml

sys.path.insert(0, {repo_root!r})
from agents.mcp_client_runner import McpStdioClient

config = yaml.safe_load(
    open(os.path.join(os.environ["GOOSE_CONFIG_DIR"], {config_filename!r}), encoding="utf-8")
)
extension = config["extensions"][{extension_name!r}]
assert extension["enabled"] is True
assert extension["type"] == "stdio"
server_command = shlex.join([extension["cmd"]] + extension["args"])

with McpStdioClient(shlex.split(server_command)) as client:
    client.initialize()
    headless = "--headless" in sys.argv
    client.call_tool("submit_answer", {{
        "root_cause": {root_cause!r},
        "evidence": [
            "headless prompt reached goose: {{}}".format(headless),
            "server script: {{}}".format(os.path.basename(extension["args"][0])),
        ],
        "fix": {fix!r},
    }})
"""


def fake_goose_command(tmp_path: Path) -> str:
    script = tmp_path / "fake_goose.py"
    script.write_text(
        FAKE_GOOSE.format(
            repo_root=str(REPO_ROOT),
            config_filename=goose_adapter.CONFIG_FILENAME,
            extension_name=goose_adapter.EXTENSION_NAME,
            root_cause=ANSWER["root_cause"],
            fix=ANSWER["fix"],
        ),
        encoding="utf-8",
    )
    return f"{sys.executable} {script}"


def adapter_command(goose_command: str) -> str:
    return (
        f"{sys.executable} {ADAPTER} "
        f"--goose-command {shlex.quote(goose_command)} "
        f"--tasks-root {shlex.quote(str(TESTS_DATA_ROOT))}"
    )


# --- config generation --------------------------------------------------------


def test_build_extension_is_a_enabled_stdio_entry():
    extension = goose_adapter.build_extension("python /srv/server.py mcp-serve --task t")

    assert extension == {
        "type": "stdio",
        "name": "bench-mcp",
        "cmd": "python",
        "args": ["/srv/server.py", "mcp-serve", "--task", "t"],
        "enabled": True,
        "timeout": goose_adapter.EXTENSION_TIMEOUT_S,
    }


def test_build_goose_config_writes_parsable_yaml(tmp_path):
    """The generated config round-trips as YAML with only the extension entry."""
    config_path = goose_adapter.build_goose_config("python /srv/server.py", tmp_path)

    assert config_path == tmp_path / goose_adapter.CONFIG_FILENAME
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert list(config) == ["extensions"]
    assert config["extensions"]["bench-mcp"]["cmd"] == "python"
    assert config["extensions"]["bench-mcp"]["args"] == ["/srv/server.py"]


def test_build_prompt_names_the_symptom_and_the_exit_tool():
    prompt = goose_adapter.build_prompt("pod crash-looping in checkout")

    assert "pod crash-looping in checkout" in prompt
    assert "submit_answer" in prompt


def test_collect_answer_missing_or_invalid_file_is_empty(tmp_path):
    assert goose_adapter.collect_answer(tmp_path / "absent.json") == {}
    (tmp_path / "bad.json").write_text("not json", encoding="utf-8")
    assert goose_adapter.collect_answer(tmp_path / "bad.json") == {}


# --- mocked end-to-end runs -----------------------------------------------------


def test_end_to_end_subprocess_through_adapter_entrypoint(tmp_path):
    """The real entrypoint (as `bench run` calls it) prints the submitted answer."""
    env = dict(
        os.environ,
        TASK_ID="monitoring-902",
        TASK_DIR=str(TEST_TASK),
    )
    proc = subprocess.run(
        shlex.split(adapter_command(fake_goose_command(tmp_path))),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    answer = json.loads(proc.stdout)
    assert answer["root_cause"] == ANSWER["root_cause"]
    assert answer["evidence"] == [
        "headless prompt reached goose: True",
        "server script: mcp_server.py",
    ]


def test_goose_without_submission_prints_empty_object_and_exits_zero(tmp_path):
    """No submit_answer: {} on stdout, exit 0 — a graded FAIL, not a crash."""
    idle_goose = tmp_path / "idle_goose.py"
    idle_goose.write_text("print('investigated, never submitted')", encoding="utf-8")
    env = dict(
        os.environ,
        TASK_ID="monitoring-902",
        TASK_DIR=str(TEST_TASK),
    )
    proc = subprocess.run(
        shlex.split(adapter_command(f"{sys.executable} {idle_goose}")),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )

    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}
    assert "submit_answer" in proc.stderr


def test_end_to_end_mocked_benchmark_run(tmp_path):
    """The full chain: harness -> adapter -> Goose -> MCP server -> grading."""
    record = runner.run_task(
        TEST_TASK,
        adapter_command(fake_goose_command(tmp_path)),
        results_root=tmp_path,
    )

    assert record["passed"] is True, record["stderr_tail"]
    assert record["score"] == 1.0
    assert record["answer"]["root_cause"] == ANSWER["root_cause"]
    assert record["timed_out"] is False


def test_adapter_is_wired_through_the_bench_cli(tmp_path, monkeypatch, capsys):
    """`bench run --agent "<adapter> ..."` grades the Goose run like any other."""
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path / "results")
    exit_code = cli_main(
        [
            "run",
            "--agent",
            adapter_command(fake_goose_command(tmp_path)),
            "--task",
            "monitoring-902",
            "--name",
            "cli-goose",
            "--tasks-root",
            "tests/data",
        ]
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert out.startswith("PASS monitoring-902 score=1.0 ")
    assert out.endswith("cli-goose: 1/1 passed\n")
    stored = json.loads(
        (tmp_path / "results" / "cli-goose" / "monitoring-902.json").read_text()
    )
    assert stored["passed"] is True
