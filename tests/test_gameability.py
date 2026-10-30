"""Gameability gate: adversarial probes must not pass any task (issue #92).

Three probe agents — cheats that never investigate — run through the real
runner (`bench.run.run_task`) against every task in the corpus:

- `keyword-soup` reads the hidden rubric and emits the union of every
  alternative; it never reads fixtures.
- `fixture-dump` concatenates raw fixture text into every answer field.
- `minimal-dump` is the cheapest dump that stays under the length caps issue
  #93 proposes, so grader hardening cannot claim victory over an illegal dump
  alone (spec decision D1).

A task is *gameable* when any probe passes it. `tests/known_gameable.txt`
acknowledges the tasks gameable under the pre-hardening grader; the gate
enforces that list in BOTH directions — a gameable task missing from the list
fails CI, and a list entry no probe passes anymore fails CI too — so the list
must shrink to empty as grader hardening (#93/#94) and the rubric audits
(#96-#99) land. Probe results land under `results/adversarial-<probe>/`,
which the leaderboard and site renderers exclude.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agents.adversarial import fixture_dump, keyword_soup, minimal_dump
from bench import run as runner
from bench.grader import grade
from bench.remediation import grade_for_task

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_ROOT = REPO_ROOT / "tasks"
RESULTS_ROOT = REPO_ROOT / "results"
KNOWN_GAMEABLE = Path(__file__).resolve().parent / "known_gameable.txt"


def _probe_command(script: str) -> str:
    # Absolute script path: the runner resolves relative paths against the
    # caller's cwd, and the gate must not depend on pytest's cwd.
    return f"{sys.executable} {REPO_ROOT / 'agents' / 'adversarial' / script}"


PROBES = (
    ("keyword-soup", _probe_command("keyword_soup.py")),
    ("fixture-dump", _probe_command("fixture_dump.py")),
    ("minimal-dump", _probe_command("minimal_dump.py")),
)


@pytest.fixture(scope="module")
def gameable_by_probe() -> dict[str, set[str]]:
    """Run every probe against every task through the real runner.

    Writes `results/adversarial-<probe>/<task-id>.json` and returns the task
    ids each probe passed. One matrix per module: both directional tests read
    the same run.
    """
    task_dirs = runner.discover_tasks(TASKS_ROOT)
    assert task_dirs, f"no tasks discovered under {TASKS_ROOT} — the gate would be vacuous"
    passed: dict[str, set[str]] = {}
    for probe, command in PROBES:
        passed[probe] = set()
        for task_dir in task_dirs:
            record = runner.run_task(
                task_dir,
                command,
                name=f"adversarial-{probe}",
                results_root=RESULTS_ROOT,
            )
            if record["passed"]:
                passed[probe].add(str(record["task"]))
    return passed


def read_known_gameable() -> set[str]:
    """Task ids from `known_gameable.txt`; `#` comments and blanks ignored."""
    ids: set[str] = set()
    for line in KNOWN_GAMEABLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            ids.add(line)
    return ids


def gameable_tasks(passed: dict[str, set[str]]) -> set[str]:
    """Tasks at least one probe passes."""
    return set().union(*passed.values()) if passed else set()


# --- the gate: enforced in both directions ----------------------------------


def test_no_unlisted_task_is_gameable(gameable_by_probe):
    """A probe passing an unacknowledged task turns CI red."""
    unexpected = sorted(gameable_tasks(gameable_by_probe) - read_known_gameable())
    assert not unexpected, (
        f"GAMEABILITY: adversarial probes pass task(s) missing from "
        f"{KNOWN_GAMEABLE.name}: {', '.join(unexpected)}. A probe that never "
        "investigates must not pass — fix the rubric's specificity (or land "
        "the issue #93/#94 grader hardening that rejects this cheat). Update "
        "the list only deliberately, with the seed-run table in the PR."
    )


def test_known_gameable_has_no_stale_entries(gameable_by_probe):
    """The list must shrink: an entry no probe passes anymore must go."""
    stale = sorted(read_known_gameable() - gameable_tasks(gameable_by_probe))
    assert not stale, (
        f"STALE GATE LIST: no probe passes {', '.join(stale)} anymore. Remove "
        f"them from {KNOWN_GAMEABLE.name} so the list keeps shrinking toward "
        "empty as grader hardening and rubric audits land."
    )


def test_probe_results_written_to_adversarial_run_folders(gameable_by_probe):
    """The matrix writes `results/adversarial-<probe>/<task-id>.json` — the
    run-name convention the leaderboard and site renderers exclude."""
    for probe, _ in PROBES:
        run_dir = RESULTS_ROOT / f"adversarial-{probe}"
        assert run_dir.is_dir() and list(run_dir.glob("*.json")), (
            f"expected probe results under {run_dir}"
        )


# --- probe builders (pure functions) ----------------------------------------


RUBRIC = {
    "root_cause": [["coredns-custom", "custom configmap"], ["10.20.1.53"]],
    "fix": [["remove the forward", "restore the default"]],
}


def test_soup_answer_passes_its_own_rubric():
    """The cheat's core property: covering every alternative matches every group."""
    graded = grade(keyword_soup.soup_answer(RUBRIC), RUBRIC)
    assert graded["passed"] is True
    assert graded["score"] == 1.0


def test_soup_answer_covers_distractor_groups():
    """The cheat pastes the whole rubric — distractors included — so a
    distractored rubric trips its own `named_distractor` trap (issue #94).
    This is what lets the rubric audits (#96-#99) un-game a task."""
    rubric = {
        "root_cause": [["coredns-custom"], ["10.20.1.53"]],
        "fix": [["remove the forward"]],
        "distractors": [["dhcpoptions", "vnet dns servers"]],
    }
    graded = grade(keyword_soup.soup_answer(rubric), rubric)
    assert graded["passed"] is False
    assert any(
        reason["reason"] == "named_distractor" for reason in graded["detail"]["reasons"]
    )


def test_soup_tolerates_malformed_rubric_entries():
    rubric = {"root_cause": ["not-a-group", None, [1, "fine"], ["ok"]], "fix": None}
    answer = keyword_soup.soup_answer(rubric)
    assert "fine" in answer["root_cause"]
    assert "ok" in answer["root_cause"]
    assert answer["fix"] == ""
    assert answer["evidence"] == []


def test_find_rubric_missing_task_raises():
    with pytest.raises(FileNotFoundError):
        keyword_soup.find_rubric("no-such-task-999")


def test_fixture_dump_covers_all_fields(tmp_path: Path):
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "b.txt").write_text("second file", encoding="utf-8")
    (fixtures / "a.txt").write_text("first file", encoding="utf-8")

    answer = fixture_dump.dump_answer(fixtures)
    assert answer["root_cause"] == "first file\nsecond file"
    assert answer["evidence"] == ["first file", "second file"]
    assert answer["fix"] == answer["root_cause"]


def test_fixture_dump_missing_fixtures_dir_yields_empty_answer(tmp_path: Path):
    answer = fixture_dump.dump_answer(tmp_path / "fixtures")
    assert answer == {
        "root_cause": "",
        "evidence": [],
        "fix": "",
        "culprit": "",
        "reasoning": "",
        "root_alert": "",
        "symptom_alerts": [],
    }


def test_minimal_dump_stays_under_future_caps(tmp_path: Path):
    """Spec D1: this probe must remain *legal* under the issue-#93 caps."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    for index in range(12):  # more files, and longer, than the caps allow
        (fixtures / f"f{index}.txt").write_text("x" * 1000, encoding="utf-8")

    answer = minimal_dump.capped_dump_answer(fixtures)
    assert len(answer["root_cause"]) <= minimal_dump.ROOT_CAUSE_MAX_CHARS
    assert len(answer["fix"]) <= minimal_dump.FIX_MAX_CHARS
    assert len(answer["evidence"]) <= minimal_dump.EVIDENCE_MAX_ITEMS
    assert all(
        len(item) <= minimal_dump.EVIDENCE_ITEM_MAX_CHARS
        for item in answer["evidence"]
    )


# --- alert-triage probes (issue #145) -------------------------------------------------


def _triage_task(tmp_path: Path) -> Path:
    """A minimal alert-triage task dir for probe grading."""
    task_dir = tmp_path / "tasks" / "aks-ops" / "triage-sample"
    fixtures = task_dir / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "alerts.json").write_text(
        json.dumps({"alerts": [{"alert_id": f"ALT-0{index}"} for index in range(1, 5)]}),
        encoding="utf-8",
    )
    (task_dir / "expected.json").write_text(
        json.dumps({"root_alert": "ALT-01"}), encoding="utf-8"
    )
    return task_dir


def test_soup_copied_root_still_fails_triage(tmp_path: Path):
    """Copying the rubric's root_alert is the rubric-read cheat; without the
    alerts fixture the spray is empty and the 0.8 gate fails the answer."""
    rubric = {"root_alert": "ALT-01"}
    answer = keyword_soup.soup_answer(rubric)
    assert answer["root_alert"] == "ALT-01"
    assert answer["symptom_alerts"] == []
    result = grade_for_task(answer, rubric, _triage_task(tmp_path), None)
    assert result["passed"] is False


def test_fixture_dump_spray_fails_triage(tmp_path: Path):
    """The dump sprays every alert id it finds as a symptom — the volume
    attack the triage grader subtracts into failure."""
    task_dir = _triage_task(tmp_path)
    rubric = json.loads((task_dir / "expected.json").read_text(encoding="utf-8"))
    answer = fixture_dump.dump_answer(task_dir / "fixtures")
    result = grade_for_task(answer, rubric, task_dir, None)
    assert result["passed"] is False
