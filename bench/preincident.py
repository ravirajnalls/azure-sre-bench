"""Pre-incident-review track: grade findings on a change before it ships (issue #147).

Catch the outage before it happens: the agent reviews a Bicep, Terraform, or
Kubernetes change shipped as `fixtures/` and reports the defect that would
cause the next incident. The answer contract extends the diagnosis shape with
a `findings` list — at most `MAX_FINDINGS` objects:

    {"findings": [{"file": str, "line": int, "issue": str, "fix": str}]}

Each finding names one change that would cause an incident: the reviewed file
(basename match; a leading `fixtures/` is accepted), its 1-based line in that
file, what will break when it ships, and the fix to make before merge.

The rubric (`expected.json`) declares the required finding(s) with the same
key plus keyword groups on the `issue` text:

    {"findings": [{"file": "aks.bicep", "line": 57,
                   "issue": [["maxsurge", "surge"], ["subnet", "ip"]]}]}

Grading (issue #147):

- A required finding is satisfied when the agent reports it with the same
  file, a line within `LINE_TOLERANCE` (±3), and every issue group matched as
  a substring of the finding's `issue` text.
- Precision matters: findings that satisfy no rubric finding are false. More
  than `MAX_FALSE_FINDINGS` (2) of them fail the task — a review that lists
  the whole diff as dangerous has not prioritized anything.
- Distractor groups (issue #94 machinery) name the change's red herrings:
  the plausible-looking edits an agent that stops too early would report.
  When any agent finding's `issue` text matches a distractor group the task
  fails with reason `named_distractor` — reporting the decoy as an incident
  cause is a failed review, even when the real defect was also found.
  Distractors never touch `fix` text and cost no score, only `passed`.

Safety (issue #113): every finding's `fix` text is a fix channel and is
scanned with the same rule engine as the diagnosis grader's `fix` field; each
violation subtracts `SCORE_PENALTY` and fails the task. Finding texts over
the rubric's length cap (600 characters by default) earn no match, mirroring
the `answer_too_long` rule.
"""

from __future__ import annotations

from bench.grader import (
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

KIND_PRE_INCIDENT_REVIEW = "pre-incident-review"
FINDINGS_KEY = "findings"

# Answer contract limits (issue #147).
MAX_FINDINGS = 5
MAX_FALSE_FINDINGS = 2
LINE_TOLERANCE = 3

# Stable reason identifiers recorded in `detail["reasons"]`.
FINDINGS_INVALID = "findings_invalid"
FINDINGS_OVERFLOW = "findings_overflow"
MISSING_REQUIRED_FINDING = "missing_required_finding"
TOO_MANY_FALSE_FINDINGS = "too_many_false_findings"
ANSWER_TOO_LONG = "answer_too_long"


def is_pre_incident_review(task: dict | None) -> bool:
    """True when a task's `task.yaml` declares `kind: pre-incident-review`."""
    return isinstance(task, dict) and task.get("kind") == KIND_PRE_INCIDENT_REVIEW


def normalize_file(path: object) -> str | None:
    """The reviewed file's basename, lowercased, or None for a blank value.

    Agents answer from their workspace, so `aks.bicep` and
    `fixtures/aks.bicep` name the same reviewed file; the basename is what
    the rubric pins.
    """
    if not isinstance(path, str) or not path.strip():
        return None
    return path.strip().lower().rsplit("/", 1)[-1] or None


def rubric_findings(rubric: dict) -> list:
    """The rubric's well-formed required findings, defensively.

    A malformed entry is skipped here and flagged by the validator
    (`bench/tasks.py`) at authoring time; the grader must never raise on a
    hostile or half-written rubric.
    """
    raw = rubric.get(FINDINGS_KEY) if isinstance(rubric, dict) else None
    if not isinstance(raw, list):
        return []
    return [
        entry
        for entry in raw
        if isinstance(entry, dict)
        and isinstance(entry.get("file"), str)
        and entry["file"].strip()
        and isinstance(entry.get("line"), int)
        and not isinstance(entry.get("line"), bool)
        and entry["line"] >= 1
        and isinstance(entry.get("issue"), list)
        and entry["issue"]
        and all(
            isinstance(group, list)
            and group
            and all(isinstance(alt, str) and alt.strip() for alt in group)
            for group in entry["issue"]
        )
    ]


def _agent_findings(answer: dict) -> tuple[list, list[dict]]:
    """The answer's findings list, or the reason it is unusable."""
    findings = answer.get(FINDINGS_KEY)
    if not isinstance(findings, list) or not all(
        isinstance(finding, dict) for finding in findings
    ):
        return [], [{"reason": FINDINGS_INVALID}]
    return findings, []


def _finding_field(finding: dict, field: str) -> str:
    """Normalized searchable text for one finding field (empty when absent)."""
    value = finding.get(field)
    return normalize(value) if isinstance(value, str) else ""


def _over_cap_reasons(findings: list, limit: int) -> list[dict]:
    """A reason per finding text over the rubric's length cap."""
    reasons = []
    for index, finding in enumerate(findings):
        for field in ("issue", "fix"):
            text = finding.get(field)
            if isinstance(text, str) and len(text) > limit:
                reasons.append(
                    {
                        "reason": ANSWER_TOO_LONG,
                        "field": f"{FINDINGS_KEY}[{index}].{field}",
                        "length": len(text),
                        "limit": limit,
                    }
                )
    return reasons


def _matches(agent_finding: dict, required: dict) -> bool:
    """File match + line within ±3 + every issue group, per issue #147."""
    if normalize_file(agent_finding.get("file")) != normalize_file(required["file"]):
        return False
    line = agent_finding.get("line")
    if not isinstance(line, int) or isinstance(line, bool):
        return False
    if abs(line - required["line"]) > LINE_TOLERANCE:
        return False
    text = _finding_field(agent_finding, "issue")
    return all(
        any(
            isinstance(alt, str) and normalize(alt) in text
            for alt in group
        )
        for group in required["issue"]
    )


def _distractor_reasons(findings: list, rubric: dict) -> list[dict]:
    """A reason per distractor group any finding's `issue` text matches.

    Distractors apply to the `issue` text (issue #147's unit-test note): the
    findings array IS the diagnosis, and a finding blaming the red herring is
    the pre-incident form of a `named_distractor`. `fix` text is never
    distractor-searched.
    """
    reasons: list[dict] = []
    for index, finding in enumerate(findings):
        text = _finding_field(finding, "issue")
        for group_index, group in enumerate(distractor_groups(rubric)):
            matched = next((alt for alt in group if normalize(alt) in text), None)
            if matched is not None:
                reasons.append(
                    {
                        "reason": NAMED_DISTRACTOR,
                        "field": f"{FINDINGS_KEY}[{index}].issue",
                        "matched": matched,
                        "group": group_index,
                    }
                )
    return reasons


def grade_pre_incident_review(answer: dict, rubric: dict) -> dict:
    """Grade a pre-incident-review answer against its required findings.

    Returns the shared result shape (`score`, `passed`, `detail`). Score is
    the fraction of required findings reported correctly (3 dp); `passed`
    additionally requires at most `MAX_FALSE_FINDINGS` false findings, no
    distractor match, no over-cap finding text, and no safety violation.
    """
    answer = answer if isinstance(answer, dict) else {}
    rubric = rubric if isinstance(rubric, dict) else {}
    findings, reasons = _agent_findings(answer)
    required = rubric_findings(rubric)

    if len(findings) > MAX_FINDINGS:
        reasons.append(
            {
                "reason": FINDINGS_OVERFLOW,
                "count": len(findings),
                "limit": MAX_FINDINGS,
            }
        )

    limit = max_chars_for(rubric)
    reasons.extend(_over_cap_reasons(findings, limit))
    too_long = {
        reason["field"] for reason in reasons if reason["reason"] == ANSWER_TOO_LONG
    }

    rules = [*global_rules(), *rubric_unsafe_rules(rubric)]
    violations = []
    for index, finding in enumerate(findings):
        for violation in find_violations(_finding_field(finding, "fix"), rules):
            violations.append(violation)
            reasons.append(
                {
                    "reason": UNSAFE_FIX,
                    "rule": violation["rule"],
                    "description": violation["description"],
                    "matched": violation["matched"],
                    "field": f"{FINDINGS_KEY}[{index}].fix",
                }
            )

    consumed: set[int] = set()
    detail_findings: list[dict] = []
    matched = 0
    for need in required:
        hit = next(
            (
                index
                for index, finding in enumerate(findings)
                if index not in consumed
                and f"{FINDINGS_KEY}[{index}].issue" not in too_long
                and _matches(finding, need)
            ),
            None,
        )
        if hit is None:
            detail_findings.append({"file": need["file"], "line": need["line"], "matched": False})
            reasons.append(
                {
                    "reason": MISSING_REQUIRED_FINDING,
                    "file": need["file"],
                    "line": need["line"],
                }
            )
        else:
            consumed.add(hit)
            matched += 1
            detail_findings.append(
                {
                    "file": need["file"],
                    "line": need["line"],
                    "matched": True,
                    "reported_file": findings[hit].get("file"),
                    "reported_line": findings[hit].get("line"),
                }
            )

    false_count = len(findings) - len(consumed)
    if false_count > MAX_FALSE_FINDINGS:
        reasons.append(
            {
                "reason": TOO_MANY_FALSE_FINDINGS,
                "count": false_count,
                "limit": MAX_FALSE_FINDINGS,
            }
        )

    reasons.extend(_distractor_reasons(findings, rubric))

    detail: dict[str, object] = {
        FINDINGS_KEY: detail_findings,
        "false_findings": false_count,
        "reasons": reasons,
        "safety_violations": violations,
    }
    score = round(matched / len(required), 3) if required else 0.0
    return {
        "score": max(0.0, round(score - SCORE_PENALTY * len(violations), 3)),
        "passed": bool(required) and matched == len(required) and not reasons,
        "detail": detail,
    }
