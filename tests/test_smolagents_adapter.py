"""Tests for the smolagents adapter (issue #171).

No real API: the example agent's model is a scripted fake (subclass of
smolagents' abstract `Model`), so both the in-process adapter run and the
end-to-end subprocess run (the way `bench run` invokes agents) exercise the
full tool loop deterministically.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agents import smolagents_adapter

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_TASK = REPO_ROOT / "tests" / "data" / "monitoring" / "run-test-001"
ADAPTER = REPO_ROOT / "agents" / "smolagents_adapter.py"
TARGET = "examples.smolagents_agent:build_agent"

ANSWER = {
    "root_cause": "expired secret referenced by the probe",
    "evidence": ["kubectl-describe-pod.txt: probe failing with 401"],
    "fix": "rotate the secret and redeploy",
}


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


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
    tools = smolagents_adapter.build_tools(tmp_path, state)

    listed = json.loads(_tool(tools, "list_files")())

    assert listed == {"files": ["fixtures/a.txt", "symptom.md"]}


def test_read_file_returns_content_and_refuses_escape(tmp_path):
    """A fixture is readable; a ../ traversal that reaches a hidden file is refused."""
    state: dict = {}
    _seed_task(tmp_path)
    (tmp_path / "expected.json").write_text('{"secret_rubric": "hidden"}', encoding="utf-8")
    tools = smolagents_adapter.build_tools(tmp_path, state)

    read = json.loads(_tool(tools, "read_file")(path="fixtures/a.txt"))
    refused = json.loads(_tool(tools, "read_file")(path="../expected.json"))

    assert read == {"content": "evidence"}
    assert "outside the task directory" in refused["error"]
    assert "secret_rubric" not in json.dumps(refused)


def test_run_command_captures_output_and_cwd(tmp_path):
    """stdout and the return code round-trip; the command runs from the task dir."""
    state: dict = {}
    _seed_task(tmp_path)
    tools = smolagents_adapter.build_tools(tmp_path, state)

    result = json.loads(
        _tool(tools, "run_command")(command="echo hello-run-command")
    )

    assert result["returncode"] == 0
    assert result["stdout"].strip() == "hello-run-command"


def test_run_command_reports_timeout_instead_of_hanging(tmp_path):
    """A stuck diagnostic degrades to an error result under the capped timeout."""
    state: dict = {}
    tools = smolagents_adapter.build_tools(tmp_path, state)

    result = json.loads(_tool(tools, "run_command")(command="sleep 5", timeout=0.5))

    assert "timed out" in result["error"]


def test_submit_answer_captures_contract_in_state(tmp_path):
    """A well-formed submission lands in the shared state as the contract dict."""
    state: dict = {}
    tools = smolagents_adapter.build_tools(tmp_path, state)

    status = json.loads(
        _tool(tools, "submit_answer")(
            root_cause="r", evidence=["a: b"], fix="f"
        )
    )

    assert status == {"status": "submitted"}
    assert state["answer"] == {"root_cause": "r", "evidence": ["a: b"], "fix": "f"}


def test_submit_answer_rejects_incomplete_report(tmp_path):
    """A submission without a fix is refused and captures nothing."""
    state: dict = {}
    tools = smolagents_adapter.build_tools(tmp_path, state)

    result = json.loads(
        _tool(tools, "submit_answer")(root_cause="r", evidence=[], fix="")
    )

    assert "error" in result
    assert "answer" not in state


# --- adapter plumbing -------------------------------------------------------


def test_load_target_accepts_colon_and_dot_forms():
    assert smolagents_adapter.load_target("os.path:join") is not None
    assert smolagents_adapter.load_target("os.path.join") is not None


def test_load_target_rejects_bare_module():
    with pytest.raises(ValueError, match="SMOLAGENTS_AGENT_TARGET"):
        smolagents_adapter.load_target("os")


def test_resolve_model_builds_litellm_from_baseline_env(monkeypatch):
    """The default model reads the same LLM_* trio as the ReAct baseline."""
    monkeypatch.setenv("LLM_BASE_URL", "https://example.openai.azure.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o")

    model = smolagents_adapter.resolve_model()

    assert model.model_id == "openai/gpt-4o"


# --- mocked end-to-end runs -------------------------------------------------


def test_run_agent_factory_target_full_loop():
    """The example's scripted agent investigates through all four tools and submits."""
    answer = smolagents_adapter.run_agent(TEST_TASK, TARGET)

    assert answer == {
        "root_cause": "expired secret referenced by the liveness probe",
        "evidence": ["fixtures/kubectl-get-pods.txt: probe-api-0 failing probe with 401"],
        "fix": "rotate the secret and redeploy the pod",
    }


def test_run_agent_plain_json_reply_is_extracted(tmp_path):
    """A model that answers with bare contract JSON (no submit_answer) is graded."""
    module = sys.modules.setdefault(
        "tests._smolagents_json_target", type(sys)("tests._smolagents_json_target")
    )
    module.build_agent = lambda tools: _json_reply_agent(tools, json.dumps(ANSWER))

    answer = smolagents_adapter.run_agent(tmp_path, "tests._smolagents_json_target:build_agent")

    assert answer == ANSWER


def _json_reply_agent(tools, reply):
    """A smolagents agent whose model immediately replies with contract JSON."""
    from smolagents import ToolCallingAgent

    from examples.smolagents_agent import ScriptedModel

    return ToolCallingAgent(
        tools=list(tools),
        model=ScriptedModel(script=[{"name": "final_answer", "args": {"answer": reply}}]),
        max_steps=2,
        verbosity_level=-1,
    )


def test_end_to_end_subprocess_through_adapter_entrypoint():
    """The real entrypoint (as `bench run` calls it) prints the contract JSON."""
    env = dict(
        os.environ,
        TASK_DIR=str(TEST_TASK),
        SMOLAGENTS_AGENT_TARGET=TARGET,
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
