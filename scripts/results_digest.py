#!/usr/bin/env python3
"""Post or update the pinned "Results digest" issue (issue #166).

Called weekly by `.github/workflows/digest.yml` with `GH_TOKEN` set. Thin on
purpose: every interesting decision lives in `bench.digest` (tested); this
wrapper only talks to the `gh` CLI — find the pinned issue, read its previous
state comment, render the new digest, create or edit, and pin on first post.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.digest import (  # noqa: E402 (path bootstrap must precede imports)
    DIGEST_TITLE,
    build_snapshot,
    diff_state,
    parse_state,
    render_digest,
)


def gh(*args: str, stdin: str | None = None) -> str:
    """One `gh` CLI call; a non-zero exit aborts the digest run loudly."""
    result = subprocess.run(
        ["gh", *args], input=stdin, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise SystemExit(
            f"gh {' '.join(args[:2])} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def find_digest_issue() -> dict | None:
    """The open issue titled exactly `Results digest`, or None."""
    issues = json.loads(gh("issue", "list", "--state", "open", "--json", "number,title,id,body"))
    for issue in issues:
        if issue["title"].strip().lower() == DIGEST_TITLE.lower():
            return issue
    return None


def main() -> int:
    snapshot = build_snapshot(Path("results"), Path("tasks"))
    issue = find_digest_issue()
    previous = parse_state(issue["body"]) if issue else None
    diff = diff_state(previous, snapshot)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    body = render_digest(snapshot, diff, generated)

    if issue is None:
        number = json.loads(
            gh("issue", "create", "--title", DIGEST_TITLE, "--body-file", "-", "--json", "number", stdin=body)
        )["number"]
        print(f"created digest issue #{number}")
        issue_id = gh("issue", "view", str(number), "--json", "id", "--jq", ".id").strip()
        # Pinning the first digest keeps the latest at the top of the Issues
        # tab forever; a pin failure must not discard the digest itself.
        try:
            gh(
                "api", "graphql",
                "-f", "query=mutation($id: ID!) { pinIssue(input: {issueId: $id}) { issue { id } } }",
                "-F", f"id={issue_id}",
            )
        except SystemExit:
            print("warning: could not pin the digest issue (continuing)", file=sys.stderr)
    else:
        gh("issue", "edit", str(issue["number"]), "--body-file", "-", stdin=body)
        print(f"updated digest issue #{issue['number']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
