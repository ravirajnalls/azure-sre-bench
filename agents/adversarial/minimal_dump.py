#!/usr/bin/env python3
"""Adversarial probe: the minimum-effort dump, capped for issue #93 (spec D1).

Like `fixture_dump.py` but truncated to the grader's length limits (issue #93) —
root_cause/fix at most 600 characters, evidence at most 8 items of 300
characters. It is the cheapest answer that is still *legal* under the caps, so
the gameability gate can tell the difference between "#93 beats the uncapped
dump" and "#93 beats dumping".

The caps below mirror the grader's constants (`bench/grader.py`, issue #93,
where they are enforced); keep them in sync if the defaults change.

This is a probe for the CI gameability gate (`tests/test_gameability.py`),
not a baseline; its results land under `results/adversarial-*` and are
excluded from the leaderboard and site.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FIXTURES_DIR = Path("fixtures")  # cwd is the agent's workspace

ROOT_CAUSE_MAX_CHARS = 600
FIX_MAX_CHARS = 600
EVIDENCE_MAX_ITEMS = 8
EVIDENCE_ITEM_MAX_CHARS = 300


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


def capped_dump_answer(fixtures_dir: Path) -> dict:
    """Contract-shaped dump that stays under the future issue-#93 caps."""
    texts = fixture_texts(fixtures_dir)
    blob = "\n".join(texts)
    return {
        "root_cause": blob[:ROOT_CAUSE_MAX_CHARS],
        "evidence": [
            text[:EVIDENCE_ITEM_MAX_CHARS] for text in texts[:EVIDENCE_MAX_ITEMS]
        ],
        "fix": blob[:FIX_MAX_CHARS],
    }


def main() -> int:
    print(json.dumps(capped_dump_answer(FIXTURES_DIR)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
