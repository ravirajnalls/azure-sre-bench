"""Tests for the task contribution pipeline (issue #120).

Covers the `bench new` scaffolder (next-free-ID computation, validating
skeleton, refusal to overwrite, CLI wiring) and the contribution templates
(the structured `new-task.yml` issue form and the PR checklist).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from bench.cli import main
from bench.new import next_task_id, scaffold_task
from bench.tasks import CATEGORIES, inspect_task

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- next_task_id (pure function) -------------------------------------------


def test_next_task_id_returns_001_for_empty_category(tmp_path: Path):
    assert next_task_id(tmp_path, "monitoring") == "monitoring-001"


def test_next_task_id_is_one_above_highest(tmp_path: Path):
    for n in ("000", "002", "007"):
        (tmp_path / "monitoring" / f"monitoring-{n}").mkdir(parents=True)
    assert next_task_id(tmp_path, "monitoring") == "monitoring-008"


def test_next_task_id_ignores_non_task_entries(tmp_path: Path):
    category_dir = tmp_path / "monitoring"
    category_dir.mkdir()
    (category_dir / "notes").mkdir()
    (category_dir / "monitoring-not-a-number").mkdir()
    assert next_task_id(tmp_path, "monitoring") == "monitoring-001"


def test_next_task_id_is_zero_padded():
    assert next_task_id(Path("nonexistent"), "cost") == "cost-001"


# --- scaffold_task -----------------------------------------------------------


@pytest.fixture
def scaffolded(tmp_path: Path) -> Path:
    return scaffold_task(tmp_path, "monitoring", "Pod crashloops after deploy")


def test_scaffold_output_passes_validate(scaffolded: Path):
    result = inspect_task(scaffolded)
    assert result.errors == [], result.errors


def test_scaffold_uses_next_free_id(scaffolded: Path):
    assert scaffolded.name == "monitoring-001"
    config = yaml.safe_load((scaffolded / "task.yaml").read_text())
    assert config["id"] == "monitoring-001"
    assert config["category"] == "monitoring"
    assert config["difficulty"] in {"easy", "medium", "hard"}


def test_scaffold_solution_passes_its_own_rubric(scaffolded: Path):
    """Placeholder rubric and reference answer interlock so solvability holds
    even before the contributor fills them in."""
    from bench.grader import grade

    answer = json.loads((scaffolded / "solution.json").read_text())
    rubric = json.loads((scaffolded / "expected.json").read_text())
    assert grade(answer, rubric)["passed"] is True


def test_scaffold_placeholder_fixtures_have_no_real_guids(scaffolded: Path):
    from bench.tasks import GUID_RE, PLACEHOLDER_GUID_RE

    for fixture in (scaffolded / "fixtures").iterdir():
        text = fixture.read_text()
        for match in GUID_RE.finditer(text):
            assert PLACEHOLDER_GUID_RE.fullmatch(match.group(0))


def test_scaffold_always_picks_free_ids_and_never_overwrites(tmp_path: Path):
    first = scaffold_task(tmp_path, "monitoring", "First")
    second = scaffold_task(tmp_path, "monitoring", "Second")
    assert first != second
    assert first.exists() and second.exists()


def test_scaffold_refuses_existing_directory(tmp_path: Path, monkeypatch):
    """Defensive TOCTOU guard: if the computed id's folder appears between
    computing the next id and creating it, the scaffold aborts instead of
    overwriting."""
    existing = tmp_path / "monitoring" / "monitoring-007"
    existing.mkdir(parents=True)
    monkeypatch.setattr("bench.new.next_task_id", lambda root, cat: "monitoring-007")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        scaffold_task(tmp_path, "monitoring", "Title")


def test_scaffold_rejects_unknown_category(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown category"):
        scaffold_task(tmp_path, "not-a-category", "Title")


# --- CLI wiring --------------------------------------------------------------


def test_bench_new_cli_end_to_end(tmp_path: Path, capsys):
    rc = main(
        ["new", "cost", "--title", "AVS spend doubled", f"--tasks-root={tmp_path}"]
    )
    assert rc == 0
    assert "cost-001" in capsys.readouterr().out
    assert inspect_task(tmp_path / "cost" / "cost-001").errors == []


def test_bench_new_cli_errors_on_unknown_category(capsys):
    assert main(["new", "nope", "--title='x'"]) == 2
    assert "unknown category" in capsys.readouterr().err


# --- contribution templates --------------------------------------------------


def test_issue_form_is_valid_yaml_with_required_fields():
    form = yaml.safe_load(
        (REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "new-task.yml").read_text()
    )
    assert form["name"] and form["description"]
    fields = {item["id"]: item for item in form["body"] if "id" in item}
    # The structured form must capture what the old new-task.md did — and
    # structure it: category, symptom, root cause, fixtures, red herring,
    # rubric, difficulty.
    assert set(fields) == {
        "category", "symptom", "root-cause", "fixtures",
        "red-herring", "rubric", "difficulty", "depends-on",
    }
    for field_id in ("category", "symptom", "root-cause", "fixtures",
                     "red-herring", "rubric", "difficulty"):
        assert fields[field_id]["validations"]["required"] is True, field_id


def test_issue_form_categories_match_validator():
    form = yaml.safe_load(
        (REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "new-task.yml").read_text()
    )
    category_field = next(
        item for item in form["body"] if item.get("id") == "category"
    )
    assert set(category_field["attributes"]["options"]) == set(CATEGORIES)


def test_old_markdown_issue_form_is_replaced():
    assert not (REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "new-task.md").exists()


def test_pr_template_mirrors_agents_definition_of_done():
    template = (
        (REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text()
    )
    # Every AGENTS.md section the definition of done depends on appears as a
    # checklist item.
    for expected in ("bench validate", "pytest -q", "bench catalog",
                     "One task per PR", "red herring"):
        assert expected in template


# --- explainer scaffold (issue #149) ------------------------------------------


def test_scaffold_includes_explainer_skeleton(scaffolded: Path):
    from bench.tasks import EXPLAINER_REQUIRED_HEADINGS

    explainer = (scaffolded / "explainer.md").read_text(encoding="utf-8")
    for heading in EXPLAINER_REQUIRED_HEADINGS:
        assert f"## {heading}" in explainer
    # The skeleton is valid output for `bench validate` as shipped.
    assert inspect_task(scaffolded).errors == []
