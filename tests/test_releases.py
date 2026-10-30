"""Release-manifest tests for `bench run --release` (issue #116).

The committed `releases/v0.1.json` is verified against the live corpus on
every test run: editing a task rubric without re-freezing the release fails
here and in CI, which is the point of freezing.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from bench import GRADER_VERSION
from bench.grader import rubric_sha256
from bench.releases import (
    MANIFEST_VERSION,
    build_manifest,
    load_release,
    verify_release,
)
from bench.run import cmd_run, load_release_checked, run_task

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_TASK = REPO_ROOT / "tests" / "data" / "monitoring" / "run-test-001"

# Emits valid answer JSON without needing any fixture content.
ECHO_AGENT = (
    'import json; print(json.dumps({"root_cause": "x", "evidence": [], "fix": "y"}))'
)


def write_agent(tmp_path: Path, body: str) -> str:
    script = tmp_path / "agent.py"
    script.write_text(body, encoding="utf-8")
    return f"{sys.executable} {script}"


def test_committed_v01_manifest_verifies_against_live_corpus():
    """Every task the frozen release names still verifies: same rubric hashes."""
    manifest = load_release("v0.1", releases_root=REPO_ROOT / "releases")
    problems = verify_release(manifest, tasks_root=REPO_ROOT / "tasks")
    assert problems == []


def test_committed_v01_manifest_freezes_grader_version():
    manifest = load_release("v0.1", releases_root=REPO_ROOT / "releases")
    assert manifest["grader_version"] == GRADER_VERSION
    assert manifest["manifest_version"] == MANIFEST_VERSION
    assert len(manifest["tasks"]) == 40


def test_build_manifest_hashes_match_loaded_rubrics():
    """build_manifest hashes each rubric through the same path the grader uses."""
    manifest = build_manifest(tasks_root=REPO_ROOT / "tasks")
    assert verify_release(manifest, tasks_root=REPO_ROOT / "tasks") == []

    expected = json.loads(
        (REPO_ROOT / "tasks" / "aks-ops" / "aks-ops-019" / "expected.json").read_text()
    )
    by_id = {task["id"]: task for task in manifest["tasks"]}
    assert by_id["aks-ops-019"]["rubric_sha256"] == rubric_sha256(expected)


def test_verify_release_detects_tampered_hash():
    manifest = build_manifest(tasks_root=REPO_ROOT / "tasks")
    manifest["tasks"][0]["rubric_sha256"] = "0" * 64
    problems = verify_release(manifest, tasks_root=REPO_ROOT / "tasks")
    assert any("rubric_sha256 mismatch" in problem for problem in problems)


def test_verify_release_detects_missing_manifest_task(tmp_path):
    """A manifest task that disappears from the corpus still fails verification."""
    manifest = build_manifest(tasks_root=REPO_ROOT / "tasks")
    removed = manifest["tasks"][0]  # stays in the manifest, vanishes from the corpus

    tasks_root = tmp_path / "tasks"
    shutil.copytree(REPO_ROOT / "tasks", tasks_root)
    shutil.rmtree(next(tasks_root.rglob(removed["id"])))

    problems = verify_release(manifest, tasks_root=tasks_root)
    assert f"task in release but missing from corpus: {removed['id']}" in problems


def test_verify_release_allows_corpus_growth():
    """Corpus tasks the manifest does not name are allowed — the subset check.

    The manifest freezes 40 tasks, then one more task lands in the corpus.
    The new task belongs to the next release, not to this manifest, so v0.1
    verification still passes without re-freezing: a `--release v0.1` run
    keeps working while the corpus grows.
    """
    manifest = build_manifest(tasks_root=REPO_ROOT / "tasks")
    grown = manifest["tasks"].pop()  # corpus now holds one task the manifest lacks

    problems = verify_release(manifest, tasks_root=REPO_ROOT / "tasks")
    assert problems == [], f"unexpected problems: {problems}"
    assert grown["id"] not in "\n".join(problems)


def test_verify_release_detects_edited_rubric(tmp_path):
    """Editing a manifest task's rubric after the freeze still fails."""
    tasks_root = tmp_path / "tasks"
    task_dir = tasks_root / "aks-ops" / "aks-ops-019"
    shutil.copytree(REPO_ROOT / "tasks" / "aks-ops" / "aks-ops-019", task_dir)
    manifest = build_manifest(tasks_root=tasks_root)

    rubric = json.loads((task_dir / "expected.json").read_text(encoding="utf-8"))
    rubric["root_cause"] = [["edited after the freeze"]]
    (task_dir / "expected.json").write_text(
        json.dumps(rubric), encoding="utf-8"
    )

    problems = verify_release(manifest, tasks_root=tasks_root)
    assert any(
        "rubric_sha256 mismatch for aks-ops-019" in problem for problem in problems
    )


def test_verify_release_detects_grader_bump():
    manifest = build_manifest(tasks_root=REPO_ROOT / "tasks")
    manifest["grader_version"] = GRADER_VERSION + 1
    problems = verify_release(manifest, tasks_root=REPO_ROOT / "tasks")
    assert any("grader_version mismatch" in problem for problem in problems)


def test_load_release_rejects_unknown_and_bad_manifests(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_release("v9.9", releases_root=tmp_path)

    bad = tmp_path / "v9.9.json"
    bad.write_text(json.dumps({"manifest_version": 999}), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest_version"):
        load_release("v9.9", releases_root=tmp_path)


def test_load_release_checked_raises_on_drift(tmp_path, monkeypatch):
    """A manifest whose hashes no longer match the corpus fails loudly."""
    tampered = load_release("v0.1", releases_root=REPO_ROOT / "releases")
    tampered["tasks"][0]["rubric_sha256"] = "0" * 64
    monkeypatch.setattr(
        "bench.run.load_release", lambda name: tampered, raising=True
    )
    with pytest.raises(ValueError, match="does not match release"):
        load_release_checked("v0.1", tasks_root=REPO_ROOT / "tasks")


def test_run_with_release_stamps_records(tmp_path):
    cmd = write_agent(tmp_path, ECHO_AGENT)
    record = run_task(TEST_TASK, cmd, results_root=tmp_path, release="v0.1")
    assert record["release"] == "v0.1"

    plain = run_task(TEST_TASK, cmd, results_root=tmp_path)
    assert "release" not in plain


def make_args() -> type:
    class Args:
        agent = ""
        task = None
        all_tasks = False
        name = "release-cli-test"
        timeout = 600
        tasks_root = str(REPO_ROOT / "tasks")
        split = "all"
        release = "v0.1"

    return Args


def test_cmd_run_requires_task_selection_before_release_check(capsys):
    args = make_args()()
    assert cmd_run(args) == 2
    assert "Specify --task" in capsys.readouterr().err


def test_cmd_run_unknown_release_fails_closed(capsys):
    args = make_args()()
    args.all_tasks = True
    args.release = "v9.9"
    assert cmd_run(args) == 2
    assert "Release check failed" in capsys.readouterr().err
