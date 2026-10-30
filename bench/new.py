"""`bench new` — scaffold the next free task ID (issue #120).

Creates `tasks/<category>/<category>-<nnn>/` with template files whose
placeholders are valid output for `bench validate` both before and after the
contributor fills them in. The next-free-ID computation is a pure function so
tests exercise it without touching the filesystem.
"""

from __future__ import annotations

import re
from pathlib import Path

from bench.tasks import CATEGORIES

# Template content lives here as constants so `scaffold_task` is a plain
# write-and-return. Placeholders use the <REPLACE-ME-...> convention: every
# occurrence must be replaced before the task is committed.
SYMPTOM_PLACEHOLDER = (
    "<REPLACE-ME: what on-call sees — alerts, user reports, dashboard "
    "movements. Specific enough to start an investigation, never specific "
    "enough to end one. Must not name the root cause.>"
)

ROOT_CAUSE_TOKEN = "<REPLACE-ME-root-cause-keyword>"
FIX_TOKEN = "<REPLACE-ME-fix-keyword>"

TASK_YAML_TEMPLATE = """\
id: {task_id}
title: "{title}"
category: {category}
difficulty: easy  # one of: easy / medium / hard
tags:
  - {category}  # replace with 2-5 specific tags
symptom: |
  {symptom}
"""

EXPECTED_TEMPLATE = """\
{{
  "root_cause": [
    ["{root_cause_token}"]
  ],
  "fix": [
    ["{fix_token}"]
  ]
}}
"""

SOLUTION_TEMPLATE = """\
{{
  "root_cause": "{root_cause_token} — <REPLACE-ME: one-sentence diagnosis>",
  "evidence": [
    "<REPLACE-ME: fixture filename and the decisive line it contains>"
  ],
  "fix": "{fix_token} — <REPLACE-ME: one-sentence remediation>"
}}
"""

FIXTURE_README = """\
Placeholder fixture — replace this file.

Fixture filenames name the command or source, never the answer (see
AGENTS.md "Fixtures"): e.g. `kubectl-describe-pod.txt`, `az-network-nsg-list.json`.
Use only placeholder GUIDs of the form 00000000-0000-0000-0000-00000000000X.
"""

FIXTURE_1_NAME = "kubectl-get-events.txt"
FIXTURE_2_NAME = "az-resource-show.json"


def next_task_id(tasks_root: Path, category: str) -> str:
    """Return the next free `<category>-<nnn>` id for `category`.

    Scans `tasks_root/<category>/` for existing task folders and returns the
    zero-padded backlog number one above the highest found (001 when the
    category is empty or missing).
    """
    category_dir = tasks_root / category
    numbers = [
        int(match.group(1))
        for path in category_dir.glob(f"{category}-*")
        if path.is_dir()
        and (match := re.fullmatch(rf"{re.escape(category)}-(\d{{3,}})", path.name))
    ]
    return f"{category}-{max(numbers, default=0) + 1:03d}"


def scaffold_task(tasks_root: Path, category: str, title: str) -> Path:
    """Create the next free task folder under `tasks_root` and return its path.

    Raises `ValueError` for an unknown category or when the target folder
    already exists (a scaffold must never overwrite work in progress).
    """
    if category not in CATEGORIES:
        raise ValueError(
            f"unknown category '{category}' (choose from {', '.join(sorted(CATEGORIES))})"
        )

    task_id = next_task_id(tasks_root, category)
    task_dir = tasks_root / category / task_id
    if task_dir.exists():
        raise ValueError(f"{task_dir} already exists; refusing to overwrite")

    # Double-quote escaping keeps a title containing quotes valid YAML.
    yaml_title = title.replace('\\', '\\\\').replace('"', '\\"')

    (task_dir / "fixtures").mkdir(parents=True)
    (task_dir / "task.yaml").write_text(
        TASK_YAML_TEMPLATE.format(
            task_id=task_id, title=yaml_title, category=category,
            symptom=SYMPTOM_PLACEHOLDER,
        ),
        encoding="utf-8",
    )
    (task_dir / "expected.json").write_text(
        EXPECTED_TEMPLATE.format(
            root_cause_token=ROOT_CAUSE_TOKEN, fix_token=FIX_TOKEN
        ),
        encoding="utf-8",
    )
    (task_dir / "solution.json").write_text(
        SOLUTION_TEMPLATE.format(
            root_cause_token=ROOT_CAUSE_TOKEN, fix_token=FIX_TOKEN
        ),
        encoding="utf-8",
    )
    (task_dir / "fixtures" / FIXTURE_1_NAME).write_text(
        FIXTURE_README, encoding="utf-8"
    )
    (task_dir / "fixtures" / FIXTURE_2_NAME).write_text(
        FIXTURE_README, encoding="utf-8"
    )
    return task_dir
