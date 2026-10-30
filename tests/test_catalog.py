"""Tests for `bench catalog` and the docs/TASKS.md generation (issue #42)."""

from pathlib import Path

import yaml

from bench.catalog import cmd_catalog, render_catalog
from bench.cli import main
from bench.tasks import inspect_task, load_tasks

TASK_YAML = """\
id: {task_id}
title: {title}
category: {category}
difficulty: {difficulty}
tags:
{tags}
symptom: Something is intermittently returning 503s and the root cause is a
  stale federated credential subject nobody may ever read here.
"""


def make_task(
    root: Path,
    category: str,
    task_id: str,
    title: str,
    difficulty: str = "easy",
    tags: list[str] | None = None,
) -> Path:
    tags = tags if tags is not None else [category, "example"]
    task_dir = root / category / task_id
    (task_dir / "fixtures").mkdir(parents=True)
    task_yaml = TASK_YAML.format(
        task_id=task_id,
        title=title,
        category=category,
        difficulty=difficulty,
        tags="\n".join(f"  - {tag}" for tag in tags),
    )
    (task_dir / "task.yaml").write_text(task_yaml, encoding="utf-8")
    return task_dir


def test_render_groups_by_category_and_sorts(tmp_path):
    make_task(tmp_path, "monitoring", "monitoring-001", "Metrics show 503s")
    make_task(tmp_path, "aks-networking", "aks-networking-002", "B Title")
    make_task(tmp_path, "aks-networking", "aks-networking-001", "A Title", "hard",
              ["aks-networking", "ingress"])

    text = render_catalog(load_tasks(tmp_path))

    sections = [line for line in text.splitlines() if line.startswith("## ")]
    assert sections == ["## aks-networking", "## monitoring"]
    # Within a category, rows sort by task id, not authoring order.
    assert text.index("aks-networking-001 |") < text.index("aks-networking-002 |")
    assert "| aks-networking-001 | A Title | hard | aks-networking, ingress |" in text


def test_render_only_exposes_public_fields(tmp_path):
    make_task(tmp_path, "monitoring", "monitoring-001", "Metrics show 503s")
    text = render_catalog(load_tasks(tmp_path))
    # task.yaml fields beyond id/title/difficulty/tags (like the symptom, and
    # anything from expected.json/solution.json) must never reach the catalog.
    assert "federated" not in text
    assert "symptom" not in text


def test_render_escapes_pipes_in_cells(tmp_path):
    make_task(tmp_path, "cost", "cost-001", "Budget | alerts misfire", tags=["cost"])
    text = render_catalog(load_tasks(tmp_path))
    assert "Budget \\| alerts misfire" in text


def test_render_with_empty_corpus():
    text = render_catalog([])
    assert "0 tasks in 0 categories" in text


def test_render_shows_placeholder_for_unparseable_task(tmp_path):
    task_dir = tmp_path / "monitoring" / "monitoring-001"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text(": not-a-mapping", encoding="utf-8")
    text = render_catalog([inspect_task(task_dir)])
    assert "| monitoring-001 | - | - | - |" in text


def test_cmd_catalog_writes_file_and_is_idempotent(tmp_path, capsys):
    make_task(tmp_path / "tasks", "monitoring", "monitoring-001", "Metrics show 503s")
    output = tmp_path / "docs" / "TASKS.md"

    assert cmd_catalog(tmp_path / "tasks", output) == 0
    first = output.read_text(encoding="utf-8")
    assert cmd_catalog(tmp_path / "tasks", output) == 0
    assert output.read_text(encoding="utf-8") == first
    assert "wrote" in capsys.readouterr().out


def test_cmd_catalog_regeneration_detects_corpus_change(tmp_path):
    tasks_root = tmp_path / "tasks"
    output = tmp_path / "docs" / "TASKS.md"
    cmd_catalog(tasks_root, output)
    before = output.read_text(encoding="utf-8")

    make_task(tasks_root, "monitoring", "monitoring-001", "Metrics show 503s")
    cmd_catalog(tasks_root, output)
    assert output.read_text(encoding="utf-8") != before
    assert "1 task in 1 category" in output.read_text(encoding="utf-8")


def test_catalog_via_main(tmp_path):
    tasks_root = tmp_path / "tasks"
    make_task(tasks_root, "monitoring", "monitoring-001", "Metrics show 503s")
    output = tmp_path / "docs" / "TASKS.md"

    assert main(["catalog", str(tasks_root), "--output", str(output)]) == 0
    assert "monitoring-001" in output.read_text(encoding="utf-8")


def test_committed_catalog_matches_corpus():
    """docs/TASKS.md must equal a fresh render of tasks/ (CI's staleness check)."""
    committed = Path("docs/TASKS.md").read_text(encoding="utf-8")
    assert committed == render_catalog(load_tasks(Path("tasks")))


def test_task_yaml_fixture_format_is_valid_yaml():
    # The heredoc template used by make_task must yield the keys the loader needs.
    config = yaml.safe_load(TASK_YAML.format(
        task_id="x-001", title="T", category="monitoring", difficulty="easy",
        tags="  - monitoring\n",
    ))
    assert config["id"] == "x-001"
