"""Tests for the AutoGen multi-agent adapter (issue #173).

No real API: the example's model client is AutoGen's `ReplayChatCompletionClient`
(scripted completions, no network), so both the in-process adapter run and the
end-to-end subprocess run (the way `bench run` invokes agents) exercise the
full two-agent loop deterministically. Tool behavior is tested directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agents import autogen_adapter

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_TASK = REPO_ROOT / "tests" / "data" / "monitoring" / "run-test-001"
ADAPTER = REPO_ROOT / "agents" / "autogen_adapter.py"
TARGET = "examples.autogen_team:build_client"

ANSWER = {
    "root_cause": "expired secret referenced by the probe",
    "evidence": ["kubectl-describe-pod.txt: probe failing with 401"],
    "fix": "rotate the secret and redeploy",
}


def _tool(tools, name):
    return next(t for t in tools if t.__name__ == name)


def _seed_task(tmp_path):
    (tmp_path / "symptom.md").write_text("symptom", encoding="utf-8")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "a.txt").write_text("evidence", encoding="utf-8")


# --- tool-level tests -------------------------------------------------------


def test_list_files_sees_only_visible_files(tmp_path):
    """Hidden rubric files (expected.json) never appear in the listing."""
    state: dict = {}
    _seed_task(tmp_path)
    (tmp_path / "expected.json").write_text('{"secret_rubric": true}', encoding="utf-8")
    tools = autogen_adapter.build_tools(tmp_path, state)

    listed = json.loads(_tool(tools, "list_files")())

    assert listed == {"files": ["fixtures/a.txt", "symptom.md"]}


def test_read_file_returns_content_and_refuses_escape(tmp_path):
    """A fixture is readable; a ../ traversal that reaches a hidden file is refused."""
    state: dict = {}
    _seed_task(tmp_path)
    (tmp_path / "expected.json").write_text(
        '{"secret_rubric": "hidden"}', encoding="utf-8"
    )
    tools = autogen_adapter.build_tools(tmp_path, state)

    read = json.loads(_tool(tools, "read_file")(path="fixtures/a.txt"))
    refused = json.loads(_tool(tools, "read_file")(path="../expected.json"))

    assert read == {"content": "evidence"}
    assert "outside the task directory" in refused["error"]
    assert "secret_rubric" not in json.dumps(refused)


def test_run_command_captures_output_and_cwd(tmp_path):
    """stdout and the return code round-trip; the command runs from the task dir."""
    state: dict = {}
    _seed_task(tmp_path)
    tools = autogen_adapter.build_tools(tmp_path, state)

    result = json.loads(_tool(tools, "run_command")(command="echo hello-run-command"))

    assert result["returncode"] == 0
    assert result["stdout"].strip() == "hello-run-command"


def test_run_command_reports_timeout_instead_of_hanging(tmp_path):
    """A stuck diagnostic degrades to an error result under the capped timeout."""
    state: dict = {}
    tools = autogen_adapter.build_tools(tmp_path, state)

    result = json.loads(_tool(tools, "run_command")(command="sleep 5", timeout=0.5))

    assert "timed out" in result["error"]


def test_submit_answer_captures_contract_in_state(tmp_path):
    """A well-formed submission lands in the shared state as the contract dict."""
    state: dict = {}
    tools = autogen_adapter.build_tools(tmp_path, state)

    status = json.loads(
        _tool(tools, "submit_answer")(root_cause="r", evidence=["a: b"], fix="f")
    )

    assert status == {"status": "submitted"}
    assert state["answer"] == {"root_cause": "r", "evidence": ["a: b"], "fix": "f"}


def test_submit_answer_rejects_incomplete_report(tmp_path):
    """A submission without a fix is refused and captures nothing."""
    state: dict = {}
    tools = autogen_adapter.build_tools(tmp_path, state)

    result = json.loads(
        _tool(tools, "submit_answer")(root_cause="r", evidence=[], fix="")
    )

    assert "error" in result
    assert "answer" not in state


# --- adapter plumbing -------------------------------------------------------


def test_load_target_accepts_colon_and_dot_forms():
    assert autogen_adapter.load_target("os.path:join") is not None
    assert autogen_adapter.load_target("os.path.join") is not None


def test_load_target_rejects_bare_module():
    with pytest.raises(ValueError, match="AUTOGEN_AGENT_TARGET"):
        autogen_adapter.load_target("os")


def test_resolve_model_client_declares_function_calling(monkeypatch):
    """The client declares function_calling so tool agents accept it (custom base URLs are not in AutoGen's known-model list)."""
    monkeypatch.setenv("LLM_BASE_URL", "https://example.openai.azure.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o")

    client = autogen_adapter.resolve_model_client()

    assert client.model_info["function_calling"] is True


# --- mocked end-to-end runs -------------------------------------------------


def test_run_agent_replay_client_full_team_loop():
    """The replayed team runs hypothesis → reviewer challenge → final JSON."""
    answer = autogen_adapter.run_agent(TEST_TASK, TARGET)

    assert answer == {
        "root_cause": "expired secret referenced by the liveness probe",
        "evidence": [
            "fixtures/kubectl-get-pods.txt: probe-api-0 failing probe with 401"
        ],
        "fix": "rotate the secret and redeploy the pod",
    }


def test_run_agent_unparseable_run_degrades_to_empty(tmp_path):
    """No JSON anywhere in the team transcript → {} (graded FAIL, never a crash)."""
    module = sys.modules.setdefault(
        "tests._autogen_noise_target", type(sys)("tests._autogen_noise_target")
    )
    from autogen_ext.models.replay import ReplayChatCompletionClient

    module.build_client = lambda: ReplayChatCompletionClient(
        ["no json here"] * autogen_adapter.MAX_TEAM_MESSAGES,
        model_info={
            "vision": False,
            "function_calling": True,
            "json_output": True,
            "structured_output": True,
            "family": "unknown",
        },
    )

    answer = autogen_adapter.run_agent(
        tmp_path, "tests._autogen_noise_target:build_client"
    )

    assert answer == {}


def test_end_to_end_subprocess_through_adapter_entrypoint():
    """The real entrypoint (as `bench run` calls it) prints the contract JSON."""
    env = dict(
        os.environ,
        TASK_DIR=str(TEST_TASK),
        AUTOGEN_AGENT_TARGET=TARGET,
    )
    proc = subprocess.run(
        [sys.executable, str(ADAPTER)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["root_cause"] == (
        "expired secret referenced by the liveness probe"
    )
