# Held-out tasks (issue #115)

Public rubrics eventually leak into model training data. The held-out split
keeps a reserve of tasks whose rubrics are **not readable in the repo**: fixtures
stay public, but the hidden rubric and reference answer ship age-encrypted, and
they decrypt only when the `HELDOUT_KEY` secret is present.

## Layout

A held-out task looks like a normal task, except:

```
tasks/<category>/<task-id>/
  task.yaml          # ... plus `split: heldout`
  fixtures/          # still public — agents and contributors can read these
  expected.json.age  # HIDDEN rubric, age-encrypted
  solution.json.age  # HIDDEN reference answer, age-encrypted
```

Keys and ciphertexts are standard age format (age-encryption.org/v1), so the
`age` CLI can open them; the harness uses the `pyrage` bindings.

## Adding a held-out task (maintainer flow)

1. **Author the task as public** — plaintext `expected.json` / `solution.json`,
   `python -m bench validate` green, integrity tests passing.
2. **Flip the split** — add `split: heldout` to `task.yaml`.
3. **Encrypt** — with the benchmark age identity in `HELDOUT_KEY`:

   ```
   HELDOUT_KEY=<age identity key> python scripts/encrypt_heldout.py tasks/<category>/<task-id>
   ```

   This writes `expected.json.age` / `solution.json.age` and removes the
   plaintext. The script refuses any task that does not declare
   `split: heldout`, so a public task cannot be encrypted by accident.
4. **Validate** — `python -m bench validate`. With `HELDOUT_KEY` set, rubric
   shape is checked on the decrypted content; without it, structure-only.
5. **Commit** — the `.age` files and the `task.yaml` change. **Never commit the
   plaintext files of a held-out task**: the validator fails a held-out task
   that still contains them, but the real guard is this step.
6. **Push to a one-time-secure channel for the key** — the maintainer holds
   `HELDOUT_KEY` out-of-band; it is configured as a GitHub Actions secret, not
   committed.

To edit a held-out task's rubric later, decrypt it in place, edit, re-encrypt:

```
HELDOUT_KEY=<key> python scripts/encrypt_heldout.py --decrypt tasks/<category>/<task-id>
# ...edit expected.json / solution.json...
HELDOUT_KEY=<key> python scripts/encrypt_heldout.py tasks/<category>/<task-id>
```

## Keys

Generate a fresh age identity with any of:

- `age-keygen -o heldout.txt` (the `age` toolchain), or
- `python -c "from bench.heldout import generate_identity; print(generate_identity())"`

The full `AGE-SECRET-KEY-1...` string is the `HELDOUT_KEY` value (CI secret and
local env). Rotation means re-encrypting every held-out task to the new
identity — keep the current key until that re-encryption lands.

## How CI behaves

- **`HELDOUT_KEY` configured** (a repo secret): `bench validate` decrypts and
  fully checks held-out rubrics; the integrity tests grade their real rubrics.
- **`HELDOUT_KEY` absent**: held-out tasks validate structure-only, and the
  three integrity tests that read rubric content *skip* them (`decrypt-or-skip`,
  spec D4) instead of failing. The corpus never blocks a key-less contributor.

## Running held-out tasks

- `bench run --split heldout --all --agent <cmd>` runs only held-out tasks
  (in CI, with the key; locally, only with the key set).
- `bench run --split public --all --agent <cmd>` runs only public tasks.
- Locked tasks print `SKIP <task-id>: ...` and never count as agent failures.
- `bench grade --task <id>` works on held-out tasks when `HELDOUT_KEY` is set.

## Leaderboard

Result records carry the task's split; `bench leaderboard` (and the README
block) ranks public and held-out scores in separate tables, so held-out results
never contaminate public comparisons.

## Scope note

No existing task ships held-out yet (spec decision D4: the split mechanics land
first; moving tasks is a separate decision). Until a task declares
`split: heldout`, every behavior above is dormant and the corpus validates
exactly as before.
