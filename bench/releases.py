"""Release manifests for versioned benchmark releases (issue #116).

A release freezes the corpus a leaderboard version refers to: the task list,
each task's rubric content hash, and the grader version that scores it. Runs
started with `bench run --release v0.1` are checked against
`releases/v0.1.json` before the first task executes — a run against a corpus
whose rubrics have drifted from the frozen manifest would produce results the
release cannot vouch for, so that fails loudly instead.

Manifest shape (version 1):

    version              release name ("0.1")
    tag                  git tag the release is cut from ("v0.1")
    created              ISO date the manifest was frozen
    commit               full SHA the manifest was frozen at (informational)
    grader_version       GRADER_VERSION at freeze time
    result_schema_version  RESULT_SCHEMA_VERSION at freeze time
    tasks                [{id, category, difficulty, split, rubric_sha256}]

`rubric_sha256` is the same `bench.grader.rubric_sha256` value every result
record carries, computed over the decrypted rubric for held-out tasks. A
held-out task that cannot be decrypted at freeze time freezes with a `null`
hash and is matched by identity only (verified when the key is present).
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from bench import BENCH_VERSION, GRADER_VERSION, RESULT_SCHEMA_VERSION
from bench.grader import rubric_sha256
from bench.heldout import HeldoutKeyError, load_hidden_json, split_of

MANIFEST_VERSION = 1
RELEASES_DIR = Path("releases")


def load_release(name: str, releases_root: Path | str | None = None) -> dict:
    """Read `releases/<name>.json`; raise FileNotFoundError when absent."""
    path = Path(releases_root or RELEASES_DIR) / f"{name}.json"
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    if not isinstance(manifest, dict):
        raise ValueError(f"{path}: manifest must be a JSON object")
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError(
            f"{path}: unsupported manifest_version "
            f"{manifest.get('manifest_version')!r} (expected {MANIFEST_VERSION})"
        )
    return manifest


def build_manifest(
    version: str = BENCH_VERSION,
    tag: str | None = None,
    commit: str | None = None,
    created: str | None = None,
    tasks_root: Path | str = "tasks",
) -> dict:
    """Freeze the corpus under `tasks_root` into a release manifest.

    The rubric hash is taken through the same loader the grader uses, so a
    held-out task freezes the hash of its *decrypted* rubric; without the key
    the hash freezes as `null` and `verify_release` matches that task by
    identity instead of content.
    """
    from bench.run import discover_tasks  # local: avoid a run<->releases cycle

    tasks: list[dict] = []
    for task_dir in discover_tasks(Path(tasks_root)):
        with open(Path(task_dir) / "task.yaml", encoding="utf-8") as fh:
            config = yaml.safe_load(fh) or {}
        task_id = str(config.get("id") or task_dir.name)
        split = split_of(config)
        rubric_hash: str | None
        try:
            rubric = load_hidden_json(task_dir, "expected.json", config=config)
            rubric_hash = rubric_sha256(rubric)
        except HeldoutKeyError:
            rubric_hash = None
        tasks.append(
            {
                "id": task_id,
                "category": config.get("category", ""),
                "difficulty": config.get("difficulty", ""),
                "split": split,
                "rubric_sha256": rubric_hash,
            }
        )
    return {
        "manifest_version": MANIFEST_VERSION,
        "version": version,
        "tag": tag,
        "created": created,
        "commit": commit,
        "grader_version": GRADER_VERSION,
        "result_schema_version": RESULT_SCHEMA_VERSION,
        "tasks": tasks,
    }


def verify_release(
    manifest: dict, tasks_root: Path | str = "tasks"
) -> list[str]:
    """Check a live corpus against a release manifest; returns problem strings.

    Empty list = the release is still verifiable: every manifest task exists in
    the corpus with an unchanged rubric hash and grader version. The manifest
    task set is a *subset* check — corpus tasks the manifest does not name are
    allowed to grow in (they belong to the next release), but a manifest task
    that disappears or drifts fails loudly. Held-out tasks frozen without a
    hash (no key at freeze time) are matched by identity only.
    """
    from bench.run import discover_tasks  # local: avoid a run<->releases cycle

    problems: list[str] = []
    if manifest.get("grader_version") != GRADER_VERSION:
        problems.append(
            f"grader_version mismatch: release froze {manifest.get('grader_version')!r}, "
            f"current grader is {GRADER_VERSION!r}"
        )

    live: dict[str, dict] = {}
    for task_dir in discover_tasks(Path(tasks_root)):
        with open(Path(task_dir) / "task.yaml", encoding="utf-8") as fh:
            config = yaml.safe_load(fh) or {}
        task_id = str(config.get("id") or task_dir.name)
        live[task_id] = {"dir": task_dir, "config": config}

    frozen = {task["id"]: task for task in manifest.get("tasks", [])}
    for task_id in sorted(set(frozen) - set(live)):
        problems.append(f"task in release but missing from corpus: {task_id}")

    # Tasks in the corpus but not in the manifest are fine: the manifest is a
    # subset check so the corpus can grow between releases.

    for task_id in sorted(set(frozen) & set(live)):
        frozen_hash = frozen[task_id].get("rubric_sha256")
        if frozen_hash is None:
            continue  # identity-only match (held-out, frozen without a key)
        config = live[task_id]["config"]
        try:
            rubric = load_hidden_json(
                live[task_id]["dir"], "expected.json", config=config
            )
        except HeldoutKeyError:
            problems.append(
                f"cannot verify rubric of held-out task {task_id}: no HELDOUT_KEY"
            )
            continue
        live_hash = rubric_sha256(rubric)
        if live_hash != frozen_hash:
            problems.append(
                f"rubric_sha256 mismatch for {task_id}: release froze {frozen_hash}, "
                f"live rubric hashes to {live_hash}"
            )
    return problems
