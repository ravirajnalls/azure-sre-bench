#!/usr/bin/env python3
"""Adversarial probe: the fixture-dump cheat (issue #92).

Concatenates the raw text of every file in its workspace `fixtures/` folder
into all three answer fields — the answer of an agent that reads everything
and understands nothing. If this agent passes a task, the rubric's groups are
satisfied by verbatim fixture text alone: no diagnosis was required, because
the evidence channel (and the fields themselves) carry the keywords.

This is a probe for the CI gameability gate (`tests/test_gameability.py`),
not a baseline; its results land under `results/adversarial-*` and are
excluded from the leaderboard and site.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FIXTURES_DIR = Path("fixtures")  # cwd is the agent's workspace


def fixture_texts(fixtures_dir: Path) -> list[str]:
    """Raw text of every fixture file, sorted by name; unreadable files skipped."""
    texts: list[str] = []
    if not fixtures_dir.is_dir():
        return texts
    for path in sorted(fixtures_dir.iterdir()):
        if not path.is_file():
            continue
        try:
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            print(f"warning: could not read {path}: {exc}", file=sys.stderr)
    return texts


def dump_answer(fixtures_dir: Path) -> dict:
    """Contract-shaped answer that dumps raw fixture text into every field."""
    texts = fixture_texts(fixtures_dir)
    blob = "\n".join(texts)
    return {
        "root_cause": blob,
        "evidence": texts,  # the grader appends evidence to the root-cause text
        "fix": blob,
    }


def main() -> int:
    print(json.dumps(dump_answer(FIXTURES_DIR)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
