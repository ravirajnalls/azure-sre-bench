"""Tests for the HolmesGPT adapter (issue #167).

No real HolmesGPT install and no API key: the `holmes` CLI is a scripted fake
executable on PATH, so both the in-process mapping tests and the end-to-end
subprocess run (the way `bench run` invokes agents) exercise the adapter
deterministically. The fake `holmes ask` answers with contract JSON whose
evidence quotes `command -v kubectl` and a `kubectl delete` attempt, proving
the simulated CLIs still win on PATH and mutating commands are still refused
by the sim.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agents.adapters import holmesgpt_adapter

REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTER = REPO_ROOT / "agents" / "adapters" / "holmesgpt_adapter.py"

JSON_ANSWER = (
    '{"root_cause": "the probe references an expired secret",'
    ' "evidence": ["kubectl describe: 401 on probe"],'
    ' "fix": "rotate the secret"}'
)

# A minimal sim commands table: one read-only entry, no mutating entries —
# the sim refuses anything else (issue #111).
COMMANDS_YAML = """\
commands:
  - command: kubectl get pods -n payments
    stdout: |
      NAME                     READY   STATUS
      payments-7d9f4b6c8c-x2k  0/1     CrashLoopBackOff
"""


@pytest.fixture
def fake_holmes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Put a fake `holmes` CLI (and a shadowing `kubectl`) on PATH.

    The fake `holmes` delegates CLI-tool questions to whatever `kubectl`
    resolves to — that is exactly the property under test.
    """
    holmes_shim = """\
#!/usr/bin/env bash
if [ "$1" = "version" ]; then
  echo "holmes 1.2.3"
  exit 0
fi
# `ask`: quote the resolved kubectl and one mutating command into the answer.
kubectl_path="$(command -v kubectl)"
delete_output="$(kubectl delete pod payments-x 2>&1; echo "rc=$?")"
delete_output="$(printf '%s' "$delete_output" | tr '\n' ' ')"
printf '{"root_cause": "crash loop", "evidence": ["kubectl at %s", "delete: %s"], "fix": "bump image"}' \\
  "$kubectl_path" "$delete_output"
"""
    sim_shim = """\
#!/usr/bin/env python3
import sys
# Repo root arrives via PYTHONPATH (set by the test), as the real runner's
# environment effectively provides for the real shims.
from bench.sim.sim_cli import main
sys.exit(main(sys.argv[1:], tool="kubectl"))
"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "holmes").write_text(holmes_shim, encoding="utf-8")
    (bin_dir / "kubectl").write_text(sim_shim, encoding="utf-8")
    for shim in ("holmes", "kubectl"):
        (bin_dir / shim).chmod(0o755)

    commands_file = tmp_path / "_commands.yaml"
    commands_file.write_text(COMMANDS_YAML, encoding="utf-8")

    # Sim first on PATH, exactly the way `bench run` composes the agent env.
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ["PATH"]]))
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("BENCH_SIM_COMMANDS", str(commands_file))
    monkeypatch.setenv("BENCH_SIM_LOG", str(tmp_path / "commands.log"))
    return bin_dir


# --- mapping tests ----------------------------------------------------------


def test_map_json_answer_to_contract():
    """A JSON reply (what the prompt requests) maps straight to the contract."""
    assert holmesgpt_adapter.map_holmes_answer(JSON_ANSWER) == {
        "root_cause": "the probe references an expired secret",
        "evidence": ["kubectl describe: 401 on probe"],
        "fix": "rotate the secret",
    }


def test_map_fenced_json_answer():
    """A reply wrapped in markdown fences still parses."""
    fenced = f"```json\n{JSON_ANSWER}\n```"
    assert holmesgpt_adapter.map_holmes_answer(fenced)["fix"] == "rotate the secret"


def test_map_non_string_evidence_is_coerced():
    """A model that returns evidence as one string still yields a list."""
    answer = holmesgpt_adapter.map_holmes_answer(
        '{"root_cause": "r", "evidence": "one observation", "fix": "f"}'
    )
    assert answer["evidence"] == ["one observation"]


def test_map_prose_answer_lands_in_root_cause():
    """A prose answer keeps its diagnosis instead of being discarded."""
    answer = holmesgpt_adapter.map_holmes_answer("The pod is in CrashLoopBackOff.")
    assert answer["root_cause"] == "The pod is in CrashLoopBackOff."
    assert answer["evidence"] == []
    assert answer["fix"] == ""


def test_map_empty_answer_is_empty_dict():
    """No answer at all degrades to {} — a graded FAIL, never a crash."""
    assert holmesgpt_adapter.map_holmes_answer("") == {}


# --- command construction ---------------------------------------------------


def test_build_command_defaults(monkeypatch: pytest.MonkeyPatch):
    """No config: bare `holmes ask --prompt-file ... --no-interactive`."""
    for var in ("HOLMES_BIN", "HOLMES_MODEL", "HOLMES_ALLOWED_TOOLSETS"):
        monkeypatch.delenv(var, raising=False)
    command = holmesgpt_adapter.build_command("/tmp/prompt.md")
    assert command[:4] == ["holmes", "ask", "--prompt-file", "/tmp/prompt.md"]
    assert "--no-interactive" in command
    assert "--model" not in command
    assert "--allowed-toolsets" not in command


def test_build_command_documents_model_and_toolsets(monkeypatch: pytest.MonkeyPatch):
    """Model and toolset configuration land on the command line."""
    monkeypatch.setenv("HOLMES_BIN", "holmes")
    monkeypatch.setenv("HOLMES_MODEL", "gpt-4o")
    monkeypatch.setenv("HOLMES_ALLOWED_TOOLSETS", "kubernetes,kubectl")
    command = holmesgpt_adapter.build_command("/tmp/prompt.md")
    assert command[command.index("--model") + 1] == "gpt-4o"
    assert command[command.index("--allowed-toolsets") + 1] == "kubernetes,kubectl"


# --- env passthrough --------------------------------------------------------


def test_run_agent_passes_sim_environment_through(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The adapter must not sanitize PATH or drop the sim wiring.

    `bench run` puts the sim first on PATH and points BENCH_SIM_COMMANDS at the
    task's commands file; HolmesGPT's own kubectl/az calls rely on both.
    """
    captured: dict = {}

    def fake_run(command, **kwargs):
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(
        holmesgpt_adapter.subprocess, "run", fake_run
    )
    monkeypatch.setenv("PATH", "/sim-first" + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("BENCH_SIM_COMMANDS", "/task/fixtures/_commands.yaml")

    holmesgpt_adapter.run_agent(tmp_path, "symptom")

    assert captured["env"]["PATH"].startswith("/sim-first")
    assert captured["env"]["BENCH_SIM_COMMANDS"] == "/task/fixtures/_commands.yaml"


# --- end-to-end subprocess run ---------------------------------------------


def _run_adapter_as_script(env: dict[str, str], symptom: str) -> dict:
    proc = subprocess.run(  # noqa: S603 — test harness
        [sys.executable, str(ADAPTER)],
        input=symptom,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_end_to_end_run_uses_simulated_kubectl(fake_holmes: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Full subprocess run: the fake holmes sees the sim's kubectl on PATH."""
    monkeypatch.setenv("TASK_DIR", str(tmp_path))

    answer = _run_adapter_as_script(dict(os.environ), "Storefront returns 502.")

    assert answer["root_cause"] == "crash loop"
    assert answer["fix"] == "bump image"
    # The fake holmes resolved kubectl through the sim shim first on PATH.
    kubectl_path = answer["evidence"][0].replace("kubectl at ", "")
    assert kubectl_path.startswith(str(fake_holmes))


def test_end_to_end_run_mutating_command_refused_by_sim(
    fake_holmes: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """`kubectl delete` still exits 1: the sim blocks mutations (issue #167)."""
    monkeypatch.setenv("TASK_DIR", str(tmp_path))

    answer = _run_adapter_as_script(dict(os.environ), "Storefront returns 502.")

    delete_evidence = next(e for e in answer["evidence"] if e.startswith("delete: "))
    assert "rc=1" in delete_evidence


def test_end_to_end_run_documents_version_and_config(
    fake_holmes: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The result records the holmes version, model, and toolset config."""
    monkeypatch.setenv("TASK_DIR", str(tmp_path))
    monkeypatch.setenv("HOLMES_MODEL", "gpt-4o")
    monkeypatch.setenv("HOLMES_ALLOWED_TOOLSETS", "kubernetes")

    answer = _run_adapter_as_script(dict(os.environ), "Storefront returns 502.")

    assert answer["holmes"] == {
        "version": "holmes 1.2.3",
        "model": "gpt-4o",
        "allowed_toolsets": "kubernetes",
    }


def test_end_to_end_run_missing_holmes_degrades_to_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """No holmes on PATH: {} and exit 0 — a graded FAIL, never a crash."""
    monkeypatch.setenv("TASK_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", "/nonexistent-holmes-bin")
    monkeypatch.setenv("HOLMES_BIN", "holmes-does-not-exist")

    answer = _run_adapter_as_script(dict(os.environ), "Storefront returns 502.")

    assert answer == {}
