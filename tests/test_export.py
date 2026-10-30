"""Dataset export and dataset card tests (issue #117).

The centerpiece is the leak guarantee (the issue's acceptance criterion): no
rubric (`expected.json`) or reference-answer (`solution.json`) content reaches
`dataset.jsonl` — checked against the real 32-task corpus, where a hidden
string colliding with public text would be a real leak. Held-out exclusion
(issue #115 interplay), the eval-file round-trip through the real grader, and
the dataset card's required sections are each covered on synthetic corpora
built from the valid fixture task.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from bench.cli import main
from bench.export import (
    DATASET_CARD_FILENAME,
    EVAL_DATASET_FILENAME,
    PUBLIC_DATASET_FILENAME,
    export_dataset,
)
from bench.remediation import grade_for_task
from bench.heldout import encrypt_hidden, generate_identity, load_hidden_json
from bench.tasks import load_tasks

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_TASKS = REPO_ROOT / "tasks"
BASE_TASK = Path(__file__).parent / "data" / "tasks" / "monitoring" / "monitoring-001"

# Degenerate rubric fragments shorter than this (e.g. identity-027's 'fic')
# collide with ordinary words in any JSON record; substring checks are noise
# at that length. The structural checks below own the short-fragment failure
# mode instead.
MIN_KEYWORD_LENGTH = 8

PUBLIC_KEYS = {
    "id", "title", "category", "difficulty", "tags", "symptom", "canary",
    "split", "fixtures",
}


def make_task(tmp_path: Path, name: str = "monitoring-001") -> Path:
    dest = tmp_path / "tasks" / "monitoring" / name
    shutil.copytree(BASE_TASK, dest)
    _rename_task(dest, name)
    return dest


def _rename_task(task_dir: Path, task_id: str) -> None:
    """Align the copied fixture task's `id` (and folder name) with `task_id`."""
    config = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
    config["id"] = task_id
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )


def make_heldout_task(tmp_path: Path, key: str, name: str = "monitoring-002") -> Path:
    task_dir = make_task(tmp_path, name)
    config = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
    config["split"] = "heldout"
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    encrypt_hidden(task_dir, key)
    return task_dir


def read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture
def no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HELDOUT_KEY", raising=False)


def test_public_record_shape(tmp_path, no_key):
    """The public record carries exactly the public fields, fixtures inlined."""
    make_task(tmp_path)
    out = tmp_path / "dist"
    export_dataset(tmp_path / "tasks", out)
    record = read_jsonl(out / PUBLIC_DATASET_FILENAME)[0]
    assert set(record) == PUBLIC_KEYS
    config = yaml.safe_load((BASE_TASK / "task.yaml").read_text(encoding="utf-8"))
    assert record["id"] == config["id"]
    assert record["symptom"] == config["symptom"].strip()
    for name, content in record["fixtures"].items():
        assert content == (BASE_TASK / "fixtures" / name).read_text(encoding="utf-8")
    assert record["fixtures"], "fixtures must be inlined, not referenced"


def test_no_rubric_or_solution_content_reaches_public_export(tmp_path, no_key):
    """The issue's acceptance criterion, run against the whole real corpus.

    A leak is hidden content present in the export that the corpus's own
    public material does not already contain. Rubric alternatives and
    reference evidence are deliberately grounded in public fixture/symptom
    text (the grader substring-matches that vocabulary), so a hidden string
    already public in the corpus is skipped — anything else appearing in the
    export means the hidden files reached it. Structural checks (exact record
    keys, exact fixture set) catch the whole-document dump failure mode.
    """
    out = tmp_path / "dist"
    summary = export_dataset(REAL_TASKS, out)
    public_text = (out / PUBLIC_DATASET_FILENAME).read_text(encoding="utf-8")
    records = read_jsonl(out / PUBLIC_DATASET_FILENAME)
    # 1. Records carry exactly the public fields — no expected/solution keys.
    assert records and all(set(record) == PUBLIC_KEYS for record in records)
    assert summary.public_count == len(records)

    for result in load_tasks(REAL_TASKS):
        config: dict = result.config or {}
        record = next(r for r in records if r["id"] == result.id)
        record_text = json.dumps(record, ensure_ascii=False)
        # 2. Fixtures are the on-disk fixture files only — no hidden file smuggled in.
        fixture_files = [
            path for path in (result.path / "fixtures").iterdir() if path.is_file()
        ]
        assert set(record["fixtures"]) == {path.name for path in fixture_files}
        assert not ({"expected.json", "solution.json"} & set(record["fixtures"]))
        # The task's own public material: fixtures (names and contents),
        # symptom, title, tags, id.
        already_public = (
            record["id"] + record["title"] + record["symptom"]
            + json.dumps(record["tags"]) + "".join(record["fixtures"].values())
            + "".join(record["fixtures"])
        )
        for hidden_name in ("expected.json", "solution.json"):
            hidden = load_hidden_json(result.path, hidden_name, config=config)
            for value in _string_leaves(hidden):
                if value in already_public or len(value) < MIN_KEYWORD_LENGTH:
                    continue
                assert value not in record_text, (
                    f"hidden {hidden_name} content leaked into the public "
                    f"export for {result.id}: {value[:80]!r}"
                )


def _string_leaves(data: object) -> list[str]:
    """Every string in a hidden JSON document, however deeply nested."""
    if isinstance(data, str):
        return [data]
    if isinstance(data, dict):
        return [s for value in data.values() for s in _string_leaves(value)]
    if isinstance(data, list):
        return [s for value in data for s in _string_leaves(value)]
    return []


def test_heldout_excluded_from_public_export(tmp_path, no_key):
    """Held-out tasks appear in neither file without HELDOUT_KEY (issue #115)."""
    key = generate_identity()
    make_task(tmp_path, "monitoring-001")
    make_heldout_task(tmp_path, key, "monitoring-002")
    out = tmp_path / "dist"
    summary = export_dataset(tmp_path / "tasks", out)

    public_ids = [record["id"] for record in read_jsonl(out / PUBLIC_DATASET_FILENAME)]
    eval_ids = [record["id"] for record in read_jsonl(out / EVAL_DATASET_FILENAME)]
    assert public_ids == ["monitoring-001"]
    assert eval_ids == ["monitoring-001"]
    assert summary.public_count == 1
    assert summary.heldout_skipped == 1
    assert summary.eval_locked == 1
    assert "monitoring-002" not in (out / PUBLIC_DATASET_FILENAME).read_text("utf-8")


def test_heldout_reaches_eval_file_only_with_key(tmp_path, monkeypatch):
    """With HELDOUT_KEY, the held-out task is eval-side only — never public."""
    key = generate_identity()
    make_task(tmp_path, "monitoring-001")
    make_heldout_task(tmp_path, key, "monitoring-002")
    monkeypatch.setenv("HELDOUT_KEY", key)
    out = tmp_path / "dist"
    summary = export_dataset(tmp_path / "tasks", out)

    public_ids = [record["id"] for record in read_jsonl(out / PUBLIC_DATASET_FILENAME)]
    eval_ids = [record["id"] for record in read_jsonl(out / EVAL_DATASET_FILENAME)]
    assert public_ids == ["monitoring-001"]
    assert eval_ids == ["monitoring-001", "monitoring-002"]
    assert summary.eval_count == 2
    assert "monitoring-002" not in (out / PUBLIC_DATASET_FILENAME).read_text("utf-8")


def test_eval_file_round_trips_through_the_real_grader(tmp_path, no_key):
    """Every eval record grades its own reference solution to a pass."""
    out = tmp_path / "dist"
    summary = export_dataset(REAL_TASKS, out)
    eval_records = read_jsonl(out / EVAL_DATASET_FILENAME)
    assert summary.eval_count == len(eval_records)
    # Grade through the shared entry point (grade_for_task), not the raw
    # diagnosis grader: eval records now include non-diagnosis kinds (e.g.
    # pre-incident reviews) whose rubrics the shape-blind grader cannot read.
    tasks = {task.id: task for task in load_tasks(REAL_TASKS)}
    for record in eval_records:
        task = tasks[record["id"]]
        result = grade_for_task(
            record["solution"], record["expected"], task.path, task.config
        )
        assert result["passed"], f"reference solution for {record['id']} failed"


def test_eval_rubric_matches_expected_json(tmp_path, no_key):
    """The eval record's rubric is the task's rubric, byte-for-byte in meaning."""
    out = tmp_path / "dist"
    export_dataset(REAL_TASKS, out)
    eval_records = {record["id"]: record for record in read_jsonl(out / EVAL_DATASET_FILENAME)}
    for result in load_tasks(REAL_TASKS):
        config: dict = result.config or {}
        expected = load_hidden_json(result.path, "expected.json", config=config)
        assert eval_records[result.id]["expected"] == expected


def test_dataset_card_sections_and_frontmatter(tmp_path, no_key):
    """The card is HF-format: frontmatter plus the issue's required sections."""
    make_task(tmp_path)
    out = tmp_path / "dist"
    export_dataset(tmp_path / "tasks", out)
    card = (out / DATASET_CARD_FILENAME).read_text(encoding="utf-8")
    assert card.startswith("<!--")
    assert card.split("---")[1].count("license: cc-by-4.0") == 1
    for section in (
        "## Motivation", "## Composition", "## Collection process",
        "## Intended use", "## Limitations", "## License", "## Canary",
    ):
        assert section in card
    assert "00000000-0000-0000-0000-000000000114" in card


def test_card_reports_heldout_exclusion(tmp_path, no_key):
    make_task(tmp_path, "monitoring-001")
    make_heldout_task(tmp_path, generate_identity(), "monitoring-002")
    out = tmp_path / "dist"
    export_dataset(tmp_path / "tasks", out)
    card = (out / DATASET_CARD_FILENAME).read_text(encoding="utf-8")
    assert "held-out task(s)" in card


def test_export_cli(tmp_path, no_key):
    out = tmp_path / "dist"
    assert main(["export", "--out", str(out), "--tasks-root", str(REAL_TASKS)]) == 0
    assert (out / PUBLIC_DATASET_FILENAME).is_file()
    assert (out / EVAL_DATASET_FILENAME).is_file()
    assert (out / DATASET_CARD_FILENAME).is_file()


def test_export_cli_rejects_unknown_format(tmp_path):
    with pytest.raises(SystemExit):
        main(["export", "--format", "csv", "--out", str(tmp_path)])
