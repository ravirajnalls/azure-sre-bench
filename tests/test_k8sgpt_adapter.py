"""Tests for the k8sgpt adapter (issue #168).

No real k8sgpt install and no cluster: the `k8sgpt` CLI is a scripted fake
executable on PATH, so the end-to-end subprocess run (the way `bench run`
invokes agents) exercises the adapter deterministically. The fake embeds its
own argv into the finding text, proving the adapter invokes
`k8sgpt analyze --explain --output json` against the live scenario's
kubeconfig.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agents.adapters import k8sgpt_adapter

REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTER = REPO_ROOT / "agents" / "adapters" / "k8sgpt_adapter.py"

# The service-selector-mismatch live scenario, as k8sgpt (with --explain)
# would report it: `error` as the list-of-{"text"} shape older releases use.
SELECTOR_MISMATCH_ANALYSIS = {
    "results": [
        {
            "kind": "Service",
            "name": "shop/storefront",
            "error": [{"text": "Service has no endpoints"}],
            "details": "Service selector matches zero pods",
            "instruction": "Set the selector labels to match the storefront pods",
            "reason": "The selector references tier: web but pods use tier: frontend",
        },
        {
            "kind": "Pod",
            "name": "shop/cart-6d8f9b4c7d-wz2kp",
            "error": [{"text": "Pod is running normally"}],
        },
    ]
}


@pytest.fixture
def fake_k8sgpt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Put a fake `k8sgpt` CLI on PATH that echoes its argv into the finding."""
    shim = """\
#!/usr/bin/env bash
if [ "$1" = "version" ]; then
  echo "0.3.8"
  exit 0
fi
python3 - "$@" <<'PY'
import json, sys
argv = " ".join(sys.argv[1:])
data = {
    "results": [
        {
            "kind": "Service",
            "name": "shop/storefront",
            "error": [{"text": "Service has no endpoints (argv: %s)" % argv}],
            "instruction": "Set the selector labels to match the storefront pods",
        }
    ]
}
print(json.dumps(data))
PY
"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "k8sgpt").write_text(shim, encoding="utf-8")
    (bin_dir / "k8sgpt").chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ["PATH"]]))
    return bin_dir


# --- mapping tests ----------------------------------------------------------


def test_map_findings_with_list_error_and_explain_fields():
    """List-of-{"text"} errors and instruction/reason/details all map."""
    answer = k8sgpt_adapter.map_findings(SELECTOR_MISMATCH_ANALYSIS)

    assert answer["root_cause"] == (
        "k8sgpt findings: Service/shop/storefront — Service has no endpoints; "
        "Pod/shop/cart-6d8f9b4c7d-wz2kp — Pod is running normally."
    )
    assert answer["evidence"] == [
        "Service/shop/storefront: Service has no endpoints",
        "Pod/shop/cart-6d8f9b4c7d-wz2kp: Pod is running normally",
    ]
    assert answer["fix"] == (
        "Service/shop/storefront: Set the selector labels to match the storefront pods"
    )


def test_map_findings_with_plain_string_error():
    """Newer releases report `error` as a plain string."""
    analysis = {
        "results": [
            {
                "kind": "Deployment",
                "name": "web/storefront",
                "error": "Replicas: 0/1 available",
                "instruction": "Scale the deployment",
            }
        ]
    }
    answer = k8sgpt_adapter.map_findings(analysis)
    assert answer["evidence"] == ["Deployment/web/storefront: Replicas: 0/1 available"]
    assert answer["fix"] == "Deployment/web/storefront: Scale the deployment"


def test_map_findings_falls_back_to_reason_then_details():
    """Without `instruction`, the fix prefers `reason`, then `details`."""
    analysis = {
        "results": [
            {"kind": "Pod", "name": "p", "error": "x", "reason": "because"},
            {"kind": "Pod", "name": "q", "error": "y", "details": "what happened"},
        ]
    }
    answer = k8sgpt_adapter.map_findings(analysis)
    assert answer["fix"] == "Pod/p: because | Pod/q: what happened"


def test_map_findings_without_explanation_omits_empty_fix():
    """A finding with no explain fields still surfaces its error, no fix."""
    analysis = {"results": [{"kind": "Pod", "name": "p", "error": "CrashLoopBackOff"}]}
    answer = k8sgpt_adapter.map_findings(analysis)
    assert answer["root_cause"] == "k8sgpt findings: Pod/p — CrashLoopBackOff."
    assert answer["fix"] == ""


def test_map_findings_with_no_results_is_empty_dict():
    """A healthy cluster (or unparseable analysis) yields {}."""
    assert k8sgpt_adapter.map_findings({"results": []}) == {}
    assert k8sgpt_adapter.map_findings({}) == {}
    assert k8sgpt_adapter.map_findings("not json") == {}


# --- live-only refusal -------------------------------------------------------


def test_run_agent_refuses_without_kubeconfig(monkeypatch: pytest.MonkeyPatch, capsys):
    """No KUBECONFIG: refuse — a graded FAIL, never a default-cluster scan."""
    monkeypatch.delenv("KUBECONFIG", raising=False)

    assert k8sgpt_adapter.run_agent() == {}
    assert "live-only" in capsys.readouterr().err


def test_run_agent_does_not_run_k8sgpt_without_kubeconfig(monkeypatch: pytest.MonkeyPatch):
    """The refusal happens before any subprocess is spawned."""
    monkeypatch.delenv("KUBECONFIG", raising=False)

    def forbidden_run(*args, **kwargs):  # pragma: no cover — must never be called
        raise AssertionError("k8sgpt must not run without an explicit KUBECONFIG")

    monkeypatch.setattr(k8sgpt_adapter.subprocess, "run", forbidden_run)
    assert k8sgpt_adapter.run_agent() == {}


# --- end-to-end subprocess run ---------------------------------------------


def _run_adapter_as_script(env: dict[str, str]) -> tuple[dict, str]:
    proc = subprocess.run(  # noqa: S603 — test harness
        [sys.executable, str(ADAPTER)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout), proc.stderr


def test_end_to_end_run_against_live_scenario_kubeconfig(
    fake_k8sgpt: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Full subprocess run: analyze --explain --output json under KUBECONFIG."""
    kubeconfig = tmp_path / "agent.kubeconfig"
    kubeconfig.write_text("# fake read-only kubeconfig", encoding="utf-8")
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))

    answer, _stderr = _run_adapter_as_script(dict(os.environ))

    # The finding quotes the exact argv the adapter used.
    assert "--explain" in answer["evidence"][0]
    assert "--output json" in answer["evidence"][0]
    assert answer["fix"] == (
        "Service/shop/storefront: Set the selector labels to match the storefront pods"
    )
    # The scan is documented in the result record.
    assert answer["k8sgpt"]["version"] == "0.3.8"
    assert answer["k8sgpt"]["live_only"] is True


def test_end_to_end_run_refuses_without_kubeconfig(fake_k8sgpt: Path, monkeypatch: pytest.MonkeyPatch):
    """Without KUBECONFIG the e2e run prints {} and exits 0."""
    monkeypatch.delenv("KUBECONFIG", raising=False)

    answer, stderr = _run_adapter_as_script(dict(os.environ))

    assert answer == {}
    assert "live-only" in stderr


def test_end_to_end_run_missing_k8sgpt_degrades_to_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """No k8sgpt on PATH: {} and exit 0 — a graded FAIL, never a crash."""
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "agent.kubeconfig"))
    monkeypatch.setenv("K8SGPT_BIN", "k8sgpt-does-not-exist")

    answer, _stderr = _run_adapter_as_script(dict(os.environ))

    assert answer == {}


def test_end_to_end_run_garbage_output_degrades_to_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Non-JSON k8sgpt output degrades to {} instead of raising."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "k8sgpt"
    shim.write_text("#!/usr/bin/env bash\necho 'not json'\n", encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ["PATH"]]))
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "agent.kubeconfig"))

    answer, _stderr = _run_adapter_as_script(dict(os.environ))

    assert answer == {}
