"""Tests for the simulated kubectl/az CLIs (issue #111).

Covers the matcher pure functions (flag order, output-format variants, `=`
forms, first-match-wins) and the real shim executables end to end through
subprocess, including the command log and the error paths an agent would see.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from bench.sim import COMMANDS_ENV, LOG_ENV, SIM_DIR
from bench.sim.sim_cli import match_entry, parse_entries

KUBECTL = SIM_DIR / "kubectl"
AZ = SIM_DIR / "az"

PODS_ENTRY = {
    "command": "kubectl get pods -n payments",
    "stdout": "NAME                      READY   STATUS\n"
    "payments-7d9f4b6c8c-x2k   0/1     CrashLoopBackOff\n",
}


def write_commands(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "_commands.yaml"
    path.write_text(
        yaml.safe_dump({"commands": entries}, sort_keys=False), encoding="utf-8"
    )
    return path


def entry_for(tmp_path: Path, entries: list[dict], argv: list[str]) -> str:
    """Write a commands file and return the entry matched for `argv` (or '')."""
    commands = write_commands(tmp_path, entries)
    loaded, errors = parse_entries(yaml.safe_load(commands.read_text()))
    assert errors == []
    matched = match_entry(loaded, argv)
    return str(matched["command"]) if matched else ""


# --- matcher: flag-order tolerance ------------------------------------------


def test_canonical_order_matches(tmp_path):
    argv = ["kubectl", "get", "pods", "-n", "payments"]
    assert entry_for(tmp_path, [PODS_ENTRY], argv) == PODS_ENTRY["command"]


def test_flags_moved_before_subcommand_still_match(tmp_path):
    argv = ["kubectl", "-n", "payments", "get", "pods"]
    assert entry_for(tmp_path, [PODS_ENTRY], argv) == PODS_ENTRY["command"]


def test_flag_value_must_match(tmp_path):
    argv = ["kubectl", "get", "pods", "-n", "other"]
    assert entry_for(tmp_path, [PODS_ENTRY], argv) == ""


def test_positional_word_must_match(tmp_path):
    argv = ["kubectl", "get", "deployments"]
    assert entry_for(tmp_path, [PODS_ENTRY], argv) == ""


def test_extra_flags_are_tolerated(tmp_path):
    argv = ["kubectl", "get", "pods", "-n", "payments", "--kubeconfig", "/tmp/k"]
    assert entry_for(tmp_path, [PODS_ENTRY], argv) == PODS_ENTRY["command"]


def test_multi_token_flag_value_is_order_tolerant(tmp_path):
    entry = {"command": "kubectl get events --sort-by .lastTimestamp", "stdout": "e\n"}
    argv = ["kubectl", "--sort-by", ".lastTimestamp", "get", "events"]
    assert entry_for(tmp_path, [entry], argv) == entry["command"]


# --- matcher: -o yaml|json|wide variants ------------------------------------


def test_output_format_variants_match_one_entry(tmp_path):
    entry = {"command": "kubectl get pods", "stdout": "canned\n"}
    for argv in (
        ["kubectl", "get", "pods", "-o", "yaml"],
        ["kubectl", "get", "pods", "-o", "json"],
        ["kubectl", "get", "pods", "-o", "wide"],
        ["kubectl", "get", "pods", "-o=json"],
        ["kubectl", "get", "pods", "-ojson"],
        ["kubectl", "get", "pods", "--output", "yaml"],
        ["kubectl", "get", "pods", "--output=json"],
        ["kubectl", "-o", "wide", "get", "pods"],
    ):
        assert entry_for(tmp_path, [entry], argv) == "kubectl get pods", argv


def test_output_format_in_entry_is_stripped_too(tmp_path):
    """A format spelled on the entry side never blocks matching, either."""
    entry = {"command": "kubectl get pod web-0 -o wide", "stdout": "wide\n"}
    argv = ["kubectl", "get", "pod", "web-0", "-o", "yaml"]
    assert entry_for(tmp_path, [entry], argv) == "kubectl get pod web-0 -o wide"


# --- matcher: normalization and ordering ------------------------------------


def test_eq_forms_normalize_for_any_flag(tmp_path):
    argv = ["kubectl", "get", "pods", "-n=payments"]
    assert entry_for(tmp_path, [PODS_ENTRY], argv) == PODS_ENTRY["command"]


def test_first_matching_entry_wins(tmp_path):
    general = {"command": "kubectl get pods", "stdout": "general\n"}
    specific = {"command": "kubectl get pods -n payments", "stdout": "specific\n"}
    argv = ["kubectl", "get", "pods", "-n", "payments"]
    # Both entries cover this invocation, so list order alone decides.
    assert entry_for(tmp_path, [general, specific], argv) == "kubectl get pods"
    assert entry_for(tmp_path, [specific, general], argv) == "kubectl get pods -n payments"


# --- commands-file shape ----------------------------------------------------


def test_parse_entries_rejects_non_mapping():
    assert parse_entries(["commands"]) == ([], ["must be a mapping with a 'commands' list"])
    assert parse_entries(None) == ([], ["must be a mapping with a 'commands' list"])


def test_parse_entries_rejects_empty_or_missing_commands_list():
    assert parse_entries({}) == ([], ["must contain a non-empty 'commands' list"])
    assert parse_entries({"commands": []}) == (
        [],
        ["must contain a non-empty 'commands' list"],
    )


def test_parse_entries_collects_all_violations():
    entries, errors = parse_entries(
        {
            "commands": [
                {"stdout": "no command key"},
                {"command": "kubectl get pods", "returncode": "zero"},
                {"command": "kubectl get pods", "stdout": 404},
            ]
        }
    )
    assert entries == []
    assert len(errors) == 3


def test_parse_entries_rejects_bool_returncode():
    _, errors = parse_entries(
        {"commands": [{"command": "kubectl get pods", "returncode": True}]}
    )
    assert errors == ["command #0: 'returncode' must be an integer"]


def test_parse_entries_rejects_unparseable_command():
    _, errors = parse_entries({"commands": [{"command": "kubectl get 'pods"}]})
    assert errors == ["command #0 is not shell-parseable (No closing quotation)"]


def test_parse_entries_accepts_minimal_entry():
    entries, errors = parse_entries({"commands": [{"command": "az group show -g rg"}]})
    assert errors == []
    assert entries == [{"command": "az group show -g rg"}]


# --- shim executables end to end --------------------------------------------


def run_shim(
    shim: Path, args: list[str], env_overrides: dict[str, str]
) -> subprocess.CompletedProcess:
    env = dict(os.environ, **env_overrides)
    return subprocess.run(
        [str(shim), *args], env=env, capture_output=True, text=True, timeout=30, check=False
    )


def test_kubectl_shim_serves_canned_stdout(tmp_path):
    commands = write_commands(tmp_path, [PODS_ENTRY])
    log = tmp_path / "commands.log"
    proc = run_shim(
        KUBECTL,
        ["-n", "payments", "get", "pods", "-o", "json"],
        {COMMANDS_ENV: str(commands), LOG_ENV: str(log)},
    )
    assert proc.returncode == 0
    assert proc.stdout == PODS_ENTRY["stdout"]
    assert proc.stderr == ""


def test_az_shim_serves_canned_response(tmp_path):
    commands = write_commands(
        tmp_path,
        [{"command": "az network nsg list -g rg --query length", "stdout": "3\n"}],
    )
    log = tmp_path / "commands.log"
    proc = run_shim(
        AZ,
        ["network", "nsg", "list", "-g", "rg", "--query", "length"],
        {COMMANDS_ENV: str(commands), LOG_ENV: str(log)},
    )
    assert proc.returncode == 0
    assert proc.stdout == "3\n"


def test_shim_serves_stderr_and_nonzero_returncode(tmp_path):
    commands = write_commands(
        tmp_path,
        [
            {
                "command": "kubectl logs pod",
                "stdout": "partial\n",
                "stderr": "boom",
                "returncode": 1,
            }
        ],
    )
    proc = run_shim(
        KUBECTL,
        ["logs", "pod"],
        {COMMANDS_ENV: str(commands), LOG_ENV: str(tmp_path / "l.log")},
    )
    assert proc.returncode == 1
    assert proc.stdout == "partial\n"
    assert proc.stderr == "boom\n"


def test_unmatched_invocation_exits_1_and_logs(tmp_path):
    commands = write_commands(tmp_path, [PODS_ENTRY])
    log = tmp_path / "commands.log"
    proc = run_shim(
        KUBECTL,
        ["get", "deployments"],
        {COMMANDS_ENV: str(commands), LOG_ENV: str(log)},
    )
    assert proc.returncode == 1
    assert "no simulated response" in proc.stderr
    assert log.read_text() == "kubectl get deployments\n"


def test_missing_config_exits_2(tmp_path):
    log = tmp_path / "commands.log"
    proc = run_shim(KUBECTL, ["get", "pods"], {COMMANDS_ENV: "", LOG_ENV: str(log)})
    assert proc.returncode == 2
    assert "_commands.yaml" in proc.stderr
    assert log.read_text() == "kubectl get pods\n"


def test_invalid_commands_file_exits_2(tmp_path):
    commands = write_commands(tmp_path, [{"stdout": "no command key"}])
    proc = run_shim(
        KUBECTL,
        ["get", "pods"],
        {COMMANDS_ENV: str(commands), LOG_ENV: str(tmp_path / "l.log")},
    )
    assert proc.returncode == 2
    assert "command #0 must have a non-empty 'command' string" in proc.stderr


def test_invocations_append_to_one_log_across_shims(tmp_path):
    commands = write_commands(tmp_path, [PODS_ENTRY])
    log = tmp_path / "commands.log"
    shared_env = {COMMANDS_ENV: str(commands), LOG_ENV: str(log)}
    run_shim(AZ, ["version"], shared_env)
    run_shim(KUBECTL, ["get", "pods"], shared_env)
    run_shim(KUBECTL, ["get", "pods"], shared_env)
    assert log.read_text() == "az version\nkubectl get pods\nkubectl get pods\n"
