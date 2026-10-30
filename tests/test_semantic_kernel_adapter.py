"""Tests for the Semantic Kernel adapter (issue #172).

No real API: the example's chat service is a scripted fake (subclass of SK's
`ChatCompletionClientBase`), so both the in-process adapter run and the
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

from agents import semantic_kernel_adapter

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_TASK = REPO_ROOT / "tests" / "data" / "monitoring" / "run-test-001"
ADAPTER = REPO_ROOT / "agents" / "semantic_kernel_adapter.py"
TARGET = "examples.semantic_kernel_agent:build_service"

ANSWER = {
    "root_cause": "expired secret referenced by the probe",
    "evidence": ["kubectl-describe-pod.txt: probe failing with 401"],
    "fix": "rotate the secret and redeploy",
}


def _seed_task(tmp_path):
    (tmp_path / "symptom.md").write_text("symptom", encoding="utf-8")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "a.txt").write_text("evidence", encoding="utf-8")


def _plugin(tmp_path, state=None):
    state = {} if state is None else state
    return semantic_kernel_adapter.SREPlugin(tmp_path, state), state


# --- plugin-level tests -----------------------------------------------------


def test_list_files_sees_only_visible_files(tmp_path):
    """Hidden rubric files (expected.json) never appear in the listing."""
    plugin, _ = _plugin(tmp_path)
    _seed_task(tmp_path)
    (tmp_path / "expected.json").write_text('{"secret_rubric": true}', encoding="utf-8")

    listed = json.loads(plugin.list_files())

    assert listed == {"files": ["fixtures/a.txt", "symptom.md"]}


def test_read_file_returns_content_and_refuses_escape(tmp_path):
    """A fixture is readable; a ../ traversal that reaches a hidden file is refused."""
    plugin, _ = _plugin(tmp_path)
    _seed_task(tmp_path)
    (tmp_path / "expected.json").write_text('{"secret_rubric": "hidden"}', encoding="utf-8")

    read = json.loads(plugin.read_file(path="fixtures/a.txt"))
    refused = json.loads(plugin.read_file(path="../expected.json"))

    assert read == {"content": "evidence"}
    assert "outside the task directory" in refused["error"]
    assert "secret_rubric" not in json.dumps(refused)


def test_run_command_captures_output_and_cwd(tmp_path):
    """stdout and the return code round-trip; the command runs from the task dir."""
    plugin, _ = _plugin(tmp_path)
    _seed_task(tmp_path)

    result = json.loads(plugin.run_command(command="echo hello-run-command"))

    assert result["returncode"] == 0
    assert result["stdout"].strip() == "hello-run-command"


def test_run_command_reports_timeout_instead_of_hanging(tmp_path):
    """A stuck diagnostic degrades to an error result under the capped timeout."""
    plugin, _ = _plugin(tmp_path)

    result = json.loads(plugin.run_command(command="sleep 5", timeout=0.5))

    assert "timed out" in result["error"]


def test_submit_answer_captures_contract_in_state(tmp_path):
    """A well-formed submission lands in the shared state as the contract dict."""
    plugin, state = _plugin(tmp_path)

    status = json.loads(plugin.submit_answer(root_cause="r", evidence=["a: b"], fix="f"))

    assert status == {"status": "submitted"}
    assert state["answer"] == {"root_cause": "r", "evidence": ["a: b"], "fix": "f"}


def test_submit_answer_rejects_incomplete_report(tmp_path):
    """A submission without a fix is refused and captures nothing."""
    plugin, state = _plugin(tmp_path)

    result = json.loads(plugin.submit_answer(root_cause="r", evidence=[], fix=""))

    assert "error" in result
    assert "answer" not in state


# --- adapter plumbing -------------------------------------------------------


def test_load_target_accepts_colon_and_dot_forms():
    assert semantic_kernel_adapter.load_target("os.path:join") is not None
    assert semantic_kernel_adapter.load_target("os.path.join") is not None


def test_load_target_rejects_bare_module():
    with pytest.raises(ValueError, match="SEMANTIC_KERNEL_AGENT_TARGET"):
        semantic_kernel_adapter.load_target("os")


def test_resolve_service_azure_when_endpoint_set(monkeypatch):
    """AZURE_OPENAI_ENDPOINT selects the Microsoft-native AzureChatCompletion."""
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o")  # deployment fallback
    monkeypatch.delenv("AZURE_OPENAI_CHAT_DEPLOYMENT_NAME", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    service = semantic_kernel_adapter.resolve_service()

    assert type(service).__name__ == "AzureChatCompletion"


def test_resolve_service_openai_compatible_fallback(monkeypatch):
    """Without AZURE_OPENAI_ENDPOINT, the LLM_* trio builds an OpenAI-compatible service."""
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "https://example.openai.azure.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o")

    service = semantic_kernel_adapter.resolve_service()

    assert type(service).__name__ == "OpenAIChatCompletion"
    assert service.ai_model_id == "gpt-4o"


# --- mocked end-to-end runs -------------------------------------------------


def test_run_agent_factory_target_full_loop():
    """The example's scripted service investigates through all four tools and submits."""
    answer = semantic_kernel_adapter.run_agent(TEST_TASK, TARGET)

    assert answer == {
        "root_cause": "expired secret referenced by the liveness probe",
        "evidence": ["fixtures/kubectl-get-pods.txt: probe-api-0 failing probe with 401"],
        "fix": "rotate the secret and redeploy the pod",
    }


def test_run_agent_plain_json_reply_is_extracted(tmp_path):
    """A service that answers with bare contract JSON (no submit_answer) is graded."""
    from examples.semantic_kernel_agent import ScriptedChatCompletion

    module = sys.modules.setdefault(
        "tests._sk_json_target", type(sys)("tests._sk_json_target")
    )
    module.build_service = lambda: ScriptedChatCompletion(
        script=[{"type": "text", "content": json.dumps(ANSWER)}]
    )

    answer = semantic_kernel_adapter.run_agent(
        tmp_path, "tests._sk_json_target:build_service"
    )

    assert answer == ANSWER


def test_end_to_end_subprocess_through_adapter_entrypoint():
    """The real entrypoint (as `bench run` calls it) prints the contract JSON."""
    env = dict(
        os.environ,
        TASK_DIR=str(TEST_TASK),
        SEMANTIC_KERNEL_AGENT_TARGET=TARGET,
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
