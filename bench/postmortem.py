"""Postmortem track: grade a blameless write-up of a resolved incident (issue #148).

The agent reads a resolved incident's fixtures plus a resolution log and the
on-call chat, and writes the blameless postmortem. The answer contract is a
single `postmortem` key carrying the full markdown document:

    {"postmortem": "# Summary\n\n..."}

The rubric (`expected.json`) declares the incident's required facts and the
claims a blameless write-up must not make, both as keyword groups:

    {"required_facts": [[["47"]], [["subject"], ["default", "old namespace"]]],
     "forbidden_claims": [["key vault outage", "rbac misconfig"]]}

Grading (issue #148):

- Keyword pre-checks, always applied: the six required sections (summary,
  impact, timeline, root cause, contributing factors, action items) must be
  present, and the document must be 300-1500 words. A write-up that skips a
  section or pads beyond the cap fails before any semantic grading.
- Required facts: every fact (a conjunction of keyword groups) must match
  somewhere in the document — the diagnosis grader's group semantics, one
  level nested. Score is the matched fraction.
- Forbidden claims: any match fails the task without changing the score —
  blaming a healthy component (Key Vault, RBAC) or a person is exactly the
  blame the postmortem process exists to prevent.
- Safety is not scanned here: the document is a narrative, not a fix
  channel, and must be free to discuss what was run.

LLM-judge mode (issue #100, when it lands) will pass a `judge` callable; the
keyword pre-checks stay authoritative for shape, and the judge's semantic
verdict (`root_cause_correct`-style booleans over the required facts)
decides `passed` only when the pre-checks pass. The judge prompt lives in
`bench/prompts/postmortem_judge.md`.
"""

from __future__ import annotations

from bench.grader import normalize

KIND_POSTMORTEM = "postmortem"
POSTMORTEM_KEY = "postmortem"
FACTS_KEY = "required_facts"
FORBIDDEN_KEY = "forbidden_claims"

# Issue #148: the six sections a blameless postmortem must contain, matched
# as normalized substrings so `## Root Cause`, `### Root cause:` all count.
REQUIRED_SECTIONS = (
    "summary",
    "impact",
    "timeline",
    "root cause",
    "contributing factors",
    "action items",
)

# Issue #148: length pre-check, in words.
WORDS_MIN = 300
WORDS_MAX = 1500

# Stable reason identifiers recorded in `detail["reasons"]`.
POSTMORTEM_INVALID = "postmortem_invalid"
MISSING_SECTION = "missing_section"
POSTMORTEM_TOO_SHORT = "postmortem_too_short"
POSTMORTEM_TOO_LONG = "postmortem_too_long"
MISSING_FACT = "missing_fact"
FORBIDDEN_CLAIM = "forbidden_claim"


def is_postmortem(task: dict | None) -> bool:
    """True when a task's `task.yaml` declares `kind: postmortem`."""
    return isinstance(task, dict) and task.get("kind") == KIND_POSTMORTEM


def rubric_facts(rubric: dict) -> list:
    """The rubric's well-formed required facts, defensively.

    Each fact is a conjunction of keyword groups — `[["subject"],
    ["default", "old namespace"]]` needs a subject mention AND an
    old-namespace mention. A malformed rubric yields an empty list here and
    is flagged by the validator (`bench/tasks.py`) at authoring time; the
    grader must never raise on a hostile or half-written rubric.
    """
    return _fact_groups(rubric.get(FACTS_KEY) if isinstance(rubric, dict) else None)


def rubric_forbidden(rubric: dict) -> list:
    """The rubric's well-formed forbidden-claim groups, defensively."""
    return _groups(rubric.get(FORBIDDEN_KEY) if isinstance(rubric, dict) else None)


def _groups(raw: object) -> list:
    """Non-empty groups of non-empty strings, or an empty list."""
    if not isinstance(raw, list):
        return []
    return [
        group
        for group in raw
        if isinstance(group, list)
        and group
        and all(isinstance(alt, str) and alt.strip() for alt in group)
    ]


def _fact_groups(raw: object) -> list:
    """Facts as conjunctions of groups; one malformed fact voids the rubric."""
    if not isinstance(raw, list) or not raw:
        return []
    facts = []
    for fact in raw:
        groups = _groups(fact)
        if not groups:
            return []
        facts.append(groups)
    return facts


def _missing_sections(document: str) -> list[str]:
    """Required sections whose normalized heading text is absent."""
    text = normalize(document)
    return [
        section
        for section in REQUIRED_SECTIONS
        if normalize(section) not in text
    ]


def _word_count(document: str) -> int:
    """Whitespace-delimited word count of the document."""
    return len(document.split())


def _matched_facts(document: str, facts: list) -> tuple[int, int, list[dict]]:
    """Count satisfied facts (each a conjunction of groups); one reason per
    missing fact, naming the groups that did not match."""
    text = normalize(document)
    reasons = []
    matched = 0
    for index, fact in enumerate(facts):
        missing = [
            group for group in fact if not any(normalize(alt) in text for alt in group)
        ]
        if not missing:
            matched += 1
        else:
            reasons.append(
                {"reason": MISSING_FACT, "fact": index, "missing_groups": missing}
            )
    return matched, len(facts), reasons


def _forbidden_reasons(document: str, forbidden: list) -> list[dict]:
    """One reason per forbidden-claim group the document matches."""
    text = normalize(document)
    reasons = []
    for index, group in enumerate(forbidden):
        matched = next((alt for alt in group if normalize(alt) in text), None)
        if matched is not None:
            reasons.append(
                {"reason": FORBIDDEN_CLAIM, "group": index, "matched": matched}
            )
    return reasons


def grade_postmortem(answer: dict, rubric: dict, judge=None) -> dict:
    """Grade a postmortem document against its rubric (issue #148).

    Returns the shared result shape (`score`, `passed`, `detail`). Score is
    the fraction of required facts present (3 dp); `passed` additionally
    requires all sections, the word-count window, and zero forbidden
    claims. When `judge` is provided (LLM-judge mode, issue #100) and the
    pre-checks pass, its boolean verdict decides `passed`.
    """
    answer = answer if isinstance(answer, dict) else {}
    rubric = rubric if isinstance(rubric, dict) else {}
    document = answer.get(POSTMORTEM_KEY)
    if not isinstance(document, str) or not document.strip():
        return {
            "score": 0.0,
            "passed": False,
            "detail": {"reasons": [{"reason": POSTMORTEM_INVALID}]},
        }

    facts = rubric_facts(rubric)
    if not facts:
        return {
            "score": 0.0,
            "passed": False,
            "detail": {"reasons": [{"reason": POSTMORTEM_INVALID}]},
        }

    reasons: list[dict] = []
    for section in _missing_sections(document):
        reasons.append({"reason": MISSING_SECTION, "section": section})
    words = _word_count(document)
    if words < WORDS_MIN:
        reasons.append(
            {"reason": POSTMORTEM_TOO_SHORT, "words": words, "limit": WORDS_MIN}
        )
    if words > WORDS_MAX:
        reasons.append(
            {"reason": POSTMORTEM_TOO_LONG, "words": words, "limit": WORDS_MAX}
        )

    matched, total, fact_reasons = _matched_facts(document, facts)
    reasons.extend(fact_reasons)
    reasons.extend(_forbidden_reasons(document, rubric_forbidden(rubric)))

    score = round(matched / total, 3) if total else 0.0
    passed = not reasons
    if passed and judge is not None:
        verdict = judge(document, rubric)
        passed = bool(verdict.get("passed", False))
        reasons.extend(verdict.get("reasons", []))
    return {
        "score": score,
        "passed": passed,
        "detail": {"reasons": reasons, "words": words, "facts_matched": matched},
    }
