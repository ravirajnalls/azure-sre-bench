#!/usr/bin/env python3
"""Grade a live-mode diagnosis against the scenario rubric.json.

Mirrors bench/grader.py keyword-group semantics so live results mean the same
thing as fixture results: text is lowercased; a group matches when any of its
alternatives is a case-insensitive substring; for root_cause the evidence list
is joined and appended to the searched text (the same _field_text behavior as
the bench grader). A field passes when every one of its groups matches; the
run passes when both fields pass.

This copy exists so a live scenario can grade without the bench package
importable — the live-mode CI job never installs the harness.

Usage:
    grade_diagnosis.py --answer answer.json [--rubric rubric.json]
    grade_diagnosis.py --self-test        # offline: grades the committed samples
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REQUIRED_FIELDS = ("root_cause", "fix")
DEFAULT_RUBRIC = Path(__file__).resolve().parent / "rubric.json"
SAMPLES_DIR = Path(__file__).resolve().parent / "samples"


def field_text(answer: dict, field: str) -> str:
    """Lowercased searchable text for one field; root_cause includes evidence."""
    parts = [str(answer.get(field) or "")]
    if field == "root_cause":
        evidence = answer.get("evidence")
        if isinstance(evidence, str):
            evidence = [evidence]
        if isinstance(evidence, list):
            parts.extend(str(item) for item in evidence)
    return " ".join(parts).lower()


def group_matches(group: list, text: str) -> bool:
    """A group matches when any alternative is a case-insensitive substring."""
    return any(str(alternative).lower() in text for alternative in group)


def grade(answer: dict, rubric: dict) -> dict:
    """Grade one answer dict; returns {passed, reasons} like bench/grader.py detail."""
    reasons: list[str] = []
    for field in REQUIRED_FIELDS:
        text = field_text(answer, field)
        for index, group in enumerate(rubric.get(field, [])):
            if not group_matches(group, text):
                reasons.append(f"{field}: concept group {index + 1} not matched: {group!r}")
    return {"passed": not reasons, "reasons": reasons}


def self_test() -> int:
    """Grade the committed sample answers; the wrong one must fail, right must pass."""
    rubric = json.loads(DEFAULT_RUBRIC.read_text())
    correct = json.loads((SAMPLES_DIR / "diagnosis-correct.json").read_text())
    wrong = json.loads((SAMPLES_DIR / "diagnosis-wrong.json").read_text())

    failures: list[str] = []
    result = grade(correct, rubric)
    if result["passed"]:
        print(f"PASS correct sample: {result['reasons']}")
    else:
        failures.append(f"correct sample failed: {result['reasons']}")

    result = grade(wrong, rubric)
    if not result["passed"]:
        print(f"PASS wrong sample rejected: {result['reasons']}")
    else:
        failures.append("wrong sample unexpectedly passed")

    for line in failures:
        print(f"FAIL {line}", file=sys.stderr)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--answer", help="path to the agent's answer JSON")
    parser.add_argument("--rubric", default=str(DEFAULT_RUBRIC), help="path to rubric JSON")
    parser.add_argument("--self-test", action="store_true", help="grade the committed samples and exit")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    if not args.answer:
        parser.error("--answer is required (or use --self-test)")
    answer = json.loads(Path(args.answer).read_text())
    rubric = json.loads(Path(args.rubric).read_text())

    result = grade(answer, rubric)
    print(json.dumps(result, indent=2))
    for reason in result["reasons"]:
        print(reason, file=sys.stderr)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
