"""Baseline sweep harness tests (issue #125).

Fully mocked: every "agent" is a throwaway script (the test_run.py house
pattern) and every run targets a synthetic task under tests/data/ — no real
API call is possible, let alone made.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

from bench.sweep import (
    DEFAULT_TIMEOUT_S,
    REPORT_SUFFIX,
    cmd_sweep,
    load_sweep_config,
    run_sweep,
    selected_task_dirs,
)
from bench.verify import verify_run

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_TASKS_ROOT = REPO_ROOT / "tests" / "data"
PASSING_AGENT = (
    "import json\n"
    'print(json.dumps({"root_cause": "Expired connection string secret in the '
    'probe deployment", "evidence": ["app-insights-query.kql: 401 spike"], '
    '"fix": "rotate the secret"}))\n'
)
FAILING_AGENT = "import json\nprint(json.dumps({}))\n"
# monitoring-001's rubric (dir name == task id, so `bench verify` can find it).
PASSING_AGENT_INGRESS = (
    "import json\n"
    'print(json.dumps({"root_cause": "Ingress saturation during peak traffic", '
    '"evidence": [], "fix": "scale the ingress controller"}))\n'
)
REPORTING_AGENT = (
    "import json, os\n"
    "print(json.dumps({\n"
    '    "root_cause": "", "evidence": [], "fix": "",\n'
    '    "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01},\n'
    "}))\n"
)


def write_agent(tmp_path: Path, body: str, name: str = "agent.py") -> str:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return f"{sys.executable} {script}"


def make_config(tmp_path: Path, agents: list[dict], **sweep_extra) -> Path:
    # Default scope: only the synthetic runner task — tests/data also holds
    # adversarial-fixture tasks we do not want a sweep to grade.
    sweep = {
        "name": "test-sweep",
        "tasks": ["run-test-001"],
        "agents": agents,
        **sweep_extra,
    }
    path = tmp_path / "sweep.yaml"
    path.write_text(
        json.dumps({"sweep": sweep}), encoding="utf-8"
    )  # YAML is a JSON superset
    return path


def test_end_to_end_mocked(tmp_path):
    """Two agents run to completion; folders, agent.yaml, and report are written."""
    config = make_config(
        tmp_path,
        [
            {
                "name": "mock-pass",
                "command": write_agent(tmp_path, PASSING_AGENT, "passing.py"),
            },
            {
                "name": "mock-fail",
                "command": write_agent(tmp_path, FAILING_AGENT, "failing.py"),
            },
        ],
    )
    results_root = tmp_path / "results"
    report = run_sweep(
        load_sweep_config(config),
        tasks_root=TEST_TASKS_ROOT,
        results_root=results_root,
        timeout=30,
    )

    assert report["sweep"] == "test-sweep"
    assert report["tasks"] == 1  # the synthetic run-test-001 task
    assert report["adversarial_clean"] is True  # no adversarial entries
    by_name = {entry["name"]: entry for entry in report["agents"]}
    assert by_name["mock-pass"]["status"] == "ran"
    assert by_name["mock-pass"]["passed"] == 1
    assert by_name["mock-fail"]["passed"] == 0

    assert (results_root / f"test-sweep{REPORT_SUFFIX}").is_file()
    for name in ("mock-pass", "mock-fail"):
        run_dir = results_root / name
        assert (run_dir / "agent.yaml").is_file()
        records = list(run_dir.glob("*.json"))
        assert records, f"no result file in {run_dir}"


def test_sweep_run_folder_passes_verify(tmp_path):
    """A sweep-produced run folder is a valid issue-#118 submission shape."""
    config = make_config(
        tmp_path,
        [{"name": "mock-swept", "command": write_agent(tmp_path, PASSING_AGENT_INGRESS)}],
        tasks=["monitoring-001"],
    )
    results_root = tmp_path / "results"
    run_sweep(
        load_sweep_config(config),
        tasks_root=TEST_TASKS_ROOT,
        results_root=results_root,
        timeout=30,
    )
    report = verify_run(results_root / "mock-swept", TEST_TASKS_ROOT)
    assert report["errors"] == []
    assert report["mismatches"] == []
    assert report["checked"] == 1


def test_missing_credentials_skip_not_crash(tmp_path, monkeypatch):
    """requires_env entries are skipped with a recorded reason when unset."""
    monkeypatch.delenv("SWEEP_TEST_CRED", raising=False)
    config = make_config(
        tmp_path,
        [
            {
                "name": "cred-agent",
                "command": write_agent(tmp_path, PASSING_AGENT),
                "requires_env": ["SWEEP_TEST_CRED"],
            }
        ],
    )
    results_root = tmp_path / "results"
    report = run_sweep(
        load_sweep_config(config),
        tasks_root=TEST_TASKS_ROOT,
        results_root=results_root,
    )
    entry = report["agents"][0]
    assert entry["status"] == "skipped"
    assert "SWEEP_TEST_CRED" in entry["reason"]
    assert not (results_root / "cred-agent").exists()
    assert (results_root / f"test-sweep{REPORT_SUFFIX}").is_file()


def test_requires_env_set_runs_normally(tmp_path, monkeypatch):
    monkeypatch.setenv("SWEEP_TEST_CRED", "yes")
    config = make_config(
        tmp_path,
        [
            {
                "name": "cred-agent-runs",
                "command": write_agent(tmp_path, PASSING_AGENT),
                "requires_env": ["SWEEP_TEST_CRED"],
            }
        ],
    )
    report = run_sweep(
        load_sweep_config(config),
        tasks_root=TEST_TASKS_ROOT,
        results_root=tmp_path / "results",
        timeout=30,
    )
    assert report["agents"][0]["status"] == "ran"


def test_env_pinned_and_restored(tmp_path, monkeypatch):
    """Entry `env` reaches the agent process and is restored after its run."""
    monkeypatch.delenv("SWEEP_MODEL", raising=False)
    probing_agent = (
        "import json, os\n"
        'print("model=" + os.environ.get("SWEEP_MODEL", ""))\n'
        "print(json.dumps({}))\n"
    )
    # A second agent that must NOT see the pinned variable afterwards.
    checker = write_agent(
        tmp_path,
        "import json, os, sys\n"
        'val = os.environ.get("SWEEP_MODEL", "")\n'
        "print(json.dumps({}))\n"
        'sys.exit(0 if val == "" else 3)\n',
        name="checker.py",
    )
    config = make_config(
        tmp_path,
        [
            {
                "name": "pinned",
                "command": write_agent(tmp_path, probing_agent, "pinned.py"),
                "env": {"SWEEP_MODEL": "test-model-7b"},
            },
            {"name": "checker", "command": checker},
        ],
    )
    results_root = tmp_path / "results"
    run_sweep(
        load_sweep_config(config),
        tasks_root=TEST_TASKS_ROOT,
        results_root=results_root,
        timeout=30,
    )
    pinned_record = json.loads(
        (results_root / "pinned" / "monitoring-901.json").read_text(encoding="utf-8")
    )
    assert pinned_record["timed_out"] is False
    # The checker exits 3 when the pinned variable leaked into its environment;
    # a non-zero returncode would prove the restore failed.
    checker_record = json.loads(
        (results_root / "checker" / "monitoring-901.json").read_text(encoding="utf-8")
    )
    assert checker_record["returncode"] == 0


def test_usage_recorded_from_agent_output(tmp_path):
    """Usage the agent prints reaches the sweep's run-folder records (#112 fields)."""
    config = make_config(
        tmp_path,
        [{"name": "usage-agent", "command": write_agent(tmp_path, REPORTING_AGENT)}],
    )
    results_root = tmp_path / "results"
    run_sweep(
        load_sweep_config(config),
        tasks_root=TEST_TASKS_ROOT,
        results_root=results_root,
        timeout=30,
    )
    record = json.loads(
        (results_root / "usage-agent" / "monitoring-901.json").read_text(encoding="utf-8")
    )
    assert record["answer"]["usage"]["cost_usd"] == 0.01


def test_gameable_adversarial_flagged_in_report(tmp_path):
    """An adversarial entry that passes a task marks the sweep not clean."""
    config = make_config(
        tmp_path,
        [
            {
                "name": "mock-adversarial",
                "command": write_agent(tmp_path, PASSING_AGENT),
                "adversarial": True,
            }
        ],
    )
    report = run_sweep(
        load_sweep_config(config),
        tasks_root=TEST_TASKS_ROOT,
        results_root=tmp_path / "results",
        timeout=30,
    )
    entry = report["agents"][0]
    assert entry["adversarial"] is True
    assert entry["gameable"] is True
    assert report["adversarial_clean"] is False


def test_invalid_config_rejected(tmp_path):
    dupes = make_config(
        tmp_path,
        [
            {"name": "same", "command": "echo hi"},
            {"name": "same", "command": "echo hi"},
        ],
    )
    with pytest.raises(ValueError, match="duplicate agent name"):
        load_sweep_config(dupes)

    no_command = make_config(tmp_path, [{"name": "half"}])
    with pytest.raises(ValueError, match="command is required"):
        load_sweep_config(no_command)

    empty = tmp_path / "empty.yaml"
    empty.write_text("sweep:\n  name: x\n  agents: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty"):
        load_sweep_config(empty)


def test_task_subset_selection(tmp_path):
    """`sweep.tasks` narrows the corpus to the named ids."""
    config = make_config(
        tmp_path,
        [{"name": "scoped", "command": write_agent(tmp_path, PASSING_AGENT)}],
        tasks=["run-test-001", "no-such-task"],
    )
    dirs = selected_task_dirs(load_sweep_config(config), TEST_TASKS_ROOT)
    assert [d.name for d in dirs] == ["run-test-001"]


def test_cli_dry_run_and_error(tmp_path, capsys):
    """`bench sweep --dry-run` prints the plan; a bad config exits 2."""
    config = make_config(
        tmp_path,
        [
            {"name": "plan-agent", "command": "python agents/naive.py"},
            {
                "name": "plan-cred",
                "command": "python agents/llm_agent.py",
                "requires_env": ["SWEEP_TEST_CRED_MISSING"],
            },
        ],
    )
    assert cmd_sweep(
        argparse.Namespace(
            config=str(config),
            tasks_root=str(TEST_TASKS_ROOT),
            results_root=str(tmp_path / "results"),
            timeout=30,
            dry_run=True,
        )
    ) == 0
    out = capsys.readouterr().out
    assert "plan-agent" in out
    assert "missing-env" in out

    bad = tmp_path / "bad.yaml"
    bad.write_text("not: a: sweep\n", encoding="utf-8")
    assert (
        cmd_sweep(
            argparse.Namespace(
                config=str(bad),
                tasks_root=str(TEST_TASKS_ROOT),
                results_root=str(tmp_path / "results"),
                timeout=30,
                dry_run=False,
            )
        )
        == 2
    )


def test_existing_run_folder_refused_unless_overwrite(tmp_path):
    """A sweep never silently clobbers a committed run folder."""
    passing = make_config(
        tmp_path,
        [{"name": "naive", "command": write_agent(tmp_path, PASSING_AGENT, "p.py")}],
    )
    results_root = tmp_path / "results"
    run_dir = results_root / "naive"
    run_dir.mkdir(parents=True)
    (run_dir / "sentinel.txt").write_text("committed run", encoding="utf-8")

    with pytest.raises(ValueError, match="already exists"):
        run_sweep(
            load_sweep_config(passing),
            tasks_root=TEST_TASKS_ROOT,
            results_root=results_root,
            timeout=30,
        )
    assert (run_dir / "sentinel.txt").is_file()  # untouched

    allow = make_config(
        tmp_path,
        [{"name": "naive", "command": write_agent(tmp_path, PASSING_AGENT, "p.py")}],
        overwrite=True,
    )
    report = run_sweep(
        load_sweep_config(allow),
        tasks_root=TEST_TASKS_ROOT,
        results_root=results_root,
        timeout=30,
    )
    assert report["agents"][0]["status"] == "ran"


def test_real_baseline_config_is_valid():
    """The shipped sweeps/baseline.yaml parses and validates."""
    config = load_sweep_config(REPO_ROOT / "sweeps" / "baseline.yaml")
    names = [entry["name"] for entry in config["sweep"]["agents"]]
    assert "naive" in names
    assert sum(1 for e in config["sweep"]["agents"] if e.get("adversarial")) == 3
    # The three-model credential gap is declared, not hidden.
    llm_entries = [
        e
        for e in config["sweep"]["agents"]
        if "LLM_API_KEY" in (e.get("requires_env") or [])
    ]
    assert len(llm_entries) == 3
    assert any("llama" in str(e.get("model", "")) for e in llm_entries)


def test_default_timeout_constant_matches_runner():
    assert DEFAULT_TIMEOUT_S == 600
