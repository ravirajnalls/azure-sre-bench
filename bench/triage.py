"""Alert-triage track: find the root alert in a storm (issue #145).

Several alerts fire at once; the agent identifies the one alert whose firing
caused all the others and classifies the rest as symptoms. The answer:

    {"root_alert": "<alert id>", "symptom_alerts": ["<id>", ...], "reasoning": str}

The rubric stores only the root — the symptom set is *derived* from the task's
`fixtures/alerts.json` (every alert id in the file except the root), so a
rubric can never drift from the evidence it is grounded in:

    {"root_alert": "ALT-09"}

- `root_alert` — exact match (normalized: case- and whitespace-insensitive).
  A wrong root fails with `root_alert_wrong` even with perfect classification:
  picking a symptom as the root is precisely the failure the task exists to
  catch.
- `symptom_alerts` — the classification half. Every listed id must be a true
  symptom; every label that names something else — another alert, the root
  itself, or an id that does not exist in the fixture at all — is a
  misclassification. The classification fraction is
  `max(0, hits - misclassified) / len(true_symptoms)`: listing everything is
  the volume attack (issue #93) and subtracts itself into failure. Passing
  the task requires the correct root **and** a classification fraction of at
  least 0.8 (issue #145).
- Labeling the root alert as a symptom contradicts a correct `root_alert`
  answer outright, so it is a named failure (`root_classified_as_symptom`)
  regardless of the fraction — the cheapest way to defeat a cheat that sets
  the root from the rubric and sprays every fixture id as a symptom.
- Duplicate entries in `symptom_alerts` are deduplicated (counted in the
  detail, never an error); unknown ids are misclassifications, not crashes.

There is no `fix` channel in an alert-triage answer, so the issue-#113 safety
scan does not apply; `unsafe` groups in a triage rubric are a validator error
(a rule with nothing to scan must never ship silently). Length caps (issue
#93) apply to `reasoning`, the only free-text field. `evidence` is never
searched and stays capped for storage by the runner.

The validator side (bench/tasks.py) grounds the rubric: the root alert id must
exist in `fixtures/alerts.json`, and the fixture itself must be well-formed
(parseable, ids present, no duplicate ids).
"""

from __future__ import annotations

import json
from pathlib import Path

from bench.grader import ANSWER_TOO_LONG, max_chars_for, normalize

KIND_TRIAGE = "alert-triage"

#: The alerts fixture every alert-triage task ships (issue #145).
ALERTS_FILENAME = "alerts.json"

#: Pass gate for the classification half (issue #145: ">=80% classification").
SYMPTOM_PASS_FRACTION = 0.8

ROOT_ALERT_WRONG = "root_alert_wrong"
ROOT_CLASSIFIED_AS_SYMPTOM = "root_classified_as_symptom"
SYMPTOM_FRACTION_BELOW_THRESHOLD = "symptom_fraction_below_threshold"
SYMPTOM_SET_UNAVAILABLE = "symptom_set_unavailable"
TRIAGE_RUBRIC_INVALID = "triage_rubric_invalid"

# The agent-facing answer contract, appended to the workspace `symptom.md`
# (bench.run) and the manual-protocol prompt (bench.manual) — the same role
# the remediation note plays for `patched_files` (issue #110).
ANSWER_CONTRACT_BLOCK = """

This is an alert-triage task: answer with a single JSON object of the shape
{"root_alert": "<alert id>", "symptom_alerts": ["<alert id>", ...],
"reasoning": "<why the root caused the rest>"}. `root_alert` is the one alert
whose firing caused all the others; `symptom_alerts` lists every other active
alert the root explains.
"""


def is_triage(task: dict | None) -> bool:
    """True when a task's `task.yaml` declares `kind: alert-triage`."""
    return isinstance(task, dict) and task.get("kind") == KIND_TRIAGE


def read_alerts(path: Path) -> tuple[list[str], list[str], str | None]:
    """Alert ids from an alerts fixture: `(ids, duplicates, error)`.

    The loader walks the parsed JSON document and collects every `alert_id`
    string value, in file order. It never raises: an unreadable or shape-
    broken file returns `([], [], error)` and the caller fails closed —
    triage grading needs the alert set, and guessing one would be worse than
    refusing. Duplicate ids are an authoring error the validator reports; the
    grader simply deduplicates.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        return [], [], f"unreadable ({exc})"
    except ValueError as exc:
        return [], [], f"does not parse as JSON ({exc})"

    ids: list[str] = []
    seen: set[str] = set()
    duplicates: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "alert_id" and isinstance(value, str) and value.strip():
                    alert_id = value.strip()
                    if alert_id in seen:
                        duplicates.append(alert_id)
                    else:
                        seen.add(alert_id)
                        ids.append(alert_id)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    if not ids:
        return [], [], "no alert_id values found"
    return ids, list(dict.fromkeys(duplicates)), None


def _length_reasons(answer: dict, rubric: dict) -> list[dict]:
    """A reason per free-text field that exceeds its length cap (issue #93)."""
    value = answer.get("reasoning")
    limit = max_chars_for(rubric)
    if isinstance(value, str) and len(value) > limit:
        return [
            {
                "reason": ANSWER_TOO_LONG,
                "field": "reasoning",
                "length": len(value),
                "limit": limit,
            }
        ]
    return []


def _symptom_entries(answer: dict) -> tuple[list[str], int]:
    """Normalized, deduplicated symptom labels: `(entries, duplicates_ignored)`.

    Non-string entries are dropped (they never name an alert); duplicates are
    counted, not doubled — an answer listing the same alert twice classified
    it once.
    """
    raw = answer.get("symptom_alerts")
    entries: list[str] = []
    raw_labels: list[object] = raw if isinstance(raw, list) else []
    for item in raw_labels:
        if not isinstance(item, str) or not item.strip():
            continue
        normalized = normalize(item)
        if normalized not in entries:
            entries.append(normalized)
    duplicates = sum(
        1 for item in raw_labels if isinstance(item, str) and item.strip()
    ) - len(entries)
    return entries, max(0, duplicates)


def grade_triage(answer: dict, rubric: dict, task_dir: Path | str) -> dict:
    """Grade an alert-triage answer against its rubric.

    Score is `(root_component + classification_fraction) / 2` — the issue-#145
    "exact root plus the fraction of correctly classified symptom alerts".
    Passing requires the correct root, a fraction of at least 0.8, and no
    reason fired. The alert set comes from `fixtures/alerts.json`; when that
    fixture is missing or unreadable the task fails closed with
    `symptom_set_unavailable` rather than grading against a guessed set.
    """
    answer = answer if isinstance(answer, dict) else {}
    rubric = rubric if isinstance(rubric, dict) else {}

    reasons: list[dict] = []

    # Rubric shape: fail closed on a malformed rubric (authoring bug).
    expected_root = rubric.get("root_alert")
    if not (isinstance(expected_root, str) and expected_root.strip()):
        reasons.append(
            {
                "reason": TRIAGE_RUBRIC_INVALID,
                "error": "'root_alert' must be a non-empty string",
            }
        )
    expected = normalize(expected_root) if isinstance(expected_root, str) else None

    # The alert set derives from the fixture, never from the answer.
    ids, duplicates, error = read_alerts(Path(task_dir) / "fixtures" / ALERTS_FILENAME)
    if error or not ids:
        reasons.append(
            {
                "reason": SYMPTOM_SET_UNAVAILABLE,
                "error": error or "no alert ids found",
                "file": f"fixtures/{ALERTS_FILENAME}",
            }
        )
    alert_ids = {normalize(alert_id) for alert_id in ids}
    true_symptoms = alert_ids - ({expected} if expected else set())

    # Root: exact match on the whole field.
    raw_root = answer.get("root_alert")
    actual_root = normalize(raw_root) if isinstance(raw_root, str) else ""
    root_matched = bool(expected) and actual_root == expected
    if not root_matched:
        reasons.append(
            {
                "reason": ROOT_ALERT_WRONG,
                "field": "root_alert",
                "expected": expected_root,
                "actual": actual_root,
            }
        )

    # Classification: hits minus mislabels, over the true symptom count.
    # A label equal to the claimed root classifies the root as its own
    # symptom — it is neither a hit nor free: it subtracts (issue #145).
    entries, duplicates_ignored = _symptom_entries(answer)
    hits = sum(
        1 for entry in entries if entry in true_symptoms and entry != actual_root
    )
    misclassified = len(entries) - hits
    raw_labels = answer.get("symptom_alerts")
    raw_labels = raw_labels if isinstance(raw_labels, list) else []
    unknown_ids = sorted(
        {
            item.strip()
            for item in raw_labels
            if isinstance(item, str) and normalize(item) not in alert_ids
        }
    )
    fraction = (
        max(0.0, (hits - misclassified) / len(true_symptoms))
        if true_symptoms
        else 0.0
    )
    if true_symptoms and entries and expected and expected in entries:
        # Naming the root a symptom contradicts the answer outright (#145).
        reasons.append(
            {
                "reason": ROOT_CLASSIFIED_AS_SYMPTOM,
                "field": "symptom_alerts",
                "alert_id": expected_root,
            }
        )
    if true_symptoms and fraction < SYMPTOM_PASS_FRACTION:
        reasons.append(
            {
                "reason": SYMPTOM_FRACTION_BELOW_THRESHOLD,
                "field": "symptom_alerts",
                "fraction": round(fraction, 3),
                "threshold": SYMPTOM_PASS_FRACTION,
            }
        )

    reasons += _length_reasons(answer, rubric)

    detail: dict[str, object] = {
        "root_alert": {
            "expected": expected_root,
            "actual": actual_root,
            "matched": root_matched,
        },
        "symptom_classification": {
            "alert_count": len(alert_ids),
            "true_symptoms": len(true_symptoms),
            "hits": hits,
            "misclassified": misclassified,
            "unknown_ids": unknown_ids,
            "duplicates_ignored": duplicates_ignored,
            "fixture_duplicates": duplicates,
            "fraction": fraction,
            "threshold": SYMPTOM_PASS_FRACTION,
        },
        # The result record reads this unconditionally (issue #113); triage
        # has no fix channel, so there is nothing to safety-scan.
        "safety_violations": [],
        "reasons": reasons,
    }
    # A self-contradictory answer — the true root also labeled a symptom —
    # forfeits the root component: the two claims cannot both hold (#145).
    root_component = 1.0 if root_matched and expected not in entries else 0.0
    score = (root_component + fraction) / 2
    return {
        "score": score,
        "passed": bool(true_symptoms) and root_matched and not reasons,
        "detail": detail,
    }
