#!/usr/bin/env python3
"""Update the `<!-- leaderboard -->` block in README.md (issue #41).

Reads the same results/ tree as `python -m bench leaderboard` and splices the
markdown table between the leaderboard markers, leaving every other README
section untouched. Run from the repo root:

    python scripts/update_leaderboard.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.leaderboard import RESULTS_DIR, leaderboard_markdown, update_readme

README = Path("README.md")


def main() -> int:
    table = leaderboard_markdown(RESULTS_DIR, Path("tasks"))
    update_readme(README, table)
    print(f"Updated the leaderboard block in {README}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
