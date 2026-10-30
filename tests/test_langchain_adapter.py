"""Tests for the LangChain adapter (issue #122).

No real API: the example agent's chat model is a scripted fake
(`FakeMessagesListChatModel`), so both the in-process adapter run and the
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

from agents import langchain_adapter

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_TASK = REPO_ROOT / "tests" / "data" / "monitoring" / "run-test-001"
ADAPTER = REPO_ROOT / "agents" / "langchain_adapter.py"
TARGET = "examples.langchain_agent:build_agent"

ANSWER = {
    "root_cause": "expired secret referenced by the probe",
    "evidence": ["kubectl-describe-pod.txt: probe failing with 401"],
    "fix": "rotate the secret and redeploy",
}


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


# --- tool-level tests -------------------------------------------------------


def test_read_file_lists_files_when_path_empty(tmp_path):
    """The no-path call returns the visible file list; hidden files never appear."""
    state: dict = {}
    tools = langchain_adapter.build_tools(tmp_path, state)
    (tmp_path / "symptom.md").write_text("symptom", encoding="utf-8")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "a.txt").write_text("evidence", encoding="utf-8")
    (tmp_path / "expected.json").write_text('{"secret_rubric": true}', encoding="utf-8")

    listed = json.loads(_tool(tools, "read_file").invoke({"path": ""}))

    assert listed == {"files": ["fixtures/a.txt", "symptom.md"]}


def test_read_file_returns_content_and_refuses_escape(tmp_path):
    """A fixture is readable; a ../ traversal that reaches a hidden file is refused."""
    state: dict = {}
    tools = langchain_adapter.build_tools(tmp_path, state)
    (tmp_path / "symptom.md").write_text("symptom", encoding="utf-8")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "a.txt").write_text("evidence", encoding="utf-8")
    secret = tmp_path / "expected.json"
    secret.write_text('{"secret_rubric": "hidden"}', encoding="utf-8")

    read = json.loads(
        _tool(tools, "read_file").invoke({"path": "fixtures/a.txt"})
    )
    refused = json.loads(
        _tool(tools, "read_file").invoke({"path": "../expected.json"})
    )

    assert read == {"content": "evidence"}
    assert "outside the task directory" in refused["error"]
    assert "secret_rubric" not in json.dumps(refused)


def test_run_command_captures_output_and_cwd(tmp_path):
    """stdout and the return code round-trip; the command runs from the task dir."""
    state: dict = {}
    tools = langchain_adapter.build_tools(tmp_path, state)
    (tmp_path / "symptom.md").write_text("symptom", encoding="utf-8")

    result = json.loads(
        _tool(tools, "run_command").invoke({"command": "echo hello-run-command"})
    )

    assert result["returncode"] == 0
    assert result["stdout"].strip() == "hello-run-command"


def test_run_command_reports_timeout_instead_of_hanging(tmp_path):
    """A stuck diagnostic degrades to an error result under the capped timeout."""
    state: dict = {}
    tools = langchain_adapter.build_tools(tmp_path, state)

    result = json.loads(
        _tool(tools, "run_command").invoke({"command": "sleep 5", "timeout": 0.5})
    )

    assert "timed out" in result["error"]


def test_submit_answer_captures_contract_in_state(tmp_path):
    """A well-formed submission lands in the shared state as the contract dict."""
    state: dict = {}
    tools = langchain_adapter.build_tools(tmp_path, state)

    status = json.loads(
        _tool(tools, "submit_answer").invoke(
            {"root_cause": "r", "evidence": ["a: b"], "fix": "f"}
        )
    )

    assert status == {"status": "submitted"}
    assert state["answer"] == {"root_cause": "r", "evidence": ["a: b"], "fix": "f"}


def test_submit_answer_rejects_incomplete_report(tmp_path):
    """A submission without a fix is refused and captures nothing."""
    state: dict = {}
    tools = langchain_adapter.build_tools(tmp_path, state)

    result = json.loads(
        _tool(tools, "submit_answer").invoke({"root_cause": "r", "evidence": []})
    )

    assert "error" in result
    assert "answer" not in state


# --- adapter plumbing -------------------------------------------------------


def test_load_target_accepts_colon_and_dot_forms():
    assert langchain_adapter.load_target("os.path:join") is not None
    assert langchain_adapter.load_target("os.path.join") is not None


def test_load_target_rejects_bare_module():
    with pytest.raises(ValueError, match="LANGCHAIN_AGENT_TARGET"):
        langchain_adapter.load_target("os")


def test_run_agent_rejects_non_callable_target():
    with pytest.raises(TypeError):
        langchain_adapter.run_agent(TEST_TASK, "os.sep")  # a str is neither


# --- mocked end-to-end runs -------------------------------------------------


def test_run_agent_factory_target_full_loop():
    """The example's scripted agent investigates through all three tools and submits."""
    answer = langchain_adapter.run_agent(TEST_TASK, TARGET)

    assert answer == {
        "root_cause": "expired secret referenced by the liveness probe",
        "evidence": ["fixtures/kubectl-get-pods.txt: probe-api-0 failing probe with 401"],
        "fix": "rotate the secret and redeploy the pod",
    }


def test_run_agent_plain_runnable_target_returns_contract_json():
    """A Runnable handed in directly is invoked with the symptom and its output graded."""
    from langchain_core.runnables import RunnableLambda

    answer = langchain_adapter.run_agent(
        TEST_TASK, _targets_runnable(RunnableLambda(lambda _: json.dumps(ANSWER)))
    )

    assert answer == ANSWER


def _targets_runnable(runnable):
    """A module attribute holding a bare Runnable; simulates a prebuilt target."""
    module = sys.modules.setdefault("tests._lc_bare_target", type(sys)("tests._lc_bare_target"))
    module.runnable = runnable
    return "tests._lc_bare_target:runnable"


def test_end_to_end_subprocess_through_adapter_entrypoint():
    """The real entrypoint (as `bench run` calls it) prints the contract JSON."""
    env = dict(
        os.environ,
        TASK_DIR=str(TEST_TASK),
        LANGCHAIN_AGENT_TARGET=TARGET,
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


def test_missing_target_prints_empty_object_and_exits_zero():
    """No LANGCHAIN_AGENT_TARGET: {} on stdout, exit 0 — a graded FAIL, not a crash."""
    env = dict(os.environ, TASK_DIR=str(TEST_TASK))
    env.pop("LANGCHAIN_AGENT_TARGET", None)
    proc = subprocess.run(
        [sys.executable, str(ADAPTER)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )

    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {}
