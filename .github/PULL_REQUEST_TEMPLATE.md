<!--
PR checklist mirroring the AGENTS.md definition of done. For task PRs, every
task section must be checked; for infra PRs, use the general section only.
-->

## Task PRs — definition of done (AGENTS.md)

- [ ] **One task per PR** — branch `task/<task-id>`, only `tasks/<category>/<task-id>/` touched
- [ ] Task ID `<category>-<nnn>` agrees with the folder name and the `id` field in `task.yaml`
- [ ] `task.yaml` has `id`, `title`, `category`, `difficulty`, `tags`, `symptom`
- [ ] **Symptom** describes what on-call sees and does not name the root cause
- [ ] **Fixtures**: 2–10 files, each under ~150 lines, named after the command or source (never the answer)
- [ ] At least one **red herring**; names, namespaces, zones, timestamps agree across files
- [ ] Only placeholder GUIDs (`00000000-0000-0000-0000-00000000000X`) — no real IDs or secrets
- [ ] **Rubric** groups are specific enough that a generic guess fails
- [ ] **Solution** (`solution.json`) passes its own task (solvability)
- [ ] `python -m bench validate` passes
- [ ] `pytest -q` passes (includes integrity checks)
- [ ] `python -m bench catalog` run and `docs/TASKS.md` committed if tasks changed

## General (all PRs)

- [ ] `pytest -q` green locally
- [ ] Docs (`README.md`, `CONTRIBUTING.md`, `.obvious/obvious.md`) updated if this changes conventions
