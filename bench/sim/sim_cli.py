"""Response engine for the simulated kubectl/az CLIs (issue #111).

`bench/sim/kubectl` and `bench/sim/az` are thin executables that delegate here.
Responses come from a task's `fixtures/_commands.yaml`:

    commands:
      - command: kubectl get pods -n payments
        stdout: |
          NAME                      READY   STATUS
          payments-7d9f4b6c8c-x2k   0/1     CrashLoopBackOff
        stderr: ""          # optional, default ""
        returncode: 0       # optional, default 0

Matching is tolerant so agents can investigate the way they naturally would:

- **Flag order does not matter.** Every token of an entry (`-n payments`, `get`,
  `pods`) must appear in the invocation at least as often; extra tokens are
  allowed. So `kubectl -n payments get pods` matches the entry above.
- **Output formats collapse to one entry.** `-o yaml`, `-o json`, `-o wide`,
  `-o=json`, `-ojson`, `--output yaml` are stripped from both sides; the canned
  `stdout` is returned verbatim regardless of format. Do not define
  format-specific entries — the first match wins.
- **`=` forms normalize.** `-n=payments` is equivalent to `-n payments`.
- **First match wins.** List specific entries before general ones.
- An invocation no entry matches exits 1 with an error on stderr, like a real
  CLI failure. Every invocation is appended to the command log either way.
"""

from __future__ import annotations

import os
import shlex
import sys
from collections import Counter
from pathlib import Path

import yaml

from bench.sim import COMMANDS_ENV, COMMANDS_FILENAME, LOG_ENV, LOG_FILENAME

OUTPUT_FLAGS = ("-o", "--output")


def parse_entries(data: object) -> tuple[list[dict], list[str]]:
    """Validate commands-file shape; return `(entries, errors)`.

    `errors` holds author-readable violations (empty when the file is valid).
    `entries` is the usable command list, populated only when `errors` is empty
    — the shims refuse to serve responses from a partially-valid file. The
    corpus validator (`bench.tasks`) reports the same errors at authoring time.
    """
    if not isinstance(data, dict):
        return [], ["must be a mapping with a 'commands' list"]
    commands = data.get("commands")
    if not isinstance(commands, list) or not commands:
        return [], ["must contain a non-empty 'commands' list"]

    errors: list[str] = []
    for index, entry in enumerate(commands):
        label = f"command #{index}"
        if not isinstance(entry, dict) or not (
            isinstance(entry.get("command"), str) and entry["command"].strip()
        ):
            errors.append(f"{label} must have a non-empty 'command' string")
            continue
        try:
            shlex.split(entry["command"])
        except ValueError as exc:
            errors.append(f"{label} is not shell-parseable ({exc})")
        for key in ("stdout", "stderr"):
            if key in entry and not isinstance(entry[key], str):
                errors.append(f"{label}: '{key}' must be a string")
        returncode = entry.get("returncode", 0)
        if isinstance(returncode, bool) or not isinstance(returncode, int):
            errors.append(f"{label}: 'returncode' must be an integer")
    if errors:
        return [], errors
    return commands, []


def _expand_eq_and_glued(tokens: list[str]) -> list[str]:
    """Normalize `-flag=value` into `-flag value`; drop glued `-o<format>`."""
    expanded: list[str] = []
    for token in tokens:
        if token.startswith("-") and "=" in token:
            flag, value = token.split("=", 1)
            expanded.extend((flag, value))
        elif token.startswith("-o") and not token.startswith("--") and len(token) > 2:
            continue  # -ojson, -owide, -oyaml: glued output format
        else:
            expanded.append(token)
    return expanded


def _tokens_for_matching(tokens: list[str]) -> tuple[list[str], list[str]]:
    """Split tokens into `(flags, positionals)`, dropping output-format flags.

    `-o`/`--output` (with its format value, in any spelling) is stripped from
    both entry and invocation — the format never affects which response is
    served. Order is discarded by the multiset comparison in `_covered`.
    """
    flags: list[str] = []
    positional: list[str] = []
    skip_value = False
    for token in _expand_eq_and_glued(tokens):
        if skip_value:
            skip_value = False
            continue
        if token in OUTPUT_FLAGS:
            skip_value = True  # the format value is dropped together with the flag
            continue
        (flags if token.startswith("-") else positional).append(token)
    return flags, positional


def _covered(needles: list[str], haystack: list[str]) -> bool:
    """Every token in `needles` appears in `haystack` at least as often."""
    return not (Counter(needles) - Counter(haystack))


def match_entry(entries: list[dict], argv: list[str]) -> dict | None:
    """Return the first entry whose tokens the invocation covers, or None."""
    inv_flags, inv_positional = _tokens_for_matching(argv)
    for entry in entries:
        entry_flags, entry_positional = _tokens_for_matching(
            shlex.split(entry["command"])
        )
        if _covered(entry_flags, inv_flags) and _covered(
            entry_positional, inv_positional
        ):
            return entry
    return None


def _commands_path() -> Path | None:
    """Commands file from the runner's env, else `fixtures/_commands.yaml` in cwd."""
    from_env = os.environ.get(COMMANDS_ENV)
    if from_env:
        return Path(from_env)
    return None


def _default_commands_path() -> Path:
    return Path.cwd() / "fixtures" / COMMANDS_FILENAME


def _log_invocation(rendered: str) -> None:
    """Append one invocation line to the command log (env path, else cwd)."""
    path = Path(os.environ.get(LOG_ENV) or LOG_FILENAME)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(rendered + "\n")
    except OSError as exc:
        # Never fail the simulated command over telemetry; surface it instead.
        print(f"sim: warning: could not append to {path}: {exc}", file=sys.stderr)


def main(argv: list[str], tool: str = "") -> int:
    """Shim entry point: log the invocation, serve the first matching entry.

    Entries are authored as full command lines starting with the tool name, so
    the wrapper passes its own name and it is prepended before matching and
    logging (`kubectl get pods -n x`, never `get pods -n x`).
    """
    full_argv = [tool, *argv] if tool else argv
    rendered = " ".join(shlex.quote(token) for token in full_argv)
    _log_invocation(rendered)
    try:
        path = _commands_path() or _default_commands_path()
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        entries, errors = parse_entries(data)
        if errors:
            raise ValueError(f"invalid {path}: {'; '.join(errors)}")
    except (OSError, yaml.YAMLError, ValueError) as exc:
        print(f"sim: {exc}", file=sys.stderr)
        return 2

    entry = match_entry(entries, full_argv)
    if entry is None:
        print(f"sim: no simulated response for: {rendered}", file=sys.stderr)
        return 1
    sys.stdout.write(entry.get("stdout", ""))
    stderr = entry.get("stderr", "")
    if stderr:
        if not stderr.endswith("\n"):
            stderr += "\n"
        sys.stderr.write(stderr)
    returncode = entry.get("returncode", 0)
    assert isinstance(returncode, int)  # parse_entries guarantees int, not bool
    return returncode
