"""Timeline track: grade the causal ordering of incident events (issue #146).

Some incidents are best reconstructed: the agent gets event records from
several sources whose timestamps are partial or skewed (sources drift, clocks
lie), and must return the events in the order they actually happened. The
answer contract is a single `order` key — the event ids in causal order:

    {"order": ["ev-01", "ev-02", "ev-03"]}

The rubric (`expected.json`) declares the reference order with the same key:

    {"order": ["ev-03", "ev-01", "ev-02"]}

Grading (issue #146):

- Kendall tau between the answer's order and the reference order, counted
  over the reference ids: a pair of events is concordant when the answer
  preserves their reference relative order, discordant otherwise.
  tau = (concordant - discordant) / pairs.
- The task passes when tau >= `TAU_PASS` (0.8) AND the first event is
  correct — the causal starting point is not negotiable, while one adjacent
  slip in the tail of a 6-event timeline still leaves tau at 0.87.
- The answer must be exactly a permutation of the reference ids: a missing,
  duplicated or unknown id is an `order_invalid` reason and fails the task
  no matter how the rest is ordered. A timeline that invents events has not
  reconstructed the incident.
"""

from __future__ import annotations

KIND_TIMELINE = "timeline"
ORDER_KEY = "order"
EVENTS_FILENAME = "events.json"

# Issue #146: pass at tau >= 0.8 and first event correct.
TAU_PASS = 0.8

# Stable reason identifiers recorded in `detail["reasons"]`.
ORDER_INVALID = "order_invalid"
FIRST_EVENT_WRONG = "first_event_wrong"
TAU_BELOW_PASS = "tau_below_pass"


def is_timeline(task: dict | None) -> bool:
    """True when a task's `task.yaml` declares `kind: timeline`."""
    return isinstance(task, dict) and task.get("kind") == KIND_TIMELINE


def rubric_order(rubric: dict) -> list[str]:
    """The rubric's well-formed reference order, defensively.

    A malformed rubric yields an empty list here and is flagged by the
    validator (`bench/tasks.py`) at authoring time; the grader must never
    raise on a hostile or half-written rubric.
    """
    raw = rubric.get(ORDER_KEY) if isinstance(rubric, dict) else None
    if not isinstance(raw, list) or not raw:
        return []
    if not all(isinstance(event_id, str) and event_id.strip() for event_id in raw):
        return []
    if len(set(raw)) != len(raw):
        return []
    return list(raw)


def _answer_order(answer: dict, reference: list[str]) -> tuple[list[str] | None, dict]:
    """The answer's event-id list, or the reason it is not a permutation."""
    order = answer.get(ORDER_KEY)
    if not isinstance(order, list) or not all(
        isinstance(event_id, str) for event_id in order
    ):
        return None, {"reason": ORDER_INVALID}
    known = set(reference)
    seen: set[str] = set()
    for event_id in order:
        if event_id not in known or event_id in seen:
            return None, {"reason": ORDER_INVALID}
        seen.add(event_id)
    if len(order) != len(reference):
        return None, {"reason": ORDER_INVALID}
    return order, {}


def kendall_tau(reference: list[str], answer: list[str]) -> float:
    """Kendall tau (no ties, exact permutation) between two orderings.

    `answer` must be a permutation of `reference`; callers validate that
    first. A single-event reference has no pairs and is trivially correct.
    """
    position = {event_id: index for index, event_id in enumerate(answer)}
    concordant = discordant = 0
    for i in range(len(reference)):
        for j in range(i + 1, len(reference)):
            if position[reference[i]] < position[reference[j]]:
                concordant += 1
            else:
                discordant += 1
    pairs = len(reference) * (len(reference) - 1) // 2
    if pairs == 0:
        return 1.0
    return (concordant - discordant) / pairs


def grade_timeline(answer: dict, rubric: dict) -> dict:
    """Grade a timeline answer against its reference order (issue #146).

    Returns the shared result shape (`score`, `passed`, `detail`). Score is
    the Kendall tau (3 dp); `passed` additionally requires a valid
    permutation, tau >= `TAU_PASS`, and a correct first event.
    """
    answer = answer if isinstance(answer, dict) else {}
    rubric = rubric if isinstance(rubric, dict) else {}
    reference = rubric_order(rubric)
    if not reference:
        return {
            "score": 0.0,
            "passed": False,
            "detail": {"reasons": [{"reason": ORDER_INVALID}]},
        }

    order, invalid = _answer_order(answer, reference)
    if invalid:
        return {"score": 0.0, "passed": False, "detail": {"reasons": [invalid]}}

    tau = round(kendall_tau(reference, order), 3)
    reasons: list[dict] = []
    if order[0] != reference[0]:
        reasons.append(
            {"reason": FIRST_EVENT_WRONG, "expected": reference[0], "got": order[0]}
        )
    if tau < TAU_PASS:
        reasons.append({"reason": TAU_BELOW_PASS, "tau": tau, "limit": TAU_PASS})
    return {
        "score": tau,
        "passed": not reasons,
        "detail": {"reasons": reasons, "tau": tau},
    }
