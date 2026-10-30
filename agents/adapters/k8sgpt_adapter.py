#!/usr/bin/env python3
"""k8sgpt adapter — run k8sgpt against a live cluster (issue #168, live-only).

k8sgpt analyzes a **real** API server, so this adapter only runs on live-mode
scenarios (issue #126): export `KUBECONFIG` to the scenario's read-only agent
kubeconfig (e.g. `live/scenarios/service-selector-mismatch/agent.kubeconfig`)
before running. Without `KUBECONFIG` the adapter refuses: it prints `{}` and
exits 0 — a graded FAIL, never a crash or, worse, a scan of an unintended
default cluster.

The scan runs `k8sgpt analyze --explain --output json` and maps the findings
into the contract JSON. k8sgpt's `error` field has changed shape across
releases (a plain string, or a list of `{"text": ...}` objects); the mapping
tolerates both. On any failure: `{}` and exit 0.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

DEFAULT_K8SGPT_BIN = "k8sgpt"
DEFAULT_TIMEOUT_S = 300


def build_command() -> list[str]:
    """The `k8sgpt analyze` invocation; cluster access comes from KUBECONFIG."""
    return [
        os.environ.get("K8SGPT_BIN", DEFAULT_K8SGPT_BIN),
        "analyze",
        "--explain",
        "--output",
        "json",
    ]


def k8sgpt_version(bin_name: str) -> str | None:
    """Best-effort `k8sgpt version`; None when the CLI does not support it."""
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [bin_name, "version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    version = proc.stdout.strip()
    return version or None


def _result_error(result: dict) -> str:
    """The finding's error text, across k8sgpt's output shapes.

    Older releases report a list of `{"text": ...}` objects; newer ones a
    plain string. Anything else collapses to "" so the finding still shows
    up by kind/name.
    """
    error = result.get("error")
    if isinstance(error, str):
        return error.strip()
    if isinstance(error, list):
        parts = [
            str(item.get("text", "")).strip()
            for item in error
            if isinstance(item, dict)
        ]
        return " ".join(part for part in parts if part)
    return ""


def _explanation(result: dict) -> str:
    """The explained remediation: instruction, then reason, then details."""
    for field in ("instruction", "reason", "details"):
        text = result.get(field)
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def map_findings(analysis: object) -> dict:
    """Map k8sgpt's JSON analysis into the contract dict.

    Each finding becomes one evidence line (`kind/name: error`) and one
    root-cause clause; the explained `instruction` fields become the fix. An
    analysis with no findings — a healthy cluster, or anything unparseable —
    yields `{}`.
    """
    results = analysis.get("results") if isinstance(analysis, dict) else None
    if not isinstance(results, list) or not results:
        return {}

    evidence: list[str] = []
    causes: list[str] = []
    fixes: list[str] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        label = "/".join(
            str(result.get(field, "")).strip() for field in ("kind", "name")
        )
        error_text = _result_error(result)
        evidence.append(f"{label}: {error_text}" if error_text else label)
        if error_text:
            causes.append(f"{label} — {error_text}")
        explanation = _explanation(result)
        if explanation:
            fixes.append(f"{label}: {explanation}")

    if not causes:
        return {}
    return {
        "root_cause": "k8sgpt findings: " + "; ".join(causes) + ".",
        "evidence": evidence,
        "fix": " | ".join(fixes),
    }


def run_agent() -> dict:
    """Run one live-cluster scan and return the contract dict."""
    if not os.environ.get("KUBECONFIG"):
        # Live-only by design (issue #168): never fall back to a default
        # kubeconfig — an unintended cluster is worse than a graded FAIL.
        print(
            "k8sgpt adapter is live-only: set KUBECONFIG to the scenario's "
            "read-only agent kubeconfig (live/scenarios/<name>/agent.kubeconfig).",
            file=sys.stderr,
        )
        return {}

    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            build_command(),
            env=dict(os.environ),  # KUBECONFIG reaches k8sgpt untouched
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("K8SGPT_TIMEOUT_S", DEFAULT_TIMEOUT_S)),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    try:
        analysis = json.loads(proc.stdout)
    except ValueError:
        return {}
    answer = map_findings(analysis)
    if answer:
        # Document the scan in the result record.
        answer["k8sgpt"] = {
            "version": k8sgpt_version(
                os.environ.get("K8SGPT_BIN", DEFAULT_K8SGPT_BIN)
            ),
            "command": "analyze --explain --output json",
            "live_only": True,
        }
    return answer


def main() -> int:
    try:
        answer = run_agent()
    except Exception:  # noqa: BLE001 — any adapter failure must degrade to a graded FAIL
        answer = {}
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
