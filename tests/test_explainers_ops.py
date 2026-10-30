"""Explainer checks for the aks-ops explainer batch (issues #224-#231, #246, #248).

The shared corpus test in ``test_tasks.py`` validates every explainer in the
tree; this module scopes the same invariants to the ``tasks/aks-ops`` batch so
a regression in these explainers fails with a task-specific message. It is
parametrized over whatever aks-ops explainers exist, so it activates
automatically as each batch PR lands.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bench.tasks import EXPLAINER_FILENAME, EXPLAINER_REQUIRED_HEADINGS

OPS_TASKS = Path(__file__).parent.parent / "tasks" / "aks-ops"


def _explainer_paths() -> list[Path]:
    return sorted(OPS_TASKS.glob(f"*/{EXPLAINER_FILENAME}"))


@pytest.mark.parametrize(
    "path", _explainer_paths(), ids=lambda p: p.parent.name
)
def test_aks_ops_explainer_has_required_headings(path: Path):
    text = path.read_text(encoding="utf-8")
    headings = {
        line.strip() for line in text.splitlines() if line.strip().startswith("##")
    }
    missing = [
        heading
        for heading in EXPLAINER_REQUIRED_HEADINGS
        if f"## {heading}" not in headings
    ]
    assert not missing, f"{path}: missing required headings {missing}"


@pytest.mark.parametrize(
    "path", _explainer_paths(), ids=lambda p: p.parent.name
)
def test_aks_ops_explainer_fixture_references_exist(path: Path):
    text = path.read_text(encoding="utf-8")
    fixtures_dir = path.parent / "fixtures"
    fixture_names = {
        f.name for f in fixtures_dir.iterdir() if f.is_file()
    } if fixtures_dir.is_dir() else set()
    for ref in sorted(set(re.findall(r"fixtures/[A-Za-z0-9._/-]+", text))):
        assert ref.rsplit("/", 1)[-1] in fixture_names, (
            f"{path}: references '{ref}' but it does not exist under fixtures/"
        )
