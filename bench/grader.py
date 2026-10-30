"""Keyword-group grader (issue #3), hardened against volume attacks (issue #93).

Scores an agent answer against a task's hidden rubric (`expected.json`). The rubric
maps each graded field (`root_cause`, `fix`) to a list of concept groups; a group
matches when any of its alternative keywords appears as a substring of the
normalized field text.

Hardening (issue #93):

- A graded field longer than the length cap — 600 characters by default, or the
  task's own `max_chars` from `expected.json` — earns no credit and fails the
  task with the `answer_too_long` reason.
- `evidence` is never searched for any field: the diagnosis must be stated in
  the answer itself, not merely cited. Evidence is still stored in results,
  capped by `cap_evidence` to at most 8 items of 300 characters each; overflow
  is ignored, never an error.
- `detail` carries a `reasons` list naming every failure. Each reason is a dict
  with a stable `reason` identifier plus context (`field`, `length`, `limit`
  for `answer_too_long`).
"""

from __future__ import annotations

import re

GRADED_FIELDS = ("root_cause", "fix")

# Volume caps (issue #93). A graded field over its cap fails the task; a task
# may raise the cap with a `max_chars` key in its `expected.json`.
DEFAULT_MAX_CHARS = 600
MAX_EVIDENCE_ITEMS = 8
MAX_EVIDENCE_ITEM_CHARS = 300

ANSWER_TOO_LONG = "answer_too_long"

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase and collapse every whitespace run to a single space."""
    return _WHITESPACE.sub(" ", text.strip().lower())


def max_chars_for(rubric: dict) -> int:
    """Length cap for graded fields: the rubric's `max_chars` when it is a
    positive integer, `DEFAULT_MAX_CHARS` otherwise (defensive: a bool or
    nonsense value falls back to the default rather than raising)."""
    override = rubric.get("max_chars") if isinstance(rubric, dict) else None
    if isinstance(override, int) and not isinstance(override, bool) and override > 0:
        return override
    return DEFAULT_MAX_CHARS


def cap_evidence(answer: dict) -> dict:
    """The answer with its `evidence` list capped for storage (issue #93).

    At most `MAX_EVIDENCE_ITEMS` items of at most `MAX_EVIDENCE_ITEM_CHARS`
    characters; overflow is ignored, never an error. Answers without a list
    `evidence` are returned unchanged — the cap bounds result size, it never
    invents or rewrites fields.
    """
    evidence = answer.get("evidence") if isinstance(answer, dict) else None
    if not isinstance(evidence, list):
        return answer
    return {
        **answer,
        "evidence": [
            str(item)[:MAX_EVIDENCE_ITEM_CHARS]
            for item in evidence[:MAX_EVIDENCE_ITEMS]
        ],
    }


def _field_text(answer: dict, field: str) -> str:
    """Normalized searchable text for a graded field.

    Missing or non-string field values count as empty; this never raises.
    Evidence is deliberately not part of the searched text (issue #93): a
    diagnosis must be stated in the field, not merely cited.
    """
    value = answer.get(field) if isinstance(answer, dict) else None
    return normalize(value) if isinstance(value, str) else ""


def _length_reasons(answer: dict, rubric: dict) -> list[dict]:
    """A reason per graded field that exceeds its length cap."""
    limit = max_chars_for(rubric)
    reasons: list[dict] = []
    for field in GRADED_FIELDS:
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


def _match_groups(text: str, groups: list) -> list[dict]:
    """Match each concept group against `text`, recording the alternative that hit."""
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


def grade(answer: dict, rubric: dict) -> dict:
    """Score an agent answer against a rubric.

    Returns `{"score": matched/total (3 dp), "passed": all matched and no
    violations, "detail": {...}}` where detail maps each graded field to its
    groups and the alternative that matched (or None), plus a `reasons` list
    naming every failure. A graded field longer than its cap earns no credit —
    its groups are all unmatched — and fails the task with `answer_too_long`
    even when every group matches. An empty rubric scores 0.0 and never passes.
    """
    answer = answer if isinstance(answer, dict) else {}
    reasons = _length_reasons(answer, rubric)
    too_long = {reason["field"] for reason in reasons}
    detail: dict[str, object] = {}
    total = 0
    matched = 0
    for field in GRADED_FIELDS:
        groups = rubric.get(field, []) if isinstance(rubric, dict) else []
        groups = groups if isinstance(groups, list) else []
        if field in too_long:
            # An over-cap answer earns nothing: skip matching entirely.
            entries = [{"group": group, "matched": None} for group in groups]
        else:
            entries = _match_groups(_field_text(answer, field), groups)
        detail[field] = entries
        total += len(entries)
        matched += sum(1 for entry in entries if entry["matched"] is not None)
    detail["reasons"] = reasons
    return {
        "score": round(matched / total, 3) if total else 0.0,
        "passed": total > 0 and matched == total and not reasons,
        "detail": detail,
    }
