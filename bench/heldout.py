"""Held-out split support (issue #115).

A task may declare `split: heldout` in its `task.yaml`. Held-out tasks keep
their `fixtures/` public but store the hidden rubric (`expected.json`) and the
reference answer (`solution.json`) age-encrypted as `expected.json.age` /
`solution.json.age`, so public rubrics cannot leak into model training data.
Decryption happens only when the `HELDOUT_KEY` environment variable holds an
age identity key; contributors without it can still validate task structure
and run public tasks. Keys and ciphertexts are standard age format
(age-encryption.org/v1), interoperable with the `age` CLI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pyrage

SPLIT_PUBLIC = "public"
SPLIT_HELDOUT = "heldout"
SPLITS = frozenset({SPLIT_PUBLIC, SPLIT_HELDOUT})

HIDDEN_FILES = ("expected.json", "solution.json")
ENCRYPTED_SUFFIX = ".age"
KEY_ENV = "HELDOUT_KEY"


class HeldoutKeyError(RuntimeError):
    """A held-out rubric was needed but no usable HELDOUT_KEY is available."""


def split_of(config: dict | None) -> str:
    """A task's split from its task.yaml config, defaulting to public."""
    if not isinstance(config, dict):
        return SPLIT_PUBLIC
    return str(config.get("split") or SPLIT_PUBLIC)


def heldout_key() -> str | None:
    """The configured age identity key; None when unset or blank."""
    return os.environ.get(KEY_ENV, "").strip() or None


def generate_identity() -> str:
    """Generate a fresh age identity and return its string form (the secret key)."""
    return str(pyrage.x25519.Identity.generate())


def _identity(key: str) -> pyrage.x25519.Identity:
    """Parse an age identity (secret key) string, wrapping errors."""
    try:
        return pyrage.x25519.Identity.from_str(key)
    except pyrage.IdentityError as exc:
        raise HeldoutKeyError(f"${KEY_ENV} is not a valid age identity key: {exc}") from exc


def encrypt_bytes(data: bytes, key: str) -> bytes:
    """Age-encrypt `data` to the public half of the identity `key`."""
    return pyrage.encrypt(data, [_identity(key).to_public()])


def decrypt_bytes(data: bytes, key: str | None = None) -> bytes:
    """Age-decrypt `data` with `key` (default: the HELDOUT_KEY environment)."""
    key = key or heldout_key()
    if not key:
        raise HeldoutKeyError(f"a held-out rubric needs an age identity key in ${KEY_ENV}")
    identity = _identity(key)
    try:
        return pyrage.decrypt(data, [identity])
    except pyrage.DecryptError as exc:
        raise HeldoutKeyError(f"${KEY_ENV} cannot decrypt this file: {exc}") from exc


def encrypt_hidden(task_dir: Path, key: str) -> list[Path]:
    """Encrypt every hidden plaintext file in `task_dir` to `<name>.age`.

    The plaintext file is removed after encryption — a held-out task must
    never ship both forms. Returns the encrypted paths.
    """
    encrypted: list[Path] = []
    for name in HIDDEN_FILES:
        plain = Path(task_dir) / name
        if plain.is_file():
            out = plain.with_name(name + ENCRYPTED_SUFFIX)
            out.write_bytes(encrypt_bytes(plain.read_bytes(), key))
            plain.unlink()
            encrypted.append(out)
    return encrypted


def decrypt_hidden(task_dir: Path, key: str) -> list[Path]:
    """Decrypt every `<name>.age` in `task_dir` back to plaintext (maintainer editing).

    Returns the restored plaintext paths.
    """
    decrypted: list[Path] = []
    for name in HIDDEN_FILES:
        encrypted = Path(task_dir) / (name + ENCRYPTED_SUFFIX)
        if encrypted.is_file():
            plain = encrypted.with_name(name)
            plain.write_bytes(decrypt_bytes(encrypted.read_bytes(), key))
            encrypted.unlink()
            decrypted.append(plain)
    return decrypted


def load_hidden_json(task_dir: Path, name: str, config: dict | None = None) -> dict:
    """Read a hidden file (`expected.json`/`solution.json`), decrypting held-out tasks.

    Public tasks read the plaintext file as-is. Held-out tasks read
    `<name>.age` and decrypt with `HELDOUT_KEY`, raising `HeldoutKeyError`
    when the key is missing or wrong — a corrupt or locked rubric must be
    loud, never silently graded as an empty one.
    """
    task_dir = Path(task_dir)
    if split_of(config) != SPLIT_HELDOUT:
        with open(task_dir / name, encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        encrypted = task_dir / (name + ENCRYPTED_SUFFIX)
        if not encrypted.is_file():
            raise FileNotFoundError(
                f"missing {name}{ENCRYPTED_SUFFIX} (held-out task {task_dir.name})"
            )
        data = json.loads(decrypt_bytes(encrypted.read_bytes()))
    return data if isinstance(data, dict) else {}
