#!/usr/bin/env python3
"""HolmesGPT adapter — benchmark Robusta's open-source SRE agent (issue #167).

HolmesGPT (`holmes`) investigates incidents by driving real CLIs — including
`kubectl` and `az` — through its toolsets. The adapter runs `holmes ask` as a
subprocess and **deliberately passes the current environment through
unchanged**: when `bench run` executes a task that ships
`fixtures/_commands.yaml`, the runner has already put the simulated CLIs
(`bench/sim`) first on PATH, so every `kubectl`/`az` call HolmesGPT makes
resolves to the shims and mutating commands are refused by the sim (issue
#111). The adapter must not sanitize or reshape PATH, or it would break that
guarantee.

Configuration is env-only, mirroring `llm_agent.py`:

    HOLMES_BIN                holmes executable (default `holmes`)
    HOLMES_MODEL              passed as `--model` (e.g. an Azure OpenAI deployment)
    HOLMES_ALLOWED_TOOLSETS   passed as `--allowed-toolsets` (comma-separated);
                              unset lets HolmesGPT use its default toolsets
    HOLMES_TIMEOUT_S          subprocess timeout (default 540; the runner's
                              `--timeout` is the outer bound)

The symptom (fed on stdin by the runner, or `symptom.md` when run by hand) is
passed as the investigation prompt via `--prompt-file`, and the answer is
mapped into the contract JSON. On any failure: `{}` and exit 0 — the runner
grades that as a FAIL, never a crash.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):  # running as a script: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agents.llm_agent import extract_answer, read_truncated  # noqa: E402

DEFAULT_HOLMES_BIN = "holmes"
DEFAULT_TIMEOUT_S = 540

# The symptom is the investigation; the JSON tail keeps the mapping honest
# even when the model prefers prose (see map_holmes_answer).
PROMPT_TEMPLATE = """\
Investigate this incident using the available tools.

{symptom}

Respond with ONLY a JSON object, no prose, in exactly this shape:
{{"root_cause": "...", "evidence": ["tool output / file: observation", "..."], "fix": "..."}}

- root_cause: the underlying cause in one paragraph.
- evidence: specific tool output or fields that support the diagnosis.
- fix: the concrete remediation."""


def holmes_config() -> dict:
    """The documented model/toolset configuration from the environment."""
    return {
        "bin": os.environ.get("HOLMES_BIN", DEFAULT_HOLMES_BIN),
        "model": os.environ.get("HOLMES_MODEL") or "(default)",
        "allowed_toolsets": os.environ.get("HOLMES_ALLOWED_TOOLSETS") or "(default)",
    }


def build_command(prompt_file: str) -> list[str]:
    """The `holmes ask` invocation for one investigation."""
    command = [
        os.environ.get("HOLMES_BIN", DEFAULT_HOLMES_BIN),
        "ask",
        "--prompt-file",
        prompt_file,
        "--no-interactive",
    ]
    model = os.environ.get("HOLMES_MODEL")
    if model:
        command += ["--model", model]
    toolsets = os.environ.get("HOLMES_ALLOWED_TOOLSETS")
    if toolsets:
        command += ["--allowed-toolsets", toolsets]
    return command


def holmes_version(bin_name: str) -> str | None:
    """Best-effort `holmes version`; None when the CLI does not support it."""
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


def map_holmes_answer(text: str) -> dict:
    """Map HolmesGPT's answer into the contract dict.

    A JSON reply (the prompt requests one) normalizes directly. A prose reply
    still carries the diagnosis, so it maps to `root_cause` with empty
    evidence/fix — the grader scores whatever keyword groups hit, which beats
    discarding the run. An unparseable empty answer yields `{}`.
    """
    stripped = text.strip()
    if not stripped:
        return {}
    parsed = extract_answer(stripped)
    if isinstance(parsed, dict) and parsed:
        answer = {
            "root_cause": str(parsed.get("root_cause", "")),
            "evidence": parsed.get("evidence", []),
            "fix": str(parsed.get("fix", "")),
        }
        if not isinstance(answer["evidence"], list) or not all(
            isinstance(item, str) for item in answer["evidence"]
        ):
            answer["evidence"] = [str(answer["evidence"])]
        return answer
    return {"root_cause": stripped, "evidence": [], "fix": ""}


def run_agent(task_dir: Path, symptom: str) -> dict:
    """Run one HolmesGPT investigation and return the contract dict."""
    config = holmes_config()
    with tempfile.NamedTemporaryFile(
        "w", suffix=".md", prefix="holmes-prompt-", encoding="utf-8"
    ) as prompt_file:
        prompt_file.write(PROMPT_TEMPLATE.format(symptom=symptom))
        prompt_file.flush()
        command = build_command(prompt_file.name)
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
                command,
                # Explicit: HolmesGPT's kubectl/az calls must resolve to the
                # sim shims (PATH first) with the sim wiring intact — never a
                # sanitized environment (issue #111).
                env=dict(os.environ),
                capture_output=True,
                text=True,
                timeout=int(os.environ.get("HOLMES_TIMEOUT_S", DEFAULT_TIMEOUT_S)),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        answer = map_holmes_answer(proc.stdout)
    if answer:
        # Document the run's version and configuration in the result record.
        answer["holmes"] = {
            "version": holmes_version(config["bin"]),
            "model": config["model"],
            "allowed_toolsets": config["allowed_toolsets"],
        }
    return answer


def read_symptom(task_dir: Path) -> str:
    """The symptom from stdin (the runner pipes it), else `symptom.md`."""
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if data.strip():
            return data
    return read_truncated(Path(task_dir) / "symptom.md") or "(no symptom provided)"


def main() -> int:
    task_dir = Path(os.environ.get("TASK_DIR", "."))
    try:
        answer = run_agent(task_dir, read_symptom(task_dir))
    except Exception:  # noqa: BLE001 — any adapter failure must degrade to a graded FAIL
        answer = {}
    print(json.dumps(answer))
    return 0


if __name__ == "__main__":
    sys.exit(main())
