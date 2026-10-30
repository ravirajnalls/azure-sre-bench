"""Held-out split tests (issue #115).

Exercises the full held-out mechanics against synthetic tasks built from the
valid fixture task: age encrypt/decrypt round-trips, split-aware validation
(with and without the key), the decrypting loader, the runner's SKIP semantics,
`bench run --split` filtering, `bench grade` on encrypted rubrics, the
maintainer encrypt/decrypt script, and public/held-out leaderboard separation.
All keys are throwaway identities generated in-process.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bench.heldout import (
    ENCRYPTED_SUFFIX,
    SPLIT_HELDOUT,
    HeldoutKeyError,
    decrypt_bytes,
    decrypt_hidden,
    encrypt_hidden,
    encrypt_bytes,
    generate_identity,
    heldout_key,
    load_hidden_json,
    split_of,
)
from bench.leaderboard import leaderboard_markdown, split_runs
from bench.run import run_task, run_tasks
from bench.tasks import inspect_task

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_TASKS = Path(__file__).parent / "data" / "tasks"
BASE_TASK = DATA_TASKS / "monitoring" / "monitoring-001"

LISTING_AGENT = """
import json, os
print(json.dumps({
    "root_cause": " ".join(sorted(os.listdir(os.environ["TASK_DIR"]))),
    "evidence": [os.environ.get("TASK_ID", "")],
    "fix": ""
}))
"""


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    """A throwaway age identity, exported as HELDOUT_KEY for the test."""
    identity = generate_identity()
    monkeypatch.setenv("HELDOUT_KEY", identity)
    return identity


def make_task(tmp_path: Path, name: str = "monitoring-001") -> Path:
    """Copy the valid fixture task under tmp_path's tasks tree."""
    dest = tmp_path / "tasks" / "monitoring" / name
    shutil.copytree(BASE_TASK, dest)
    return dest


def make_heldout_task(tmp_path: Path, key: str, name: str = "monitoring-001") -> Path:
    """The fixture task converted to a locked held-out task."""
    task_dir = make_task(tmp_path, name)
    config = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
    config["split"] = "heldout"
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    encrypt_hidden(task_dir, key)
    return task_dir


def answer_agent_cmd(tmp_path: Path, payload: dict) -> str:
    """An agent command that prints the JSON stored in a file beside the task."""
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps(payload), encoding="utf-8")
    return (
        f"{sys.executable} -c \"import sys; print(open(sys.argv[1]).read())\" {answer}"
    )


def write_agent(tmp_path: Path, body: str) -> str:
    """Write a throwaway agent script; returns an absolute command string."""
    script = tmp_path / "agent.py"
    script.write_text(body, encoding="utf-8")
    return f"{sys.executable} {script}"


# --- crypto round-trip --------------------------------------------------------


def test_encrypt_decrypt_round_trip(key: str):
    data = json.dumps({"root_cause": [[("mismatched selector")]], "fix": [["fix it"]]}).encode()
    assert decrypt_bytes(encrypt_bytes(data, key), key) == data


def test_encrypt_hidden_replaces_plaintext_with_age_files(tmp_path, key: str):
    task_dir = make_heldout_task(tmp_path, key)
    assert not (task_dir / "expected.json").exists()
    assert not (task_dir / "solution.json").exists()
    assert (task_dir / f"expected.json{ENCRYPTED_SUFFIX}").is_file()
    assert (task_dir / f"solution.json{ENCRYPTED_SUFFIX}").is_file()


def test_encrypt_hidden_is_idempotent_safe(tmp_path, key: str):
    """Encrypting twice is a no-op the second time (nothing plaintext left)."""
    task_dir = make_task(tmp_path)
    assert encrypt_hidden(task_dir, key)
    assert encrypt_hidden(task_dir, key) == []


def test_decrypt_hidden_restores_plaintext(tmp_path, key: str):
    original = {
        "expected": json.loads((BASE_TASK / "expected.json").read_text(encoding="utf-8")),
        "solution": json.loads((BASE_TASK / "solution.json").read_text(encoding="utf-8")),
    }
    task_dir = make_heldout_task(tmp_path, key)
    assert decrypt_hidden(task_dir, key)
    assert json.loads((task_dir / "expected.json").read_text(encoding="utf-8")) == original["expected"]
    assert json.loads((task_dir / "solution.json").read_text(encoding="utf-8")) == original["solution"]


def test_wrong_key_fails_loudly(tmp_path, key: str):
    task_dir = make_heldout_task(tmp_path, key)
    wrong_key = generate_identity()  # a different identity, passed explicitly
    with pytest.raises(HeldoutKeyError):
        decrypt_bytes((task_dir / f"expected.json{ENCRYPTED_SUFFIX}").read_bytes(), wrong_key)


# --- loader -------------------------------------------------------------------


def test_load_hidden_json_round_trip_public(tmp_path):
    task_dir = make_task(tmp_path)
    expected = json.loads((BASE_TASK / "expected.json").read_text(encoding="utf-8"))
    assert load_hidden_json(task_dir, "expected.json", config={}) == expected


def test_load_hidden_json_decrypts_with_env_key(tmp_path, key: str):
    task_dir = make_heldout_task(tmp_path, key)
    expected = json.loads((BASE_TASK / "expected.json").read_text(encoding="utf-8"))
    config = {"split": SPLIT_HELDOUT}
    assert load_hidden_json(task_dir, "expected.json", config=config) == expected
    assert load_hidden_json(task_dir, "solution.json", config=config) == json.loads(
        (BASE_TASK / "solution.json").read_text(encoding="utf-8")
    )


def test_load_hidden_json_without_key_raises(tmp_path, key: str, monkeypatch):
    task_dir = make_heldout_task(tmp_path, key)
    monkeypatch.delenv("HELDOUT_KEY")
    with pytest.raises(HeldoutKeyError):
        load_hidden_json(task_dir, "expected.json", config={"split": SPLIT_HELDOUT})


def test_heldout_key_treats_blank_env_as_absent(monkeypatch):
    monkeypatch.setenv("HELDOUT_KEY", "   ")
    assert heldout_key() is None


def test_split_of_defaults_to_public():
    assert split_of(None) == "public"
    assert split_of({}) == "public"
    assert split_of({"split": "heldout"}) == "heldout"


# --- validator ----------------------------------------------------------------


def test_validator_accepts_heldout_with_key(tmp_path, key: str):
    task_dir = make_heldout_task(tmp_path, key)
    assert inspect_task(task_dir).errors == []


def test_validator_accepts_heldout_structure_without_key(tmp_path, key: str, monkeypatch):
    """Contributors without HELDOUT_KEY can still validate structure."""
    task_dir = make_heldout_task(tmp_path, key)
    monkeypatch.delenv("HELDOUT_KEY")
    assert inspect_task(task_dir).errors == []


def test_validator_rejects_plaintext_in_heldout(tmp_path, key: str):
    task_dir = make_heldout_task(tmp_path, key)
    (task_dir / "expected.json").write_text("{}", encoding="utf-8")
    errors = inspect_task(task_dir).errors
    assert any("plaintext expected.json" in error for error in errors)


def test_validator_rejects_missing_encrypted_rubric(tmp_path, key: str):
    task_dir = make_heldout_task(tmp_path, key)
    (task_dir / f"expected.json{ENCRYPTED_SUFFIX}").unlink()
    errors = inspect_task(task_dir).errors
    assert any("missing expected.json.age" in error for error in errors)


def test_validator_rejects_unknown_split(tmp_path):
    task_dir = make_task(tmp_path)
    config = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
    config["split"] = "secret"
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    errors = inspect_task(task_dir).errors
    assert any("split must be one of" in error for error in errors)


def test_validator_reports_wrong_key(tmp_path, key: str, monkeypatch):
    task_dir = make_heldout_task(tmp_path, key)
    monkeypatch.setenv("HELDOUT_KEY", generate_identity())
    errors = inspect_task(task_dir).errors
    assert any("cannot decrypt" in error for error in errors)


def test_validator_checks_decrypted_rubric_shape_with_key(tmp_path, key: str):
    """With the key, a rubric that decrypts to garbage shape still fails."""
    task_dir = make_task(tmp_path)
    config = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
    config["split"] = "heldout"
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    (task_dir / "expected.json").write_text(json.dumps({"root_cause": [], "fix": []}), encoding="utf-8")
    encrypt_hidden(task_dir, key)
    errors = inspect_task(task_dir).errors
    assert any("'root_cause' must be a non-empty list" in error for error in errors)


def test_validator_skips_rubric_shape_without_key(tmp_path, key: str, monkeypatch):
    """Same garbage rubric, no key: structure-only validation passes (issue #115)."""
    task_dir = make_task(tmp_path)
    config = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
    config["split"] = "heldout"
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    (task_dir / "expected.json").write_text(json.dumps({"root_cause": [], "fix": []}), encoding="utf-8")
    encrypt_hidden(task_dir, key)
    monkeypatch.delenv("HELDOUT_KEY")
    assert inspect_task(task_dir).errors == []


# --- runner -------------------------------------------------------------------


def test_run_task_grades_heldout_with_key(tmp_path, key: str):
    """Solvability through the real runner: a correct answer decrypts and passes."""
    task_dir = make_heldout_task(tmp_path, key)
    solution = json.loads((BASE_TASK / "solution.json").read_text(encoding="utf-8"))
    record = run_task(task_dir, answer_agent_cmd(tmp_path, solution), results_root=tmp_path)
    assert record["passed"] is True
    assert record["split"] == "heldout"


def test_run_task_naive_fails_heldout_with_key(tmp_path, key: str):
    task_dir = make_heldout_task(tmp_path, key)
    record = run_task(task_dir, f"{sys.executable} {REPO_ROOT / 'agents' / 'naive.py'}", results_root=tmp_path)
    assert record["passed"] is False
    assert record["split"] == "heldout"


def test_run_task_without_key_raises_not_graded(tmp_path, key: str, monkeypatch):
    task_dir = make_heldout_task(tmp_path, key)
    monkeypatch.delenv("HELDOUT_KEY")
    with pytest.raises(HeldoutKeyError):
        run_task(task_dir, f"{sys.executable} {REPO_ROOT / 'agents' / 'naive.py'}", results_root=tmp_path)


def test_workspace_never_contains_age_files(tmp_path, key: str):
    """Information boundary holds for held-out tasks: no .age file in the workspace."""
    task_dir = make_heldout_task(tmp_path, key)
    record = run_task(task_dir, write_agent(tmp_path, LISTING_AGENT), results_root=tmp_path)
    listing = record["answer"]["root_cause"]
    assert "fixtures" in listing
    assert "expected.json.age" not in listing
    assert "solution.json.age" not in listing


def test_run_tasks_skips_locked_and_runs_public(tmp_path, key: str, monkeypatch, capsys):
    public_dir = make_task(tmp_path, name="monitoring-002")
    config = yaml.safe_load((public_dir / "task.yaml").read_text(encoding="utf-8"))
    config["id"] = "monitoring-002"
    (public_dir / "task.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    heldout_dir = make_heldout_task(tmp_path, key)
    monkeypatch.delenv("HELDOUT_KEY")
    run_tasks([heldout_dir, public_dir], f"{sys.executable} {REPO_ROOT / 'agents' / 'naive.py'}", results_root=tmp_path)
    out = capsys.readouterr().out
    assert "SKIP monitoring-001" in out
    assert "FAIL monitoring-002" in out
    assert "0/2 passed, 1 skipped (locked held-out)" in out


# --- CLI: run --split and grade -----------------------------------------------


def run_cli(*args: str, cwd: Path, env_extra: dict[str, str] | None = None):
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-m", "bench", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )


def test_cmd_run_split_filter_mismatch_is_an_error(tmp_path, key: str):
    make_heldout_task(tmp_path, key)
    proc = run_cli(
        "run", "--agent", f"{sys.executable} {REPO_ROOT / 'agents' / 'naive.py'}",
        "--task", "monitoring-001", "--split", "public",
        "--tasks-root", "tasks",
        cwd=tmp_path,
    )
    assert proc.returncode == 2
    assert "not in the 'public' split" in proc.stderr


def test_cmd_run_split_heldout_without_key_skips(tmp_path, key: str, monkeypatch):
    make_heldout_task(tmp_path, key)
    monkeypatch.delenv("HELDOUT_KEY")
    proc = run_cli(
        "run", "--agent", f"{sys.executable} {REPO_ROOT / 'agents' / 'naive.py'}",
        "--all", "--split", "heldout",
        "--tasks-root", "tasks",
        cwd=tmp_path,
    )
    assert proc.returncode == 0
    assert "SKIP monitoring-001" in proc.stdout


def test_cmd_grade_heldout_with_key(tmp_path, key: str):
    task_dir = make_heldout_task(tmp_path, key)
    solution = json.loads((BASE_TASK / "solution.json").read_text(encoding="utf-8"))
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps(solution), encoding="utf-8")
    proc = run_cli("grade", "--task", "monitoring-001", "--answer", str(answer), cwd=tmp_path)
    assert proc.returncode == 0
    assert '"passed": true' in proc.stdout


def test_cmd_grade_heldout_without_key_fails_cleanly(tmp_path, key: str, monkeypatch):
    make_heldout_task(tmp_path, key)
    monkeypatch.delenv("HELDOUT_KEY")
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"root_cause": "x", "evidence": [], "fix": "y"}), encoding="utf-8")
    proc = run_cli("grade", "--task", "monitoring-001", "--answer", str(answer), cwd=tmp_path)
    assert proc.returncode == 2
    assert "error:" in proc.stderr


# --- maintainer script --------------------------------------------------------


def test_encrypt_script_encrypts_and_refuses_public(tmp_path, key: str):
    public_dir = make_task(tmp_path, name="monitoring-003")
    heldout_dir = make_task(tmp_path)  # still public in task.yaml
    config = yaml.safe_load((heldout_dir / "task.yaml").read_text(encoding="utf-8"))
    config["split"] = "heldout"
    (heldout_dir / "task.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    env = {"HELDOUT_KEY": key}
    refused = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "encrypt_heldout.py"), str(public_dir)],
        capture_output=True, text=True, cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), **env},
    )
    assert refused.returncode == 2
    assert (public_dir / "expected.json").is_file()
    encrypted = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "encrypt_heldout.py"), str(heldout_dir)],
        capture_output=True, text=True, cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), **env},
    )
    assert encrypted.returncode == 0
    assert not (heldout_dir / "expected.json").exists()
    assert (heldout_dir / f"expected.json{ENCRYPTED_SUFFIX}").is_file()


def test_encrypt_script_decrypt_round_trip(tmp_path, key: str):
    task_dir = make_heldout_task(tmp_path, key)
    script = REPO_ROOT / "scripts" / "encrypt_heldout.py"
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "HELDOUT_KEY": key}
    proc = subprocess.run(
        [sys.executable, str(script), "--decrypt", str(task_dir)],
        capture_output=True, text=True, cwd=tmp_path, env=env,
    )
    assert proc.returncode == 0
    assert (task_dir / "expected.json").is_file()
    assert not (task_dir / f"expected.json{ENCRYPTED_SUFFIX}").exists()


# --- leaderboard split separation ---------------------------------------------


def write_record(results_root: Path, run: str, task: str, split: str | None, passed: bool):
    run_dir = results_root / run
    run_dir.mkdir(parents=True, exist_ok=True)
    record = {"task": task, "score": 1.0 if passed else 0.0, "passed": passed}
    if split is not None:
        record["split"] = split
    (run_dir / f"{task}.json").write_text(json.dumps(record), encoding="utf-8")


def test_leaderboard_defaults_pre_split_records_to_public(tmp_path):
    write_record(tmp_path, "agent-a", "monitoring-032", split=None, passed=True)
    table = leaderboard_markdown(tmp_path)
    assert table.splitlines()[0].startswith("| Rank |")
    assert "Held-out" not in table


def test_leaderboard_separates_public_and_heldout(tmp_path):
    write_record(tmp_path, "agent-a", "monitoring-032", split="public", passed=True)
    write_record(tmp_path, "agent-a", "monitoring-042", split="heldout", passed=False)
    table = leaderboard_markdown(tmp_path)
    lines = table.splitlines()
    assert "Held-out split" in table
    public_row = next(line for line in lines if "monitoring" not in line and "agent-a" in line and "1/1" in line)
    heldout_section = table.split("Held-out split", 1)[1]
    assert "0/1" in heldout_section
    assert public_row


def test_split_runs_groups_by_record_split():
    runs = {
        "agent-a": [
            {"task": "t1", "split": "public"},
            {"task": "t2", "split": "heldout"},
            {"task": "t3"},  # pre-split record -> public
        ]
    }
    grouped = split_runs(runs)
    assert [r["task"] for r in grouped["public"]["agent-a"]] == ["t1", "t3"]
    assert [r["task"] for r in grouped["heldout"]["agent-a"]] == ["t2"]


def test_collect_records_empty_root_headers_only():
    assert leaderboard_markdown(Path("no-such-results-dir")).splitlines()[0].startswith("| Rank |")
