"""Tests for the static leaderboard site builder (`bench site`, issue #40).

The runner's result-record shape (`bench/run.py`) is the contract the site
reads: `{"task": str, "score": float, "passed": bool, ...}`, one file per task
under `results/<run-name>/<task-id>.json`. Since the versioning extension
(issue #95) records also carry `grader_version`/`rubric_sha256`; the `record`
helper emits that current shape, with legacy/stale rendering covered in its
own tests.
"""

import json
import subprocess
import sys
from pathlib import Path

from bench import GRADER_VERSION
from bench.site import aggregate, load_results, render_html, task_category


def record(task: str, score: float, passed: bool) -> dict:
    """A minimal result record in the shape `bench run` writes."""
    return {
        "task": task,
        "score": score,
        "passed": passed,
        "grader_version": GRADER_VERSION,
        "rubric_sha256": "a" * 64,
    }


def write_run(results_root: Path, run_name: str, records: list[dict]) -> None:
    run_dir = results_root / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    for rec in records:
        (run_dir / f"{rec['task']}.json").write_text(
            json.dumps(rec), encoding="utf-8"
        )


# --- task_category ---------------------------------------------------------


def test_task_category_handles_hyphenated_categories():
    # `aks-networking` must win over a naive first-token split on "aks".
    assert task_category("aks-networking-001") == "aks-networking"
    assert task_category("monitoring-001") == "monitoring"


def test_task_category_unknown_falls_back():
    assert task_category("not-a-category-999") == "other"


# --- load_results ----------------------------------------------------------


def test_load_results_missing_root_is_empty(tmp_path: Path):
    assert load_results(tmp_path / "nope") == {}


def test_load_results_skips_corrupt_files(tmp_path: Path):
    write_run(tmp_path, "naive", [record("monitoring-001", 0.0, False)])
    (tmp_path / "naive" / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "naive" / "list.json").write_text("[1, 2]", encoding="utf-8")

    runs = load_results(tmp_path)
    assert [r["task"] for r in runs["naive"]] == ["monitoring-001"]


def test_load_results_skips_adversarial_runs(tmp_path: Path):
    """Adversarial probe runs (issue #92) never render as agents on the site."""
    write_run(
        tmp_path, "adversarial-keyword-soup", [record("monitoring-001", 1.0, True)]
    )
    write_run(tmp_path, "naive", [record("monitoring-001", 0.0, False)])

    runs = load_results(tmp_path)
    assert list(runs) == ["naive"]


# --- aggregate -------------------------------------------------------------


def test_aggregate_zero_results(tmp_path: Path):
    agg = aggregate(load_results(tmp_path))
    assert agg["agents"] == []
    assert agg["tasks"] == []
    assert agg["overall"] == []
    assert agg["by_category"] == []
    assert agg["heatmap"] == []


def test_aggregate_one_run(tmp_path: Path):
    write_run(
        tmp_path,
        "naive",
        [
            record("monitoring-001", 0.0, False),
            record("aks-networking-001", 0.5, False),
        ],
    )
    agg = aggregate(load_results(tmp_path))

    assert agg["agents"] == ["naive"]
    assert agg["overall"] == [
        {"agent": "naive", "passed": 0, "total": 2, "mean_score": 0.25, "status": "current"}
    ]
    assert [b["category"] for b in agg["by_category"]] == [
        "aks-networking",
        "monitoring",
    ]
    heatmap = {row["task"]: row["cells"] for row in agg["heatmap"]}
    assert heatmap == {
        "aks-networking-001": ["fail"],
        "monitoring-001": ["fail"],
    }


def test_aggregate_many_runs_and_missing_cells(tmp_path: Path):
    write_run(
        tmp_path,
        "naive",
        [record("monitoring-001", 0.0, False), record("cost-001", 0.0, False)],
    )
    write_run(
        tmp_path,
        "llm",
        [record("monitoring-001", 1.0, True), record("cost-001", 0.5, False)],
    )
    write_run(tmp_path, "react", [record("monitoring-001", 1.0, True)])
    agg = aggregate(load_results(tmp_path))

    assert agg["agents"] == ["llm", "naive", "react"]
    by_agent = {row["agent"]: row for row in agg["overall"]}
    assert by_agent["llm"] == {
        "agent": "llm",
        "passed": 1,
        "total": 2,
        "mean_score": 0.75,
        "status": "current",
    }
    assert by_agent["react"]["total"] == 1
    # react never ran cost-001: the heatmap cell must be "none", not a failure.
    heatmap = {row["task"]: row["cells"] for row in agg["heatmap"]}
    assert heatmap["cost-001"] == ["fail", "fail", "none"]
    assert heatmap["monitoring-001"] == ["pass", "fail", "pass"]


# --- render_html -----------------------------------------------------------


def test_render_zero_results_shows_empty_state():
    html_text = render_html(aggregate({}))
    assert "<!DOCTYPE html>" in html_text
    assert "No results yet" in html_text
    # Phone readability: the viewport meta must be present on every render.
    assert 'name="viewport"' in html_text


def test_render_escapes_untrusted_text():
    agg = aggregate({"naive": [record("monitoring-001", 0.0, False)]})
    agg["agents"] = ['<script>alert("x")</script>']
    html_text = render_html(agg)
    assert "<script>" not in html_text
    assert "&lt;script&gt;" in html_text


def test_render_many_runs_has_all_tables():
    results = {
        "naive": [record("monitoring-001", 0.0, False)],
        "react": [record("monitoring-001", 1.0, True)],
    }
    html_text = render_html(aggregate(results))
    assert "Overall" in html_text
    assert "By category" in html_text
    assert "Task" in html_text
    assert "PASS" in html_text
    assert "FAIL" in html_text


# --- CLI wiring ------------------------------------------------------------


def test_cli_site_zero_one_and_many_folders(tmp_path: Path):
    out = tmp_path / "out" / "index.html"

    # Zero result folders.
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "bench",
            "site",
            "--results-root",
            str(tmp_path / "results"),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert proc.returncode == 0, proc.stderr
    assert "No results yet" in out.read_text(encoding="utf-8")

    # One result folder.
    write_run(tmp_path / "results", "naive", [record("monitoring-001", 0.0, False)])
    proc = subprocess.run(
        [sys.executable, "-m", "bench", "site", "--results-root",
         str(tmp_path / "results"), "--out", str(out)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert proc.returncode == 0, proc.stderr
    text = out.read_text(encoding="utf-8")
    assert "No results yet" not in text
    assert "naive" in text

    # Many result folders.
    write_run(tmp_path / "results", "react", [record("monitoring-001", 1.0, True)])
    proc = subprocess.run(
        [sys.executable, "-m", "bench", "site", "--results-root",
         str(tmp_path / "results"), "--out", str(out)],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert proc.returncode == 0, proc.stderr
    text = out.read_text(encoding="utf-8")
    assert "naive" in text and "react" in text


# --- stale/legacy rendering (issue #95) --------------------------------------


def test_aggregate_marks_stale_and_legacy_cells_and_counts_current_only():
    results = {
        "mixed": [
            record("monitoring-001", 1.0, True),  # current
            {  # stale: older grader version
                **record("cost-001", 1.0, True),
                "grader_version": GRADER_VERSION + 1,
            },
            # legacy: the version fields are stripped
            {"task": "aks-ops-022", "score": 1.0, "passed": True},
        ]
    }
    agg = aggregate(results)

    assert agg["overall"] == [
        {"agent": "mixed", "passed": 1, "total": 1, "mean_score": 1.0, "status": "current"}
    ]
    heatmap = {row["task"]: row["cells"] for row in agg["heatmap"]}
    assert heatmap["monitoring-001"] == ["pass"]
    assert heatmap["cost-001"] == ["stale"]
    assert heatmap["aks-ops-022"] == ["legacy"]


def test_aggregate_stale_hash_mismatch_via_current_rubrics():
    record_stale = {**record("monitoring-001", 1.0, True), "rubric_sha256": "old"}
    agg = aggregate({"a": [record_stale]}, {"monitoring-001": "fresh"})
    assert agg["overall"][0]["status"] == "stale"
    assert agg["overall"][0]["passed"] == 0


def test_render_mixed_version_runs_marks_cells_without_crashing():
    # The legacy record's pass renders as LEGACY (not PASS) — a passing
    # current-version record supplies the PASS cell, a failing one the FAIL.
    results = {
        "legacy-agent": [{"task": "monitoring-001", "score": 1.0, "passed": True}],
        "current-agent": [
            record("monitoring-001", 1.0, True),
            record("cost-001", 0.0, False),
        ],
    }
    html_text = render_html(aggregate(results))
    assert "LEGACY" in html_text
    assert "PASS" in html_text and "FAIL" in html_text
    assert "Status" in html_text


def test_render_stale_cells_show_stale_label():
    stale = {**record("monitoring-001", 1.0, True), "grader_version": GRADER_VERSION + 9}
    html_text = render_html(aggregate({"a": [stale]}))
    assert "STALE" in html_text
