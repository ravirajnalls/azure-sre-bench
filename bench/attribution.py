"""Change-attribution track: name the change that caused the incident (issue #144).

A diagnosis task asks *what* broke; a change-attribution task asks *which
change* broke it. The agent gets the usual incident evidence plus a change log
(`fixtures/changes.json` — recent deploys, PRs and config changes with ids,
diffs and timestamps), and must answer:

    {"culprit": "<change id>", "reasoning": str, "fix": str}

The rubric (`expected.json`) mirrors the diagnosis shape but swaps the
`root_cause` groups for an exact-match culprit:

    {"culprit": "CHG-1042", "fix": [[...]], "distractors": [[...]]}

- `culprit` — exact match (normalized: case- and whitespace-insensitive). An
  answer that names more than one change id in the `culprit` field fails with
  `multiple_culprits` even before the exact-match check: hedging across the
  change log is not attribution.
- `fix` — keyword groups, graded exactly like a diagnosis `fix` (issue #3).
- `distractors` — the red-herring change the fixtures suggest (issue #94
  semantics, shifted one field over): they are matched against the stated
  `reasoning` — the attribution task's diagnosis field — never against
  `culprit`, `fix`, or `evidence`. Blaming the decoy in the reasoning fails
  the task with `named_distractor` without changing the score.
- The `fix` field is a fix channel: the issue-#113 safety scan applies to it
  exactly as it does on diagnosis tasks. `reasoning` is diagnosis, not a fix
  channel, and is never safety-scanned — mirroring how `root_cause` is
  treated on diagnosis tasks.

Length caps (issue #93) apply to the free-text fields (`reasoning`, `fix`):
over the cap earns no credit for that field and fails with `answer_too_long`.
The `culprit` field is an id, not prose — it is exact-matched and never
length-capped. `evidence` is never searched (issue #93) and stays capped for
storage by the runner, as on every other kind.

The validator side (bench/tasks.py) grounds the rubric: the culprit id must
appear in a fixture (the change log), and a distractor alternative may not
overlap the culprit — a distractor containing the culprit id would fail every
correct reasoning that names it.
"""

from __future__ import annotations

import re

from bench.grader import (
    ANSWER_TOO_LONG,
    NAMED_DISTRACTOR,
    UNSAFE_FIX,
    distractor_groups,
    max_chars_for,
    normalize,
)
from bench.safety import (
    SCORE_PENALTY,
    find_violations,
    global_rules,
    rubric_unsafe_rules,
)

KIND_ATTRIBUTION = "change-attribution"

#: Free-text fields subject to the issue-#93 length cap. `culprit` is exact-
#: matched and never capped.
ATTRIBUTION_TEXT_FIELDS = ("reasoning", "fix")

#: A change-id-shaped token, e.g. `CHG-2213`, `DEP-1042`. Applied to the
#: upper-culprit field for the multi-id guard (issue #144 acceptance: answers
#: naming more than one change id fail).
CHANGE_ID_RE = re.compile(r"\b[A-Z][A-Z0-9]+-[0-9]+\b")

CULPRIT_WRONG = "culprit_wrong"
MULTIPLE_CULPRITS = "multiple_culprits"
ATTRIBUTION_RUBRIC_INVALID = "attribution_rubric_invalid"

# The agent-facing answer contract, appended to the workspace `symptom.md`
# (bench.run) and the manual-protocol prompt (bench.manual) — the same role
# the remediation note plays for `patched_files` (issue #110).
ANSWER_CONTRACT_BLOCK = """

This is a change-attribution task: answer with a single JSON object of the
shape {"culprit": "<change id>", "reasoning": "<why it caused the incident>",
"fix": "<the concrete remediation>"}. `culprit` must be exactly one change id
from the change log; `reasoning` must argue why that change caused the
incident and no other change did; `fix` states the remediation that undoes it.
"""


def is_attribution(task: dict | None) -> bool:
    """True when a task's `task.yaml` declares `kind: change-attribution`."""
    return isinstance(task, dict) and task.get("kind") == KIND_ATTRIBUTION


def change_ids_in(text: str) -> list[str]:
    """Distinct change-id-shaped tokens in `text`, upper-cased, order kept.

    The multi-id guard counts these: a `culprit` field holding two or more
    distinct ids is hedging, and hedging fails (issue #144).
    """
    if not isinstance(text, str):
        return []
    return list(dict.fromkeys(CHANGE_ID_RE.findall(text.upper())))


def _field_text(answer: dict, field: str) -> str:
    """Normalized searchable text for a free-text field; missing/non-string is empty."""
    value = answer.get(field) if isinstance(answer, dict) else None
    return normalize(value) if isinstance(value, str) else ""


def _length_reasons(answer: dict, rubric: dict) -> list[dict]:
    """A reason per free-text field that exceeds its length cap (issue #93)."""
    limit = max_chars_for(rubric)
    reasons: list[dict] = []
    for field in ATTRIBUTION_TEXT_FIELDS:
        value = answer.get(field)
        if isinstance(value, str) and len(value) > limit:
            reasons.append(
                {
                    "reason": ANSWER_TOO_LONG,
                    "field": field,
                    "length": len(value),
                    "limit": limit,
                }
            )
    return reasons


def _distractor_reasons(answer: dict, rubric: dict) -> list[dict]:
    """A reason per distractor group the `reasoning` field matches (issue #94).

    Same contract as the diagnosis grader's distractor check, shifted to the
    attribution task's diagnosis field: only the stated reasoning is searched,
    never `culprit`, `fix`, or `evidence`, and a match costs no score — it
    only flips `passed`.
    """
    text = _field_text(answer, "reasoning")
    reasons: list[dict] = []
    for index, group in enumerate(distractor_groups(rubric)):
        matched = next((alt for alt in group if normalize(alt) in text), None)
        if matched is not None:
            reasons.append(
                {
                    "reason": NAMED_DISTRACTOR,
                    "field": "reasoning",
                    "matched": matched,
                    "group": index,
                }
            )
    return reasons


def _match_groups(text: str, groups: list) -> list[dict]:
    """Match each concept group against `text`, recording the alternative that hit.

    Same group semantics as the diagnosis grader (issue #3): a group matches
    when any alternative is a normalized substring of the field text.
    """
    entries = []
    for group in groups:
        matched = None
        if isinstance(group, list):
            for alternative in group:
                if isinstance(alternative, str) and normalize(alternative) in text:
                    matched = alternative
                    break
        entries.append({"group": group, "matched": matched})
    return entries


def _rubric_reasons(rubric: dict) -> list[dict]:
    """Fail-closed reasons for a malformed attribution rubric.

    The validator flags these at authoring time; a rubric that reaches the
    grader anyway (manual submits, unit tests) must never grade as a pass.
    """
    reasons: list[dict] = []
    culprit = rubric.get("culprit")
    if not (isinstance(culprit, str) and culprit.strip()):
        reasons.append(
            {
                "reason": ATTRIBUTION_RUBRIC_INVALID,
                "error": "'culprit' must be a non-empty string",
            }
        )
    fix = rubric.get("fix")
    if not (
        isinstance(fix, list)
        and fix
        and all(
            isinstance(group, list)
            and group
            and all(isinstance(alt, str) and alt.strip() for alt in group)
            for group in fix
        )
    ):
        reasons.append(
            {
                "reason": ATTRIBUTION_RUBRIC_INVALID,
                "error": "'fix' must be a non-empty list of non-empty string lists",
            }
        )
    return reasons


def grade_attribution(answer: dict, rubric: dict) -> dict:
    """Grade a change-attribution answer against its rubric.

    The culprit is one scoring unit (exact match); each `fix` group is another.
    The task passes only when the culprit is exactly right, every fix group
    matches, the reasoning names no distractor, and no reason fired (length
    caps, safety). Score is `matched/total` minus the issue-#113 safety
    penalty, floored at zero.
    """
    answer = answer if isinstance(answer, dict) else {}
    rubric = rubric if isinstance(rubric, dict) else {}

    reasons = _rubric_reasons(rubric)
    reasons += _length_reasons(answer, rubric)
    reasons += _distractor_reasons(answer, rubric)

    # Culprit: exact match on the whole field, with the multi-id guard first
    # (issue #144: hedging across several change ids must fail by name).
    expected = rubric.get("culprit")
    expected_text = normalize(expected) if isinstance(expected, str) else None
    raw = answer.get("culprit")
    actual = normalize(raw) if isinstance(raw, str) else ""
    ids = change_ids_in(raw)
    if len(ids) > 1:
        reasons.append({"reason": MULTIPLE_CULPRITS, "field": "culprit", "ids": ids})
    culprit_matched = bool(expected_text) and actual == expected_text
    if not culprit_matched:
        reasons.append(
            {
                "reason": CULPRIT_WRONG,
                "field": "culprit",
                "expected": expected,
                "actual": actual,
            }
        )

    # Fix: keyword groups over the fix text; over-cap earns nothing (#93).
    fix_groups = rubric.get("fix") if isinstance(rubric.get("fix"), list) else []
    fix_text = _field_text(answer, "fix")
    fix_too_long = any(
        reason.get("field") == "fix" and reason["reason"] == ANSWER_TOO_LONG
        for reason in reasons
    )
    if fix_too_long:
        fix_entries = [{"group": group, "matched": None} for group in fix_groups]
    else:
        fix_entries = _match_groups(fix_text, fix_groups)

    # Safety: the fix field is a fix channel (issue #113) — scanned like a
    # diagnosis `fix`; reasoning is never scanned.
    violations = find_violations(
        fix_text, [*global_rules(), *rubric_unsafe_rules(rubric)]
    )
    reasons.extend(
        {
            "reason": UNSAFE_FIX,
            "rule": violation["rule"],
            "description": violation["description"],
            "matched": violation["matched"],
        }
        for violation in violations
    )

    total = 1 + len(fix_entries)
    matched = (1 if culprit_matched else 0) + sum(
        1 for entry in fix_entries if entry["matched"] is not None
    )
    detail: dict[str, object] = {
        "culprit": {
            "expected": expected,
            "actual": actual,
            "matched": culprit_matched,
            "change_ids": ids,
        },
        "fix": fix_entries,
        "reasons": reasons,
        "safety_violations": violations,
    }
    base = round(matched / total, 3) if total else 0.0
    return {
        "score": max(0.0, round(base - SCORE_PENALTY * len(violations), 3)),
        "passed": total > 0 and matched == total and not reasons,
        "detail": detail,
    }
