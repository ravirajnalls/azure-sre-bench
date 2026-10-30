#!/usr/bin/env python3
"""Encrypt or decrypt the hidden files of held-out tasks (issue #115).

The maintainer flow for a held-out task (full guide: docs/HELDOUT.md):

    HELDOUT_KEY=<age identity key> python scripts/encrypt_heldout.py tasks/<category>/<task-id>

age-encrypts `expected.json` and `solution.json` to `<name>.age` and removes
the plaintext. `--decrypt` restores the plaintext for editing, using the same
key. Both directions refuse tasks whose task.yaml does not declare
`split: heldout`, so public tasks can never be encrypted by accident.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.heldout import (  # noqa: E402
    KEY_ENV,
    SPLIT_HELDOUT,
    HeldoutKeyError,
    decrypt_hidden,
    encrypt_hidden,
    heldout_key,
    split_of,
)


def main(argv: list[str]) -> int:
    args = list(argv)
    mode = "encrypt"
    if args and args[0] == "--decrypt":
        mode = "decrypt"
        args = args[1:]
    if not args:
        print("usage: scripts/encrypt_heldout.py [--decrypt] <task-dir> ...", file=sys.stderr)
        return 2
    key = heldout_key()
    if not key:
        print(f"error: set ${KEY_ENV} to an age identity key (see docs/HELDOUT.md)", file=sys.stderr)
        return 2
    status = 0
    for raw in args:
        task_dir = Path(raw)
        task_yaml = task_dir / "task.yaml"
        if not task_yaml.is_file():
            print(f"error: {task_dir} has no task.yaml", file=sys.stderr)
            status = 2
            continue
        config = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
        if split_of(config) != SPLIT_HELDOUT:
            print(
                f"error: {task_dir} does not declare `split: heldout` in task.yaml; refused",
                file=sys.stderr,
            )
            status = 2
            continue
        action = encrypt_hidden if mode == "encrypt" else decrypt_hidden
        for path in action(task_dir, key):
            print(f"{mode}ed {path}")
    return status


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except HeldoutKeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
