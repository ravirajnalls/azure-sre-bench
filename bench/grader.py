"""Keyword-group grader (issue #3).

Scores an agent answer against a task's hidden rubric (`expected.json`). The rubric
maps each graded field (`root_cause`, `fix`) to a list of concept groups; a group
matches when any of its alternative keywords appears as a substring of the
normalized field text. For `root_cause`, the joined `evidence` list is appended to
the searched text so a citation can satisfy a group. Evidence is never searched for
`fix`: the fix must be stated in the answer itself, not merely cited.
"""

from __future__ import annotations

import re

GRADED_FIELDS = ("root_cause", "fix")

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase and collapse every whitespace run to a single space."""
    return _WHITESPACE.sub(" ", text.strip().lower())


def _field_text(answer: dict, field: str) -> str:
    """Normalized searchable text for a graded field.

    Missing or non-string field values count as empty; this never raises. Evidence
    is joined and appended only for `root_cause`.
    """
    parts: list[str] = []
    value = answer.get(field)
    if isinstance(value, str):
        parts.append(normalize(value))
    if field == "root_cause":
        evidence = answer.get("evidence")
        if isinstance(evidence, list):
            parts.append(normalize(" ".join(str(item) for item in evidence)))
    return " ".join(part for part in parts if part)


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

    Returns `{"score": matched/total (3 dp), "passed": all matched, "detail": {...}}`
    where detail maps each graded field to its groups and the alternative that
    matched (or None). An empty rubric scores 0.0 and never passes.
    """
    detail: dict[str, list[dict]] = {}
    total = 0
    matched = 0
    for field in GRADED_FIELDS:
        text = _field_text(answer if isinstance(answer, dict) else {}, field)
        groups = rubric.get(field, []) if isinstance(rubric, dict) else []
        entries = _match_groups(text, groups if isinstance(groups, list) else [])
        detail[field] = entries
        total += len(entries)
        matched += sum(1 for entry in entries if entry["matched"] is not None)
    return {
        "score": round(matched / total, 3) if total else 0.0,
        "passed": total > 0 and matched == total,
        "detail": detail,
    }
