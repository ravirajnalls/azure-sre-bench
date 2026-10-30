"""Failure taxonomy over graded results (issue #163).

Every failed record is classified into exactly one category, so agents can be
compared by *how* they fail rather than only how often. The classifier reads
only what the runner and grader already recorded — no re-grading, no rubric
access — so it works on any results tree, including historical ones.

Precedence (the most fundamental failure explains the run):

1. ``timeout``            — the process was killed; there is no graded attempt.
2. ``no_answer``          — nothing parseable was submitted to grade.
3. ``named_distractor``   — the diagnosis names the red herring (grader-marked).
4. ``unsafe_fix``         — the fix trips a safety rule, regardless of the rest.
5. ``too_long``           — an over-cap answer earned nothing by rule, so its
                            partial matches say nothing about diagnosis quality.
6. ``right_cause_wrong_fix`` — the diagnosis fully matched; the fix did not.
7. ``wrong_layer``        — the diagnosis missed (the fallback).
"""

from __future__ import annotations

# Canonical order used by every table and chart.
CATEGORIES = (
    "named_distractor",
    "wrong_layer",
    "right_cause_wrong_fix",
    "too_long",
    "unsafe_fix",
    "no_answer",
    "timeout",
)

# Grader detail carries the same reason strings (bench.grader / bench.safety).
_NAMED_DISTRACTOR = "named_distractor"
_UNSAFE_FIX = "unsafe_fix"
_ANSWER_TOO_LONG = "answer_too_long"
_GRADED_FIELDS = ("root_cause", "fix")


def classify_failure(record: dict) -> str | None:
    """Return the taxonomy category for one failed record, or None for a pass.

    Tolerates records missing any field (manual grades, older schemas): every
    lookup is defensive, and an unrecognizable failure falls through to
    ``wrong_layer`` — the diagnosis did not match, whatever else happened.
    """
    if not isinstance(record, dict) or record.get("passed"):
        return None
    if record.get("timed_out"):
        return "timeout"
    # An absent `answer` (hand-graded or legacy records) says nothing about
    # what the agent submitted — only a present-but-empty answer is no_answer.
    if "answer" in record:
        answer = record.get("answer")
        answer = answer if isinstance(answer, dict) else {}
        if not any(str(answer.get(field) or "").strip() for field in _GRADED_FIELDS):
            return "no_answer"
    detail = record.get("detail")
    detail = detail if isinstance(detail, dict) else {}
    reasons = {
        reason.get("reason")
        for reason in detail.get("reasons", [])
        if isinstance(reason, dict)
    }
    if _NAMED_DISTRACTOR in reasons:
        return "named_distractor"
    if _UNSAFE_FIX in reasons or detail.get("safety_violations"):
        return "unsafe_fix"
    if _ANSWER_TOO_LONG in reasons:
        return "too_long"
    root_entries = [
        entry for entry in detail.get("root_cause", []) if isinstance(entry, dict)
    ]
    if root_entries and all(
        entry.get("matched") is not None for entry in root_entries
    ):
        return "right_cause_wrong_fix"
    return "wrong_layer"


def failure_counts(records: list[dict]) -> dict[str, int]:
    """Count failed records per category, zero-filled in canonical order."""
    counts = {category: 0 for category in CATEGORIES}
    for record in records:
        category = classify_failure(record)
        if category is not None:
            counts[category] += 1
    return counts


def failure_examples(records: list[dict]) -> dict[str, str]:
    """Map each present category to the first task that exhibited it."""
    examples: dict[str, str] = {}
    for record in records:
        category = classify_failure(record)
        if category is not None and category not in examples:
            examples[category] = str(record.get("task") or "unknown")
    return examples


def failures_report(agent: str, records: list[dict]) -> str:
    """Markdown block for `bench failures <agent>`: header line + table."""
    counts = failure_counts(records)
    failed = sum(counts.values())
    examples = failure_examples(records)
    plural = "s" if failed != 1 else ""
    lines = [
        f"{failed} failed result{plural} across {len(records)} for {agent}:",
        "",
        "| Category | Count | Share | Example task |",
        "|---|---|---|---|",
    ]
    for category in CATEGORIES:
        count = counts[category]
        share = f"{count / failed:.1%}" if failed else "—"
        lines.append(
            f"| {category} | {count} | {share} | {examples.get(category, '—')} |"
        )
    return "\n".join(lines)
