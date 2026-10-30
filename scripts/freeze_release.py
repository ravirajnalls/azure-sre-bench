#!/usr/bin/env python3
"""Freeze a release manifest: `releases/<version>.json` (issue #116).

Usage:  python scripts/freeze_release.py [--version 0.1] [--tag v0.1]

Freezes the live corpus under `tasks/` — task list, rubric content hashes,
grader version — then verifies the fresh manifest against the corpus it was
built from (a self-check that the freeze is internally consistent) and writes
`releases/<version>.json`. Cut a release only after the rubric audits merge:
the manifest freezes rubric hashes, so editing a rubric afterwards invalidates
the release and `bench run --release` will refuse it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench import BENCH_VERSION  # noqa: E402
from bench.releases import build_manifest, verify_release  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=BENCH_VERSION, help="release name")
    parser.add_argument("--tag", help="git tag (default: v<version>)")
    parser.add_argument(
        "--commit", help="frozen-at commit (default: current git HEAD)"
    )
    parser.add_argument(
        "--created", help="freeze date, ISO (default: today)"
    )
    args = parser.parse_args()

    commit = args.commit or subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    manifest = build_manifest(
        version=args.version,
        tag=args.tag or f"v{args.version}",
        commit=commit,
        created=args.created or date.today().isoformat(),
        tasks_root=REPO_ROOT / "tasks",
    )

    problems = verify_release(manifest, tasks_root=REPO_ROOT / "tasks")
    if problems:
        print("manifest does not verify against the corpus it froze:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    # The issue fixes both conventions: the file is `releases/v<version>.json`
    # and the run flag is `bench run --release v<version>`.
    out = REPO_ROOT / "releases" / f"v{args.version}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"froze {len(manifest['tasks'])} tasks as release {args.version} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
