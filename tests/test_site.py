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

import pytest
import yaml

from bench import GRADER_VERSION
from bench.stats import bootstrap_pass_rate_ci
from bench.site import (
    aggregate,
    cost_accuracy_points,
    is_open_source,
    load_agent_metadata_map,
    load_explainers,
    load_results,
    pareto_frontier,
    pass_rate,
    parse_result_stem,
    render_category_heatmap,
    render_cost_chart,
    render_difficulty_chart,
    render_explainer_html,
    render_html,
    render_learn_html,
    safe_repo_url,
    task_category,
    task_difficulties,
)


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
        {
            "agent": "naive",
            "passed": 0,
            "total": 2,
            "mean_score": 0.25,
            "pass_rate": 0.0,
            "ci_low": 0.0,
            "ci_high": 0.0,
            "pass_at_n": None,
            "flaky": None,
            "repeats": None,
            "median_elapsed_s": None,
            "median_commands": None,
            "total_cost_usd": None,
            "status": "current",
            "mode": "runner",
            "metadata": None,
        }
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
        "pass_rate": 0.5,
        "ci_low": 0.0,
        "ci_high": 1.0,
        "pass_at_n": None,
        "flaky": None,
        "repeats": None,
        "median_elapsed_s": None,
        "median_commands": None,
        "total_cost_usd": None,
        "status": "current",
        "mode": "runner",
        "metadata": None,
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
    assert "<script>alert(" not in html_text
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
        {
            "agent": "mixed",
            "passed": 1,
            "total": 1,
            "mean_score": 1.0,
            "pass_rate": 1.0,
            "ci_low": 1.0,
            "ci_high": 1.0,
            "pass_at_n": None,
            "flaky": None,
            "repeats": None,
            "median_elapsed_s": None,
            "median_commands": None,
            "total_cost_usd": None,
            "status": "current",
            "mode": "runner",
            "metadata": None,
        }
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


# --- efficiency and cost columns (issue #112) ---------------------------------


def test_aggregate_and_render_efficiency_metrics():
    """Overall rows carry median time/commands and total cost; the page renders them."""
    results = {
        "rich": [
            {
                **record("monitoring-001", 1.0, True),
                "elapsed_s": 2.0,
                "efficiency": {"commands_run": 4},
                "usage": {"cost_usd": 0.02},
            },
            {
                **record("cost-001", 1.0, True),
                "elapsed_s": 6.0,
                "efficiency": {"commands_run": 8},
                "usage": {"cost_usd": 0.05},
            },
        ],
        "sparse": [record("monitoring-001", 1.0, True)],
    }
    agg = aggregate(results)
    by_agent = {row["agent"]: row for row in agg["overall"]}
    assert by_agent["rich"]["median_elapsed_s"] == 4.0
    assert by_agent["rich"]["median_commands"] == 6.0
    assert by_agent["rich"]["total_cost_usd"] == pytest.approx(0.07)
    assert by_agent["sparse"]["median_elapsed_s"] is None

    html_text = render_html(agg)
    assert "<td class=\"num\">4.0s</td>" in html_text
    assert "<td class=\"num\">6</td>" in html_text
    assert "<td class=\"num\">$0.0700</td>" in html_text
    # Three em dashes in sparse's overall row; the missing cost-001 cell in
    # the by-category table is a dash too, but now carries the agent's
    # data-agent key for the issue-#119 column filter, so it renders apart.
    assert html_text.count('<td class="num none">&mdash;</td>') == 3
    assert '<td class="num none" data-agent="sparse">&mdash;</td>' in html_text


def test_render_escapes_efficiency_free_rows_without_crashing():
    """Records predating the schema extension render dashes in every column."""
    html_text = render_html(
        aggregate({"legacy-agent": [{"task": "monitoring-001", "score": 1.0, "passed": True}]})
    )
    # Three overall dashes; the agent's by-category cell is a dash too, but
    # attributed with data-agent for the issue-#119 column filter.
    assert html_text.count('<td class="num none">&mdash;</td>') == 3
    assert '<td class="num none" data-agent="legacy-agent">&mdash;</td>' in html_text


# --- agent metadata and filters (issue #119) ----------------------------------


def agent_yaml(**overrides) -> str:
    """A minimal-valid #118 descriptor; overrides replace named fields."""
    fields = {
        "name": "react",
        "version": "0.3.1",
        "model": "gpt-4o",
        "provider": "openai",
        "framework": "langchain",
        "open-source": "yes",
        "command": "python -m my_agent.bench_runner",
        "date": "2026-01-15",
        "contact": "you@example.com",
        **overrides,
    }
    return yaml.safe_dump(fields)


def write_metadata(results_root: Path, run_name: str, text: str | None) -> None:
    run_dir = results_root / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    if text is not None:
        (run_dir / "agent.yaml").write_text(text, encoding="utf-8")


def test_load_agent_metadata_map_keeps_valid_and_skips_invalid(tmp_path: Path):
    write_metadata(tmp_path, "good", agent_yaml())
    write_metadata(tmp_path, "bad", agent_yaml(date="not-a-date"))
    write_metadata(tmp_path, "absent", None)

    metadata = load_agent_metadata_map(tmp_path)

    assert set(metadata) == {"good"}
    assert metadata["good"]["model"] == "gpt-4o"


def test_load_agent_metadata_map_missing_root(tmp_path: Path):
    assert load_agent_metadata_map(tmp_path / "nope") == {}


def test_task_difficulties_reads_task_yaml_and_skips_bad(tmp_path: Path):
    task_dir = tmp_path / "monitoring-001"
    task_dir.mkdir()
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump({"id": "monitoring-001", "difficulty": "easy"}),
        encoding="utf-8",
    )
    broken = tmp_path / "monitoring-002"
    broken.mkdir()
    (broken / "task.yaml").write_text(": [unclosed", encoding="utf-8")

    difficulties = task_difficulties(tmp_path)

    assert difficulties == {"monitoring-001": "easy"}


def test_safe_repo_url_only_allows_http_s():
    assert safe_repo_url("https://github.com/you/agent") == "https://github.com/you/agent"
    assert safe_repo_url("http://example.com/x") == "http://example.com/x"
    assert safe_repo_url("javascript:alert(1)") is None
    assert safe_repo_url("data:text/html,hi") is None
    assert safe_repo_url(42) is None
    assert safe_repo_url(None) is None


def test_is_open_source_defaults_closed():
    assert is_open_source({"open-source": "yes"}) is True
    assert is_open_source({"open-source": True}) is True
    assert is_open_source({"open-source": "no"}) is False
    assert is_open_source({}) is False
    assert is_open_source(None) is False


def test_render_with_agent_metadata_links_and_subtitle():
    results = {"react": [record("monitoring-001", 1.0, True)]}
    metadata = {
        "react": yaml.safe_load(
            agent_yaml(repository="https://github.com/you/my-agent")
        )
    }
    html_text = render_html(aggregate(results, agent_metadata=metadata))

    assert '<a href="https://github.com/you/my-agent">react</a>' in html_text
    assert "gpt-4o · openai · langchain · 0.3.1" in html_text
    assert "you@example.com" in html_text
    assert 'data-open-source="1"' in html_text
    assert 'data-mode="runner"' in html_text


def test_render_without_agent_metadata_still_renders(tmp_path: Path):
    html_text = render_html(
        aggregate({"no-meta": [record("monitoring-001", 1.0, True)]})
    )
    assert "no-meta" in html_text
    # No descriptor: the name is plain text, nothing leaks a raw None.
    assert "<a href=" not in html_text.split("<h2>Overall</h2>")[1].split("</h2>")[1][:400]
    assert ">None<" not in html_text


def test_render_manual_mode_record_flags_the_row():
    manual = {**record("monitoring-001", 1.0, True), "mode": "manual"}
    html_text = render_html(aggregate({"chat": [manual]}))
    assert 'data-mode="manual"' in html_text


def test_render_includes_filter_controls_and_script():
    results = {"a": [record("monitoring-001", 1.0, True)]}
    html_text = render_html(aggregate(results))
    assert 'id="filter-category"' in html_text
    assert 'id="filter-difficulty"' in html_text
    assert 'id="filter-mode"' in html_text
    assert 'id="filter-oss"' in html_text
    assert "<script>" in html_text


def test_aggregate_heatmap_rows_carry_category_and_difficulty():
    results = {"a": [record("monitoring-001", 1.0, True)]}
    agg = aggregate(results, difficulties={"monitoring-001": "easy"})
    assert agg["heatmap"][0]["category"] == "monitoring"
    assert agg["heatmap"][0]["difficulty"] == "easy"
    # Without a difficulty map the row must say unknown, never crash.
    assert aggregate(results)["heatmap"][0]["difficulty"] == "unknown"


def test_render_bakes_filter_keys_into_heatmap_rows():
    results = {"a": [record("monitoring-001", 1.0, True)]}
    html_text = render_html(
        aggregate(results, difficulties={"monitoring-001": "easy"})
    )
    assert 'data-category="monitoring"' in html_text
    assert 'data-difficulty="easy"' in html_text


def test_render_javascript_url_repo_is_not_linked():
    results = {"evil": [record("monitoring-001", 1.0, True)]}
    metadata = {"evil": yaml.safe_load(agent_yaml(repository="javascript:alert(1)"))}
    html_text = render_html(aggregate(results, agent_metadata=metadata))
    assert 'href="javascript:' not in html_text


def test_cli_site_bakes_difficulty_from_tasks_root(tmp_path: Path):
    """`bench site` reads difficulty from --tasks-root (issue #119)."""
    write_run(tmp_path / "results", "a", [record("monitoring-001", 1.0, True)])
    task_dir = tmp_path / "tasks" / "monitoring-001"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump({"id": "monitoring-001", "difficulty": "medium"}),
        encoding="utf-8",
    )
    out = tmp_path / "out" / "index.html"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "bench",
            "site",
            "--results-root",
            str(tmp_path / "results"),
            "--tasks-root",
            str(tmp_path / "tasks"),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert proc.returncode == 0, proc.stderr
    assert 'data-difficulty="medium"' in out.read_text(encoding="utf-8")


# --- Learn page (issue #149) --------------------------------------------------


def write_task(tmp_path: Path, task_id: str, title: str, explainer: str | None) -> Path:
    task_dir = tmp_path / "tasks" / "monitoring" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        f"id: {task_id}\ntitle: \"{title}\"\n", encoding="utf-8"
    )
    if explainer is not None:
        (task_dir / "explainer.md").write_text(explainer, encoding="utf-8")
    return task_dir


EXPLAINER_MD = """\
# monitoring-001 — explainer

## How to investigate

Read the logs first.

## Why the red herring misleads

Metrics dip predates the incident.

## Root cause

Wrong backend path.

## Fix

Correct it.

## Prevention

Manifest lint.

## Further reading

- docs
"""


def test_load_explainers_reads_title_and_content(tmp_path: Path):
    write_task(tmp_path, "monitoring-001", "Ingress 502", EXPLAINER_MD)
    explainers = load_explainers(tmp_path / "tasks")
    assert list(explainers) == ["monitoring-001"]
    assert explainers["monitoring-001"]["title"] == "Ingress 502"
    assert "<h3>How to investigate</h3>" in explainers["monitoring-001"]["html"]


def test_load_explainers_skips_tasks_without_one(tmp_path: Path):
    write_task(tmp_path, "monitoring-001", "Ingress 502", EXPLAINER_MD)
    write_task(tmp_path, "monitoring-002", "No explainer", None)
    assert list(load_explainers(tmp_path / "tasks")) == ["monitoring-001"]


def test_load_explainers_missing_root_is_empty(tmp_path: Path):
    assert load_explainers(tmp_path / "nope") == {}


def test_learn_page_collapsed_by_default():
    page = render_learn_html(
        {"monitoring-001": {"title": "t", "html": "<p>x</p>"}}
    )
    assert "<details>" in page
    assert "<details open" not in page


def test_learn_page_escapes_html():
    page = render_learn_html(
        {"monitoring-001": {"title": "<script>alert(1)</script>", "html": "<p>x</p>"}}
    )
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_render_explainer_html_markdown_subset():
    html_out = render_explainer_html(
        "## How to investigate\n\nplain <b>text</b>\n\n- item one\n- item two\n\n```bash\nkubectl <n>\n```"
    )
    assert "<h3>How to investigate</h3>" in html_out
    assert "<p>plain &lt;b&gt;text&lt;/b&gt;</p>" in html_out
    assert "<li>item one</li>" in html_out
    assert "<pre><code>" in html_out


def test_site_cli_writes_learn_page(tmp_path, capsys):
    write_task(tmp_path, "monitoring-001", "Ingress 502", EXPLAINER_MD)
    write_run(tmp_path / "results", "naive", [record("monitoring-001", 1.0, True)])
    from bench.cli import main

    exit_code = main(
        [
            "site",
            "--results-root",
            str(tmp_path / "results"),
            "--tasks-root",
            str(tmp_path / "tasks"),
            "--out",
            str(tmp_path / "out" / "index.html"),
            "--learn-out",
            str(tmp_path / "out" / "learn.html"),
        ]
    )
    assert exit_code == 0
    learn = (tmp_path / "out" / "learn.html").read_text(encoding="utf-8")
    assert "<details>" in learn
    assert "monitoring-001" in learn
    assert "explainer pages" in capsys.readouterr().out



# --- repeated runs (issue #162) ----------------------------------------------


def test_parse_result_stem_splits_repeat_suffix():
    assert parse_result_stem("monitoring-001") == ("monitoring-001", None)
    assert parse_result_stem("monitoring-001.2") == ("monitoring-001", 2)
    # Non-numeric suffixes are not repeats — a hand-named file keeps its stem.
    assert parse_result_stem("monitoring-001.final") == ("monitoring-001.final", None)


def test_load_results_groups_repeat_files(tmp_path):
    run_dir = tmp_path / "rep"
    run_dir.mkdir()
    files = [
        ("cost-001.json", 1.0, True),
        ("cost-001.0.json", 1.0, True),
        ("cost-001.1.json", 0.0, False),
        ("cost-001.2.json", 1.0, True),
    ]
    for stem, score, passed in files:
        (run_dir / stem).write_text(
            json.dumps({"task": "cost-001", "score": score, "passed": passed}),
            encoding="utf-8",
        )
    records = load_results(tmp_path)["rep"]
    # All four files belong to one task; repeats keep file order after the
    # pre-existing single-run record.
    assert [(r["task"], r.get("repeat")) for r in records] == [
        ("cost-001", None),
        ("cost-001", 0),
        ("cost-001", 1),
        ("cost-001", 2),
    ]


def repeat_record(task: str, k: int, passed: bool, score: float = 1.0) -> dict:
    # Built on the shared `record` helper so the version fields classify as
    # current — the same contract a real `bench run --repeat` record has.
    return {**record(task, score, passed), "repeat": k}


def test_aggregate_repeat_metrics_and_flaky_heatmap_cell():
    agg = aggregate(
        {
            "agent": [
                repeat_record("steady-001", 0, True),
                repeat_record("steady-001", 1, True),
                repeat_record("steady-001", 2, True),
                repeat_record("mixed-001", 0, True),
                repeat_record("mixed-001", 1, False, 0.0),
                repeat_record("mixed-001", 2, True),
                repeat_record("always-fail-001", 0, False, 0.0),
                repeat_record("always-fail-001", 1, False, 0.0),
            ]
        }
    )
    row = agg["overall"][0]
    # Pooled per-run counts: 5 passes across 8 runs (steady 3, mixed 2, fail 0).
    assert row["passed"] == 5 and row["total"] == 8
    assert row["pass_rate"] == pytest.approx(0.625)
    # Pass@k: 2 of 3 tasks solved at least once, measured at k=3.
    assert row["pass_at_n"] == pytest.approx(2 / 3)
    assert row["repeats"] == 3
    # One flaky task: mixed-001 passed in some but not all runs.
    assert row["flaky"] == 1
    heatmap = {r["task"]: r["cells"] for r in agg["heatmap"]}
    assert heatmap["steady-001"] == ["pass"]
    assert heatmap["mixed-001"] == ["flaky"]
    assert heatmap["always-fail-001"] == ["fail"]


def test_render_overall_shows_pass_at_k_flaky_and_heatmap_flaky_cell():
    agg = aggregate(
        {
            "agent": [
                repeat_record("mixed-001", 0, True),
                repeat_record("mixed-001", 1, False, 0.0),
            ]
        }
    )
    page = render_html(agg)
    assert "Pass@k" in page
    # One task, solved in at least one of its two runs → pass@2 = 1.00.
    assert "1.00 (k=2)" in page
    # The flaky heat cell keeps its agent filter attribute.
    assert 'class="heat flaky" data-agent="agent"' in page
    assert "FLAKY" in page

# --- cost vs accuracy chart (issue #164) ---------------------------------------


def costly_record(task: str, score: float, passed: bool, cost: float) -> dict:
    """A result record in the #112 shape: usage telemetry carries the cost."""
    return {**record(task, score, passed), "usage": {"cost_usd": cost}}


def test_cost_accuracy_points_splits_on_cost_telemetry():
    agg = {
        "overall": [
            {"agent": "rich", "passed": 2, "total": 4, "total_cost_usd": 0.07},
            {"agent": "frugal", "passed": 1, "total": 2, "total_cost_usd": 0.01},
            {"agent": "quiet", "passed": 1, "total": 2, "total_cost_usd": None},
        ]
    }
    points, no_cost = cost_accuracy_points(agg)
    assert points == [
        {"agent": "rich", "cost_usd": 0.07, "rate": 0.5},
        {"agent": "frugal", "cost_usd": 0.01, "rate": 0.5},
    ]
    assert no_cost == ["quiet"]


def test_cost_accuracy_points_skips_zero_total_runs():
    # A run with no counted tasks has no pass rate — it cannot sit on either axis.
    points, no_cost = cost_accuracy_points(
        {"overall": [{"agent": "empty", "passed": 0, "total": 0, "total_cost_usd": 0.0}]}
    )
    assert points == []
    assert no_cost == ["empty"]


def test_pareto_frontier_dominates():
    points = [
        {"agent": "a", "cost_usd": 0.10, "rate": 0.5},
        # b: same rate as a, cheaper — dominates a.
        {"agent": "b", "cost_usd": 0.05, "rate": 0.5},
        # c: pricier than a, better rate — nobody beats it on rate.
        {"agent": "c", "cost_usd": 0.20, "rate": 1.0},
        {"agent": "d", "cost_usd": 0.40, "rate": 0.5},
    ]
    assert pareto_frontier(points) == {"b", "c"}


def test_pareto_frontier_tie_shows_both():
    points = [
        {"agent": "a", "cost_usd": 0.10, "rate": 0.5},
        {"agent": "b", "cost_usd": 0.10, "rate": 0.5},
    ]
    assert pareto_frontier(points) == {"a", "b"}


def test_render_cost_chart_plots_points_and_frontier():
    agg = {
        "overall": [
            {"agent": "a", "passed": 1, "total": 2, "total_cost_usd": 0.10},
            {"agent": "b", "passed": 1, "total": 2, "total_cost_usd": 0.05},
            {"agent": "c", "passed": 2, "total": 2, "total_cost_usd": 0.20},
        ]
    }
    svg, no_cost = render_cost_chart(agg)
    assert no_cost == []
    assert '<circle class="pt frontier"' in svg
    assert '<polyline class="frontier-line"' in svg
    # Agent names are labels; costs and rates land inside the viewBox.
    assert ">a</text>" in svg and ">c</text>" in svg


def test_render_cost_chart_no_cost_data_returns_none():
    svg, no_cost = render_cost_chart(
        {"overall": [{"agent": "naive", "passed": 0, "total": 2, "total_cost_usd": None}]}
    )
    assert svg is None
    assert no_cost == ["naive"]


def test_render_html_renders_cost_section(tmp_path: Path):
    write_run(
        tmp_path,
        "rich",
        [costly_record("monitoring-001", 1.0, True, 0.02)],
    )
    write_run(tmp_path, "naive", [record("monitoring-001", 0.0, False)])
    page = render_html(aggregate(load_results(tmp_path)))
    assert "Cost vs accuracy" in page
    assert '<circle class="pt' in page
    assert "No cost data: naive" in page


def test_render_html_empty_results_has_no_chart():
    page = render_html(aggregate(load_results(Path("/nonexistent"))))
    assert "Cost vs accuracy" not in page
# --- pass-rate CI column (issue #161) ----------------------------------------


def test_render_overall_shows_pass_rate_ci():
    # The point rate renders with the same deterministic interval
    # `bench.leaderboard` shows; all-pass runs have the degenerate exact one.
    agg = aggregate(
        {"naive": [record("t1", 1.0, True), record("t2", 1.0, True), record("t3", 0.0, False)]}
    )
    low, high = bootstrap_pass_rate_ci([True, True, False])
    expected = f"{2 / 3 * 100:.1f}% ({low * 100:.1f}&ndash;{high * 100:.1f})"
    page = render_html(agg)
    assert "Pass rate (95% CI)" in page
    assert expected in page


def test_render_ci_is_em_dash_without_current_records():
    agg = aggregate({"naive": [{"task": "t1", "score": 1.0, "passed": True}]})
    page = render_html(agg)
    assert "Pass rate (95% CI)" in page
    assert '<td class="num">&mdash;</td>' in page



# --- breakdown charts (issue #165) ---------------------------------------------


def seed_task(tasks_root: Path, task_id: str, difficulty: str) -> None:
    """A minimal task.yaml so the difficulty breakdown has something to read."""
    category, _, _ = task_id.partition("-00")
    task_dir = tasks_root / category / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.yaml").write_text(
        f"id: {task_id}\ndifficulty: {difficulty}\n", encoding="utf-8"
    )


def test_aggregate_by_difficulty_groups_and_orders(tmp_path: Path):
    tasks_root = tmp_path / "tasks"
    seed_task(tasks_root, "monitoring-001", "hard")
    seed_task(tasks_root, "monitoring-002", "easy")
    seed_task(tasks_root, "monitoring-003", "medium")
    write_run(
        tmp_path,
        "naive",
        [
            record("monitoring-001", 1.0, True),
            record("monitoring-002", 0.5, False),
            record("monitoring-003", 1.0, True),
        ],
    )
    agg = aggregate(load_results(tmp_path), difficulties=task_difficulties(tasks_root))
    assert [b["difficulty"] for b in agg["by_difficulty"]] == [
        "easy",
        "medium",
        "hard",
    ]
    easy = agg["by_difficulty"][0]["agents"]
    assert easy == [
        {
            "agent": "naive",
            "passed": 0,
            "total": 1,
            "mean_score": 0.5,
            "pass_rate": 0.0,
            "ci_low": 0.0,
            "ci_high": 0.0,
            "pass_at_n": None,
            "flaky": None,
            "repeats": None,
            "median_elapsed_s": None,
            "median_commands": None,
            "total_cost_usd": None,
        }
    ]


def test_aggregate_by_difficulty_unknown_without_task_yaml(tmp_path: Path):
    write_run(tmp_path, "naive", [record("monitoring-001", 1.0, True)])
    agg = aggregate(load_results(tmp_path), difficulties={})
    assert [b["difficulty"] for b in agg["by_difficulty"]] == ["unknown"]


def test_render_category_heatmap_cells_and_labels(tmp_path: Path):
    write_run(
        tmp_path,
        "naive",
        [
            record("monitoring-001", 1.0, True),
            record("monitoring-002", 1.0, True),
            record("cost-001", 0.0, False),
        ],
    )
    agg = aggregate(load_results(tmp_path))
    svg = render_category_heatmap(agg)
    assert '<svg class="chart heat-grid"' in svg
    assert ">100%</text>" in svg  # monitoring: 2/2
    assert ">0%</text>" in svg  # cost: 0/1
    assert "<title>naive: 2/2 passed</title>" in svg
    assert '<text class="cell-text light"' in svg  # high-rate cells go white


def test_render_category_heatmap_empty_is_none():
    assert render_category_heatmap({"by_category": [], "agents": []}) is None


def test_render_difficulty_chart_rows_and_bars(tmp_path: Path):
    tasks_root = tmp_path / "tasks"
    seed_task(tasks_root, "monitoring-001", "hard")
    seed_task(tasks_root, "monitoring-002", "easy")
    write_run(
        tmp_path,
        "naive",
        [record("monitoring-001", 1.0, True), record("monitoring-002", 0.0, False)],
    )
    agg = aggregate(load_results(tmp_path), difficulties=task_difficulties(tasks_root))
    svg = render_difficulty_chart(agg)
    assert '<svg class="chart diff-chart"' in svg
    assert '<rect class="bar bar-easy"' in svg  # easy row has a 0% bar (width 0)
    assert '<rect class="bar bar-hard"' in svg
    assert '<rect class="track"' in svg
    assert "<title>naive: 1/1 passed (hard)</title>" in svg


def test_render_difficulty_chart_empty_is_none():
    assert render_difficulty_chart({"by_difficulty": [], "agents": []}) is None


def test_render_html_includes_breakdown_charts(tmp_path: Path):
    write_run(tmp_path, "naive", [record("monitoring-001", 1.0, True)])
    page = render_html(aggregate(load_results(tmp_path)))
    assert "Category &times; agent" in page
    assert "Difficulty &times; agent" in page
    assert 'class="chart heat-grid"' in page
    assert 'class="chart diff-chart"' in page


def test_task_category_and_difficulties_importable_from_tasks(tmp_path: Path):
    # The #165 refactor moved both helpers to bench.tasks; the site module
    # re-exports them, and the canonical implementations behave the same.
    from bench import tasks as tasks_module

    assert tasks_module.task_category("aks-networking-001") == "aks-networking"
    assert tasks_module.task_category("mystery-999") == "other"
    seed_task(tmp_path / "tasks", "monitoring-001", "easy")
    assert tasks_module.task_difficulties(tmp_path / "tasks") == {
        "monitoring-001": "easy"
    }
# --- failure taxonomy chart (issue #163) --------------------------------------


def test_aggregate_carries_per_agent_failure_counts():
    agg = aggregate(
        {
            "agent": [
                record("cost-001", 1.0, True),
                record("cost-002", 0.0, False),  # unmatched diagnosis → wrong_layer
                {
                    **record("cost-003", 0.0, False),
                    "timed_out": True,
                },
            ]
        }
    )
    counts = agg["failures"]["agent"]
    assert counts["wrong_layer"] == 1
    assert counts["timeout"] == 1
    assert sum(counts.values()) == 2


def test_render_html_shows_failure_modes_section():
    agg = aggregate(
        {
            "agent": [
                record("cost-001", 1.0, True),
                {
                    **record("cost-002", 0.0, False),
                    "timed_out": True,
                },
            ],
            "clean": [record("cost-001", 1.0, True)],
        }
    )
    page = render_html(agg)
    assert "<h2>Failure modes</h2>" in page
    assert 'aria-label="Failure taxonomy per agent"' in page
    assert "timeout" in page
    assert "No failed results: clean" in page


def test_render_html_failure_section_without_any_failures():
    agg = aggregate({"agent": [record("cost-001", 1.0, True)]})
    page = render_html(agg)
    assert "<h2>Failure modes</h2>" in page
    assert "No failed results to classify yet." in page

