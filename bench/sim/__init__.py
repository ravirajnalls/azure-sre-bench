"""Simulated kubectl/az CLIs for agent investigation (issue #111).

When a task ships `fixtures/_commands.yaml`, the runner puts this directory
first on the agent's PATH, so `kubectl` and `az` invocations resolve to the
shims here and return canned responses instead of hitting a real cluster.
Shared constants live in this package; the engine is `sim_cli.py`.
"""

from __future__ import annotations

from pathlib import Path

# Reserved fixture filename: configures the simulated CLIs, is not agent-visible
# evidence, and is exempt from the fixture count and GUID rules (bench.tasks).
COMMANDS_FILENAME = "_commands.yaml"

# Runner -> shim wiring: where the commands file lives and where invocations
# are appended (the runner captures that log into the result record).
COMMANDS_ENV = "BENCH_SIM_COMMANDS"
LOG_ENV = "BENCH_SIM_LOG"
LOG_FILENAME = "commands.log"

# This directory — placed on PATH so `kubectl`/`az` resolve to the shims.
SIM_DIR = Path(__file__).resolve().parent
